"""Europe PMC como motor de busqueda e investigacion de PsiHub.

Motivo del cambio: OpenAlex deja fuera el texto completo y su resumen a veces
falta; Europe PMC devuelve el abstract, el tipo de estudio (meta-analisis, ECA,
revision sistematica), el acceso abierto verificado y, para los articulos del
PMC, el XML JATS con el articulo entero. Ese XML es la base de la lectura y la
traduccion: no se descarga un PDF para leerlo con vision, se traduce el texto
estructurado, que es mas barato y mas fiel.

El modulo no sabe nada de FastAPI ni del chat: expone busqueda, recuperacion por
identificador, referencias y conversion de JATS a Markdown / modelo de documento.

Si algun dia Europe PMC no responde, `chat.py` y `app.py` caen a OpenAlex; este
modulo es solo el motor primario.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import httpx

EPMC_BASE = os.getenv('EUROPEPMC_BASE', 'https://www.ebi.ac.uk/europepmc/webservices/rest').rstrip('/')
EPMC_API_KEY = os.getenv('EUROPEPMC_API_KEY', '').strip()
EPMC_MAILTO = os.getenv('OPENALEX_MAILTO', 'psyhub.app.research@gmail.com').strip()
EPMC_TIMEOUT = float(os.getenv('EUROPEPMC_TIMEOUT', '20'))
EPMC_CACHE_TTL = int(os.getenv('EUROPEPMC_CACHE_TTL', '900'))
EPMC_PAGE_SIZE = max(1, min(100, int(os.getenv('EUROPEPMC_PAGE_SIZE', '25'))))

USER_AGENT = f'PsiHub/11 (+https://melaraneax.github.io/PsiHub/; {EPMC_MAILTO})'

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')
_JATS_NS_RE = re.compile(r'^\{[^}]+\}')

# ---------------------------------------------------------------------------
# Estado del motor (para el fallback y para /health)
# ---------------------------------------------------------------------------

class EngineState:
    """Cuenta fallos consecutivos para decidir cuando caer a OpenAlex."""

    def __init__(self) -> None:
        self.failures = 0
        self.down_until = 0.0
        self.last_error = ''

    def ok(self) -> None:
        self.failures = 0
        self.down_until = 0.0
        self.last_error = ''

    def fail(self, error: object) -> None:
        self.failures += 1
        self.last_error = f'{type(error).__name__}: {error}'[:200]
        # Un fallo aislado no debe apagar el motor; tres seguidos si.
        if self.failures >= 3:
            self.down_until = time.time() + 120

    @property
    def available(self) -> bool:
        return time.time() >= self.down_until

    def snapshot(self) -> dict:
        return {
            'failures': self.failures,
            'down': not self.available,
            'last_error': self.last_error,
        }


STATE = EngineState()

# Cache en memoria de busquedas: clave -> (vencimiento, works, meta)
_search_cache: dict[str, tuple[float, list[dict], dict]] = {}
_cache_lock = asyncio.Lock()


def _cache_get(key: str) -> Optional[tuple[list[dict], dict]]:
    entry = _search_cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1], entry[2]
    return None


def _cache_put(key: str, works: list[dict], meta: dict) -> None:
    if len(_search_cache) > 400:
        _search_cache.clear()
    _search_cache[key] = (time.time() + EPMC_CACHE_TTL, works, meta)


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def clean_text(value: object, limit: int = 20000) -> str:
    """Quita etiquetas JATS/HTML y normaliza espacios."""
    raw = str(value or '')
    if not raw:
        return ''
    raw = re.sub(r'<jats:title[^>]*>.*?</jats:title>', ' ', raw, flags=re.I | re.S)
    raw = _TAG_RE.sub(' ', raw)
    raw = html.unescape(raw)
    raw = raw.replace('’', "'").replace('‘', "'")
    raw = raw.replace('“', '"').replace('”', '"')
    raw = _WS_RE.sub(' ', raw).strip()
    return raw[:limit]


def normalize(text: object) -> str:
    raw = unicodedata.normalize('NFKD', str(text or ''))
    ascii_text = ''.join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    return re.sub(r'[^a-z0-9 ]+', ' ', ascii_text)


def clean_doi(value: object) -> str:
    raw = str(value or '').strip().lower()
    raw = re.sub(r'^https?://(dx\.)?doi\.org/', '', raw)
    raw = re.sub(r'^doi:\s*', '', raw)
    return raw if raw.startswith('10.') else ''


def clean_query(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').replace('"', ' ')).strip()


# ---------------------------------------------------------------------------
# Revistas: impacto aproximado
# ---------------------------------------------------------------------------
# Europe PMC no publica factor de impacto. Esta tabla cubre las revistas que
# aparecen una y otra vez en psicologia, psiquiatria y medicina basada en
# evidencia; el valor es una posicion relativa 0-1 usada SOLO para ordenar.
# Fuera de la tabla se estima por senales del nombre (Cochrane, JAMA, etc.).

_JOURNAL_IMPACT_HINTS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r'cochrane database', re.I), 1.0),
    (re.compile(r'\b(?:the )?lancet\b', re.I), 0.99),
    (re.compile(r'new england journal|\bnejm\b', re.I), 0.99),
    (re.compile(r'\bjama\b|journal of the american medical association', re.I), 0.98),
    (re.compile(r'\bbmj\b|british medical journal', re.I), 0.97),
    (re.compile(r'world psychiatry', re.I), 0.97),
    (re.compile(r'nature (?:human behaviour|medicine|neuroscience|mental health)', re.I), 0.96),
    (re.compile(r'molecular psychiatry', re.I), 0.95),
    (re.compile(r'american journal of psychiatry', re.I), 0.94),
    (re.compile(r'jama psychiatry|archives of general psychiatry', re.I), 0.94),
    (re.compile(r'lancet psychiatry', re.I), 0.94),
    (re.compile(r'psychological bulletin', re.I), 0.93),
    (re.compile(r'psychotherapy and psychosomatics', re.I), 0.92),
    (re.compile(r'clinical psychology review', re.I), 0.92),
    (re.compile(r'annual review of (?:clinical )?psychology', re.I), 0.91),
    (re.compile(r'biological psychiatry', re.I), 0.90),
    (re.compile(r'molecular psychiatry', re.I), 0.90),
    (re.compile(r'journal of abnormal psychology|journal of psychopathology', re.I), 0.86),
    (re.compile(r'journal of consulting and clinical psychology', re.I), 0.88),
    (re.compile(r'health psychology review', re.I), 0.86),
    (re.compile(r'psychological medicine', re.I), 0.88),
    (re.compile(r'schizophrenia bulletin', re.I), 0.87),
    (re.compile(r'british journal of psychiatry', re.I), 0.86),
    (re.compile(r'depression and anxiety', re.I), 0.82),
    (re.compile(r'journal of anxiety disorders', re.I), 0.80),
    (re.compile(r'behaviour research and therapy', re.I), 0.82),
    (re.compile(r'cognitive behaviour therapy', re.I), 0.72),
    (re.compile(r'journal of affective disorders', re.I), 0.80),
    (re.compile(r'journal of child psychology and psychiatry', re.I), 0.88),
    (re.compile(r'child development|developmental psychology', re.I), 0.80),
    (re.compile(r'personality and social psychology review', re.I), 0.84),
    (re.compile(r'journal of personality and social psychology', re.I), 0.86),
    (re.compile(r'psychotherapy research', re.I), 0.74),
    (re.compile(r'journal of psychotherapy integration', re.I), 0.62),
    (re.compile(r'psychoanalytic|psychodynamic', re.I), 0.55),
    (re.compile(r'frontiers in (?:psychology|psychiatry)', re.I), 0.55),
    (re.compile(r'\bplos one\b', re.I), 0.52),
    (re.compile(r'bmc psychiatry', re.I), 0.60),
    (re.compile(r'journal of clinical (?:psychology|psychiatry)', re.I), 0.72),
    (re.compile(r'comprehensive psychiatry', re.I), 0.72),
    (re.compile(r'european (?:archives of )?psychiatry', re.I), 0.78),
    (re.compile(r'acta psychiatrica scandinavica', re.I), 0.76),
    (re.compile(r'journal of psychiatric research', re.I), 0.74),
    (re.compile(r'psychiatry research', re.I), 0.62),
    (re.compile(r'journal of traumatic stress', re.I), 0.74),
    (re.compile(r'european journal of psychotraumatology', re.I), 0.62),
    (re.compile(r'mindfulness', re.I), 0.60),
    (re.compile(r'journal of contextual behavioral science', re.I), 0.58),
]

# Revistas predadoras o de divulgacion: no deben encabezar resultados.
_WEAK_JOURNAL_RE = re.compile(
    r'(cureus|international journal of (?:advanced|innovative|recent|emerging)|'
    r'journal of (?:advanced|innovative|emerging) research|'
    r'performing arts|visual arts|fine arts|shodh|tourism|marketing|'
    r'business|management|engineering|computer science|civil|mechanical|agricultur)',
    re.I,
)


def journal_impact(journal: object) -> float:
    """Posicion relativa aproximada de la revista (0-1). Desconocida = 0.5."""
    name = clean_text(journal, 300)
    if not name:
        return 0.4
    for pattern, value in _JOURNAL_IMPACT_HINTS:
        if pattern.search(name):
            return value
    if _WEAK_JOURNAL_RE.search(name):
        return 0.15
    return 0.5


def is_weak_journal(journal: object) -> bool:
    return bool(_WEAK_JOURNAL_RE.search(clean_text(journal, 300)))


# ---------------------------------------------------------------------------
# Normalizacion de resultados a la forma que ya usa PsiHub
# ---------------------------------------------------------------------------
# El resto del backend (chat.py) y el frontend trabajan con el shape de
# OpenAlex. Europe PMC se traduce a ese shape para no duplicar logica de
# ranking, acceso y digest.

_PUB_TYPE_DESIGN = [
    (re.compile(r'meta-analysis', re.I), 'meta-análisis'),
    (re.compile(r'systematic review', re.I), 'revisión sistemática'),
    (re.compile(r'randomized controlled trial|randomised controlled trial', re.I), 'ECA'),
    (re.compile(r'clinical trial', re.I), 'ensayo clínico'),
    (re.compile(r'practice guideline', re.I), 'guía de práctica'),
    (re.compile(r'\breview\b', re.I), 'revisión narrativa'),
]


def pub_types(result: dict) -> list[str]:
    raw = ((result.get('pubTypeList') or {}).get('pubType') or [])
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item not in out:
            out.append(item)
    return out


def _design_from_types(types: Iterable[str], title: str = '') -> list[str]:
    blob = ' '.join(types) + ' ' + title
    tags: list[str] = []
    for pattern, label in _PUB_TYPE_DESIGN:
        if pattern.search(blob) and label not in tags:
            tags.append(label)
            break
    return tags


def _year(result: dict) -> Optional[int]:
    raw = (
        result.get('pubYear')
        or (result.get('journalInfo') or {}).get('yearOfPublication')
        or result.get('firstPublicationDate')
        or result.get('firstIndexDate')
    )
    match = re.search(r'\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b', str(raw or ''))
    return int(match.group(1)) if match else None


def _authors(result: dict) -> list[str]:
    people = ((result.get('authorList') or {}).get('author') or [])
    names: list[str] = []
    for person in people:
        if not isinstance(person, dict):
            continue
        name = (person.get('fullName') or person.get('collectiveName') or '').strip()
        if name:
            names.append(name)
    if not names:
        raw = str(result.get('authorString') or '')
        names = [part.strip() for part in raw.split(',') if part.strip()]
    out: list[str] = []
    for name in names:
        if name and name.lower() not in {n.lower() for n in out}:
            out.append(name)
    return out[:40]


def _journal(result: dict) -> str:
    info = result.get('journalInfo') or {}
    journal = info.get('journal') or {}
    return clean_text(
        journal.get('title') or result.get('journalTitle') or journal.get('medlineAbbreviation'),
        300,
    )


def _topics(result: dict) -> list[str]:
    out: list[str] = []
    for heading in ((result.get('meshHeadingList') or {}).get('meshHeading') or []):
        if isinstance(heading, dict):
            name = heading.get('descriptorName')
            if name and name not in out:
                out.append(name)
    for keyword in ((result.get('keywordList') or {}).get('keyword') or []):
        if isinstance(keyword, str) and keyword not in out:
            out.append(keyword)
    return out[:12]


def work_key(result: dict) -> str:
    """Identificador estable y unico: PMC > PMID > DOI > titulo."""
    for field in ('pmcid', 'pmid', 'id'):
        value = str(result.get(field) or '').strip()
        if value:
            source = str(result.get('source') or 'MED').upper()
            return f'{source}:{value}'.lower()
    doi = clean_doi(result.get('doi'))
    if doi:
        return doi
    return normalize(result.get('title'))[:120]


def _fulltext_urls(result: dict) -> tuple[Optional[str], Optional[str]]:
    """(xml_url, pdf_url) oficiales de Europe PMC para este registro."""
    pmcid = str(result.get('pmcid') or '').strip()
    in_epmc = str(result.get('inEPMC') or '').upper() == 'Y'
    xml_url = f'{EPMC_BASE}/{pmcid}/fullTextXML' if pmcid and in_epmc else None
    pdf_url = None
    for entry in ((result.get('fullTextUrlList') or {}).get('fullTextUrl') or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get('documentStyle') or '').lower() == 'pdf':
            candidate = str(entry.get('url') or '')
            if candidate and 'pdf=render' in candidate:
                pdf_url = candidate
                break
    return xml_url, pdf_url


def to_work(result: dict) -> dict:
    """Vista compatible con OpenAlex + campos propios de Europe PMC."""
    title = clean_text(result.get('title'), 500) or 'Sin titulo'
    types = pub_types(result)
    journal = _journal(result)
    year = _year(result)
    abstract = clean_text(result.get('abstractText'))
    pmcid = str(result.get('pmcid') or '').strip()
    xml_url, pdf_url = _fulltext_urls(result)
    is_oa = str(result.get('isOpenAccess') or '').upper() == 'Y'
    in_pmc = str(result.get('inPMC') or '').upper() == 'Y'
    key = work_key(result)
    open_url = None
    if pmcid:
        open_url = f'https://europepmc.org/article/PMC/{pmcid}'
    elif result.get('doi'):
        open_url = f'https://doi.org/{clean_doi(result.get("doi"))}'

    design = _design_from_types(types, title)
    return {
        'id': f'europepmc:{key}',
        'europepmcKey': key,
        'doi': clean_doi(result.get('doi')) or None,
        'title': title,
        'publication_year': year,
        'cited_by_count': int(result.get('citedByCount') or 0),
        'authorships': [
            {'author': {'display_name': name}} for name in _authors(result)
        ],
        'primary_location': {
            'source': {'display_name': journal or None} if journal else None,
            'pdf_url': pdf_url,
            'landing_page_url': open_url,
        },
        'best_oa_location': (
            {'pdf_url': pdf_url, 'is_oa': True, 'landing_page_url': open_url}
            if (pdf_url or xml_url) else None
        ),
        'locations': [],
        'open_access': {
            'is_oa': is_oa or bool(xml_url),
            'oa_status': 'gold' if is_oa else ('green' if xml_url else 'closed'),
            'oa_url': open_url,
        },
        'abstract_text': abstract,
        'primary_topic': None,
        'topics': [],
        'type': 'review' if any('review' in t.lower() or 'meta-analysis' in t.lower() for t in types) else 'article',
        'referenced_works': [],
        # Campos propios
        'epmc': {
            'key': key,
            'source': str(result.get('source') or ''),
            'pmcid': pmcid or None,
            'pmid': str(result.get('pmid') or '') or None,
            'pub_types': types,
            'design': design,
            'journal_impact': journal_impact(journal),
            'journal': journal or None,
            'in_pmc': in_pmc,
            'in_epmc': str(result.get('inEPMC') or '').upper() == 'Y',
            'xml_url': xml_url,
            'pdf_url': pdf_url,
            'license': str(result.get('license') or ''),
            'keywords': _topics(result),
            'has_references': str(result.get('hasReferences') or '').upper() == 'Y',
        },
    }


# ---------------------------------------------------------------------------
# Construccion de la consulta
# ---------------------------------------------------------------------------

_FREE_TEXT_FIELDS = ('TITLE', 'ABSTRACT', 'KW')


def build_query(
    text: str,
    access: str = 'all',
    scope: str = 'psy',
    mode: str = 'broad',
    extra: str = '',
) -> str:
    """Consulta Europe PMC a partir del texto libre del usuario.

    - mode 'precise': exige los terminos en titulo o resumen.
    - mode 'title': solo titulo.
    - mode 'broad': buscador general (incluye texto completo), mas amplio.
    """
    terms = [t for t in clean_query(text).split() if t]
    # Europe PMC ignora los operadores booleanos sueltos en minuscula.
    clauses: list[str] = []
    phrase = clean_query(text)
    if phrase:
        if mode == 'title':
            clauses.append(f'TITLE:"{phrase[:250]}"')
        elif mode == 'precise':
            inner = ' AND '.join(f'TITLE_ABS:{quote(t, safe="")}' for t in terms[:10])
            clauses.append(f'({inner})' if inner else f'TITLE_ABS:"{phrase[:250]}"')
        else:
            clauses.append(f'({phrase[:250]})')
    if extra:
        clauses.append(f'({extra})')

    filters: list[str] = ['(SRC:MED OR SRC:PMC)']
    if access in ('free', 'oneclick'):
        filters.append('OPEN_ACCESS:Y')
    elif access == 'paid':
        filters.append('OPEN_ACCESS:N')
    # Editoriales, cartas y erratas no son evidencia.
    filters.append(
        '(NOT PUB_TYPE:"Editorial" AND NOT PUB_TYPE:"Comment" AND NOT PUB_TYPE:"Letter" '
        'AND NOT PUB_TYPE:"Published Erratum" AND NOT PUB_TYPE:"Retraction of Publication" '
        'AND NOT PUB_TYPE:"Published Erratum")'
    )
    body = ' AND '.join(clauses) if clauses else '(psychology)'
    return f'{body} AND ' + ' AND '.join(filters)


_SORT_MAP = {
    'cited_by_count:desc': 'CITED desc',
    'publication_date:desc': 'P_PDATE_D desc',
    'publication_year:desc': 'P_PDATE_D desc',
    'relevance': '',
}


def _params(query: str, page_size: int, cursor: str, sort: Optional[str]) -> dict:
    params = {
        'query': query,
        'format': 'json',
        'resultType': 'core',
        'pageSize': str(max(1, min(100, page_size))),
        'synonym': 'TRUE',
    }
    if cursor and cursor != '*':
        params['cursorMark'] = cursor
    order = _SORT_MAP.get(str(sort or ''), '')
    if order:
        params['sort'] = order
    if EPMC_API_KEY:
        params['apiKey'] = EPMC_API_KEY
    return params


# ---------------------------------------------------------------------------
# Orden por relevancia (propio)
# ---------------------------------------------------------------------------
#
# Sin `sort`, Europe PMC devuelve practicamente lo mas nuevo primero: para un
# tema clasico la primera pagina se llena de articulos de este anio con cero
# citas. Eso no es "relevancia", es fecha. Para la busqueda de la interfaz se
# piden dos corrientes a la vez —la del motor y la de mas citados— y se mezclan
# con una puntuacion propia: coincidencia con lo buscado, citas, juventud
# suave y tipo de estudio. El chat no usa este camino: el ya reordena con su
# jerarquia de evidencia.

_RELEVANCE_STOPWORDS = {
    'the', 'and', 'of', 'with', 'for', 'from', 'this', 'that', 'these', 'those',
    'into', 'about', 'between', 'among', 'over', 'under', 'their', 'there',
    'para', 'por', 'con', 'los', 'las', 'del', 'una', 'unos', 'unas', 'que',
}


def _query_terms(text: str) -> list[str]:
    words = re.findall(r'[a-z0-9áéíóúüñ-]{3,}', clean_query(text or '').lower())
    seen: list[str] = []
    for word in words:
        if word in _RELEVANCE_STOPWORDS or word in seen:
            continue
        seen.append(word)
    return seen[:12]


def relevance_score(
    work: dict,
    terms: list[str],
    max_cited: int,
    current_year: int,
) -> float:
    """Puntuacion de relevancia para mezclar las dos corrientes de Europe PMC."""
    import math

    title = str(work.get('title') or '').lower()
    abstract = str(work.get('abstract_text') or '').lower()[:6000]
    title_hits = sum(1 for term in terms if term in title)
    body_hits = sum(1 for term in terms if term in abstract)
    total = max(1, len(terms))
    match = title_hits / total
    body = body_hits / total

    cited = math.log1p(max(0, int(work.get('cited_by_count') or 0)))
    cited_norm = cited / math.log1p(max_cited) if max_cited > 0 else 0.0

    year = int(work.get('publication_year') or 0)
    freshness = max(0.0, 1.0 - max(0, current_year - year) / 15.0) if year else 0.0

    types = ' '.join((work.get('epmc') or {}).get('pub_types') or []).lower()
    if 'meta-analysis' in types:
        evidence = 0.45
    elif 'systematic review' in types or 'review' in types:
        evidence = 0.30
    elif 'randomized controlled trial' in types or 'clinical trial' in types:
        evidence = 0.20
    else:
        evidence = 0.0

    return 2.6 * match + 0.9 * body + 1.5 * cited_norm + 0.6 * freshness + evidence


def _encode_cursor(first: str, second: str, seen: list[str]) -> str:
    import base64
    import json as _json

    raw = _json.dumps({'a': first or '*', 'b': second or '*', 'seen': seen[-200:]})
    return 'r:' + base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii').rstrip('=')


def _decode_cursor(cursor: str) -> tuple[str, str, list[str]]:
    import base64
    import json as _json

    raw = str(cursor or '*')
    if not raw.startswith('r:'):
        # Cursor del motor (o de una version anterior): arranca las dos corrientes.
        return (raw or '*', raw or '*', [])
    try:
        payload = base64.urlsafe_b64decode(raw[2:] + '=' * (-len(raw[2:]) % 4))
        data = _json.loads(payload.decode('utf-8'))
        return (
            str(data.get('a') or '*'),
            str(data.get('b') or '*'),
            list(data.get('seen') or []),
        )
    except Exception:  # noqa: BLE001 - cursor corrupto: se reinicia la cadena
        return ('*', '*', [])


# ---------------------------------------------------------------------------
# Cliente HTTP
# ---------------------------------------------------------------------------

def client(timeout: float = EPMC_TIMEOUT) -> httpx.AsyncClient:
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        headers=headers,
    )


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    url = path if path.startswith('http') else f'{EPMC_BASE}{path}'
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = await client.get(url, params=params)
            if response.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(min(1.5 * (2 ** attempt), 6.0))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Respuesta invalida de Europe PMC.')
            return payload
        except Exception as exc:  # noqa: BLE001 - se reintenta
            last = exc
            await asyncio.sleep(min(1.2 * (attempt + 1), 4.0))
    raise last or RuntimeError('Europe PMC no respondio.')


async def search(
    text: str,
    page_size: int = EPMC_PAGE_SIZE,
    cursor: str = '*',
    access: str = 'all',
    sort: Optional[str] = None,
    scope: str = 'psy',
    mode: str = 'broad',
    client_: Optional[httpx.AsyncClient] = None,
    balanced: bool = False,
) -> tuple[list[dict], dict]:
    """Una pagina de resultados. Devuelve (works, meta).

    `balanced` solo lo usa la busqueda de la interfaz cuando el orden pedido es
    relevancia: mezcla la corriente del motor con la de mas citados para que la
    primera pagina no sea unicamente lo publicado este anio.
    """
    query = build_query(text, access=access, scope=scope, mode=mode)
    balanced = balanced and (sort or '') in ('', 'relevance')
    cache_key = f'{query}|{page_size}|{cursor}|{"balanced" if balanced else (sort or "")}'
    cached = _cache_get(cache_key)
    if cached:
        works, meta = cached
        return works, {**meta, 'cached': True}

    owned = client_ is None
    http = client_ or client()
    try:
        if balanced:
            works, meta = await _balanced_page(http, text, query, page_size, cursor)
        else:
            payload = await _get_json(http, '/search', _params(query, page_size, cursor, sort))
            results = ((payload.get('resultList') or {}).get('result') or [])
            works = [to_work(r) for r in results if isinstance(r, dict)]
            meta = {
                'count': int(payload.get('hitCount') or 0),
                'cursor': payload.get('nextCursorMark') or '',
                'query': query,
                'engine': 'europepmc',
                'cached': False,
            }
    finally:
        if owned:
            await http.aclose()

    _cache_put(cache_key, works, meta)
    return works, meta


async def _balanced_page(
    http: httpx.AsyncClient,
    text: str,
    query: str,
    page_size: int,
    cursor: str,
) -> tuple[list[dict], dict]:
    """Mezcla la corriente del motor (fecha) con la de mas citados y reordena."""
    size = max(1, min(100, page_size))
    pool_size = min(100, max(size * 2, size))
    first_cursor, second_cursor, seen = _decode_cursor(cursor)

    first, second = await asyncio.gather(
        _get_json(http, '/search', _params(query, pool_size, first_cursor, None)),
        _get_json(http, '/search', _params(query, pool_size, second_cursor, 'cited_by_count:desc')),
        return_exceptions=True,
    )
    payloads = [p for p in (first, second) if isinstance(p, dict)]
    if not payloads:
        error = first if isinstance(first, Exception) else second
        raise error if isinstance(error, Exception) else RuntimeError('Europe PMC no respondio.')

    works: list[dict] = []
    seen_set = set(seen)
    hit_count = 0
    for payload in payloads:
        hit_count = max(hit_count, int(payload.get('hitCount') or 0))
        for result in ((payload.get('resultList') or {}).get('result') or []):
            work = to_work(result) if isinstance(result, dict) else None
            if not work:
                continue
            key = work.get('europepmcKey') or work.get('id')
            if not key or key in seen_set:
                continue
            seen_set.add(key)
            works.append(work)

    terms = _query_terms(text)
    max_cited = max((int(w.get('cited_by_count') or 0) for w in works), default=0)
    current_year = time.gmtime().tm_year
    works.sort(
        key=lambda w: relevance_score(w, terms, max_cited, current_year),
        reverse=True,
    )
    page = works[:size]
    shown = [str(w.get('europepmcKey') or w.get('id')) for w in page]

    next_first = str(first.get('nextCursorMark') or '') if isinstance(first, dict) else ''
    next_second = str(second.get('nextCursorMark') or '') if isinstance(second, dict) else ''
    return page, {
        'count': hit_count,
        'cursor': _encode_cursor(next_first, next_second, seen + shown),
        'query': query,
        'engine': 'europepmc',
        'cached': False,
    }


async def fetch_by_ids(
    keys: list[str],
    client_: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Recupera registros concretos por clave `SRC:ID` (med:123, pmc:PMC123)."""
    clean = [k for k in (str(x or '').strip() for x in keys) if ':' in k]
    if not clean:
        return []
    works: list[dict] = []
    owned = client_ is None
    http = client_ or client()
    try:
        for start in range(0, len(clean), 25):
            batch = clean[start:start + 25]
            parts = []
            for key in batch:
                source, _, ident = key.partition(':')
                parts.append(f'(EXT_ID:{quote(ident, safe="")} AND SRC:{source.upper()})')
            query = '(' + ' OR '.join(parts) + ')'
            try:
                payload = await _get_json(http, '/search', {
                    'query': query, 'format': 'json', 'resultType': 'core',
                    'pageSize': str(len(batch)),
                })
            except Exception:  # noqa: BLE001 - expansion opcional
                continue
            for result in ((payload.get('resultList') or {}).get('result') or []):
                if isinstance(result, dict):
                    works.append(to_work(result))
    finally:
        if owned:
            await http.aclose()
    return works


