"""Chat de investigacion: OpenAlex -> digest de abstracts -> DeepSeek streaming.

Objetivo de tokens: el modelo solo recibe un digest compacto (titulo, ano,
revista y un recorte del abstract) de como maximo 8 papers. Los abstracts se
guardan en Markdown (memoria + disco + GCS) para no reconstruirlos ni volver a
pagarlos en consultas repetidas. El prompt de sistema es estable para que el
cache automatico de DeepSeek pueda reutilizar el prefijo.

Cada respuesta registra su costo en el ledger (`kind: chat`) y deja una linea
`[cost-chat]` en stdout, que Cloud Run envia a Cloud Logging.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx

import storage
from costs import estimate_cost, record_cost

DEEPSEEK_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')
CHAT_MODEL = os.getenv('CHAT_MODEL', os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'))

OPENALEX_WORKS_URL = 'https://api.openalex.org/works'
OPENALEX_MAILTO = os.getenv('OPENALEX_MAILTO', 'psyhub.app.research@gmail.com').strip()
OPENALEX_API_KEY = os.getenv('OPENALEX_API_KEY', '').strip()

MAX_SOURCES = max(1, int(os.getenv('CHAT_MAX_SOURCES', '8')))
ABSTRACT_CHARS = max(300, int(os.getenv('CHAT_ABSTRACT_CHARS', '900')))
MAX_OUTPUT_TOKENS = max(200, int(os.getenv('CHAT_MAX_OUTPUT_TOKENS', '1000')))
MAX_HISTORY_MESSAGES = max(0, int(os.getenv('CHAT_HISTORY_MESSAGES', '6')))
HISTORY_CHARS = max(200, int(os.getenv('CHAT_HISTORY_CHARS', '900')))
SEARCH_CACHE_TTL = int(os.getenv('CHAT_SEARCH_CACHE_TTL', '1800'))
SEARCH_RESULTS = max(MAX_SOURCES, int(os.getenv('CHAT_SEARCH_RESULTS', '14')))
ONECLICK_CANDIDATES = max(1, int(os.getenv('CHAT_ONECLICK_CANDIDATES', '12')))

CACHE_DIR = Path(os.getenv('CACHE_DIR', './cache')).resolve()
ABSTRACT_DIR = CACHE_DIR / 'abstracts'
ABSTRACT_DIR.mkdir(parents=True, exist_ok=True)

PSY_FIELDS = 'primary_topic.field.id:32|27'
WORK_TYPES = 'type:article|review'
SELECT_FIELDS = ','.join([
    'id', 'doi', 'title', 'publication_year', 'cited_by_count', 'authorships',
    'primary_location', 'best_oa_location', 'locations', 'open_access',
    'abstract_inverted_index', 'primary_topic', 'topics', 'type',
])

# Dominios que casi siempre piden login/aceptacion aunque la URL parezca un PDF.
GATED_HOST_RE = re.compile(
    r'(^|\.)(sciencedirect\.com|link\.springer\.com|onlinelibrary\.wiley\.com|'
    r'tandfonline\.com|sagepub\.com|jstor\.org|researchgate\.net|academia\.edu|'
    r'psycnet\.apa\.org|proquest\.com|ssrn\.com)$',
    re.I,
)

SYSTEM_PROMPT = """Eres el asistente de investigación de PsiHub, especializado en psicología y psicoterapia.
Respondes SIEMPRE en español, con un tono claro, académico y directo. Escribí con
ortografía correcta: tildes, eñes y signos de apertura (¿, ¡).

Reglas:
1. Respondé la pregunta usando ÚNICAMENTE las FUENTES numeradas que recibís.
2. Cada afirmación relevante debe llevar su cita en el formato [1], [2], etc. Podés citar varias: [1][3].
3. Sintetizá: no copies párrafos enteros ni el abstract. Extraé objetivo, hallazgos y conclusión.
4. Cuando las fuentes discrepen o sean limitadas, decilo explícitamente.
5. No inventes datos, autores, años ni resultados que no aparezcan en las fuentes.
6. No incluyas URLs, DOIs ni enlaces: el lector puede abrir cada fuente desde la interfaz.
7. Estructura sugerida cuando aporte claridad: una respuesta breve al inicio, luego los hallazgos
   principales en párrafos o viñetas, y al final una línea de "Límites" si corresponde.
