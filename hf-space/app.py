"""PsiHub Reader API.

Pipeline: PDF -> imagenes de pagina -> DeepSeek (vision) -> JSON estructurado
(titulo, autores, abstract, bloques, notas, figuras, columnas) con el contenido
traducido al espanol -> Markdown y PDF generados por Python.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_lib
import io
import ipaddress
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import pymupdf
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import storage
import enrich
import europepmc
import exports as export_service
import llm
import pmc_translate
import auth_supabase
import summarize as summarize_service
from costs import (PRICE_INPUT, PRICE_OUTPUT, admin_token_matches, estimate_cost,
                   list_costs, log_usage, summarize)
from exporters import document_to_markdown, document_to_pdf, markdown_to_pdf
from vision_extract import extract_document, PAGE_PROMPT, VISION_DPI
import chat as chat_service

load_dotenv()

DEEPSEEK_API_KEYS = [os.getenv(f'DEEPSEEK_API_KEY_{i}', '').strip() for i in range(1, 7)]
_legacy_key = os.getenv('DEEPSEEK_API_KEY', '').strip()
if not any(DEEPSEEK_API_KEYS) and _legacy_key:
    DEEPSEEK_API_KEYS = [_legacy_key]
DEEPSEEK_API_KEYS = [k for k in DEEPSEEK_API_KEYS if k]

# El chat puede usar una key dedicada (DEEPSEEK_API_CHAT, admite varias
# separadas por coma) para no competir con la traduccion. Si no existe,
# reutiliza las mismas keys de traduccion.
_DEEPSEEK_CHAT_KEYS = [k.strip() for k in os.getenv('DEEPSEEK_API_CHAT', '').split(',') if k.strip()]
if not _DEEPSEEK_CHAT_KEYS:
    _DEEPSEEK_CHAT_KEYS = DEEPSEEK_API_KEYS

# LLM_PROVIDER=0 usa DeepSeek; LLM_PROVIDER=1 usa Claude. Las claves de cada
# motor viven en su propio juego de variables (ver llm.py).
CHAT_API_KEYS = llm.chat_keys(_DEEPSEEK_CHAT_KEYS)
TRAD_API_KEYS = llm.trad_keys(DEEPSEEK_API_KEYS)
VISION_API_KEYS = llm.vision_keys(DEEPSEEK_API_KEYS)

# ID canonico de DeepSeek-V4.1-Flash (el alias deepseek-v4-flash apunta aqui).
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
PIPELINE_VERSION = '2026-10-26-vision-system-cache-v2'

CACHE_DIR = Path(os.getenv('CACHE_DIR', './cache')).resolve()
SOURCE_PDF_DIR = CACHE_DIR / 'source_pdfs'
IMAGES_STORE_DIR = CACHE_DIR / 'images'
MD_STORE_DIR = CACHE_DIR / 'translated_md'
JSON_STORE_DIR = CACHE_DIR / 'documents'
PDF_STORE_DIR = CACHE_DIR / 'translated_pdfs'
for directory in (CACHE_DIR, SOURCE_PDF_DIR, IMAGES_STORE_DIR, MD_STORE_DIR,
                  JSON_STORE_DIR, PDF_STORE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '300'))
LOG_BUFFER: deque = deque(maxlen=3000)

# Proxy de OpenAlex: la IP del backend tiene su propia cuota y las respuestas
# se cachean para no gastar créditos en búsquedas repetidas.
OPENALEX_WORKS_URL = 'https://api.openalex.org/works'
OPENALEX_MAILTO = os.getenv('OPENALEX_MAILTO', 'psyhub.app.research@gmail.com').strip()
OPENALEX_API_KEY = os.getenv('OPENALEX_API_KEY', '').strip()
OPENALEX_CACHE_TTL = int(os.getenv('OPENALEX_CACHE_TTL', '600'))
_openalex_cache: dict[str, tuple[float, dict]] = {}


def log(message: str) -> None:
    print(message, flush=True)
    LOG_BUFFER.append({'t': time.time(), 'msg': message})


# ---------------------------------------------------------------------------
# Cache / artefactos
# ---------------------------------------------------------------------------

def cache_key(content: bytes, model: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    model_hash = hashlib.sha256(model.encode()).hexdigest()[:12]
    version_hash = hashlib.sha256(PIPELINE_VERSION.encode()).hexdigest()[:12]
    # El prompt y la resolucion cambian la salida: incluirlos evita servir un
    # documento viejo tras una mejora de traduccion sin bumpear la version.
    prompt_hash = hashlib.sha256(PAGE_PROMPT.encode()).hexdigest()[:8]
    return f'{digest}_{model_hash}_{version_hash}_{prompt_hash}_{VISION_DPI}'


def load_cache(key: str) -> Optional[dict]:
    path = CACHE_DIR / f'{key}.json'
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except Exception:
            pass
    remote = storage.get(f'cache/{key}.json')
    if not remote:
        return None
    try:
        data = json.loads(remote.decode('utf-8'))
        path.write_bytes(remote)
        return data
    except Exception:
        return None


def save_cache(key: str, data: dict) -> None:
    path = CACHE_DIR / f'{key}.json'
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)
    try:
        storage.put(f'cache/{key}.json', path.read_bytes(), 'application/json')
    except Exception:
        pass


_ARTIFACT_KEYS = {
    'md': ('translated.md', 'text/markdown; charset=utf-8'),
    'json': ('document.json', 'application/json'),
    'pdf': ('translated.pdf', 'application/pdf'),
    'source': ('source.pdf', 'application/pdf'),
}


def _artifact_paths(doc_id: str) -> dict[str, Path]:
    return {
        'md': MD_STORE_DIR / f'{doc_id}.md',
        'json': JSON_STORE_DIR / f'{doc_id}.json',
        'pdf': PDF_STORE_DIR / f'{doc_id}.pdf',
        'source': SOURCE_PDF_DIR / f'{doc_id}.pdf',
    }


def _artifact_key(doc_id: str, kind: str) -> str:
    return f'artifacts/{doc_id}/{_ARTIFACT_KEYS[kind][0]}'


def _persist_artifact(doc_id: str, kind: str, path: Path) -> None:
    if not path.exists():
        return
    filename, content_type = _ARTIFACT_KEYS[kind]
    try:
        storage.put(f'artifacts/{doc_id}/{filename}', path.read_bytes(), content_type)
    except Exception:
        pass


def _persist_images(doc_id: str) -> None:
    """Sube las figuras extraidas para que sobrevivan a la instancia."""
    directory = IMAGES_STORE_DIR / doc_id
    if not directory.is_dir():
        return
    for image in directory.iterdir():
        if image.is_file():
            try:
                storage.put(f'artifacts/{doc_id}/images/{image.name}', image.read_bytes(),
                            'image/png' if image.suffix.lower() == '.png' else 'image/jpeg')
            except Exception:
                pass


def _restore_images(doc_id: str) -> None:
    """Baja las figuras desde GCS antes de reconstruir un PDF."""
    directory = IMAGES_STORE_DIR / doc_id
    prefix = f'artifacts/{doc_id}/images/'
    for key in storage.list_keys(prefix):
        name = Path(key).name
        if not name or (directory / name).exists():
            continue
        data = storage.get(key)
        if data is None:
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        except Exception:
            pass


def _restore_artifact(doc_id: str, kind: str) -> Optional[Path]:
    """Trae un artefacto desde GCS al disco local si falta."""
    path = _artifact_paths(doc_id)[kind]
    if path.exists():
        return path
    remote = storage.get(_artifact_key(doc_id, kind))
    if remote is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(remote)
        return path
    except Exception:
        return None


def _artifact_response(document_id: str, kind: str, media_type: str,
                       filename: Optional[str] = None, detail: str = 'Archivo no encontrado.'):
    path = _restore_artifact(document_id, kind)
    if path is None:
        raise HTTPException(status_code=404, detail=detail)
    return FileResponse(path=str(path), media_type=media_type, filename=filename)


def ensure_artifacts(result: dict) -> None:
    """Regenera los archivos que falten a partir del resultado cacheado."""
    doc_id = result.get('document_id')
    document = result.get('document_json')
    if not doc_id or not document:
        return
    paths = _artifact_paths(doc_id)
    markdown = result.get('markdown') or document_to_markdown(document)
    if not paths['md'].exists():
        if _restore_artifact(doc_id, 'md') is None:
            paths['md'].write_text(markdown, encoding='utf-8')
            _persist_artifact(doc_id, 'md', paths['md'])
    if not paths['json'].exists():
        if _restore_artifact(doc_id, 'json') is None:
            paths['json'].write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
            _persist_artifact(doc_id, 'json', paths['json'])
    if not paths['pdf'].exists():
        if _restore_artifact(doc_id, 'pdf') is None:
            _restore_images(doc_id)
            try:
                document_to_pdf(document, paths['pdf'], IMAGES_STORE_DIR / doc_id)
                _persist_artifact(doc_id, 'pdf', paths['pdf'])
            except Exception as exc:  # noqa: BLE001 - el PDF es opcional
                log(f'[PDF] No se pudo regenerar el PDF: {exc}')


def _attach_urls(result: dict, doc_id: str) -> dict:
    """Anade las rutas publicas de descarga al resultado."""
    result['document_id'] = doc_id
    result['pdf_url'] = f'/api/source-pdf/{doc_id}'
    result['translated_pdf_url'] = f'/api/download-pdf/{doc_id}'
    result['markdown_url'] = f'/api/download-md/{doc_id}'
    result['json_url'] = f'/api/download-json/{doc_id}'
    return result


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

async def process_pdf(pdf_bytes: bytes, model: Optional[str] = None,
                      progress=None, source: str = 'api') -> dict:
    if not pdf_bytes:
        raise ValueError('PDF vacio.')
    if not pdf_bytes[:10].startswith(b'%PDF'):
        raise ValueError('El archivo no es un PDF valido.')
    if not VISION_API_KEYS:
        raise RuntimeError(
            f'No hay API key de {llm.PROVIDER.capitalize()} para leer el PDF.'
        )

    selected_model = (model or '').strip() or llm.model_for(DEEPSEEK_MODEL)
    doc_id = hashlib.sha256(pdf_bytes).hexdigest()
    key = cache_key(pdf_bytes, selected_model)

    cached = load_cache(key)
    if cached:
        log(f'[PDF] Cache hit: {doc_id[:12]}...')
        ensure_artifacts(cached)
        _attach_urls(cached, doc_id)
        return cached

    log(f'[PDF] Procesando documento ({len(pdf_bytes):,} bytes, modelo: {selected_model})')
    paths = _artifact_paths(doc_id)
    paths['source'].write_bytes(pdf_bytes)
    _persist_artifact(doc_id, 'source', paths['source'])

    document = await extract_document(
        pdf_bytes,
        VISION_API_KEYS,
        model=selected_model,
        progress=progress,
        images_dir=IMAGES_STORE_DIR / doc_id,
        doc_id=doc_id,
        log_fn=log,
    )
    _persist_images(doc_id)

    markdown = document_to_markdown(document)
    paths['md'].write_text(markdown, encoding='utf-8')
    paths['json'].write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    _persist_artifact(doc_id, 'md', paths['md'])
    _persist_artifact(doc_id, 'json', paths['json'])

    pdf_error = None
    try:
        document_to_pdf(document, paths['pdf'], IMAGES_STORE_DIR / doc_id)
        _persist_artifact(doc_id, 'pdf', paths['pdf'])
    except Exception as exc:  # noqa: BLE001 - no bloquear la traduccion por el PDF
        pdf_error = str(exc)
        log(f'[PDF] Error generando PDF: {exc}')

    meta = document.get('document', {})
    stats = document.get('stats', {}) or {}
    pages = stats.get('pages') or document.get('layout', {}).get('pages') or 0

    usage = stats.get('usage', {}) or {}
    cost = estimate_cost(usage)
    log_usage(
        'translation', usage,
        model=selected_model,
        elapsed_sec=stats.get('elapsed_sec'),
        title=meta.get('title_es') or meta.get('title') or '',
        doc_id=doc_id,
        source=source,
        pages=pages,
        pipeline='vision',
    )
    has_body = any(block.get('type') == 'paragraph' for block in document.get('blocks', []))
    quality = 95 if meta.get('title') and has_body else 80

    document_metadata = {
        'title': meta.get('title_es') or meta.get('title'),
        'title_original': meta.get('title'),
        'subtitle': meta.get('subtitle_es') or meta.get('subtitle'),
        'authors': meta.get('authors') or [],
        'journal': meta.get('journal'),
        'year': meta.get('year'),
        'doi': meta.get('doi'),
        'abstract': meta.get('abstract_es') or meta.get('abstract_original'),
        'keywords': meta.get('keywords_es') or meta.get('keywords') or [],
        'language': meta.get('language'),
        'columns': document.get('layout', {}).get('columns'),
    }

    result = {
        'success': True,
        'document_id': doc_id,
        'markdown': markdown,
        'document_json': document,
        'document_metadata': document_metadata,
        'layout': document.get('layout', {}),
        'quality_score': quality,
        'quality_report': {'average_score': quality, 'page_scores': {}},
        'ocr_stats': {
            'engine': 'deepseek-vision',
            'pages_total': pages,
            'pages_analyzed': pages,
            'pages_mistral': 0,
            'estimated_cost_usd': 0,
        },
        'translation_stats': {
            'status': 'complete',
            'pages': pages,
            'usage': stats.get('usage', {}),
            'elapsed_sec': stats.get('elapsed_sec'),
            'estimated_cost_usd': cost['cost_usd'],
        },
        'images_extracted': stats.get('figures', 0),
        'source_language': meta.get('language') or 'English',
        'model_used': selected_model,
        'pdf_error': pdf_error,
    }
    _attach_urls(result, doc_id)

    save_cache(key, result)
    log(f'[PDF] Documento procesado: {doc_id[:12]}... ({pages} paginas)')
    return result


async def download_pdf(url: str) -> bytes:
    headers = {
        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'),
        'Accept': 'application/pdf,application/octet-stream,*/*;q=0.8',
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=15.0),
                                 follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content
        content_type = (response.headers.get('content-type') or '').lower()
    if not (data.startswith(b'%PDF') or 'application/pdf' in content_type or b'%PDF' in data[:4096]):
        raise ValueError(f'La URL no devuelve un PDF (Content-Type: {content_type!r}).')
    if not data.startswith(b'%PDF'):
        index = data.find(b'%PDF')
        if 0 < index < 4096:
            data = data[index:]
    return data


async def process_url(url: str, model: Optional[str] = None) -> dict:
    log(f'[DOWNLOAD] {url}')
    pdf_bytes = await download_pdf(url)
    return await process_pdf(pdf_bytes, model=model, source='url')


# ---------------------------------------------------------------------------
# Articulos de Europe PMC: XML JATS en vez de PDF
# ---------------------------------------------------------------------------
# Para los articulos del PMC no se descarga el PDF ni se usan modelos de vision:
# el articulo llega como XML estructurado, se traduce por bloques y se generan
# el Markdown y el PDF en espanol. Es mas barato, no pierde tablas ni formulas y
# funciona incluso cuando el editor bloquea la descarga del PDF.

PMC_XML_DIR = CACHE_DIR / 'pmc_xml'
PMC_XML_DIR.mkdir(parents=True, exist_ok=True)


def _pmc_doc_id(pmcid: str) -> str:
    return hashlib.sha256(f'pmc:{pmcid}'.encode('utf-8')).hexdigest()


async def process_pmc(pmcid: str, model: Optional[str] = None, source: str = 'pmc') -> dict:
    """Traduce el articulo completo de un registro PMC a Markdown y PDF."""
    clean = re.sub(r'[^A-Za-z0-9]', '', str(pmcid or '')).upper()
    if not clean.startswith('PMC'):
        clean = f'PMC{clean}'
    if clean == 'PMC':
        raise ValueError('Falta el identificador PMC.')

    xml_text = await europepmc.full_text_xml_cached(clean)
    if not xml_text:
        raise ValueError(
            'Este articulo no tiene texto completo abierto en el PMC; '
            'probá con el PDF original o con otra fuente.'
        )
    if not TRAD_API_KEYS:
        raise RuntimeError(
            f'No hay API key de {llm.PROVIDER.capitalize()} configurada.'
        )

    selected_model = (model or '').strip() or DEEPSEEK_MODEL
    doc_id = _pmc_doc_id(clean)
    # El sufijo separa el pipeline PMC del de vision: un documento viejo —de
    # cuando el XML se guardaba sin traducir— no debe servirse como si fuera la
    # traduccion actual.
    # El motor entra en la clave: la traduccion de Claude no es la de DeepSeek.
    key = cache_key(xml_text.encode('utf-8'), selected_model) + (
        '_pmc2_claude' if llm.USE_CLAUDE else '_pmc2'
    )

    cached = load_cache(key)
    if cached:
        ensure_artifacts(cached)
        _attach_urls(cached, doc_id)
        # En PMC el "original" es el XML, no hay PDF de origen que ofrecer.
        cached['pdf_url'] = cached.get('translated_pdf_url')
        log(f'[PMC] Cache hit: {doc_id[:12]}...')
        return cached

    log(f'[PMC] Procesando {clean} ({len(xml_text):,} caracteres de XML, modelo: {selected_model})')
    document = europepmc.xml_to_document(xml_text)
    document, stats = await pmc_translate.translate_document(
        document, TRAD_API_KEYS, model=selected_model, log_fn=log,
    )

    paths = _artifact_paths(doc_id)
    markdown = document_to_markdown(document)
    paths['md'].write_text(markdown, encoding='utf-8')
    paths['json'].write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    _persist_artifact(doc_id, 'md', paths['md'])
    _persist_artifact(doc_id, 'json', paths['json'])
    try:
        (PMC_XML_DIR / f'{clean}.xml').write_text(xml_text, encoding='utf-8')
    except Exception:  # noqa: BLE001 - el XML ya esta en el cache del motor
        pass

    pdf_error = None
    try:
        document_to_pdf(document, paths['pdf'], IMAGES_STORE_DIR / doc_id)
        _persist_artifact(doc_id, 'pdf', paths['pdf'])
    except Exception as exc:  # noqa: BLE001 - no bloquear la traduccion por el PDF
        pdf_error = str(exc)
        log(f'[PMC] Error generando PDF: {exc}')

    meta = document.get('document', {})
    usage = (stats or {}).get('usage') or {}
    # pmc_translate ya registra el uso (incluye cache y tiempo); aca solo se
    # reutiliza el costo para la respuesta del lector.
    cost = estimate_cost(usage)

    blocks = document.get('blocks') or []
    document_metadata = {
        'title': meta.get('title_es') or meta.get('title'),
        'title_original': meta.get('title'),
        'subtitle': meta.get('subtitle_es') or meta.get('subtitle'),
        'authors': meta.get('authors') or [],
        'journal': meta.get('journal'),
        'year': meta.get('year'),
        'doi': meta.get('doi'),
        'pmcid': meta.get('pmcid') or clean,
        'abstract': meta.get('abstract_es') or meta.get('abstract_original'),
        'keywords': meta.get('keywords_es') or meta.get('keywords') or [],
        'language': meta.get('language') or 'English',
        'columns': 1,
    }

    result = {
        'success': True,
        'document_id': doc_id,
        'engine': 'europepmc',
        'source_kind': 'xml',
        'pmcid': clean,
        'markdown': markdown,
        'document_json': document,
        'document_metadata': document_metadata,
        'layout': document.get('layout', {}),
        'quality_score': 95 if meta.get('title') and blocks else 80,
        'quality_report': {'average_score': 95 if blocks else 80, 'page_scores': {}},
        'ocr_stats': {
            'engine': 'jats-xml',
            'pages_total': 0,
            'pages_analyzed': 0,
            'pages_mistral': 0,
            'estimated_cost_usd': 0,
        },
        'translation_stats': {
            'status': 'complete',
            'pages': 0,
            'blocks': (stats or {}).get('blocks'),
            'translated_blocks': (stats or {}).get('translated'),
            'usage': usage,
            'elapsed_sec': (stats or {}).get('elapsed_sec'),
            'estimated_cost_usd': cost['cost_usd'],
        },
        'images_extracted': 0,
        'source_language': 'English',
        'model_used': selected_model,
        'pdf_error': pdf_error,
    }
    _attach_urls(result, doc_id)
    # Sin PDF de origen: el boton "ver PDF" muestra el PDF traducido.
    result['pdf_url'] = result.get('translated_pdf_url')

    save_cache(key, result)
    log(f'[PMC] Documento procesado: {doc_id[:12]}...')
    return result


# ---------------------------------------------------------------------------
# Verificacion de PDFs descargables
# ---------------------------------------------------------------------------
# El navegador no puede confirmar una descarga directa cuando el servidor del
# editor no expone CORS. Aqui se descarga una muestra real del PDF desde el
# backend, que si puede seguir redirecciones y leer el Content-Type. Solo los
# PDFs confirmados se marcan como "Libre" (traduccion automatica).
PDF_PROBE_TIMEOUT = 8.0
PDF_PROBE_MAX_BYTES = 8192
PDF_PROBE_CACHE_TTL = 900
PDF_PROBE_MAX_URLS = 20
_pdf_probe_cache: dict[str, tuple[float, dict]] = {}


def _pdf_url_is_safe(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.internal')):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)


async def probe_pdf(url: str) -> dict[str, object]:
    """Confirma que la URL entrega un PDF descargable sin login."""
    verdict: dict[str, object] = {
        'ok': False, 'reason': 'error', 'status': None, 'content_type': '',
    }
    if not _pdf_url_is_safe(url):
        verdict['reason'] = 'invalid-url'
        return verdict

    now = time.time()
    cached = _pdf_probe_cache.get(url)
    if cached and cached[0] > now:
        return cached[1]

    headers = {
        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'),
        'Accept': 'application/pdf,application/octet-stream,*/*;q=0.8',
        'Range': f'bytes=0-{PDF_PROBE_MAX_BYTES - 1}',
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PDF_PROBE_TIMEOUT, connect=6.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream('GET', url) as response:
                content_type = (response.headers.get('content-type') or '').lower()
                verdict['status'] = response.status_code
                verdict['content_type'] = content_type
                if response.status_code in (401, 403):
                    verdict['reason'] = 'auth'
                elif response.status_code in (404, 410):
                    verdict['reason'] = 'missing'
                elif response.status_code >= 400:
                    verdict['reason'] = f'http-{response.status_code}'
                else:
                    head = b''
                    async for chunk in response.aiter_bytes():
                        head += chunk
                        if len(head) >= PDF_PROBE_MAX_BYTES:
                            break
                    if b'%PDF' in head[:4096] or 'application/pdf' in content_type:
                        verdict['ok'] = True
                        verdict['reason'] = 'verified'
                    elif 'html' in content_type or head.lstrip()[:1] == b'<':
                        verdict['reason'] = 'landing'
                    else:
                        verdict['reason'] = 'not-pdf'
    except httpx.TimeoutException:
        verdict['reason'] = 'timeout'
    except Exception as exc:  # noqa: BLE001
        log(f'[CHECK-PDF] {type(exc).__name__}: {exc}')
        verdict['reason'] = 'error'
    _pdf_probe_cache[url] = (now + PDF_PROBE_CACHE_TTL, verdict)
    return verdict


async def probe_pmc(pmcid: str) -> dict:
    """Confirma que existe el articulo completo en XML para un registro PMC."""
    clean = re.sub(r'[^A-Za-z0-9]', '', pmcid or '').upper()
    if not clean.startswith('PMC'):
        clean = f'PMC{clean}'
    if clean == 'PMC':
        return {'ok': False, 'reason': 'missing-id'}
    async with _pmc_probe_lock:
        now = time.time()
        cached = _pdf_probe_cache.get(f'pmc:{clean}')
        if cached and cached[0] > now:
            return cached[1]
    xml_text = await europepmc.full_text_xml_cached(clean)
    verdict: dict = {'ok': bool(xml_text), 'reason': 'verified' if xml_text else 'no-fulltext'}
    if xml_text:
        verdict['chars'] = len(xml_text)
    async with _pmc_probe_lock:
        _pdf_probe_cache[f'pmc:{clean}'] = (time.time() + PDF_PROBE_CACHE_TTL, verdict)
    return verdict


_pmc_probe_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(title='PsiHub Reader', version='10.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


class TranslateRequest(BaseModel):
    url: str = ''
    model: Optional[str] = None
    # Ruta preferente: articulo abierto del PMC leido desde su XML.
    pmcid: Optional[str] = None
    doi: Optional[str] = None
    paper_id: Optional[str] = None
    id: Optional[str] = None


class CheckPdfsRequest(BaseModel):
    urls: list[str] = []


class ChatRequest(BaseModel):
    question: str
    query: Optional[str] = None
    access: str = 'all'
    # 'quick' | 'deep' (compatibilidad); el tamaño real viaja en `size`.
    depth: str = 'quick'
    # Extension relativa al plan: small | medium | large.
    size: str = 'medium'
    # 'pensar mas': activa el razonamiento del modelo, no la extension.
    think: bool = False
    # Plan del usuario (basic | pro): cambia los topes de la extension.
    plan: str = 'basic'
    filters: dict = {}
    history: list[dict] = []
    client_id: str = ''
    # PDFs, imagenes o papers guardados que el usuario adjunta a la consulta.
    attachments: list[dict] = []


class ExportRequest(BaseModel):
    format: str = 'ris'
    papers: list[dict] = []


class ChatPdfRequest(BaseModel):
    question: str = ''
    answer: str
    sources: list[dict] = []


class EnrichRequest(BaseModel):
    items: list[dict] = []


class SummarizeRequest(BaseModel):
    title: str = ''
    abstract: str = ''
    authors: list = []
    year: Optional[int] = None
    journal: str = ''
    doi: str = ''
    type: str = ''
    design: list = []
    sample: str = ''
    mode: str = 'basic'
    pdf_url: str = ''


@app.get('/api/document/{document_id}/image/{image_filename}')
async def api_get_image(document_id: str, image_filename: str):
    safe_name = Path(image_filename).name
    image_path = IMAGES_STORE_DIR / document_id / safe_name
    if not image_path.exists():
        remote = storage.get(f'artifacts/{document_id}/images/{safe_name}')
        if remote is not None:
            try:
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(remote)
            except Exception:
                image_path = None
    if image_path is None or not image_path.exists():
        raise HTTPException(status_code=404, detail='Imagen no encontrada.')
    ext_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.webp': 'image/webp', '.gif': 'image/gif'}
    media_type = ext_map.get(image_path.suffix.lower(), 'application/octet-stream')
    return FileResponse(path=str(image_path), media_type=media_type)


@app.get('/image_url')
async def image_url_compat(
    doc_id: str = Query('', alias='doc_id'),
    filename: str = Query('', alias='filename'),
    document_id: str = Query('', alias='document_id'),
    image_filename: str = Query('', alias='image_filename'),
):
    return await api_get_image(doc_id or document_id, filename or image_filename)


@app.get('/api/download-md/{document_id}')
async def api_download_md(document_id: str):
    return _artifact_response(document_id, 'md', 'text/markdown; charset=utf-8',
                              filename=f'psihub-{document_id[:8]}.md',
                              detail='Markdown no encontrado.')


@app.get('/api/download-pdf/{document_id}')
async def api_download_pdf(document_id: str):
    return _artifact_response(document_id, 'pdf', 'application/pdf',
                              filename=f'psihub-{document_id[:8]}.pdf',
                              detail='PDF traducido no encontrado.')


@app.get('/api/download-json/{document_id}')
async def api_download_json(document_id: str):
    return _artifact_response(document_id, 'json', 'application/json; charset=utf-8',
                              filename=f'psihub-{document_id[:8]}.json',
                              detail='JSON no encontrado.')


@app.get('/api/source-pdf/{document_id}')
async def api_source_pdf(document_id: str):
    return _artifact_response(document_id, 'source', 'application/pdf',
                              detail='PDF original no encontrado.')


@app.post('/api/translate')
async def api_translate(request: TranslateRequest):
    try:
        pmcid = (request.pmcid or '').strip()
        if not pmcid:
            # El frontend manda la clave del motor como paper_id (p. ej.
            # "pmc:PMC123"); tambien se acepta una URL de Europe PMC.
            for raw in (request.paper_id, request.id, request.url):
                match = re.search(r'(PMC\d{4,})', str(raw or ''), re.I)
                if match:
                    pmcid = match.group(1)
                    break
        if pmcid:
            try:
                return JSONResponse(content=await process_pmc(pmcid, request.model))
            except ValueError as exc:
                # Sin texto completo en PMC: si hay URL de PDF, se usa el
                # pipeline de vision como antes.
                if not request.url:
                    raise
                log(f'[PMC] {pmcid} sin texto completo ({exc}); se intenta el PDF.')
        url = (request.url or '').strip()
        if not url:
            raise HTTPException(status_code=400, detail='Falta la URL del documento.')
        result = await process_url(url, request.model)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f'Error descargando el PDF: {exc}')
    except Exception as exc:  # noqa: BLE001
        log(f'[ERROR] {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/api/openalex')
async def api_openalex(request: Request):
    allowed = {'filter', 'search', 'sort', 'page', 'per-page', 'select', 'api_key'}
    params = {k: v for k, v in request.query_params.items() if k in allowed}
    if not params.get('api_key') and OPENALEX_API_KEY:
        params['api_key'] = OPENALEX_API_KEY
    if not params.get('api_key'):
        params.setdefault('mailto', OPENALEX_MAILTO)

    cache_key = json.dumps({k: v for k, v in params.items() if k != 'api_key'}, sort_keys=True)
    now = time.time()
    cached = _openalex_cache.get(cache_key)
    if cached and cached[0] > now:
        return JSONResponse(content=cached[1])

    headers = {'User-Agent': f'PsiHub/10 (+https://melaraneax.github.io/PsiHub/; {OPENALEX_MAILTO})'}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(OPENALEX_WORKS_URL, params=params)
            if response.status_code == 503:
                await asyncio.sleep(1.0)
                response = await client.get(OPENALEX_WORKS_URL, params=params)
    except httpx.HTTPError as exc:
        log(f'[OPENALEX] {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=502, detail='No se pudo contactar a OpenAlex.') from exc

    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail='Respuesta inválida de OpenAlex.')

    if response.status_code == 200 and isinstance(payload, dict):
        # No cachear páginas vacías ni respuestas con total 0: un hipo
        # temporal de OpenAlex quedaría pegado 10 minutos y los reintentos
        # del cliente devolverían siempre lo mismo.
        results = payload.get('results')
        meta = payload.get('meta') or {}
        count = meta.get('count')
        if isinstance(results, list) and results and isinstance(count, int) and count > 0:
            if len(_openalex_cache) > 500:
                _openalex_cache.clear()
            _openalex_cache[cache_key] = (now + OPENALEX_CACHE_TTL, payload)
    return JSONResponse(content=payload, status_code=response.status_code)


@app.get('/api/check-pdf')
async def api_check_pdf(url: str = Query(''), pmcid: str = Query('')):
    if pmcid.strip():
        return JSONResponse(content=await probe_pmc(pmcid.strip()))
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail='Falta la URL.')
    return JSONResponse(content=await probe_pdf(url))


# ---------------------------------------------------------------------------
# Busqueda: Europe PMC (motor primario) con OpenAlex como respaldo
# ---------------------------------------------------------------------------

@app.get('/api/search')
async def api_search(
    query: str = Query(''),
    cursor: str = Query('*'),
    page_size: int = Query(0, ge=0, le=100),
    access: str = Query('all'),
    sort: str = Query(''),
    scope: str = Query('psy'),
    mode: str = Query('broad'),
):
    """Pagina de resultados ya normalizada para la interfaz.

    El cursor es opaco: el cliente guarda el que devuelve cada pagina. Europe
    PMC no admite saltar a una pagina arbitraria, asi que el frontend encadena
    cursores en vez de calcular offsets.
    """
    text = (query or '').strip()
    if not text:
        raise HTTPException(status_code=400, detail='Falta la consulta.')
    if not europepmc.STATE.available:
        raise HTTPException(status_code=503, detail='Europe PMC no está disponible en este momento.')

    size = page_size or europepmc.EPMC_PAGE_SIZE
    try:
        works, meta = await europepmc.search(
            text,
            page_size=size,
            cursor=cursor or '*',
            access=access,
            sort=sort or None,
            scope=scope,
            mode=mode,
            # Relevancia: el orden propio del motor es "lo mas nuevo primero",
            # asi que para la interfaz se mezcla con los mas citados.
            balanced=not sort or sort == 'relevance',
        )
    except Exception as exc:  # noqa: BLE001
        europepmc.STATE.fail(exc)
        log(f'[EPMC] busqueda fallida: {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=502, detail='No se pudo contactar a Europe PMC.') from exc
    europepmc.STATE.ok()

    # Los registros que solo viven en PMC no traen resumen en la busqueda; sin
    # el, la tarjeta queda vacia. Se completa con el XML, pero con un techo de
    # tiempo: antes el endpoint esperaba hasta 10 descargas y la primera pagina
    # tardaba varios segundos. Lo que no llegue se resuelve al abrir la ficha.
    try:
        await asyncio.wait_for(
            europepmc.fill_abstracts(works, limit=min(len(works), 8), concurrency=8),
            timeout=2.5,
        )
    except Exception as exc:  # noqa: BLE001 - el resumen es opcional
        log(f'[EPMC] abstracts: {type(exc).__name__}: {exc}')

    papers = [europepmc.to_paper(w) for w in works]
    return JSONResponse(content={
        'results': papers,
        'meta': {
            'count': meta.get('count', len(papers)),
            'cursor': meta.get('cursor', ''),
            'engine': 'europepmc',
            'pageSize': size,
        },
    })


@app.get('/api/work')
async def api_work(key: str = Query(''), doi: str = Query('')):
    """Ficha de un paper por clave `src:id` o DOI (favoritos e historias)."""
    clean_key = (key or '').strip().lower()
    clean_doi = (doi or '').strip()
    try:
        if clean_key and ':' in clean_key:
            source, _, ident = clean_key.partition(':')
            works = await europepmc.fetch_by_ids([f'{source}:{ident}'])
            if works:
                return JSONResponse(content=europepmc.to_paper(works[0]))
            raise HTTPException(status_code=404, detail='Paper no encontrado.')
        if clean_doi:
            work = await europepmc.lookup_doi(clean_doi)
            if work:
                return JSONResponse(content=europepmc.to_paper(work))
            raise HTTPException(status_code=404, detail='DOI no encontrado.')
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        europepmc.STATE.fail(exc)
        raise HTTPException(status_code=502, detail='No se pudo contactar a Europe PMC.') from exc
    raise HTTPException(status_code=400, detail='Falta la clave o el DOI.')


@app.get('/api/work-meta')
async def api_work_meta(key: str = Query(''), doi: str = Query('')):
    """Afiliaciones, financiamiento, licencia y datos editoriales del XML.

    La busqueda de Europe PMC no trae estos campos: viven solo en el JATS del
    texto completo. Si el paper no tiene XML abierto se responde 200 con
    `available: false` para que la ficha muestre "no informado" sin romperse.
    """
    clean_key = (key or '').strip().lower()
    clean_doi = (doi or '').strip()
    pmcid = ''
    try:
        if clean_key.startswith('pmc:'):
            pmcid = clean_key.split(':', 1)[1]
        elif clean_key and ':' in clean_key:
            works = await europepmc.fetch_by_ids([clean_key])
            if works:
                pmcid = str((works[0].get('epmc') or {}).get('pmcid') or '')
        elif clean_doi:
            work = await europepmc.lookup_doi(clean_doi)
            if work:
                pmcid = str((work.get('epmc') or {}).get('pmcid') or '')
    except Exception as exc:  # noqa: BLE001 - la ficha cae a lo que ya tenia
        log(f'[work-meta] resolucion fallida: {type(exc).__name__}: {exc}')

    if not pmcid:
        return JSONResponse(content={
            'available': False,
            'reason': 'Sin texto completo abierto en PMC: la fuente no publica afiliaciones ni financiamiento.',
        })
    try:
        xml_text = await asyncio.wait_for(
            europepmc.full_text_xml_cached(pmcid), timeout=9.0,
        )
    except Exception as exc:  # noqa: BLE001
        log(f'[work-meta] XML fallido {pmcid}: {type(exc).__name__}: {exc}')
        xml_text = None
    if not xml_text:
        return JSONResponse(content={
            'available': False,
            'pmcid': pmcid,
            'reason': 'No se pudo leer el XML del texto completo.',
        })
    extras = europepmc.xml_extras(xml_text)
    extras['pmcid'] = pmcid
    return JSONResponse(content=extras)


@app.post('/api/export')
async def api_export(request: ExportRequest):
    """Exporta papers a RIS (Zotero/Mendeley/EndNote) o BibTeX."""
    papers = [p for p in (request.papers or []) if isinstance(p, dict)][:500]
    if not papers:
        raise HTTPException(status_code=400, detail='No hay papers para exportar.')
    fmt = str(request.format or 'ris').strip().lower()
    if fmt in ('bib', 'bibtex'):
        content = export_service.to_bibtex(papers)
        media = 'application/x-bibtex'
        filename = 'psihub.bib'
    elif fmt == 'ris':
        content = export_service.to_ris(papers)
        media = 'application/x-research-info-systems'
        filename = 'psihub.ris'
    else:
        raise HTTPException(status_code=400, detail='Formato no soportado.')
    return Response(
        content=content.encode('utf-8'),
        media_type=f'{media}; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.post('/api/check-pdfs')
async def api_check_pdfs(request: CheckPdfsRequest):
    urls: list[str] = []
    for raw in request.urls[:PDF_PROBE_MAX_URLS]:
        url = (raw or '').strip()
        if url and url not in urls:
            urls.append(url)
    semaphore = asyncio.Semaphore(8)

    async def run(url: str):
        async with semaphore:
            return url, await probe_pdf(url)

    results = dict(await asyncio.gather(*(run(u) for u in urls))) if urls else {}
    return JSONResponse(content={'results': results})


@app.post('/api/enrich')
async def api_enrich(request: EnrichRequest):
    """Completa abstract/año/DOI/autores faltantes con otras APIs academicas."""
    items = []
    for raw in request.items[:enrich.MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        items.append({
            'title': str(raw.get('title') or '')[:500],
            'doi': str(raw.get('doi') or '')[:300],
            'year': raw.get('year'),
            'abstract': str(raw.get('abstract') or '')[:4000],
            'authors': raw.get('authors') if isinstance(raw.get('authors'), list) else [],
            'journal': str(raw.get('journal') or '')[:300],
        })
    try:
        results = await enrich.enrich_items(items)
    except Exception as exc:  # noqa: BLE001
        log(f'[ENRICH] {type(exc).__name__}: {exc}')
        results = [{} for _ in items]
    return JSONResponse(content={'results': results})


@app.post('/api/summarize')
async def api_summarize(request: SummarizeRequest):
    """Resumen basico (abstract) o avanzado (PDF descargable en 1 clic)."""
    if not CHAT_API_KEYS:
        raise HTTPException(status_code=503, detail='No hay API key de DeepSeek configurada.')
    data = {
        'title': request.title,
        'abstract': request.abstract,
        'authors': request.authors,
        'year': request.year,
        'journal': request.journal,
        'doi': request.doi,
        'type': request.type,
        'design': request.design,
        'sample': request.sample,
    }
    mode = 'advanced' if request.mode == 'advanced' else 'basic'
    try:
        if mode == 'advanced':
            if not request.pdf_url:
                raise HTTPException(status_code=400, detail='Falta la URL del PDF.')
            if not _pdf_url_is_safe(request.pdf_url):
                raise HTTPException(status_code=400, detail='La URL del PDF no es valida.')
            log(f'[SUMMARY] avanzado: {request.pdf_url[:160]}')
            pdf_bytes = await download_pdf(request.pdf_url)
            result = await summarize_service.summarize_full_text(data, pdf_bytes, CHAT_API_KEYS)
        else:
            result = await summarize_service.summarize_basic(data, CHAT_API_KEYS)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log(f'[SUMMARY] {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=502, detail=f'No se pudo generar el resumen: {exc}') from exc
    return JSONResponse(content={
        'summary': result['summary'],
        'mode': result['mode'],
        'cost_usd': (result.get('cost') or {}).get('cost_usd', 0),
        'pages': result.get('pages'),
    })


@app.post('/api/summarize-pdf')
async def api_summarize_pdf(
    file: UploadFile = File(...),
    payload: str = Form('{}'),
):
    """Resumen avanzado con un PDF que sube el usuario (articulos de pago)."""
    if not CHAT_API_KEYS:
        raise HTTPException(status_code=503, detail='No hay API key de DeepSeek configurada.')
    try:
        data = json.loads(payload or '{}')
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    raw = await file.read()
    if not raw.startswith(b'%PDF') and b'%PDF' not in raw[:4096]:
        raise HTTPException(status_code=400, detail='El archivo no es un PDF valido.')
    if len(raw) > 80 * 1024 * 1024:
        raise HTTPException(status_code=400, detail='El PDF supera los 80 MB.')
    try:
        result = await summarize_service.summarize_full_text(data, raw, CHAT_API_KEYS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log(f'[SUMMARY-PDF] {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=502, detail=f'No se pudo generar el resumen: {exc}') from exc
    return JSONResponse(content={
        'summary': result['summary'],
        'mode': 'advanced',
        'cost_usd': (result.get('cost') or {}).get('cost_usd', 0),
        'pages': result.get('pages'),
    })


@app.post('/api/chat')
async def api_chat(request: ChatRequest):
    """Chat de investigacion con streaming SSE (Europe PMC + DeepSeek)."""
    access = request.access if request.access in ('all', 'free', 'oneclick', 'paid') else 'all'
    depth = 'deep' if str(request.depth).lower() == 'deep' else 'quick'
    size = str(request.size or 'medium').lower()
    if size not in ('small', 'medium', 'large'):
        size = 'medium'
    plan = 'pro' if str(request.plan or '').lower() == 'pro' else 'basic'
    filters = request.filters if isinstance(request.filters, dict) else {}
    question = (request.question or '').strip()
    if not question:
        raise HTTPException(status_code=400, detail='Falta la pregunta.')
    if not CHAT_API_KEYS:
        raise HTTPException(status_code=503, detail='No hay API key de DeepSeek configurada.')

    generator = chat_service.stream_chat(
        question=question,
        query=(request.query or question).strip(),
        access=access,
        history=request.history[-8:],
        api_keys=CHAT_API_KEYS,
        probe_fn=probe_pdf,
        log_fn=log,
        client_id=request.client_id,
        depth=depth,
        size=size,
        think=bool(request.think),
        plan=plan,
        filters=filters,
        attachments=request.attachments,
    )
    return StreamingResponse(
        generator,
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


ATTACH_MAX_BYTES = 25 * 1024 * 1024
ATTACH_PDF_MAX_CHARS = 80000
ATTACH_PDF_MAX_PAGES = 80


def _pdf_text(data: bytes) -> tuple[str, int, bool]:
    """Texto plano del PDF, sin OCR. Devuelve (texto, paginas, recortado)."""
    with pymupdf.open(stream=data, filetype='pdf') as doc:
        total = len(doc)
        parts: list[str] = []
        used = 0
        for index in range(min(total, ATTACH_PDF_MAX_PAGES)):
            text = doc[index].get_text('text').strip()
            if not text:
                continue
            parts.append(text)
            used += len(text)
            if used >= ATTACH_PDF_MAX_CHARS:
                break
    joined = '\n\n'.join(parts).strip()
    truncated = len(joined) > ATTACH_PDF_MAX_CHARS or total > ATTACH_PDF_MAX_PAGES
    return joined[:ATTACH_PDF_MAX_CHARS], total, truncated


async def _image_text(data: bytes, mime: str) -> str:
    """Transcribe una imagen con el modelo de vision del motor activo."""
    keys = VISION_API_KEYS
    if not keys:
        raise HTTPException(
            status_code=503,
            detail=f'No hay API key de {llm.PROVIDER.capitalize()} para leer imagenes.',
        )
    prompt = (
        'Transcribi literalmente todo el texto visible de esta imagen (en su idioma '
        'original) y describi en espanol tablas, figuras y graficos relevantes. '
        'No resumas el contenido textual ni inventes lo que no se lee. Si es una '
        'pagina de un paper, conserva titulos, autores y secciones.'
    )
    image_b64 = base64.b64encode(data).decode('ascii')
    if llm.USE_CLAUDE:
        text, _usage, _stop = await llm.vision_page(
            image_b64, prompt, keys[0], 2400, media_type=mime or 'image/jpeg',
        )
        if not text:
            raise HTTPException(status_code=422, detail='La imagen no devolvio texto legible.')
        return text

    payload = {
        'model': chat_service.CHAT_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {
                    'url': f'data:{mime};base64,' + image_b64,
                }},
            ],
        }],
        'temperature': 0.1,
        'stream': False,
        'thinking': {'type': 'disabled'},
        'max_tokens': 2400,
    }
    started = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        response = await client.post(
            os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions'),
            headers={'Authorization': f'Bearer {keys[0]}', 'Content-Type': 'application/json'},
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail='El modelo no pudo leer la imagen.')
    body = response.json()
    text = ''
    try:
        text = str(body['choices'][0]['message']['content'] or '').strip()
    except (KeyError, IndexError, TypeError):
        text = ''
    if not text:
        raise HTTPException(status_code=422, detail='La imagen no devolvio texto legible.')
    usage = body.get('usage') or {}
    log_usage(
        'attachment',
        usage,
        model=chat_service.CHAT_MODEL,
        elapsed_sec=round(time.time() - started, 2),
        title='Imagen adjunta al chat',
        source='chat',
        detail='image',
    )
    return text[:ATTACH_PDF_MAX_CHARS]


@app.post('/api/chat/attach')
async def api_chat_attach(file: UploadFile = File(...)):
    """Extrae texto de un PDF o transcribe una imagen para usarla en el chat.

    El archivo se procesa en memoria y no se guarda. El cliente recibe el texto
    y lo reenvia dentro de `attachments` al pedir la respuesta.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail='El archivo esta vacio.')
    if len(data) > ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail='El archivo supera los 25 MB.')
    name = (file.filename or 'adjunto').split('/')[-1][:120]
    if data.startswith(b'%PDF'):
        try:
            text, pages, truncated = _pdf_text(data)
        except Exception as exc:  # noqa: BLE001
            log(f'[attach] PDF invalido: {type(exc).__name__}: {exc}')
            raise HTTPException(status_code=422, detail='No se pudo leer el PDF.')
        if len(text) < 120:
            raise HTTPException(
                status_code=422,
                detail='El PDF no tiene texto seleccionable (parece escaneado). '
                       'Subí capturas de las páginas como imágenes.',
            )
        return JSONResponse({
            'ok': True,
            'kind': 'pdf',
            'name': name,
            'text': text,
            'pages': pages,
            'truncated': truncated,
        })
    mime = str(file.content_type or '').lower()
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise HTTPException(status_code=415, detail='Formato no soportado. Usá PDF, PNG, JPG o WebP.')
    text = await _image_text(data, mime)
    return JSONResponse({
        'ok': True,
        'kind': 'image',
        'name': name,
        'text': text,
        'truncated': len(text) >= ATTACH_PDF_MAX_CHARS,
    })


