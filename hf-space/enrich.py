"""Completa metadatos faltantes de un paper con APIs academicas gratuitas.

OpenAlex a veces devuelve trabajos sin resumen, año o DOI (sobre todo articulos
antiguos o de revistas que no exponen el abstract). Antes de descartarlos, el
backend consulta Crossref, Europe PMC y Semantic Scholar y rellena SOLO los
campos vacios. Asi el buscador puede mostrar el resumen y el chat puede usar el
paper para investigar.

El resultado se cachea en memoria (7 dias) y es conservador: nunca reemplaza un
dato que OpenAlex ya trajo, y para busquedas por titulo exige una similitud alta
para no pegar el resumen de otro articulo.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import time
import unicodedata
from typing import Optional
from urllib.parse import quote

import httpx

OPENALEX_MAILTO = os.getenv('OPENALEX_MAILTO', 'psyhub.app.research@gmail.com').strip()
SEMANTIC_SCHOLAR_API_KEY = os.getenv('SEMANTIC_SCHOLAR_API_KEY', '').strip()
CROSSREF_URL = 'https://api.crossref.org/works'
EPMC_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
S2_URL = 'https://api.semanticscholar.org/graph/v1'
USER_AGENT = f'PsiHub/10 (+https://melaraneax.github.io/PsiHub/; {OPENALEX_MAILTO})'

REQUEST_TIMEOUT = 8.0
CACHE_TTL = 7 * 24 * 3600
MAX_ITEMS = 20
CONCURRENCY = 6
MIN_TITLE_SIMILARITY = 0.62

_cache: dict[str, tuple[float, dict]] = {}
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _norm_title(text: object) -> str:
    raw = unicodedata.normalize('NFKD', str(text or ''))
    ascii_text = ''.join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    return re.sub(r'[^a-z0-9 ]+', ' ', ascii_text)


def _title_tokens(text: object) -> set[str]:
    return {w for w in _norm_title(text).split() if len(w) > 2}


def _title_similarity(a: object, b: object) -> float:
    left, right = _title_tokens(a), _title_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _clean_doi(value: object) -> str:
    raw = str(value or '').strip().lower()
    raw = re.sub(r'^https?://(dx\.)?doi\.org/', '', raw)
    raw = re.sub(r'^doi:\s*', '', raw)
    return raw if raw.startswith('10.') else ''


def _clean_abstract(value: object) -> str:
    raw = str(value or '')
    if not raw:
        return ''
    raw = re.sub(r'<jats:title[^>]*>.*?</jats:title>', ' ', raw, flags=re.I | re.S)
    raw = _TAG_RE.sub(' ', raw)
    raw = html.unescape(raw)
    raw = re.sub(r'^\s*(?:abstract|resumen)\s*[:.\-–]?\s*', '', raw, flags=re.I)
    raw = _WS_RE.sub(' ', raw).strip()
    return raw if len(raw) >= 120 else ''


def _year(value: object) -> Optional[int]:
    match = re.search(r'\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b', str(value or ''))
    return int(match.group(1)) if match else None


def _authors_from_names(names: list[object]) -> list[str]:
    seen: list[str] = []
    for name in names:
        clean = _WS_RE.sub(' ', str(name or '')).strip(' ,;')
        if clean and clean.lower() not in {s.lower() for s in seen}:
            seen.append(clean)
    return seen[:40]


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------

def _from_crossref(message: dict) -> dict:
    if not isinstance(message, dict):
        return {}
    titles = message.get('title') or []
    title = titles[0] if isinstance(titles, list) and titles else ''
    issued = message.get('issued') or message.get('published-print') or message.get('published-online') or {}
    parts = (issued.get('date-parts') or [[]])[0]
    year = parts[0] if parts and isinstance(parts[0], int) else None
    authors = []
    for author in message.get('author') or []:
        if not isinstance(author, dict):
            continue
        given = str(author.get('given') or '').strip()
        family = str(author.get('family') or '').strip()
        name = ' '.join(part for part in (given, family) if part)
        if name:
            authors.append(name)
    containers = message.get('container-title') or []
    journal = containers[0] if isinstance(containers, list) and containers else ''
    return {
        'title': str(title or '').strip(),
        'abstract': _clean_abstract(message.get('abstract')),
        'year': year,
        'doi': _clean_doi(message.get('DOI')),
        'authors': _authors_from_names(authors),
        'journal': str(journal or '').strip(),
        'source': 'crossref',
    }


def _from_epmc(result: dict) -> dict:
    if not isinstance(result, dict):
        return {}
    journal_info = (result.get('journalInfo') or {}).get('journal') or {}
    return {
        'title': str(result.get('title') or '').strip().rstrip('.'),
        'abstract': _clean_abstract(result.get('abstractText')),
        'year': _year(result.get('pubYear') or result.get('firstPublicationDate')),
        'doi': _clean_doi(result.get('doi')),
        'authors': _authors_from_names(
            [a.get('fullName') for a in ((result.get('authorList') or {}).get('author') or []) if isinstance(a, dict)]
        ),
        'journal': str(journal_info.get('title') or result.get('journalTitle') or '').strip(),
        'source': 'europepmc',
    }


def _from_s2(paper: dict) -> dict:
    if not isinstance(paper, dict):
        return {}
    external = paper.get('externalIds') or {}
    return {
        'title': str(paper.get('title') or '').strip(),
        'abstract': _clean_abstract(paper.get('abstract')),
        'year': _year(paper.get('year')),
        'doi': _clean_doi(external.get('DOI')),
        'authors': _authors_from_names(
            [a.get('name') for a in (paper.get('authors') or []) if isinstance(a, dict)]
        ),
        'journal': str(paper.get('venue') or '').strip(),
        'source': 'semanticscholar',
    }


async def _json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None,
                headers: Optional[dict] = None) -> Optional[dict]:
    try:
        response = await client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - una fuente caida no invalida las demas
        return None


async def _crossref(client: httpx.AsyncClient, item: dict) -> dict:
    doi = _clean_doi(item.get('doi'))
    if doi:
        payload = await _json(client, f'{CROSSREF_URL}/{quote(doi, safe="/:")}')
        message = (payload or {}).get('message')
        if isinstance(message, dict):
            return _from_crossref(message)
    title = str(item.get('title') or '').strip()
    if not title:
        return {}
    payload = await _json(client, CROSSREF_URL, params={
        'query.bibliographic': title[:220],
        'rows': '3',
        'select': 'DOI,title,author,issued,container-title,abstract',
        'mailto': OPENALEX_MAILTO,
    })
    items = ((payload or {}).get('message') or {}).get('items') or []
    best = None
    best_score = 0.0
    for candidate in items:
        score = _title_similarity(title, candidate.get('title', [''])[0]
                                  if isinstance(candidate.get('title'), list) else candidate.get('title'))
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= MIN_TITLE_SIMILARITY:
        return _from_crossref(best)
    return {}


async def _epmc(client: httpx.AsyncClient, item: dict) -> dict:
    doi = _clean_doi(item.get('doi'))
    title = str(item.get('title') or '').strip()
    if doi:
        query = f'DOI:"{doi}"'
    elif title:
        safe = title.replace('"', ' ')[:200]
        query = f'TITLE:"{safe}"'
    else:
        return {}
    payload = await _json(client, EPMC_URL, params={
        'query': query,
        'format': 'json',
        'resultType': 'core',
        'pageSize': '3',
    })
    results = ((payload or {}).get('resultList') or {}).get('result') or []
    best = None
    best_score = 0.0
    for candidate in results:
        score = 1.0 if doi and _clean_doi(candidate.get('doi')) == doi else _title_similarity(title, candidate.get('title'))
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= (1.0 if doi else MIN_TITLE_SIMILARITY):
        return _from_epmc(best)
    return {}


async def _semantic_scholar(client: httpx.AsyncClient, item: dict) -> dict:
    headers = {'x-api-key': SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else {}
    doi = _clean_doi(item.get('doi'))
    if doi:
        payload = await _json(
            client,
            f'{S2_URL}/paper/DOI:{quote(doi, safe="")}',
            params={'fields': 'title,abstract,year,authors,venue,externalIds'},
            headers=headers,
        )
        if isinstance(payload, dict) and payload.get('title'):
            return _from_s2(payload)
    title = str(item.get('title') or '').strip()
    if not title:
        return {}
    payload = await _json(client, f'{S2_URL}/paper/search', params={
        'query': title[:220],
        'fields': 'title,abstract,year,authors,venue,externalIds',
        'limit': '3',
    }, headers=headers)
    best = None
    best_score = 0.0
    for candidate in (payload or {}).get('data') or []:
        score = _title_similarity(title, candidate.get('title'))
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= MIN_TITLE_SIMILARITY:
        return _from_s2(best)
    return {}


def _merge(item: dict, candidates: list[dict]) -> dict:
    """Rellena solo campos vacios, en orden de prioridad de las fuentes."""
    filled: dict = {}
    sources: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        if candidate.get('source'):
            sources.append(candidate['source'])
        for field in ('abstract', 'year', 'doi', 'authors', 'journal', 'title'):
            if filled.get(field):
                continue
            value = candidate.get(field)
            if not value:
                continue
            # No pisar nunca lo que ya traia el paper.
            if item.get(field):
                continue
            filled[field] = value
    result = dict(filled)
    result['sources'] = list(dict.fromkeys(sources))
    return result


async def _enrich_one(client: httpx.AsyncClient, item: dict) -> dict:
    key = _clean_doi(item.get('doi')) or _norm_title(item.get('title'))[:160]
    if not key:
        return {}
    cached = _cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]

    # Crossref y Europe PMC en paralelo; Semantic Scholar solo si aun falta el
    # resumen (su API gratuita tiene limites mas estrictos).
    crossref, epmc = await asyncio.gather(
        _crossref(client, item), _epmc(client, item), return_exceptions=True,
    )
    candidates = [c for c in (crossref, epmc) if isinstance(c, dict) and c]
    if not any(c.get('abstract') for c in candidates) and not item.get('abstract'):
        s2 = await _semantic_scholar(client, item)
        if s2:
            candidates.append(s2)

    merged = _merge(item, candidates)
    _cache[key] = (time.time() + CACHE_TTL, merged)
    if len(_cache) > 5000:
        _cache.clear()
    return merged


async def enrich_items(items: list[dict], limit: int = MAX_ITEMS) -> list[dict]:
    """Devuelve, por item, solo los campos que se pudieron completar."""
    cleaned: list[dict] = []
    for raw in (items or [])[:limit]:
        if isinstance(raw, dict):
            cleaned.append(raw)
    if not cleaned:
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=4.0),
        follow_redirects=True,
        headers={'User-Agent': USER_AGENT},
    ) as client:
        async def run(item: dict) -> dict:
            async with semaphore:
                try:
                    return await _enrich_one(client, item)
                except Exception:  # noqa: BLE001
                    return {}

        return await asyncio.gather(*(run(item) for item in cleaned))


def needs_enrichment(item: dict) -> bool:
    """Solo vale la pena consultar si falta algo que el chat o la ficha usan."""
    return not item.get('abstract') or not item.get('year') or not item.get('doi')