8. Si las fuentes no alcanzan para responder, decilo y sugerí reformular la búsqueda.

No agregues una lista de referencias al final: la interfaz ya muestra las fuentes."""


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted: Optional[dict]) -> str:
    if not isinstance(inverted, dict):
        return ''
    words: list[str] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and 0 <= pos < 100_000:
                while len(words) <= pos:
                    words.append('')
                words[pos] = word
    return ' '.join(w for w in words if w).strip()


def _extract_pdf_url(work: dict) -> Optional[str]:
    candidates: list[str] = []
    best = work.get('best_oa_location') or {}
    if best.get('pdf_url'):
        candidates.append(best['pdf_url'])
    for loc in work.get('locations') or []:
        if isinstance(loc, dict) and loc.get('pdf_url'):
            candidates.append(loc['pdf_url'])
    primary = work.get('primary_location') or {}
    if primary.get('pdf_url'):
        candidates.append(primary['pdf_url'])

    seen: set[str] = set()
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            return url

    oa_url = (work.get('open_access') or {}).get('oa_url')
    if oa_url and (
        oa_url.lower().endswith('.pdf')
        or re.search(r'/(?:pdf|download|fulltext|full-text|article/[^/]+/pdf)', oa_url, re.I)
    ):
        return oa_url
    return None


def _is_direct_pdf(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        path = parsed.path.lower()
        return bool(
            path.endswith('.pdf')
            or re.search(r'/(?:pdf|download|fulltext|full-text|bitstream|bitstreams)(?:/|$)', path)
            or 'pdf' in parsed.query.lower()
        )
    except Exception:
        return False


def _work_to_source(work: dict) -> dict:
    abstract = _reconstruct_abstract(work.get('abstract_inverted_index'))
    authors = [
        (a.get('author') or {}).get('display_name')
        for a in (work.get('authorships') or [])
        if (a.get('author') or {}).get('display_name')
    ]
    source = (work.get('primary_location') or {}).get('source') or {}
    primary_topic = (work.get('primary_topic') or {}).get('display_name')
    subfield = ((work.get('primary_topic') or {}).get('subfield') or {}).get('display_name')
    topics = []
    for topic in [primary_topic, subfield] + [
        t.get('display_name') for t in (work.get('topics') or []) if isinstance(t, dict)
    ]:
        if topic and topic not in topics:
            topics.append(topic)

    pdf_url = _extract_pdf_url(work)
    is_oa = bool((work.get('open_access') or {}).get('is_oa'))
    work_id = str(work.get('id') or '')
    if work_id.startswith('https://openalex.org/'):
        work_id = work_id.rsplit('/', 1)[-1]

    return {
        'id': work.get('id'),
        'openalexId': work_id,
        'doi': work.get('doi'),
        'title': work.get('title') or 'Sin titulo',
        'authors': authors,
        'year': work.get('publication_year'),
        'journal': source.get('display_name'),
        'citations': work.get('cited_by_count') or 0,
        'abstract': abstract,
        'topics': topics[:5],
        'type': work.get('type'),
        'isOa': is_oa,
        'oaStatus': (work.get('open_access') or {}).get('oa_status'),
        'oaUrl': (work.get('open_access') or {}).get('oa_url'),
        'pdfUrl': pdf_url,
        'isAutoDownloadable': bool(is_oa and pdf_url),
    }


def _params(query: str, access: str, per_page: int, sort: Optional[str] = None) -> dict:
    # OpenAlex trata "*" y "?" como wildcards y exige busqueda exacta si aparecen;
    # por eso se quitan junto con el resto de la puntuacion de la pregunta.
    clean = re.sub(r'[,|"\'()?*¿¡!;:]', ' ', query or '')
    clean = re.sub(r'\s+', ' ', clean).strip()[:300]
    filters = [PSY_FIELDS, WORK_TYPES]
    if access in ('free', 'oneclick'):
        filters.append('is_oa:true')
    params = {
        'search': clean,
        'filter': ','.join(filters),
        'per-page': str(per_page),
        'select': SELECT_FIELDS,
    }
    if OPENALEX_API_KEY:
        params['api_key'] = OPENALEX_API_KEY
    else:
        params['mailto'] = OPENALEX_MAILTO
    if sort:
        params['sort'] = sort
    return params


_search_cache: dict[str, tuple[float, list[dict]]] = {}


async def _openalex_get(params: dict) -> dict:
    headers = {'User-Agent': f'PsiHub/10 (+https://melaraneax.github.io/PsiHub/; {OPENALEX_MAILTO})'}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=8.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(OPENALEX_WORKS_URL, params=params)
        if response.status_code in (429, 503):
            await asyncio.sleep(1.0)
            response = await client.get(OPENALEX_WORKS_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('Respuesta invalida de OpenAlex.')
    return payload


async def _probe_oneclick(sources: list[dict], probe_fn) -> list[dict]:
    """Conserva solo papers cuyo PDF responde sin login (verificacion real)."""
    candidates = [s for s in sources if s.get('pdfUrl') and s.get('isAutoDownloadable')][:ONECLICK_CANDIDATES]
    if not candidates:
        return []
    semaphore = asyncio.Semaphore(6)

    async def verify(source: dict) -> Optional[dict]:
        url = source['pdfUrl']
        try:
            host = (urlparse(url).hostname or '').lower()
        except Exception:
            return None
        if GATED_HOST_RE.search(host) or not _is_direct_pdf(url):
            return None
        async with semaphore:
            try:
                verdict = await probe_fn(url)
            except Exception:
                return None
        return source if verdict.get('ok') is True else None

    checked = await asyncio.gather(*(verify(s) for s in candidates))
    return [s for s in checked if s]


async def search_sources(
    query: str,
    access: str = 'all',
    probe_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    log_fn: Callable[[str], None] = print,
) -> tuple[list[dict], dict]:
    """Busca papers y devuelve (fuentes, metadatos de la busqueda)."""
    query = (query or '').strip()
    if not query:
        return [], {'count': 0, 'query': ''}

    cache_key = f'{query.lower()}|{access}'
    cached = _search_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1], {'count': len(cached[1]), 'query': query, 'cached': True}

    payload = await _openalex_get(_params(query, access, SEARCH_RESULTS))
    works = [w for w in (payload.get('results') or []) if isinstance(w, dict)]
    sources = [_work_to_source(w) for w in works]

    # Si el orden por relevancia dejo pocos abstracts, probamos por citas.
    with_abstract = sum(1 for s in sources if s.get('abstract'))
    if with_abstract < 4:
        try:
            alt = await _openalex_get(_params(query, access, SEARCH_RESULTS, sort='cited_by_count:desc'))
            seen = {s.get('id') for s in sources}
            for work in alt.get('results') or []:
                source = _work_to_source(work)
                if source.get('id') not in seen:
                    seen.add(source.get('id'))
                    sources.append(source)
        except Exception as exc:  # noqa: BLE001 - la busqueda principal sigue siendo valida
            log_fn(f'[chat-search] fallback por citas fallo: {exc}')

    # Preferimos papers con abstract: son los que permiten sintetizar.
    sources.sort(key=lambda s: (bool(s.get('abstract')), int(s.get('citations') or 0)), reverse=True)

    if access == 'oneclick' and probe_fn is not None:
        sources = await _probe_oneclick(sources, probe_fn)

    sources = sources[:MAX_SOURCES]
    # Los abstracts se cachean en paralelo para no sumar latencia a la busqueda.
    await asyncio.gather(*(_remember_abstract(source) for source in sources))
    _search_cache[cache_key] = (time.time() + SEARCH_CACHE_TTL, sources)
    if len(_search_cache) > 300:
        _search_cache.clear()
    return sources, {'count': len(sources), 'query': query, 'cached': False}


# ---------------------------------------------------------------------------
# Cache de abstracts en Markdown
# ---------------------------------------------------------------------------

_abstract_cache: dict[str, str] = {}


def _abstract_key(source: dict) -> str:
    return str(source.get('openalexId') or source.get('id') or '').rsplit('/', 1)[-1]


async def _remember_abstract(source: dict) -> None:
    """Guarda el abstract como Markdown (memoria + disco + GCS, sin bloquear)."""
    key = _abstract_key(source)
    abstract = source.get('abstract') or ''
    if not key or not abstract:
        return
    if key in _abstract_cache:
        return
    _abstract_cache[key] = abstract
    if len(_abstract_cache) > 2000:
        _abstract_cache.clear()
        _abstract_cache[key] = abstract

    path = ABSTRACT_DIR / f'{key}.md'
    try:
        await asyncio.to_thread(path.write_text, abstract, encoding='utf-8')
    except Exception:
        pass

    if storage.enabled():
        def upload() -> None:
            storage.put(f'abstracts/{key}.md', abstract.encode('utf-8'), 'text/markdown; charset=utf-8')

        try:
            await asyncio.to_thread(upload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _digest(sources: list[dict]) -> str:
    blocks: list[str] = []
    for index, source in enumerate(sources, start=1):
        title = source.get('title') or 'Sin título'
        year = source.get('year') or 's.f.'
        journal = source.get('journal') or 'Revista no informada'
        abstract = (source.get('abstract') or '').strip()
        if not abstract:
            abstract = 'OpenAlex no incluye el resumen de este artículo.'
        elif len(abstract) > ABSTRACT_CHARS:
            abstract = abstract[:ABSTRACT_CHARS].rstrip() + ' [...]'
        access = 'acceso abierto' if source.get('isOa') else 'de pago'
        blocks.append(
            f'[{index}] {title} ({year}) · {journal} · {access}\n'
            f'Resumen: {abstract}'
        )
    return '\n\n'.join(blocks)


def _history_messages(history: Optional[list[dict]]) -> list[dict]:
    if not history:
        return []
    cleaned: list[dict] = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        role = item.get('role')
        content = (item.get('content') or '').strip()
        if role not in ('user', 'assistant') or not content:
            continue
        cleaned.append({'role': role, 'content': content[:HISTORY_CHARS]})
    return cleaned


def _build_messages(question: str, sources: list[dict], history: Optional[list[dict]]) -> list[dict]:
    messages: list[dict] = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages.extend(_history_messages(history))
    messages.append({
        'role': 'user',
        'content': (
            f'PREGUNTA DEL USUARIO:\n{question.strip()}\n\n'
            f'FUENTES DISPONIBLES:\n{_digest(sources)}\n\n'
            'Redacta la respuesta en espanol citando las fuentes con [n].'
        ),
    })
    return messages


# ---------------------------------------------------------------------------
# Streaming DeepSeek
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


class ChatStartError(RuntimeError):
    pass


async def _stream_completion(
    messages: list[dict],
    api_keys: list[str],
    log_fn: Callable[[str], None],
):
    if not api_keys:
        raise ChatStartError('No hay API key de DeepSeek configurada.')

    payload = {
        'model': CHAT_MODEL,
        'messages': messages,
        'temperature': 0.25,
        'stream': True,
        'stream_options': {'include_usage': True},
        'thinking': {'type': 'disabled'},
        'max_tokens': MAX_OUTPUT_TOKENS,
    }
    last_error: Optional[BaseException] = None

    for attempt in range(min(len(api_keys), 3)):
        key = api_keys[attempt % len(api_keys)]
        produced = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
                async with client.stream(
                    'POST', DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread())[:300].decode('utf-8', 'replace')
                        raise ChatStartError(f'DeepSeek HTTP {response.status_code}: {body}')
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if not data or data == '[DONE]':
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get('usage')
                        if usage:
                            yield ('usage', usage)
                        choices = chunk.get('choices') or []
                        if not choices:
                            continue
                        delta = (choices[0].get('delta') or {}).get('content') or ''
                        if delta:
                            produced = True
                            yield ('delta', delta)
            return
        except ChatStartError as exc:
            # Rotamos a la siguiente key: una key vencida o sin credito no
            # debe tumbar el chat si hay otra configurada.
            last_error = exc
            if produced:
                raise
            log_fn(f'[chat] key {attempt + 1} rechazada: {exc}')
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if produced:
                raise
            wait = min(2 ** attempt, 8)
            log_fn(f'[chat] intento {attempt + 1} fallo: {exc}. Reintentando en {wait}s')
            await asyncio.sleep(wait)

    raise ChatStartError(f'No se pudo contactar a DeepSeek: {last_error}')


def _redact(text: object) -> str:
    """Evita filtrar la API key de OpenAlex en los logs de error."""
    return re.sub(r'(api_key=)[^&\s\']+', r'\1***', str(text))


async def stream_chat(
    question: str,
    query: str,
    access: str,
    history: Optional[list[dict]],
    api_keys: list[str],
    probe_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    log_fn: Callable[[str], None] = print,
    client_id: str = '',
) -> AsyncIterator[str]:
    """Genera eventos SSE: sources, delta, done o error."""
    started = time.time()
    question = (question or '').strip()[:2000]
    if not question:
        yield _sse({'type': 'error', 'message': 'La pregunta esta vacia.'})
        return

    try:
        sources, search_meta = await search_sources(
            query or question, access=access, probe_fn=probe_fn, log_fn=log_fn,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat] busqueda fallo: {type(exc).__name__}: {_redact(exc)}')
        yield _sse({'type': 'error', 'message': 'No se pudo buscar en OpenAlex. Intenta de nuevo.'})
        return

    yield _sse({
        'type': 'sources',
        'sources': sources,
        'access': access,
        'query': search_meta.get('query') or query,
        'cached': bool(search_meta.get('cached')),
    })

    if not sources:
        yield _sse({
            'type': 'delta',
            'text': ('No encontre papers suficientes para responder con evidencia. '
                     'Proba con otros terminos o cambia el filtro de acceso.'),
        })
        yield _sse({'type': 'done', 'usage': {}, 'cost_usd': 0, 'sources_count': 0})
        return

    messages = _build_messages(question, sources, history)
    usage: dict = {}
    answer_parts: list[str] = []

    try:
        async for kind, value in _stream_completion(messages, api_keys, log_fn):
            if kind == 'usage':
                usage = value
            elif kind == 'delta':
                answer_parts.append(value)
                yield _sse({'type': 'delta', 'text': value})
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat] generacion fallo: {type(exc).__name__}: {exc}')
        yield _sse({'type': 'error', 'message': str(exc) or 'DeepSeek no respondio.'})
        return

    cost = estimate_cost(usage)
    elapsed = round(time.time() - started, 2)
    record = {
        'doc_id': f"chat-{int(time.time() * 1000)}",
        'kind': 'chat',
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'title': question[:120],
        'pages': 0,
        'model': CHAT_MODEL,
        'source': 'chat',
        'access': access,
        'sources': len(sources),
        'cache_hit_tokens': int(usage.get('prompt_cache_hit_tokens') or 0),
        'elapsed_sec': elapsed,
        'client_id': client_id[:64],
        **cost,
    }
    try:
        record_cost(record)
    except Exception:
        pass

    cache_hit = record['cache_hit_tokens']
    log_fn(
        f"[cost-chat] {record['doc_id']} | {len(sources)} fuentes | "
        f"{cost['total_tokens']} tokens (cache {cache_hit}) | "
        f"${cost['cost_usd']:.6f} USD | {elapsed}s"
    )
    yield _sse({
        'type': 'done',
        'usage': usage,
        'cost_usd': cost['cost_usd'],
        'sources_count': len(sources),
        'elapsed_sec': elapsed,
    })