@app.post('/api/chat/pdf')
async def api_chat_pdf(request: ChatPdfRequest):
    """Exporta una respuesta del chat a PDF (sin costo de modelo)."""
    answer = (request.answer or '').strip()
    if not answer:
        raise HTTPException(status_code=400, detail='Falta la respuesta.')
    question = (request.question or '').strip()[:400]
    sources = [
        {
            'title': str(s.get('title') or '')[:300],
            'year': s.get('year'),
            'journal': str(s.get('journal') or '')[:180],
            'doi': str(s.get('doi') or '')[:200],
        }
        for s in (request.sources or [])[:60]
        if isinstance(s, dict)
    ]
    buffer = io.BytesIO()
    try:
        markdown_to_pdf(
            answer[:80000],
            buffer,
            title=question or 'Respuesta de PsiHub',
            sources=sources,
        )
    except Exception as exc:  # noqa: BLE001
        log(f'[ERROR] chat-pdf {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=500, detail='No se pudo generar el PDF.')
    return Response(
        content=buffer.getvalue(),
        media_type='application/pdf',
        headers={'Content-Disposition': 'attachment; filename="psihub-respuesta.pdf"'},
    )


@app.post('/api/translate-file')
async def api_translate_file(file: UploadFile = File(...), model: Optional[str] = Form(None)):
    try:
        pdf_bytes = await file.read()
        if not pdf_bytes.startswith(b'%PDF'):
            raise HTTPException(status_code=400, detail='El archivo no es un PDF valido.')
        result = await process_pdf(pdf_bytes, model=model, source='upload')
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f'[ERROR] {type(exc).__name__}: {exc}')
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/api/auth/me')
async def api_auth_me(authorization: str = Header('')):
    """Valida una sesion de Supabase. La app sigue funcionando sin cuenta."""
    user = auth_supabase.user_from_authorization(authorization)
    if not user:
        raise HTTPException(status_code=401, detail='Sesion no valida.')
    return JSONResponse(content={'user': user, 'supabase': True})


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'service': 'PsiHub Reader',
        'version': '10.0',
        'engine': 'deepseek-vision-json',
        'llm_provider': llm.PROVIDER,
        'deepseek': bool(DEEPSEEK_API_KEYS),
        'deepseek_keys': len(DEEPSEEK_API_KEYS),
        'trad_keys': len(TRAD_API_KEYS),
        'model': DEEPSEEK_MODEL,
        'chat_model': chat_service.CHAT_MODEL,
        'chat_keys': len(CHAT_API_KEYS),
        'chat_pipeline': chat_service.CHAT_PIPELINE_VERSION,
        'pipeline_version': PIPELINE_VERSION,
        'search_engine': 'europepmc',
        'search_fallback': 'openalex',
        'europepmc': europepmc.STATE.snapshot(),
        'europepmc_key': bool(europepmc.EPMC_API_KEY),
        'enrich': True,
        'summarize': bool(CHAT_API_KEYS),
        'supabase_auth': auth_supabase.configured(),
        'exports': ['ris', 'bibtex'],
    }


