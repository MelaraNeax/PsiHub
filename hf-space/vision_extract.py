"""Extraccion y traduccion de PDF con vision (DeepSeek) + estructura JSON.

El PDF se renderiza pagina a pagina como imagen. Cada pagina se envia al modelo
de vision, que devuelve un JSON con la estructura del documento y el contenido
traducido al espanol. Este modulo fusiona las paginas en un unico documento
JSON listo para exportarse a Markdown o PDF.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pymupdf

VISION_DPI = int(os.getenv('VISION_DPI', '144'))
VISION_JPEG_QUALITY = int(os.getenv('VISION_JPEG_QUALITY', '82'))
VISION_MAX_TOKENS = int(os.getenv('VISION_MAX_TOKENS', '16000'))
VISION_TIMEOUT = int(os.getenv('VISION_TIMEOUT', '300'))
VISION_CONCURRENCY = max(1, int(os.getenv('VISION_CONCURRENCY', '6')))
VISION_MAX_ATTEMPTS = max(2, int(os.getenv('VISION_MAX_ATTEMPTS', '4')))
FIGURE_DPI = int(os.getenv('FIGURE_DPI', '200'))

DEFAULT_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')
DEFAULT_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')

PAGE_PROMPT = """Eres un sistema experto en digitalizacion y traduccion de articulos cientificos.

Recibes la IMAGEN de UNA pagina de un articulo academico. Devuelve un UNICO objeto JSON valido.

REGLA DE IDIOMA (OBLIGATORIA): TODOS los campos de texto de contenido deben estar en ESPANOL:
block.text, block.items, block.caption, block.headers, block.rows, block.text de ecuaciones,
page_header/page_footer (si tienen prosa), document.title_es, document.subtitle_es,
document.abstract_es, document.keywords_es y footnotes[].text.
La unica excepcion son las entradas de la lista de referencias bibliograficas, que se conservan
en su idioma original. Si devuelves el texto original en ingles, el resultado es incorrecto.

1. LECTURA Y TRANSCRIPCION FIEL
- Lee y comprende TODO el contenido visible. NO resumas, NO omitas, NO inventes, NO recortes parrafos.
- Orden de lectura: si hay varias columnas, termina cada columna de arriba a abajo antes de pasar a la siguiente; las columnas se leen de izquierda a derecha.
- Si el primer bloque de texto continua una frase de la pagina anterior, marca layout.continues_previous = true y NO repitas lo que ya estaba en la pagina anterior.
- Conserva signos de puntuacion, citas numericas ([1], [2,3]), siglas, simbolos de afiliacion (*, †, ‡), formulas y DOIs.

2. TRADUCCION
- Traduce al espanol academico natural TODO el contenido de prosa: cuerpo, titulos de seccion, subtitulos, abstract, palabras clave, pies de figura y de tabla, y notas al pie. El campo text de cada block debe ser la TRADUCCION, nunca el original.
- NO traduzcas la lista de referencias bibliograficas: conserva cada entrada exactamente como aparece.
- Manten el mismo nivel de detalle y el mismo numero de parrafos que el original.
- Conserva nombres propios, siglas y terminos tecnicos precisos (p. ej. "sesgo atencional", "DSM-5-TR").

3. ESTRUCTURA
- page_header / page_footer: cabecera y pie editoriales (nombre de revista, ISSN, logo, "Page X of Y", copyright, volume, issue, URL). NUNCA los incluyas en blocks.
- El titulo, subtitulo, autores, afiliaciones, abstract y palabras clave van en document. En blocks no los repitas. Si un campo no aparece en esta pagina, usa null o [].
- Abstract: document.abstract_original (original completo) y document.abstract_es (traduccion completa). No lo pongas en blocks.
- Notas al pie: solo las notas fisicamente al pie de pagina (simbolos *, †, letras o numeros pequenos). En el texto deja la marca como [^marcador]. Las citas [1] dentro del texto NO son notas al pie.
- Referencias: cuando aparezca la bibliografia, usa un heading "Referencias" seguido de un block type="list" con una entrada por item (sin traducir).
- Tablas: block type="table" con caption, headers y rows. Nunca como parrafos.
- Figuras: solo figuras cientificas reales (graficos, diagramas, fotografias, esquemas). Ignora logos, iconos, marcas de agua y adornos. Incluye caption traducido y bbox normalizado [x0,y0,x1,y1] (0-1, relativo a la pagina) que cubra ESTRICTAMENTE el area grafica: el bbox debe terminar justo en el borde inferior de la imagen, por ENCIMA de la primera linea de la leyenda. Nunca incluyas la leyenda, el pie "Fig. N..." ni texto corrido dentro del bbox.
- Ecuaciones: type="equation" con latex si es posible; usa text como respaldo.

