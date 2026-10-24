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
import math
import os
import re
import time
import unicodedata
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

MAX_SOURCES = max(1, int(os.getenv('CHAT_MAX_SOURCES', '14')))
ABSTRACT_CHARS = max(300, int(os.getenv('CHAT_ABSTRACT_CHARS', '1100')))
MAX_OUTPUT_TOKENS = max(200, int(os.getenv('CHAT_MAX_OUTPUT_TOKENS', '1400')))
MAX_HISTORY_MESSAGES = max(0, int(os.getenv('CHAT_HISTORY_MESSAGES', '6')))
HISTORY_CHARS = max(200, int(os.getenv('CHAT_HISTORY_CHARS', '900')))
SEARCH_CACHE_TTL = int(os.getenv('CHAT_SEARCH_CACHE_TTL', '1800'))
# Papers que se piden por cada consulta generada (el pool final se recorta).
SEARCH_RESULTS = max(MAX_SOURCES, int(os.getenv('CHAT_SEARCH_RESULTS', '25')))
ONECLICK_CANDIDATES = max(1, int(os.getenv('CHAT_ONECLICK_CANDIDATES', '20')))
# Relevancia minima (0-1) para considerar que un paper tiene relacion real.
MIN_RELEVANCE = float(os.getenv('CHAT_MIN_RELEVANCE', '0.14'))
# Consultas simultaneas contra OpenAlex (limite comodo del polite pool).
SEARCH_CONCURRENCY = max(1, int(os.getenv('CHAT_SEARCH_CONCURRENCY', '4')))
PLANNER_MAX_OUTPUT = max(120, int(os.getenv('CHAT_PLANNER_TOKENS', '320')))

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
1. Respondé la pregunta usando ÚNICAMENTE las FUENTES numeradas que recibís. Son muchas: apoyate en
   las que de verdad tratan el tema y descartá sin citar las que solo lo rozan.
2. Cada afirmación relevante debe llevar su cita en el formato [1], [2], etc. Podés citar varias: [1][3].
   Si varias fuentes coinciden, citá las más sólidas (revisiones, meta-análisis, mayor muestra).
3. Sintetizá: no copies párrafos enteros ni el abstract. Extraé objetivo, hallazgos y conclusión.
4. Cuando las fuentes discrepen o sean limitadas, decilo explícitamente.
5. No inventes datos, autores, años ni resultados que no aparezcan en las fuentes.
6. No incluyas URLs, DOIs ni enlaces: el lector puede abrir cada fuente desde la interfaz.
7. Estructura sugerida cuando aporte claridad: una respuesta breve al inicio, luego los hallazgos
   principales en párrafos o viñetas, y al final una línea de "Límites" si corresponde.
8. Si las fuentes no alcanzan para responder, decilo y sugerí reformular la búsqueda.

No agregues una lista de referencias al final: la interfaz ya muestra las fuentes."""


# ---------------------------------------------------------------------------
# Planificacion de la busqueda
# ---------------------------------------------------------------------------

PLANNER_PROMPT = """Eres un bibliotecario cientifico especializado en psicologia. Conviertes la pregunta
de un usuario en consultas de busqueda para OpenAlex, una base de articulos academicos.

Respondes SIEMPRE con un objeto JSON valido, sin texto adicional ni bloques de codigo:
{"queries": ["...", "..."], "keywords": ["...", "..."]}

Reglas:
- "queries": entre 3 y 5 consultas EN INGLES de 2 a 7 palabras cada una.
- Usa vocabulario academico: expande siglas y conceptos tecnicos (TCC -> cognitive behavioral
  therapy; red neuronal por defecto -> default mode network; DMN -> default mode network).
- Cada consulta debe atacar un angulo distinto: el concepto central, la relacion entre los
  conceptos de la pregunta, la poblacion o el metodo (p. ej. resting-state functional
  connectivity depression).
- Si la pregunta es amplia o conceptual, dedica una consulta a revisiones y meta-analisis
  (agregando review o meta-analysis a los conceptos centrales).
- No uses comillas, comodines, parentesis, operadores booleanos ni signos de puntuacion.
- "keywords": entre 4 y 8 conceptos sueltos EN INGLES (una o dos palabras) que deberian
  aparecer en el titulo o el resumen de un articulo realmente relevante.
"""

# Palabras vacias que no aportan a la relevancia.
_STOPWORDS = set("""
a al algo algun alguna algunas alguno algunos ante antes aquel aquella aquellas aquello aquellos
asi aun bajo bien cada como con contra cual cuales cualquier cuando de del desde donde dos durante
e el ella ellas ello ellos en entre era eran eres es esa esas ese eso esos esta estaba estado estan
estas este esto estos estoy fue fueron ha habia han hasta hay la las le les lo los mas me mi mis
mucho muy no nos nuestra nuestro o os otra otro para pero poco por porque que quien se sea segun
ser si sin so sobre son su sus tambien tan te tiene tienen todo todos tu tus un una unas uno unos
u y ya yo