@app.get('/api/logs')
async def api_logs(since: float = 0.0, limit: int = 1000):
    entries = [entry for entry in LOG_BUFFER if entry['t'] >= since]
    return JSONResponse(content={'entries': entries[-limit:], 'count': len(entries)})


def _require_admin(token: str, header_token: str = '') -> None:
    if not admin_token_matches(token or header_token):
        raise HTTPException(status_code=404, detail='Not found')


@app.get('/api/costs')
async def api_costs(limit: int = Query(300, ge=1, le=2000), token: str = Query(''),
                    x_admin_token: str = Header('')):
    _require_admin(token, x_admin_token)
    records = list_costs(limit)
    return JSONResponse(content={'summary': summarize(records), 'documents': records})


@app.get('/costos')
async def costs_page(token: str = Query(''), x_admin_token: str = Header('')):
    _require_admin(token, x_admin_token)
    records = list_costs(300)
    totals = summarize(records)
    average = totals['cost_usd'] / totals['entries'] if totals['entries'] else 0
    rows = []
    for record in records:
        kind = 'Chat' if record.get('kind') == 'chat' else 'Traducción'
        title = html_lib.escape(str(record.get('title') or 'Sin titulo'))
        stamp = html_lib.escape(str(record.get('timestamp') or ''))
        rows.append(
            '<tr>'
            f'<td class="date">{stamp}</td>'
            f'<td class="kind">{kind}</td>'
            f'<td class="title">{title}</td>'
            f'<td class="num">{int(record.get("pages") or 0)}</td>'
            f'<td class="num tokens">{int(record.get("total_tokens") or 0):,}</td>'
            f'<td class="num cost">${float(record.get("cost_usd") or 0):.4f}</td>'
            '</tr>'
        )
    body = ''.join(rows) or '<tr><td colspan="6" class="empty">Sin registros todavía.</td></tr>'
    markup = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PsiHub · Costos</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px 16px 48px; font-family: Georgia, 'Times New Roman', serif;
         background: #f7f6f2; color: #1d1d1a; }}
  main {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: .01em; }}
  .sub {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; color: #6c6a62; margin-bottom: 22px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 24px; }}
  .card {{ background: #fff; border: 1px solid #e2dfd7; border-radius: 10px; padding: 12px 14px; }}
  .card .label {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size: 10px; text-transform: uppercase;
                 letter-spacing: .08em; color: #7b786f; }}
  .card .value {{ font-size: 20px; margin-top: 4px; font-variant-numeric: tabular-nums; }}
  .table-wrap {{ overflow-x: auto; background: #fff; border: 1px solid #e2dfd7; border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: .06em;
        color: #7b786f; padding: 10px 12px; border-bottom: 1px solid #e2dfd7; white-space: nowrap; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #eeece6; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .cost {{ font-weight: 600; }}
  .kind {{ color: #6c6a62; white-space: nowrap; }}
  .date {{ color: #6c6a62; white-space: nowrap; }}
  .title {{ max-width: 380px; }}
  .empty {{ text-align: center; color: #8a877e; padding: 26px; }}
  @media (max-width: 640px) {{
    .date, .tokens {{ display: none; }}
    .title {{ max-width: none; }}
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #151513; color: #ece9e1; }}
    .card, .table-wrap {{ background: #1d1d1a; border-color: #33322d; }}
    th {{ border-color: #33322d; }} td {{ border-color: #292823; }}
    .card .label, th, .sub, .date {{ color: #9a978e; }}
  }}
</style>
</head>
<body><main>
  <h1>Costos de API</h1>
  <p class="sub">DeepSeek V4.1 Flash · ${PRICE_INPUT:.2f} entrada / ${PRICE_OUTPUT:.2f} salida por millón de tokens · traducciones y chat (no caché).</p>
  <div class="cards">
    <div class="card"><div class="label">Gasto total</div><div class="value">${totals['cost_usd']:.4f}</div></div>
    <div class="card"><div class="label">Traducciones</div><div class="value">{totals['documents']}</div></div>
    <div class="card"><div class="label">Chats</div><div class="value">{totals['chats']}</div></div>
    <div class="card"><div class="label">Costo medio</div><div class="value">${average:.4f}</div></div>
    <div class="card"><div class="label">Tokens</div><div class="value">{totals['total_tokens']:,}</div></div>
  </div>
  <div class="table-wrap"><table>
    <thead><tr><th class="date">Fecha (UTC)</th><th>Tipo</th><th>Documento / Pregunta</th><th class="num">Págs.</th><th class="num tokens">Tokens</th><th class="num">Costo</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</main></body></html>"""
    return HTMLResponse(markup)


@app.get('/api/logs/stream')
async def api_logs_stream():
    async def generate():
        last_t = time.time()
        while True:
            await asyncio.sleep(0.5)
            new = [entry for entry in LOG_BUFFER if entry['t'] > last_t]
            if new:
                last_t = new[-1]['t']
                for entry in new:
                    yield f'data: {json.dumps(entry)}\n\n'
            else:
                yield ': keepalive\n\n'
    return StreamingResponse(generate(), media_type='text/event-stream')


# ---------------------------------------------------------------------------
# Interfaz Gradio (diagnostico)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.getenv('PORT', '8000'))
    host = os.getenv('HOST', '0.0.0.0')
    if not DEEPSEEK_API_KEYS:
        log('[WARN] Sin API keys de DeepSeek.')
    uvicorn.run(app, host=host, port=port)