Devuelve SOLO JSON, sin markdown ni texto adicional, con esta forma:

{
  "page": 1,
  "layout": {"columns": 2, "continues_previous": false},
  "page_header": "texto o vacio",
  "page_footer": "texto o vacio",
  "document": {
    "title": null, "title_es": null,
    "subtitle": null, "subtitle_es": null,
    "authors": [], "affiliations": [],
    "journal": null, "year": null, "doi": null,
    "abstract_original": null, "abstract_es": null,
    "keywords": [], "keywords_es": [],
    "language": "en"
  },
  "blocks": [
    {"type": "heading", "level": 1, "text": "titulo de seccion TRADUCIDO"},
    {"type": "paragraph", "text": "parrafo TRADUCIDO al espanol"},
    {"type": "list", "ordered": false, "items": ["item TRADUCIDO"]},
    {"type": "quote", "text": "cita TRADUCIDA"},
    {"type": "table", "caption": "...", "headers": ["..."], "rows": [["..."]]},
    {"type": "figure", "label": "Figura 1", "caption": "...", "bbox": [0.1, 0.2, 0.9, 0.5]},
    {"type": "equation", "latex": "...", "text": "..."}
  ],
  "footnotes": [{"marker": "1", "text": "..."}]
}"""


class VisionError(RuntimeError):
    """Fallo irrecuperable durante la extraccion con vision."""


# ---------------------------------------------------------------------------
# Utilidades de imagen y JSON
# ---------------------------------------------------------------------------

def render_page_jpeg(doc: pymupdf.Document, index: int, dpi: int = VISION_DPI,
                     quality: int = VISION_JPEG_QUALITY) -> bytes:
    page = doc[index]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return pix.tobytes('jpeg', jpg_quality=quality)


def _parse_json_content(content: str) -> dict:
    text = (content or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z0-9]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    raise ValueError('El modelo no devolvio JSON valido.')


def _as_text(value: Any, limit: int = 200000) -> str:
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return ''
    text = str(value).strip()
    return text[:limit]


def _as_str_list(value: Any, limit: int = 200) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _as_text(item, 20000)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        number = int(value)
    except Exception:
        return default
    return number


# Enumeradores impresos ("25.", "25)") y numeros de pagina sueltos que el
# modelo de vision a veces arrastra al cambiar de pagina. Los quitamos antes de
# exportar porque el exportador ya numera las listas ordenadas: sin esto el
# resultado repite el numero ("25. 25. JEONG...") o reinicia en cada pagina.
_LIST_ENUM_RE = re.compile(r'^\s*(?:[-–—•]\s*)?\(?\d{1,4}[.)]\s*')
_PAGE_NUMBER_RE = re.compile(
    r'^\s*(?:p[áa]gina\s*)?\d{1,4}(?:\s*(?:of|de)\s*\d{1,4})?\s*$', re.IGNORECASE)


_PAGE_BREAK_MARKER_RE = re.compile(r'^\s*[-–—•]\s*\(?\d{1,4}[.)]\s+')


def _clean_list_items(items: list[str], ordered: bool) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        if ordered:
            text = _LIST_ENUM_RE.sub('', item).strip()
        else:
            # En listas sin numerar solo quitamos marcas de salto de pagina
            # ("- 1. ..."), no guiones normales de item.
            text = _PAGE_BREAK_MARKER_RE.sub('', item).strip()
        if not text or _PAGE_NUMBER_RE.match(text):
            continue
        cleaned.append(text)
    return cleaned


def normalize_page_payload(obj: dict, page_number: int) -> dict:
    """Valida y normaliza el JSON de una pagina para que el resto del pipeline
    no tenga que lidiar con tipos inesperados."""
    if not isinstance(obj, dict):
        raise ValueError('Payload de pagina invalido.')

    layout = obj.get('layout') if isinstance(obj.get('layout'), dict) else {}
    columns = _as_int(layout.get('columns'), 1) or 1
    columns = max(1, min(columns, 6))

    document = obj.get('document') if isinstance(obj.get('document'), dict) else {}
    doc = {
        'title': _as_text(document.get('title')) or None,
        'title_es': _as_text(document.get('title_es')) or None,
        'subtitle': _as_text(document.get('subtitle')) or None,
        'subtitle_es': _as_text(document.get('subtitle_es')) or None,
        'authors': _as_str_list(document.get('authors')),
        'affiliations': _as_str_list(document.get('affiliations')),
        'journal': _as_text(document.get('journal')) or None,
        'year': _as_int(document.get('year')),
        'doi': _as_text(document.get('doi')) or None,
        'abstract_original': _as_text(document.get('abstract_original')) or None,
        'abstract_es': _as_text(document.get('abstract_es')) or None,
        'keywords': _as_str_list(document.get('keywords')),
        'keywords_es': _as_str_list(document.get('keywords_es')),
        'language': _as_text(document.get('language')) or None,
    }

    blocks: list[dict] = []
    for raw in obj.get('blocks') or []:
        if not isinstance(raw, dict):
            continue
        btype = _as_text(raw.get('type')).lower()
        if btype == 'heading':
            text = _as_text(raw.get('text'))
            if text:
                level = _as_int(raw.get('level'), 1) or 1
                blocks.append({'type': 'heading', 'level': max(1, min(level, 4)), 'text': text})
        elif btype in ('paragraph', 'quote'):
            text = _as_text(raw.get('text'))
            if text:
                blocks.append({'type': btype, 'text': text})
        elif btype == 'list':
            items = _as_str_list(raw.get('items'))
            ordered = bool(raw.get('ordered'))
            items = _clean_list_items(items, ordered)
            if items:
                blocks.append({'type': 'list', 'ordered': ordered, 'items': items})
        elif btype == 'table':
            headers = _as_str_list(raw.get('headers'))
            rows = []
            for row in raw.get('rows') or []:
                if isinstance(row, list):
                    cells = [_as_text(cell, 20000) for cell in row]
                    if any(cells):
                        rows.append(cells)
            if headers or rows:
                blocks.append({
                    'type': 'table',
                    'caption': _as_text(raw.get('caption')),
                    'headers': headers,
                    'rows': rows,
                })
        elif btype == 'figure':
            bbox = raw.get('bbox')
            bbox = bbox if isinstance(bbox, list) and len(bbox) == 4 else None
            if bbox:
                try:
                    x0, y0, x1, y1 = [float(v) for v in bbox]
                    x0, y0 = max(0.0, min(x0, 1.0)), max(0.0, min(y0, 1.0))
                    x1, y1 = max(0.0, min(x1, 1.0)), max(0.0, min(y1, 1.0))
                    if x1 > x0 and y1 > y0:
                        bbox = [x0, y0, x1, y1]
                    else:
                        bbox = None
                except Exception:
                    bbox = None
            blocks.append({
                'type': 'figure',
                'label': _as_text(raw.get('label')),
                'caption': _as_text(raw.get('caption')),
                'bbox': bbox,
            })
        elif btype == 'equation':
            latex = _as_text(raw.get('latex'))
            text = _as_text(raw.get('text'))
            if latex or text:
                blocks.append({'type': 'equation', 'latex': latex, 'text': text})

    footnotes = []
    for raw in obj.get('footnotes') or []:
        if isinstance(raw, dict):
            marker = _as_text(raw.get('marker')) or _as_text(raw.get('id'))
            text = _as_text(raw.get('text'))
            if marker and text:
                footnotes.append({'marker': marker, 'text': text})
        elif isinstance(raw, str) and raw.strip():
            footnotes.append({'marker': str(len(footnotes) + 1), 'text': raw.strip()})

    return {
        'page': page_number,
        'layout': {
            'columns': columns,
            'continues_previous': bool(layout.get('continues_previous')),
        },
        'page_header': _as_text(obj.get('page_header')),
        'page_footer': _as_text(obj.get('page_footer')),
        'document': doc,
        'blocks': blocks,
        'footnotes': footnotes,
    }


# ---------------------------------------------------------------------------
# Llamada al modelo de vision
# ---------------------------------------------------------------------------

async def _request_page(client: httpx.AsyncClient, url: str, api_key: str, model: str,
                        image_b64: str, page_number: int) -> tuple[dict, dict]:
    payload = {
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': PAGE_PROMPT},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image_b64}},
            ],
        }],
        'temperature': 0.1,
        'stream': False,
        'response_format': {'type': 'json_object'},
        'thinking': {'type': 'disabled'},
        'max_tokens': VISION_MAX_TOKENS,
    }
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    response = await client.post(url, headers=headers, json=payload)
    if response.status_code == 429 or response.status_code >= 500:
        raise httpx.HTTPStatusError(f'HTTP {response.status_code}', request=response.request, response=response)
    if response.status_code >= 400:
        detail = response.text[:400]
        raise VisionError(f'DeepSeek HTTP {response.status_code}: {detail}')
    data = response.json()
    choice = (data.get('choices') or [{}])[0]
    content = (choice.get('message') or {}).get('content') or ''
    finish = choice.get('finish_reason')
    if not content.strip():
        raise VisionError('DeepSeek devolvio una respuesta vacia.')
    obj = _parse_json_content(content)
    if finish == 'length':
        # La respuesta se corto: es preferible reintentar que aceptar una pagina incompleta.
        raise VisionError('Respuesta truncada por limite de tokens.')
    usage = data.get('usage') or {}
    return obj, usage


async def _extract_page(client: httpx.AsyncClient, url: str, keys: list[str], model: str,
                        image_b64: str, page_number: int,
                        log_fn: Optional[Callable[[str], None]] = None) -> tuple[dict, dict]:
    log_fn = log_fn or log
    last_error: Optional[BaseException] = None
    attempts = max(VISION_MAX_ATTEMPTS, len(keys))
    for attempt in range(attempts):
        key = keys[attempt % len(keys)]
        try:
            obj, usage = await _request_page(client, url, key, model, image_b64, page_number)
            return normalize_page_payload(obj, page_number), usage
        except Exception as exc:  # noqa: BLE001 - se reintenta con otra key
            last_error = exc
            wait = min(2 ** attempt, 20)
            log_fn(f'[VISION] Pagina {page_number}, intento {attempt + 1}/{attempts} fallo: {exc}. Esperando {wait}s')
            await asyncio.sleep(wait)
    raise VisionError(f'No se pudo procesar la pagina {page_number}: {last_error}')


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Fusion de paginas
# ---------------------------------------------------------------------------

def _norm_artifact(text: str) -> str:
    text = re.sub(r'\s+', ' ', (text or '').strip().lower())
    return re.sub(r'\b\d+\b', '#', text)


_ARTIFACT_MIN_LEN = 4


def _artifact_lines(page: dict) -> set[str]:
    lines = set()
    for key in ('page_header', 'page_footer'):
        raw = page.get(key) or ''
        for line in re.split(r'[\n|·]', raw):
            norm = _norm_artifact(line)
            if len(norm) >= _ARTIFACT_MIN_LEN:
                lines.add(norm)
    return lines


def _looks_like_artifact(text: str, artifacts: set[str]) -> bool:
    norm = _norm_artifact(text)
    if not norm:
        return True
    if norm in artifacts:
        return True
    if len(norm) < 40 and any(norm in art or art in norm for art in artifacts):
        return True
    return False


def _first_non_empty(pages: list[dict], field: str) -> Any:
    for page in pages:
        value = page.get('document', {}).get(field)
        if value:
            return value
    return None


def _renumber_footnotes(page: dict, counter: list[int]) -> None:
    """Reasigna las marcas locales de una pagina a indices globales 1..N."""
    mapping: dict[str, str] = {}
    for note in page.get('footnotes') or []:
        marker = str(note.get('marker') or '').strip()
        if not marker:
            continue
        counter[0] += 1
        mapping[marker] = str(counter[0])
        note['marker'] = str(counter[0])
    if not mapping:
        return

    def replace(text: str) -> str:
        def repl(match: re.Match) -> str:
            marker = match.group(1).strip()
            return f'[^{mapping.get(marker, marker)}]'
        return re.sub(r'\[\^([^\]]+)\]', repl, text)

    for block in page.get('blocks') or []:
        if 'text' in block:
            block['text'] = replace(block['text'])
        if 'items' in block:
            block['items'] = [replace(item) for item in block['items']]
        if 'caption' in block:
            block['caption'] = replace(block['caption'])


_REFERENCE_HEADINGS = (
    'referencias', 'referenciasbibliograficas', 'references', 'bibliografia',
)


def _is_reference_heading(block: dict) -> bool:
    if block.get('type') != 'heading':
        return False
    title = re.sub(r'[^a-záéíóúñ]', '', (block.get('text') or '').lower())
    return title in _REFERENCE_HEADINGS


def _join_split_items(previous: list[str], incoming: list[str]) -> list[str]:
    """Une una lista partida por un salto de pagina.

    Si el ultimo item termina en guion de particion ("de-") y el primero de la
    pagina siguiente continua en minuscula ("pression..."), se reconstruye la
    palabra; asi no quedan restos como "de- pression" ni reinicios de numero.
    """
    merged = list(previous)
    for item in incoming:
        text = item.strip()
        if not text:
            continue
        if merged and re.search(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]-$', merged[-1]) and text[:1].islower():
            merged[-1] = merged[-1][:-1] + text
        else:
            merged.append(text)
    return merged


def _collect_references(blocks: list[dict]) -> list[str]:
    references: list[str] = []
    in_refs = False
    for block in blocks:
        if block.get('type') == 'heading':
            title = re.sub(r'[^a-záéíóúñ]', '', (block.get('text') or '').lower())
            in_refs = title in _REFERENCE_HEADINGS
            continue
        if in_refs and block.get('type') == 'list':
            references.extend(block.get('items') or [])
    return references


def merge_pages(pages: list[dict]) -> dict:
    pages = sorted(pages, key=lambda p: p.get('page', 0))
    document = {
        'title': _first_non_empty(pages, 'title'),
        'title_es': _first_non_empty(pages, 'title_es'),
        'subtitle': _first_non_empty(pages, 'subtitle'),
        'subtitle_es': _first_non_empty(pages, 'subtitle_es'),
        'authors': _first_non_empty(pages, 'authors') or [],
        'affiliations': _first_non_empty(pages, 'affiliations') or [],
        'journal': _first_non_empty(pages, 'journal'),
        'year': _first_non_empty(pages, 'year'),
        'doi': _first_non_empty(pages, 'doi'),
        'abstract_original': _first_non_empty(pages, 'abstract_original'),
        'abstract_es': _first_non_empty(pages, 'abstract_es'),
        'keywords': _first_non_empty(pages, 'keywords') or [],
        'keywords_es': _first_non_empty(pages, 'keywords_es') or [],
        'language': _first_non_empty(pages, 'language'),
    }

    blocks: list[dict] = []
    footnotes: list[dict] = []
    counter = [0]
    in_references = False
    for page in pages:
        _renumber_footnotes(page, counter)
        footnotes.extend(page.get('footnotes') or [])
        artifacts = _artifact_lines(page)
        page_blocks = []
        for block in page.get('blocks') or []:
            text = block.get('text') or block.get('caption') or ''
            if text and _looks_like_artifact(text, artifacts):
                continue
            block = dict(block)
            block['page'] = page.get('page')
            page_blocks.append(block)

        continues = bool(page.get('layout', {}).get('continues_previous'))
        if blocks and page_blocks:
            prev, first = blocks[-1], page_blocks[0]
            same_list = prev.get('type') == 'list' and first.get('type') == 'list'
            if same_list and (in_references or continues):
                # Bibliografia o lista numerada que continua en la pagina
                # siguiente: un solo bloque para que la numeracion no reinicie.
                prev['items'] = _join_split_items(
                    prev.get('items') or [], first.get('items') or [])
                prev['ordered'] = bool(prev.get('ordered') or first.get('ordered'))
                page_blocks = page_blocks[1:]
            elif continues and prev.get('type') == 'paragraph' and first.get('type') == 'paragraph':
                prev_text = prev.get('text', '').rstrip()
                next_text = first.get('text', '').lstrip()
                if prev_text and next_text:
                    if re.search(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]-$', prev_text) and next_text[:1].islower():
                        prev['text'] = prev_text[:-1] + next_text
                    else:
                        prev['text'] = prev_text + ' ' + next_text
                    page_blocks = page_blocks[1:]

        for block in page_blocks:
            if _is_reference_heading(block):
                in_references = True
            elif block.get('type') not in ('list',):
                in_references = False
        blocks.extend(page_blocks)

    # Columnas: la moda entre las paginas con valor > 1; si no, la de la pagina 1.
    column_counts = [p.get('layout', {}).get('columns', 1) for p in pages]
    columns = max(set(column_counts), key=column_counts.count) if column_counts else 1
    document['references'] = _collect_references(blocks)

    return {
        'document': document,
        'blocks': blocks,
        'footnotes': footnotes,
        'layout': {
            'columns': columns,
            'pages': len(pages),
            'page_layouts': [p.get('layout', {}) for p in pages],
        },
    }


# ---------------------------------------------------------------------------
# Figuras y entrada principal
# ---------------------------------------------------------------------------

def _caption_trim_ratio(pix: pymupdf.Pixmap) -> float:
    """Detecta una banda de texto pegada al borde inferior (resto de leyenda).

    Devuelve la fraccion de altura que conviene conservar. Solo recorta cuando
    hay una banda fina de tinta al pie separada por un hueco blanco amplio, que
    es la firma tipica de una linea de leyenda atrapada por el bbox.
    """
    width, height, components = pix.width, pix.height, pix.n
    if height < 80 or width < 80:
        return 1.0
    samples = pix.samples
    stride = pix.stride
    step = 4
    samples_x = range(0, width, step)
    total = width / step
    dark_threshold = 190

    def row_ink(y: int) -> float:
        base = y * stride
        dark = 0
        for x in samples_x:
            i = base + x * components
            if (samples[i] < dark_threshold and samples[i + 1] < dark_threshold
                    and samples[i + 2] < dark_threshold):
                dark += 1
        return dark / total

    inks = [row_ink(y) for y in range(height)]
    threshold = 0.004
    y = height - 1
    if inks[y] < threshold:
        while y >= 0 and inks[y] < threshold:
            y -= 1
        if y < int(height * 0.5):
            return 1.0
    band_bottom = y
    top = band_bottom
    gap = 0
    while y >= 0:
        if inks[y] >= threshold:
            top = y
            gap = 0
        else:
            gap += 1
            if gap > 2:
                break
        y -= 1
    band_height = band_bottom - top + 1
    white_gap = 0
    probe = top - 1
    while probe >= 0 and inks[probe] < threshold:
        white_gap += 1
        probe -= 1
    min_gap = max(8, int(height * 0.012))
    max_band = int(height * 0.08)
    if white_gap >= min_gap and band_height <= max_band and top > height * 0.5:
        keep = top - white_gap * 0.35
        return max(0.5, keep / height)
    return 1.0


def crop_figures(doc: pymupdf.Document, merged: dict, out_dir: Path,
                 doc_id: str) -> int:
    """Recorta las figuras indicadas por el modelo y anade la ruta local."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for block in merged.get('blocks', []):
        if block.get('type') != 'figure':
            continue
        bbox = block.get('bbox')
        page_number = block.get('page')
        if not bbox or not page_number or page_number > len(doc):
            continue
        page = doc[page_number - 1]
        x0, y0, x1, y1 = bbox
        width, height = page.rect.width, page.rect.height
        pad_x, pad_y = width * 0.01, height * 0.004
        rect = pymupdf.Rect(
            max(0, x0 * width - pad_x), max(0, y0 * height - pad_y),
            min(width, x1 * width + pad_x), min(height, y1 * height + pad_y),
        )
        if rect.width < 40 or rect.height < 40:
            continue
        area_ratio = (rect.width * rect.height) / (width * height)
        if area_ratio < 0.015 or area_ratio > 0.985:
            continue
        probe = page.get_pixmap(clip=rect, dpi=72, alpha=False)
        keep = _caption_trim_ratio(probe)
        if keep < 0.999:
            rect.y1 = rect.y0 + rect.height * keep
            if rect.height < 40:
                continue
        pix = page.get_pixmap(clip=rect, dpi=FIGURE_DPI, alpha=False)
        filename = f'figure-p{page_number:03d}-{saved + 1:02d}.png'
        target = out_dir / filename
        pix.save(str(target))
        block['image_file'] = filename
        block['image_url'] = f'/api/document/{doc_id}/image/{filename}'
        saved += 1
    return saved