async def references(
    key: str,
    limit: int = 80,
    client_: Optional[httpx.AsyncClient] = None,
) -> list[str]:
    """Claves de los trabajos que cita un articulo (para expansion por bola de nieve)."""
    source, _, ident = str(key or '').partition(':')
    if not source or not ident:
        return []
    owned = client_ is None
    http = client_ or client()
    try:
        payload = await _get_json(http, f'/{source.upper()}/{quote(ident, safe="")}/references', {
            'format': 'json', 'pageSize': str(min(200, limit)),
        })
    except Exception:  # noqa: BLE001
        return []
    finally:
        if owned:
            await http.aclose()
    refs: list[str] = []
    for entry in ((payload.get('referenceList') or {}).get('reference') or []):
        if not isinstance(entry, dict):
            continue
        src = str(entry.get('source') or '').upper()
        ident_ref = str(entry.get('id') or '').strip()
        if src and ident_ref:
            refs.append(f'{src}:{ident_ref}')
    return refs


async def lookup_doi(doi: str, client_: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Ficha de un DOI. Devuelve el work normalizado o None."""
    clean = clean_doi(doi)
    if not clean:
        return None
    owned = client_ is None
    http = client_ or client()
    try:
        payload = await _get_json(http, '/search', {
            'query': f'DOI:"{clean}"', 'format': 'json', 'resultType': 'core', 'pageSize': '3',
        })
    except Exception:  # noqa: BLE001
        return None
    finally:
        if owned:
            await http.aclose()
    results = ((payload.get('resultList') or {}).get('result') or [])
    for result in results:
        if isinstance(result, dict) and clean_doi(result.get('doi')) == clean:
            return to_work(result)
    if results and isinstance(results[0], dict):
        return to_work(results[0])
    return None


async def full_text_xml(pmcid: str, client_: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """XML JATS del articulo completo (solo si esta en el PMC)."""
    clean = re.sub(r'[^A-Za-z0-9]', '', str(pmcid or ''))
    if not clean.upper().startswith('PMC'):
        clean = f'PMC{clean}'
    if clean.upper() == 'PMC':
        return None
    owned = client_ is None
    http = client_ or client(timeout=30.0)
    try:
        url = f'{EPMC_BASE}/{clean.upper()}/fullTextXML'
        # El endpoint de XML responde 406 si se le pide JSON: el Accept de la
        # sesion (application/json) no sirve aca.
        response = await http.get(url, headers={'Accept': 'application/xml, text/xml, */*'})
        if response.status_code != 200:
            return None
        text = response.text
        if '<article' not in text[:8000]:
            return None
        return text
    except Exception:  # noqa: BLE001
        return None
    finally:
        if owned:
            await http.aclose()


# --- cache de texto completo ------------------------------------------------
# El XML se usa tres veces (abstract, chat y lector): guardarlo en disco evita
# volver a descargarlo y hace que la lectura sea instantanea la segunda vez.

_FULLTEXT_DIR = (Path(os.getenv('CACHE_DIR', './cache')).resolve() / 'fulltext')
_memory_xml: dict[str, str] = {}


def _xml_path(pmcid: str) -> Path:
    return _FULLTEXT_DIR / f'{re.sub(r"[^A-Za-z0-9]", "", pmcid).upper()}.xml'


async def full_text_xml_cached(
    pmcid: str,
    client_: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    clean = re.sub(r'[^A-Za-z0-9]', '', str(pmcid or '')).upper()
    if not clean:
        return None
    if clean in _memory_xml:
        return _memory_xml[clean]
    path = _xml_path(clean)
    try:
        if path.exists() and path.stat().st_size > 800:
            text = await asyncio.to_thread(path.read_text, encoding='utf-8')
            _memory_xml[clean] = text
            return text
    except Exception:  # noqa: BLE001
        pass
    text = await full_text_xml(clean, client_)
    if not text:
        return None
    _memory_xml[clean] = text
    if len(_memory_xml) > 60:
        _memory_xml.pop(next(iter(_memory_xml)))
    try:
        await asyncio.to_thread(_FULLTEXT_DIR.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, text, encoding='utf-8')
    except Exception:  # noqa: BLE001 - el cache es opcional
        pass
    return text


def xml_abstract(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ''
    front = _child(root, 'front')
    article_meta = _child(front, 'article-meta') if front is not None else None
    abstract = _child(article_meta, 'abstract') if article_meta is not None else None
    if abstract is None:
        return ''
    parts: list[str] = []
    for node in abstract:
        tag = _local(node.tag)
        if tag == 'p':
            parts.append(_plain(node))
        elif tag == 'sec':
            head = _plain(_child(node, 'title'))
            body = ' '.join(_plain(p) for p in _children(node, 'p'))
            parts.append(f'{head.rstrip(":")}: {body}'.strip(': '))
    return clean_text(' '.join(p for p in parts if p))


async def fill_abstracts(
    works: list[dict],
    limit: int = 10,
    concurrency: int = 4,
    client_: Optional[httpx.AsyncClient] = None,
) -> int:
    """Completa el resumen de los papers que solo lo tienen en el texto completo.

    Los registros que existen unicamente en PMC no traen `abstractText` en la
    busqueda; sin resumen no se pueden ordenar ni citar. Se rellena desde el
    XML, que igual se va a usar despues para leer el articulo.
    """
    targets = [
        w for w in works
        if not w.get('abstract_text') and (w.get('epmc') or {}).get('pmcid')
    ][:limit]
    if not targets:
        return 0
    semaphore = asyncio.Semaphore(max(1, concurrency))
    filled = 0

    async def one(work: dict) -> None:
        nonlocal filled
        pmcid = (work.get('epmc') or {}).get('pmcid') or ''
        async with semaphore:
            xml_text = await full_text_xml_cached(pmcid, client_)
        if not xml_text:
            return
        abstract = xml_abstract(xml_text)
        if abstract:
            work['abstract_text'] = abstract
            filled += 1

    await asyncio.gather(*(one(w) for w in targets))
    return filled


# ---------------------------------------------------------------------------
# JATS (XML de PMC) -> Markdown / modelo de documento
# ---------------------------------------------------------------------------

def _local(tag: object) -> str:
    return _JATS_NS_RE.sub('', str(tag))


def _child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _itertext(element: Optional[ET.Element]) -> str:
    if element is None:
        return ''
    return clean_text(''.join(element.itertext()))


def _plain(element: Optional[ET.Element]) -> str:
    """Texto de un nodo JATS conservando cursivas como énfasis Markdown."""
    if element is None:
        return ''
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        tag = _local(child.tag)
        inner = _plain(child)
        if tag in ('italic', 'i'):
            parts.append(f'_{inner}_' if inner.strip() else inner)
        elif tag in ('bold', 'b', 'strong'):
            parts.append(f'**{inner}**' if inner.strip() else inner)
        elif tag == 'sup':
            parts.append(f'^{inner}^' if inner.strip() else inner)
        elif tag == 'sub':
            parts.append(f'~{inner}~' if inner.strip() else inner)
        elif tag in ('xref', 'ext-link', 'uri'):
            parts.append(inner)
        else:
            parts.append(inner)
        if child.tail:
            parts.append(child.tail)
    text = ''.join(parts)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


_SKIP_SECTIONS = re.compile(
    r'^(references|bibliography|acknowledg|funding|author contribution|'
    r'conflict of interest|competing interest|data availability|'
    r'supplementary|publisher.s note|generative ai|correction note|footnotes|'
    r'ethics statement|associated data|abbreviation)',
    re.I,
)


def _front_meta(root: ET.Element) -> dict:
    meta: dict = {'authors': [], 'keywords': [], 'affiliations': []}
    front = _child(root, 'front')
    article_meta = _child(front, 'article-meta') if front is not None else None
    journal_meta = _child(front, 'journal-meta') if front is not None else None

    title_group = _child(article_meta, 'title-group') if article_meta is not None else None
    meta['title'] = _itertext(_child(title_group, 'article-title') if title_group is not None else None)

    subtitle = _child(title_group, 'subtitle') if title_group is not None else None
    if subtitle is not None:
        meta['subtitle'] = _itertext(subtitle)

    if journal_meta is not None:
        group = _child(journal_meta, 'journal-title-group')
        meta['journal'] = _itertext(_child(group, 'journal-title') if group is not None else None)

    if article_meta is not None:
        for ident in _children(article_meta, 'article-id'):
            kind = str(ident.get('pub-id-type') or '').lower()
            if kind == 'doi':
                meta['doi'] = clean_doi(_itertext(ident))
            elif kind == 'pmcid':
                meta['pmcid'] = _itertext(ident)
            elif kind == 'pmid':
                meta['pmid'] = _itertext(ident)
        for pub_date in _children(article_meta, 'pub-date'):
            year = _itertext(_child(pub_date, 'year'))
            match = re.search(r'(1[5-9]\d{2}|20\d{2}|21\d{2})', year)
            if match:
                meta['year'] = int(match.group(1))
                break
        for affiliation in _children(article_meta, 'aff'):
            text = _itertext(affiliation)
            if text:
                meta['affiliations'].append(text)
        kwd_group = _child(article_meta, 'kwd-group')
        if kwd_group is not None:
            meta['keywords'] = [
                _itertext(kwd) for kwd in _children(kwd_group, 'kwd') if _itertext(kwd)
            ]

    contributions = _child(article_meta, 'contrib-group') if article_meta is not None else None
    if contributions is not None:
        for contrib in _children(contributions, 'contrib'):
            name = _child(contrib, 'name')
            if name is None:
                continue
            given = _itertext(_child(name, 'given-names'))
            surname = _itertext(_child(name, 'surname'))
            full = ' '.join(part for part in (given, surname) if part) or _itertext(name)
            if full:
                meta['authors'].append(full)

    abstract = _child(article_meta, 'abstract') if article_meta is not None else None
    if abstract is not None:
        parts: list[str] = []
        for node in abstract:
            tag = _local(node.tag)
            if tag == 'p':
                parts.append(_plain(node))
            elif tag == 'sec':
                head = _plain(_child(node, 'title'))
                body = ' '.join(_plain(p) for p in _children(node, 'p'))
                parts.append(f'{head.rstrip(":")}: {body}'.strip(': '))
        meta['abstract'] = ' '.join(p for p in parts if p)
    return meta


def _table_block(table_wrap: ET.Element) -> Optional[dict]:
    label = _plain(_child(table_wrap, 'label'))
    caption = _plain(_child(table_wrap, 'caption'))
    table = _child(table_wrap, 'table')
    if table is None:
        return None
    rows: list[list[str]] = []
    headers: list[str] = []
    for row in (table.iter() if table is not None else []):
        if _local(row.tag) != 'tr':
            continue
        cells: list[str] = []
        is_header = False
        for cell in row:
            if _local(cell.tag) not in ('td', 'th'):
                continue
            if _local(cell.tag) == 'th':
                is_header = True
            cells.append(_plain(cell))
        if not cells:
            continue
        if is_header and not headers:
            headers = cells
        else:
            rows.append(cells)
    if not rows and not headers:
        return None
    if len(rows) > 24:
        rows = rows[:24]
    return {
        'type': 'table',
        'caption': ' '.join(part for part in (label, caption) if part) or None,
        'headers': headers[:8],
        'rows': [r[:8] for r in rows],
    }


def xml_to_document(xml_text: str) -> dict:
    """Convierte JATS en el documento estructurado que usan los exportadores.

    Mantiene el texto en el idioma original; la traduccion se hace despues y
    reemplaza los textos, nunca la estructura.
    """
    root = ET.fromstring(xml_text)
    meta = _front_meta(root)
    blocks: list[dict] = []

    def add_para(text: str) -> None:
        text = text.strip()
        if text:
            blocks.append({'type': 'paragraph', 'text': text})

    def walk_section(section: ET.Element, level: int) -> None:
        head = _plain(_child(section, 'title'))
        if head and not _SKIP_SECTIONS.search(head):
            blocks.append({'type': 'heading', 'level': min(level, 4), 'text': head})
        for node in section:
            tag = _local(node.tag)
            if tag == 'title':
                continue
            if tag == 'p':
                add_para(_plain(node))
            elif tag == 'sec':
                walk_section(node, level + 1)
            elif tag == 'table-wrap':
                block = _table_block(node)
                if block:
                    blocks.append(block)
            elif tag == 'fig':
                caption = _plain(_child(node, 'caption'))
                label = _plain(_child(node, 'label'))
                blocks.append({
                    'type': 'figure',
                    'label': label or None,
                    'caption': caption or label or None,
                })
            elif tag in ('list', 'def-list'):
                items = [_plain(item) for item in node if _local(item.tag) in ('list-item', 'def-item')]
                items = [i for i in items if i]
                if items:
                    blocks.append({'type': 'list', 'ordered': False, 'items': items})
            elif tag == 'boxed-text':
                add_para(_plain(node))
            elif tag == 'disp-quote':
                add_para(_plain(node))
        return

    body = _child(root, 'body')
    if body is not None:
        for node in body:
            tag = _local(node.tag)
            if tag == 'p':
                add_para(_plain(node))
            elif tag == 'sec':
                walk_section(node, 1)
            elif tag == 'table-wrap':
                block = _table_block(node)
                if block:
                    blocks.append(block)

    meta.setdefault('language', 'English')
    return {
        'document': {
            'title': meta.get('title') or 'Articulo de PMC',
            'subtitle': meta.get('subtitle'),
            'authors': meta.get('authors') or [],
            'affiliations': (meta.get('affiliations') or [])[:6],
            'journal': meta.get('journal'),
            'year': meta.get('year'),
            'doi': meta.get('doi'),
            'pmcid': meta.get('pmcid'),
            'pmid': meta.get('pmid'),
            'abstract_original': meta.get('abstract') or '',
            'keywords': meta.get('keywords') or [],
            'language': 'English',
        },
        'blocks': blocks,
        'layout': {'columns': 1, 'pages': 0},
        'stats': {'pages': 0, 'figures': sum(1 for b in blocks if b.get('type') == 'figure')},
    }


def xml_to_markdown(xml_text: str) -> str:
    """Markdown en el idioma original (para lectura rapida y para el chat)."""
    from exporters import document_to_markdown
    return document_to_markdown(xml_to_document(xml_text))


_SECTION_KEYS = re.compile(r'(result|finding|conclusion|discussion|outcome|abstract)', re.I)


def xml_sections(xml_text: str) -> dict[str, str]:
    """Secciones del articulo indexadas por titulo, para citar el texto real."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}

    def walk(section: ET.Element) -> None:
        title = _plain(_child(section, 'title'))
        paragraphs = [_plain(p) for p in _children(section, 'p')]
        body = ' '.join(p for p in paragraphs if p)
        if title and body:
            out[title] = body
        for child in _children(section, 'sec'):
            walk(child)

    body = _child(root, 'body')
    if body is not None:
        for section in _children(body, 'sec'):
            walk(section)
        for paragraph in _children(body, 'p'):
            text = _plain(paragraph)
            if text:
                out.setdefault('Body', '')
                out['Body'] = (out['Body'] + ' ' + text).strip()
    return out


def to_paper(work: dict) -> dict:
    """Forma que consume la interfaz (la misma que devolvia OpenAlex)."""
    epmc = work.get('epmc') or {}
    authors = [
        (a.get('author') or {}).get('display_name') for a in (work.get('authorships') or [])
    ]
    authors = [a for a in authors if a]
    journal = epmc.get('journal') or ((work.get('primary_location') or {}).get('source') or {}).get('display_name')
    has_xml = bool(epmc.get('xml_url'))
    is_oa = bool((work.get('open_access') or {}).get('is_oa'))
    if has_xml:
        access = 'oneclick'
    elif is_oa:
        access = 'free'
    else:
        access = 'paid'
    topics = list(epmc.get('keywords') or [])
    return {
        'id': work.get('europepmcKey') or work.get('id'),
        'key': epmc.get('key'),
        'doi': work.get('doi'),
        'title': work.get('title') or 'Sin título',
        'authors': authors,
        'firstInstitution': None,
        'journal': journal,
        'year': work.get('publication_year'),
        'isOa': is_oa,
        'oaStatus': (work.get('open_access') or {}).get('oa_status'),
        'oaUrl': (work.get('open_access') or {}).get('oa_url'),
        'pdfUrl': epmc.get('pdf_url'),
        'isAutoDownloadable': has_xml,
        'citations': int(work.get('cited_by_count') or 0),
        'abstract': clean_text(work.get('abstract_text'), 6000),
        'topics': topics[:10],
        'type': work.get('type'),
        'mesh': topics[:10],
        'design': epmc.get('design') or [],
        'pubTypes': epmc.get('pub_types') or [],
        'pmcid': epmc.get('pmcid'),
        'pmid': epmc.get('pmid'),
        'hasFullText': has_xml,
        'journalImpact': epmc.get('journal_impact'),
        'engine': 'europepmc',
    }


def key_sections(xml_text: str, budget: int = 2400) -> str:
    """Resumen del texto completo con las partes que sostienen la conclusion.

    Se priorizan resultados y discusion/conclusion; si no aparecen, se usan las
    primeras secciones. Nunca inventa: solo recorta lo que el XML trae.
    """
    sections = xml_sections(xml_text)
    if not sections:
        return ''

    def weight(title: str) -> int:
        if re.search(r'conclusion', title, re.I):
            return 3
        if re.search(r'result|finding|outcome', title, re.I):
            return 2
        if re.search(r'discussion|limitation', title, re.I):
            return 1
        return 0

    ordered = sorted(sections.items(), key=lambda item: -weight(item[0]))
    parts: list[str] = []
    used = 0
    for title, body in ordered:
        if not body:
            continue
        remaining = budget - used
        if remaining <= 120:
            break
        chunk = body[:remaining]
        if len(body) > remaining:
            cut = chunk.rsplit('. ', 1)[0]
            chunk = (cut + '.') if len(cut) > remaining * 0.5 else chunk
        parts.append(f'{title}: {chunk}')
        used += len(chunk)
    return '\n'.join(parts).strip()