about after again against all also an and any are because been before being between both but by can
could did do does doing down during each few for from further had has have having here how into its
itself just more most not now off on once only other our out over own same should some such than that
the their them then there these they this those through too under until up very was were what when
where which while who why will with would you your

study studies paper papers article articles research evidence review reviews result results effect
effects association associations role relationship between among using based approach
""".split())


def _normalize(text: object) -> str:
    """Minusculas y sin tildes, para comparar terminos en espanol e ingles."""
    raw = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(ch for ch in raw if not unicodedata.combining(ch)).lower()


def _word_set(text: object) -> set[str]:
    return set(re.findall(r'[a-z0-9]{3,}', _normalize(text)))


def _stems(words: set[str]) -> set[str]:
    """Prefijos de 6 letras: aproximacion barata a un stemmer."""
    return {word[:6] for word in words}


def _hits(terms: list[str], stems: set[str]) -> int:
    return sum(1 for term in terms if term[:6] in stems)


def _clean_query(text: str) -> str:
    # OpenAlex trata "*" y "?" como wildcards y exige busqueda exacta si aparecen;
    # por eso se quitan junto con el resto de la puntuacion.
    clean = re.sub(r'[,|"\'()?*¿¡!;:\[\]{}]', ' ', str(text or ''))
    return re.sub(r'\s+', ' ', clean).strip()


def _parse_json_object(raw: str) -> Optional[dict]:
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'```\s*$', '', text).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fallback_plan(question: str, query: str) -> dict:
    """Plan sin costo: variantes heuristicas de la consulta traducida."""
    base = _clean_query(query or question)
    words = [w for w in re.findall(r'[a-zA-Z0-9]{3,}', base) if w.lower() not in _STOPWORDS]
    keywords = list(dict.fromkeys(w.lower() for w in words))[:8]
    key_query = ' '.join(words[:6])
    queries = [base]
    if key_query and key_query.lower() != base.lower():
        queries.append(key_query)
    if len(words) > 3:
        queries.append(' '.join(words[:3]))
    return {'queries': [q for q in queries if q], 'keywords': keywords}


def _plan_terms(plan: Optional[dict], question: str, query: str) -> list[str]:
    """Terminos con los que se puntua la relevancia de cada paper."""
    terms: list[str] = []
    for keyword in (plan or {}).get('keywords') or []:
        terms.extend(_word_set(keyword))
    terms.extend(_word_set(query))
    terms.extend(_word_set(question))
    ordered = [t for t in dict.fromkeys(terms) if t not in _STOPWORDS and len(t) >= 4]
    return ordered[:24]


def _plan_queries(plan: Optional[dict], question: str, query: str) -> list[str]:
    candidates: list[str] = []
    for item in (plan or {}).get('queries') or []:
        if isinstance(item, str):
            candidates.append(item)
    if not candidates:
        candidates = _fallback_plan(question, query)['queries']
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        value = _clean_query(item)[:160]
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned[:5]


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


def _params(
    query: str,
    access: str,
    per_page: int,
    sort: Optional[str] = None,
    mode: str = 'broad',
) -> dict:
    clean = _clean_query(query)[:300]
    filters = [PSY_FIELDS, WORK_TYPES]
    if access in ('free', 'oneclick'):
        filters.append('is_oa:true')
    params = {
        'filter': ','.join(filters),
        'per-page': str(per_page),
        'select': SELECT_FIELDS,
    }
    # "precise" exige los terminos en titulo/resumen; "broad" usa el buscador
    # general de OpenAlex; "title" restringe al titulo.
    if mode == 'precise' and clean:
        params['filter'] = f"{params['filter']},title_and_abstract.search:{clean}"
    elif mode == 'title' and clean:
        params['filter'] = f"{params['filter']},title.search:{clean}"
    elif clean:
        params['search'] = clean
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


def _work_key(work: dict) -> str:
    oid = str(work.get('id') or '')
    if oid:
        return oid.rsplit('/', 1)[-1].lower()
    doi = str(work.get('doi') or '').lower()
    if doi:
        return doi
    return _normalize(work.get('title'))[:120]


def _score_source(source: dict, terms: list[str]) -> tuple[float, float]:
    """Devuelve (puntaje, relevancia). La relevancia es la fraccion de terminos
    de la pregunta presentes en titulo, resumen y topicos del paper."""
    title_stems = _stems(_word_set(source.get('title')))
    abstract_stems = _stems(_word_set(source.get('abstract')))
    topic_stems = _stems(_word_set(' '.join(source.get('topics') or [])))

    if not terms:
        relevance = 0.0
    else:
        title_hits = _hits(terms, title_stems)
        abstract_hits = _hits(terms, abstract_stems)
        topic_hits = _hits(terms, topic_stems)
        relevance = min(
            1.0,
            (3.0 * title_hits + abstract_hits + 2.0 * topic_hits) / (3.0 * len(terms)),
        )

    score = relevance * 10.0
    # Sin resumen no se puede sintetizar: penaliza fuerte frente a uno con texto.
    score += 0.6 if source.get('abstract') else -1.2
    # Senales de calidad: citas (en escala log) y vigencia.
    score += min(math.log1p(max(0, int(source.get('citations') or 0))), 4.0) * 0.30
    year = int(source.get('year') or 0)
    if year >= time.gmtime().tm_year - 6:
        score += 0.30
    if source.get('type') == 'review':
        score += 0.20
    return score, relevance


async def _collect(
    params: dict,
    semaphore: asyncio.Semaphore,
    log_fn: Callable[[str], None],
) -> list[dict]:
    async with semaphore:
        try:
            payload = await _openalex_get(params)
        except Exception as exc:  # noqa: BLE001 - una consulta caida no invalida el resto
            log_fn(f'[chat-search] consulta fallo: {type(exc).__name__}: {_redact(exc)}')
            return []
    return [w for w in (payload.get('results') or []) if isinstance(w, dict)]


async def search_sources(
    query: str,
    access: str = 'all',
    probe_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    log_fn: Callable[[str], None] = print,
    plan: Optional[dict] = None,
    question: str = '',
) -> tuple[list[dict], dict]:
    """Busca papers con varias consultas y devuelve solo los realmente afines."""
    query = (query or '').strip()
    question = (question or '').strip()
    if not query and not question:
        return [], {'count': 0, 'query': '', 'candidates': 0}

    queries = _plan_queries(plan, question, query)
    if not queries:
        return [], {'count': 0, 'query': query, 'candidates': 0}

    cache_key = f'{access}|' + '|'.join(sorted(q.lower() for q in queries))
    cached = _search_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1], {
            'count': len(cached[1]),
            'query': query,
            'candidates': len(cached[1]),
            'queries': queries,
            'cached': True,
        }

    # Cada consulta se lanza dos veces: restringida a titulo+resumen y en el
    # buscador general. Se solapan en paralelo para ganar cobertura sin sumar
    # latencia, y luego se deduplica por id/doi/titulo.
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
    tasks = []
    for item in queries[:4]:
        tasks.append(_collect(_params(item, access, SEARCH_RESULTS, mode='precise'), semaphore, log_fn))
        tasks.append(_collect(_params(item, access, SEARCH_RESULTS, mode='broad'), semaphore, log_fn))
    groups = await asyncio.gather(*tasks)

    pool: dict[str, dict] = {}
    for works in groups:
        for work in works:
            key = _work_key(work)
            if key and key not in pool:
                pool[key] = work

    sources = [_work_to_source(work) for work in pool.values()]
    candidates = len(sources)
    log_fn(f'[chat-search] {len(queries)} consultas | {candidates} papers unicos')

    terms = _plan_terms(plan, question, query)
    scored: list[tuple[float, float, dict]] = []
    for source in sources:
        score, relevance = _score_source(source, terms)
        scored.append((score, relevance, source))
    scored.sort(key=lambda item: item[0], reverse=True)

    related = [source for _, relevance, source in scored if relevance >= MIN_RELEVANCE]
    if len(related) < 4:
        # Muy pocos papers superan el umbral: nos quedamos con los mejores por
        # puntaje para no dejar la respuesta sin evidencia.
        related = [source for _, _, source in scored][:MAX_SOURCES]
    else:
        related = related[:MAX_SOURCES]

    if access == 'oneclick' and probe_fn is not None:
        related = await _probe_oneclick(related, probe_fn)

    # Los abstracts se cachean en paralelo para no sumar latencia a la busqueda.
    await asyncio.gather(*(_remember_abstract(source) for source in related))
    _search_cache[cache_key] = (time.time() + SEARCH_CACHE_TTL, related)
    if len(_search_cache) > 300:
        _search_cache.clear()
    return related, {
        'count': len(related),
        'query': query,
        'candidates': candidates,
        'queries': queries,
        'cached': False,
    }


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


async def _complete(
    messages: list[dict],
    api_keys: list[str],
    max_tokens: int,
    temperature: float = 0.2,
) -> str:
    """Llamada corta sin streaming (planificacion de la busqueda)."""
    if not api_keys:
        raise ChatStartError('No hay API key de DeepSeek configurada.')

    payload = {
        'model': CHAT_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': False,
        'thinking': {'type': 'disabled'},
        'max_tokens': max_tokens,
    }
    last_error: Optional[BaseException] = None
    for attempt in range(min(len(api_keys), 2)):
        key = api_keys[attempt % len(api_keys)]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.post(
                    DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                )
            if response.status_code >= 400:
                raise ChatStartError(f'DeepSeek HTTP {response.status_code}: {response.text[:200]}')
            data = response.json()
            choices = data.get('choices') or []
            if not choices:
                return ''
            return (choices[0].get('message') or {}).get('content') or ''
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise ChatStartError(f'No se pudo planificar la busqueda: {last_error}')


async def _plan_search(
    question: str,
    query: str,
    api_keys: list[str],
    log_fn: Callable[[str], None],
) -> dict:
    """Pide al modelo 3-5 consultas y palabras clave para OpenAlex.

    Si el modelo falla se usa un plan heuristico: la busqueda nunca se detiene
    por culpa de la planificacion.
    """
    fallback = _fallback_plan(question, query)
    try:
        raw = await _complete(
            [
                {'role': 'system', 'content': PLANNER_PROMPT},
                {
                    'role': 'user',
                    'content': (
                        f'Pregunta del usuario: {question[:1200]}\n'
                        f'Consulta traducida al ingles: {query[:300]}'
                    ),
                },
            ],
            api_keys,
            max_tokens=PLANNER_MAX_OUTPUT,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat-plan] fallo: {type(exc).__name__}: {exc}')
        return fallback

    parsed = _parse_json_object(raw)
    if not parsed:
        log_fn('[chat-plan] respuesta no parseable; usando plan heuristico')
        return fallback

    queries = [q.strip() for q in (parsed.get('queries') or []) if isinstance(q, str) and q.strip()]
    keywords = [k.strip() for k in (parsed.get('keywords') or []) if isinstance(k, str) and k.strip()]
    if not queries:
        queries = fallback['queries']
    if not keywords:
        keywords = fallback['keywords']
    plan = {'queries': queries[:5], 'keywords': keywords[:10]}
    log_fn(f'[chat-plan] {len(plan["queries"])} consultas: ' + ' | '.join(plan['queries']))
    return plan


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
    """Genera eventos SSE: status, delta, sources, done o error.

    Las fuentes viajan al final, despues del texto, para que la respuesta se
    lea como una redaccion y la lista de papers aparezca recien entonces.
    """
    started = time.time()
    question = (question or '').strip()[:2000]
    if not question:
        yield _sse({'type': 'error', 'message': 'La pregunta esta vacia.'})
        return

    def sources_event(sources: list[dict], meta: dict) -> str:
        return _sse({
            'type': 'sources',
            'sources': sources,
            'access': access,
            'query': meta.get('query') or query,
            'cached': bool(meta.get('cached')),
        })

    yield _sse({'type': 'status', 'text': 'Analizando la pregunta…'})
    try:
        plan = await _plan_search(question, query or question, api_keys, log_fn)
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat-plan] error inesperado: {exc}')
        plan = _fallback_plan(question, query or question)

    query_count = len(_plan_queries(plan, question, query or question))
    yield _sse({
        'type': 'status',
        'text': f'Buscando en {query_count} consultas de OpenAlex…',
    })

    try:
        sources, search_meta = await search_sources(
            query or question,
            access=access,
            probe_fn=probe_fn,
            log_fn=log_fn,
            plan=plan,
            question=question,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat] busqueda fallo: {type(exc).__name__}: {_redact(exc)}')
        yield _sse({'type': 'error', 'message': 'No se pudo buscar en OpenAlex. Intenta de nuevo.'})
        return

    if not sources:
        yield _sse({
            'type': 'status',
            'text': 'Sin papers suficientemente relacionados.',
        })
        yield _sse({
            'type': 'delta',
            'text': ('No encontre papers suficientemente relacionados con la pregunta. '
                     'Proba reformulandola o cambia el filtro de acceso.'),
        })
        yield sources_event([], search_meta)
        yield _sse({'type': 'done', 'usage': {}, 'cost_usd': 0, 'sources_count': 0})
        return

    candidates = int(search_meta.get('candidates') or len(sources))
    yield _sse({
        'type': 'status',
        'text': f'{candidates} papers revisados, {len(sources)} relevantes. Redactando…',
    })

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
        yield sources_event(sources, search_meta)
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
        'candidates': candidates,
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
    # Primero las fuentes y al final el cierre: el cliente las muestra recien
    # cuando el texto ya esta completo.
    yield sources_event(sources, search_meta)
    yield _sse({
        'type': 'done',
        'usage': usage,
        'cost_usd': cost['cost_usd'],
        'sources_count': len(sources),
        'elapsed_sec': elapsed,
    })