async def extract_document(pdf_bytes: bytes, api_keys: list[str], model: Optional[str] = None,
                           progress: Optional[Callable[[int, int], None]] = None,
                           images_dir: Optional[Path] = None,
                           doc_id: str = '',
                           log_fn: Optional[Callable[[str], None]] = None) -> dict:
    """Renderiza, extrae y fusiona todas las paginas de un PDF."""
    if not api_keys:
        raise VisionError('No hay API keys de DeepSeek configuradas.')
    if not pdf_bytes[:10].startswith(b'%PDF'):
        raise VisionError('El archivo no es un PDF valido.')

    log_fn = log_fn or log
    selected_model = (model or '').strip() or DEFAULT_MODEL
    url = DEFAULT_URL
    doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    total = len(doc)
    if total == 0:
        doc.close()
        raise VisionError('El PDF no tiene paginas.')
    log_fn(f'[VISION] {total} paginas, modelo {selected_model}, concurrencia {min(VISION_CONCURRENCY, len(api_keys))}')

    render_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(min(VISION_CONCURRENCY, len(api_keys), total))
    usage_total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    done = [0]
    started = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(VISION_TIMEOUT, connect=20.0),
        limits=httpx.Limits(max_connections=max(10, VISION_CONCURRENCY * 2),
                            max_keepalive_connections=max(5, VISION_CONCURRENCY)),
    ) as client:
        async def process(index: int) -> dict:
            async with semaphore:
                async with render_lock:
                    image = render_page_jpeg(doc, index)
                image_b64 = base64.b64encode(image).decode('ascii')
                page_obj, usage = await _extract_page(
                    client, url, api_keys, selected_model, image_b64, index + 1, log_fn=log_fn)
                for key in usage_total:
                    usage_total[key] += int(usage.get(key) or 0)
                done[0] += 1
                log_fn(f'[VISION] Pagina {index + 1}/{total} lista ({done[0]}/{total})')
                if progress:
                    progress(done[0], total)
                return page_obj

        results = await asyncio.gather(*(process(i) for i in range(total)))

    merged = merge_pages(results)
    merged['document']['model'] = selected_model
    figures = crop_figures(doc, merged, Path(images_dir), doc_id) if images_dir and doc_id else 0
    merged['stats'] = {
        'pages': total,
        'figures': figures,
        'usage': usage_total,
        'elapsed_sec': round(time.perf_counter() - started, 2),
        'model': selected_model,
    }
    doc.close()
    return merged
