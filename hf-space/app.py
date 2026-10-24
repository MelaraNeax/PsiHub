"""PsiHub Reader API.

Pipeline: PDF -> imagenes de pagina -> DeepSeek (vision) -> JSON estructurado
(titulo, autores, abstract, bloques, notas, figuras, columnas) con el contenido
traducido al espanol -> Markdown y PDF generados por Python.
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import ipaddress
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import gradio as gr

import storage
from costs import (PRICE_INPUT, PRICE_OUTPUT, admin_token_matches, estimate_cost,
                   list_costs, record_cost, summarize)
from exporters import document_to_markdown, document_to_pdf
from vision_extract import extract_document
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
CHAT_API_KEYS = [k.strip() for k in os.getenv('DEEPSEEK_API_CHAT', '').split(',') if k.strip()]
if not CHAT_API_KEYS:
    CHAT_API_KEYS = DEEPSEEK_API_KEYS

# ID canonico de DeepSeek-V4.1-Flash (el alias deepseek-v4-flash apunta aqui).
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
PIPELINE_VERSION = '2026-10-19-vision-json-v1'

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
    return f'{digest}_{model_hash}_{version_hash}'


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
    if not DEEPSEEK_API_KEYS:
        raise RuntimeError('No hay API key de DeepSeek configurada.')

    selected_model = (model or '').strip() or DEEPSEEK_MODEL
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
        DEEPSEEK_API_KEYS,
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
    record_cost({
        'doc_id': doc_id,
        'kind': 'translation',
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'title': meta.get('title_es') or meta.get('title') or '',
        'pages': pages,
        'model': selected_model,
        'source': source,
        'elapsed_sec': stats.get('elapsed_sec'),
        **cost,
    })
    # Deja el costo tambien en Cloud Logging (ademas del ledger en GCS).
    log(f"[Cost] {doc_id[:12]}… {pages} pag · {cost['total_tokens']} tokens · ${cost['cost_usd']:.4f} USD")
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
    url: str
    model: Optional[str] = None


class CheckPdfsRequest(BaseModel):
    urls: list[str] = []


class ChatRequest(BaseModel):
    question: str
    query: Optional[str] = None
    access: str = 'all'
    history: list[dict] = []
    client_id: str = ''


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
        result = await process_url(request.url, request.model)
        return JSONResponse(content=result)
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
async def api_check_pdf(url: str = Query('')):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail='Falta la URL.')
    return JSONResponse(content=await probe_pdf(url))


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


@app.post('/api/chat')
async def api_chat(request: ChatRequest):
    """Chat de investigacion con streaming SSE (OpenAlex + DeepSeek)."""
    access = request.access if request.access in ('all', 'free', 'oneclick') else 'all'
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


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'service': 'PsiHub Reader',
        'version': '10.0',
        'engine': 'deepseek-vision-json',
        'deepseek': bool(DEEPSEEK_API_KEYS),
        'deepseek_keys': len(DEEPSEEK_API_KEYS),
        'model': DEEPSEEK_MODEL,
        'chat_model': chat_service.CHAT_MODEL,
        'chat_keys': len(CHAT_API_KEYS),
        'pipeline_version': PIPELINE_VERSION,
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

def _info_markdown(result: dict) -> str:
    meta = result.get('document_metadata', {}) or {}
    stats = result.get('translation_stats', {}) or {}
    layout = result.get('layout', {}) or {}
    authors = ', '.join(meta.get('authors') or []) or '—'
    return (
        '### Documento\n\n'
        f'**Titulo:** {meta.get("title") or "—"}\n\n'
        f'**Autores:** {authors}\n\n'
        f'**Revista:** {meta.get("journal") or "—"}\n\n'
        f'**Anio:** {meta.get("year") or "—"}\n\n'
        f'**DOI:** {meta.get("doi") or "—"}\n\n'
        '### Pipeline\n\n'
        f'**Motor:** DeepSeek Vision (`{result.get("model_used")}`)\n\n'
        f'**Paginas:** {stats.get("pages", "—")}\n\n'
        f'**Columnas:** {layout.get("columns", "—")}\n\n'
        f'**Figuras:** {result.get("images_extracted", 0)}\n\n'
        f'**Tiempo:** {stats.get("elapsed_sec", "—")} s\n\n'
        f'**Uso:** {stats.get("usage", {})}'
    )


async def _gradio_process_url(url: str, progress=gr.Progress()):
    if not url or not url.strip():
        return ('Introduce una URL.', '', '', None)
    try:
        progress(0.05, desc='Descargando PDF...')
        pdf_bytes = await download_pdf(url.strip())
        def on_progress(done, total):
            progress(0.1 + 0.85 * done / max(total, 1), desc=f'Procesando pagina {done}/{total}...')
        result = await process_pdf(pdf_bytes, progress=on_progress, source='gradio-url')
        progress(1.0, desc='Completado')
        return (result['markdown'], _info_markdown(result), result['document_id'], result)
    except Exception as exc:  # noqa: BLE001
        return (f'Error: {exc}', '', None, None)


async def _gradio_process_file(file, progress=gr.Progress()):
    if file is None:
        return ('Sube un PDF.', '', None, None)
    try:
        file_path = file.name if hasattr(file, 'name') else str(file)
        pdf_bytes = Path(file_path).read_bytes()
        def on_progress(done, total):
            progress(0.1 + 0.85 * done / max(total, 1), desc=f'Procesando pagina {done}/{total}...')
        result = await process_pdf(pdf_bytes, progress=on_progress, source='gradio-file')
        progress(1.0, desc='Completado')
        return (result['markdown'], _info_markdown(result), result['document_id'], result)
    except Exception as exc:  # noqa: BLE001
        return (f'Error: {exc}', '', None, None)


def _download_updates(result: Optional[dict]):
    if not result or not result.get('document_id'):
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)
    doc_id = str(result['document_id'])
    paths = _artifact_paths(doc_id)
    md_ok = paths['md'].exists()
    pdf_ok = paths['pdf'].exists()
    json_ok = paths['json'].exists()
    return (
        gr.update(value=str(paths['md']) if md_ok else None, visible=md_ok),
        gr.update(value=str(paths['pdf']) if pdf_ok else None, visible=pdf_ok),
        gr.update(value=str(paths['json']) if json_ok else None, visible=json_ok),
    )


with gr.Blocks(title='PsiHub Reader') as demo:
    gr.Markdown('# PsiHub Reader v10\n\nPDF → vision (DeepSeek) → JSON estructurado → Markdown y PDF traducidos.')
    with gr.Tab('URL'):
        url_in = gr.Textbox(label='URL del PDF', placeholder='https://...')
        btn_url = gr.Button('Traducir PDF', variant='primary')
        with gr.Tabs():
            with gr.Tab('Modo lectura'):
                out_url = gr.Markdown(label='Traduccion')
            with gr.Tab('Markdown'):
                out_url_raw = gr.Code(label='Markdown', language='markdown')
            with gr.Tab('JSON'):
                out_url_json = gr.Code(label='JSON estructurado', language='json')
            with gr.Tab('Detalles'):
                out_url_details = gr.Markdown(label='Detalles')
        info_url = gr.Markdown(label='Informacion')
        with gr.Row():
            dl_url_md = gr.DownloadButton('Markdown (.md)', variant='secondary', visible=False)
            dl_url_pdf = gr.DownloadButton('PDF traducido (.pdf)', variant='secondary', visible=False)
            dl_url_json = gr.DownloadButton('JSON (.json)', variant='secondary', visible=False)

        async def _wrap_url(url):
            md, info, doc_id, result = await _gradio_process_url(url)
            raw_json = json.dumps(result.get('document_json', {}), ensure_ascii=False, indent=2) if result else ''
            return (md, md, raw_json, info) + _download_updates(result)

        btn_url.click(_wrap_url, inputs=[url_in],
                      outputs=[out_url, out_url_raw, out_url_json, info_url,
                               dl_url_md, dl_url_pdf, dl_url_json])

    with gr.Tab('Archivo'):
        f_in = gr.File(label='PDF', file_types=['.pdf'])
        btn_f = gr.Button('Traducir PDF', variant='primary')
        with gr.Tabs():
            with gr.Tab('Modo lectura'):
                out_f = gr.Markdown(label='Traduccion')
            with gr.Tab('Markdown'):
                out_f_raw = gr.Code(label='Markdown', language='markdown')
            with gr.Tab('JSON'):
                out_f_json = gr.Code(label='JSON estructurado', language='json')
            with gr.Tab('Detalles'):
                out_f_details = gr.Markdown(label='Detalles')
        info_f = gr.Markdown(label='Informacion')
        with gr.Row():
            dl_f_md = gr.DownloadButton('Markdown (.md)', variant='secondary', visible=False)
            dl_f_pdf = gr.DownloadButton('PDF traducido (.pdf)', variant='secondary', visible=False)
            dl_f_json = gr.DownloadButton('JSON (.json)', variant='secondary', visible=False)

        async def _wrap_file(file):
            md, info, doc_id, result = await _gradio_process_file(file)
            raw_json = json.dumps(result.get('document_json', {}), ensure_ascii=False, indent=2) if result else ''
            return (md, md, raw_json, info) + _download_updates(result)

        btn_f.click(_wrap_file, inputs=[f_in],
                    outputs=[out_f, out_f_raw, out_f_json, info_f,
                             dl_f_md, dl_f_pdf, dl_f_json])

demo.queue(max_size=20)

app = gr.mount_gradio_app(
    app, demo, path='/', show_error=True,
    allowed_paths=[str(MD_STORE_DIR), str(PDF_STORE_DIR), str(JSON_STORE_DIR),
                   str(SOURCE_PDF_DIR), str(CACHE_DIR), str(IMAGES_STORE_DIR)],
)

if __name__ == '__main__':
    port = int(os.getenv('PORT', '8000'))
    host = os.getenv('HOST', '0.0.0.0')
    if not DEEPSEEK_API_KEYS:
        log('[WARN] Sin API keys de DeepSeek.')
    uvicorn.run(app, host=host, port=port)
