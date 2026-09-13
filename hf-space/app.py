# ============================================================
# PsiHub Reader
# PDF académico -> Markdown limpio -> traducción académica
#
# Arquitectura:
#   PDF
#    ↓
#   análisis estructural del layout
#    ↓
#   perfil editorial
#    ↓
#   limpieza física conservadora
#    ↓
#   pymupdf4llm  (+ fallback nativo por página / global)
#    ↓
#   limpieza estructural del Markdown
#    ↓
#   detección de idioma
#    ↓
#   aislamiento de tablas
#    ↓
#   GUARD pre-traducción
#    ↓
#   traducción por chunks (con filtro de chunks vacíos)
#    ↓
#   restauración de tablas/imágenes
#    ↓
#   Markdown optimizado para lectura
# ============================================================

import os
import sys
import re
import time
import json
import hashlib
import tempfile
import base64
import asyncio
from pathlib import Path
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Optional

import httpx
import fitz
import pymupdf4llm

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pydantic import BaseModel

import gradio as gr
import uvicorn

from dotenv import load_dotenv

from visual_layout_ai import (
    analyze_pdf_boundaries,
    analyze_page_columns_with_vision,   # ← NUEVA
    collect_editorial_patterns,
    boundary_hints_by_page,
    audit_pages_with_vision,
)
from document_model import (
    build_document_model,
    validate_model,
    page_visual_candidates,
    build_visual_router,
    audit_block_sequence,
)

from collections import deque

LOG_BUFFER: deque = deque(maxlen=2000)
LOG_BUFFER_LOCK = asyncio.Lock()


def log(msg: str):
    """Imprime en stdout Y guarda en buffer consultable vía HTTP."""
    print(msg)
    LOG_BUFFER.append({
        "t": time.time(),
        "msg": msg,
    })

# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv()

DEEPSEEK_API_KEYS = [
    os.getenv(f"DEEPSEEK_API_KEY_{i}", "").strip()
    for i in range(1, 7)
]

_legacy_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
if not any(DEEPSEEK_API_KEYS) and _legacy_key:
    DEEPSEEK_API_KEYS = [_legacy_key]

DEEPSEEK_API_KEYS = [key for key in DEEPSEEK_API_KEYS if key]

DEEPSEEK_API_KEY = DEEPSEEK_API_KEYS[0] if DEEPSEEK_API_KEYS else ""

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com/chat/completions"
)

TRANSLATION_CONCURRENCY = max(
    1,
    min(
        int(os.getenv("TRANSLATION_CONCURRENCY", "6")),
        len(DEEPSEEK_API_KEYS) if DEEPSEEK_API_KEYS else 1
    )
)

TRANSLATION_DELAY = float(os.getenv("TRANSLATION_DELAY", "0"))

CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_CHARS_PER_CHUNK = int(os.getenv("MAX_CHARS_PER_CHUNK", "18000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

DEEPSEEK_HTTP_LIMITS = httpx.Limits(
    max_connections=max(20, TRANSLATION_CONCURRENCY * 3),
    max_keepalive_connections=max(10, TRANSLATION_CONCURRENCY * 2),
)
DEEPSEEK_HTTP_CLIENT = httpx.AsyncClient(
    limits=DEEPSEEK_HTTP_LIMITS,
    timeout=httpx.Timeout(REQUEST_TIMEOUT),
)

# IMPORTANTE: subir esta versión invalida todos los caches anteriores.
PIPELINE_VERSION = "2026-09-12-reader-master-v20"

PDF_STORE_DIR = CACHE_DIR / "source_pdfs"
PDF_STORE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# BUFFER DE LOGS CONSULTABLE VÍA HTTP
# ============================================================

LOG_BUFFER: deque = deque(maxlen=2000)


def log(msg: str):
    """Imprime en stdout Y guarda en buffer consultable vía HTTP."""
    print(msg, flush=True)
    LOG_BUFFER.append({
        "t": time.time(),
        "msg": msg,
    })


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(title="PsiHub Reader", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TranslateRequest(BaseModel):
    url: str
    model: Optional[str] = None


# ============================================================
# UTILIDADES GENERALES
# ============================================================

def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def normalize_editorial_text(text: str) -> str:
    text = normalize_whitespace(text)
    text = re.sub(r"\b\d+\b", "#", text)
    text = re.sub(r"\s*[-–—|]\s*", " - ", text)
    return text.lower().strip()


def is_page_number(text: str) -> bool:
    s = normalize_whitespace(text)
    patterns = [
        r"^\d+$",
        r"^[-–—]?\s*\d+\s*[-–—]?$",
        r"^page\s+\d+$",
        r"^p\.?\s*\d+$",
        r"^\d+\s+(?:of|de)\s+\d+$",
        r"^page\s+\d+\s+(?:of|de)\s+\d+$",
        r"^\d+\s*/\s*\d+$",
    ]
    return any(
        re.fullmatch(pattern, s, re.IGNORECASE)
        for pattern in patterns
    )


def looks_like_email(text: str) -> bool:
    return bool(re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        text, re.IGNORECASE
    ))


def looks_like_doi(text: str) -> bool:
    return bool(re.search(
        r"(?:doi\s*:\s*|https?://doi\.org/)\S+",
        text, re.IGNORECASE
    ))


def looks_like_url(text: str) -> bool:
    return bool(re.search(r"https?://\S+", text, re.IGNORECASE))


def is_copyright_line(text: str) -> bool:
    return bool(re.search(
        r"(?:copyright|©|\(c\))\s*(?:19|20)\d{2}",
        text, re.IGNORECASE
    ))


# ============================================================
# PERFIL DE LAYOUT
# ============================================================

@dataclass
class LayoutElement:
    text: str
    normalized: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    width: float
    height: float
    region: str


@dataclass
class DocumentLayoutProfile:
    page_count: int
    page_width: float
    page_height: float
    header_height: float
    footer_height: float
    repeated_headers: list
    repeated_footers: list
    page_numbers: bool
    likely_columns: int
    body_top: float
    body_bottom: float
    first_page_special: bool
    elements_removed_estimate: int = 0


# ============================================================
# ANÁLISIS DEL LAYOUT
# ============================================================

def classify_region(block_rect, page_rect, header_height, footer_height) -> str:
    if block_rect.y1 <= header_height:
        return "header"
    if block_rect.y0 >= page_rect.height - footer_height:
        return "footer"
    return "body"


def collect_page_elements(doc, header_fraction=0.08, footer_fraction=0.08):
    elements = []
    for page_index, page in enumerate(doc):
        rect = page.rect
        header_height = rect.height * header_fraction
        footer_height = rect.height * footer_fraction
        page_dict = page.get_text("dict")

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(
                    span.get("text", "") for span in spans
                ).strip()
                if not text:
                    continue
                bbox = line.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x0, y0, x1, y1 = bbox
                block_rect = fitz.Rect(x0, y0, x1, y1)
                region = classify_region(
                    block_rect, rect, header_height, footer_height
                )
                elements.append(LayoutElement(
                    text=text,
                    normalized=normalize_editorial_text(text),
                    x0=x0, y0=y0, x1=x1, y1=y1,
                    page=page_index + 1,
                    width=x1 - x0,
                    height=y1 - y0,
                    region=region,
                ))
    return elements


def detect_repeated_elements(elements, page_count, region):
    if not elements or page_count <= 1:
        return set()
    pages_by_text = defaultdict(set)
    for element in elements:
        if element.region != region:
            continue
        normalized = element.normalized.strip()
        if not normalized or len(normalized) < 2:
            continue
        pages_by_text[normalized].add(element.page)
    threshold = max(2, int(page_count * 0.40 + 0.9999))
    return {
        text for text, pages in pages_by_text.items()
        if len(pages) >= threshold
    }


def detect_page_numbers(elements):
    pages_with_numbers = set()
    for element in elements:
        if element.region != "footer":
            continue
        if is_page_number(element.text):
            pages_with_numbers.add(element.page)
    return pages_with_numbers


def detect_columns(doc):
    if doc.page_count == 0:
        return 1
    sample_pages = min(doc.page_count, 5)
    column_scores = []
    for i in range(sample_pages):
        page = doc[i]
        rect = page.rect
        blocks = []
        for block in page.get_text("blocks"):
            if len(block) < 5:
                continue
            x0, y0, x1, y1, text = block[:5]
            text = text.strip()
            if not text:
                continue
            if y0 < rect.height * 0.12:
                continue
            if y1 > rect.height * 0.88:
                continue
            blocks.append((x0, x1, y0, y1, text))
        if len(blocks) < 4:
            column_scores.append(1)
            continue
        page_center = rect.width / 2
        left = 0
        right = 0
        for x0, x1, *_ in blocks:
            center = (x0 + x1) / 2
            if center < page_center:
                left += 1
            else:
                right += 1
        column_scores.append(2 if (left >= 2 and right >= 2) else 1)
    if not column_scores:
        return 1
    return 2 if sum(column_scores) / len(column_scores) >= 1.5 else 1


def calculate_body_bounds(doc, elements, repeated_headers, repeated_footers):
    if doc.page_count == 0:
        return 0, 0
    page_heights = [page.rect.height for page in doc]
    average_height = sum(page_heights) / len(page_heights)
    header_positions = []
    footer_positions = []
    for element in elements:
        if element.normalized in repeated_headers:
            header_positions.append(element.y1)
        if element.normalized in repeated_footers:
            footer_positions.append(element.y0)
    body_top = (max(header_positions) + 5) if header_positions else average_height * 0.08
    body_bottom = (min(footer_positions) - 5) if footer_positions else average_height * 0.92
    return body_top, body_bottom


def is_publisher_landing_page(page) -> bool:
    text = page.get_text("text").lower()
    markers = [
        "to cite this article",
        "to link to this article",
        "published online",
        "submit your article",
        "article views:",
        "view related articles",
        "citing articles:",
        "full terms & conditions",
    ]
    hits = sum(1 for marker in markers if marker in text)
    return (
        hits >= 3
        and (
            "to cite this article" in text
            or "to link to this article" in text
        )
    )


def remove_publisher_landing_page(doc) -> bool:
    if doc.page_count <= 1:
        return False
    if not is_publisher_landing_page(doc[0]):
        return False
    print("[PDF CLEAN] Primera página detectada como portada/página editorial; se elimina.")
    doc.delete_page(0)
    return True


def is_editorial_artifact_line(text: str) -> bool:
    s = normalize_whitespace(text)
    if re.search(r"pages_[^\s]+\.qxd", s, re.IGNORECASE):
        return True
    if re.search(
        r"^dialogues\s+clin\s+neurosci\.\s*\d{4};\d+:\d+[-–]\d+\.?$",
        s, re.IGNORECASE
    ):
        return True
    return False


def analyze_document_layout(doc) -> DocumentLayoutProfile:
    if doc.page_count == 0:
        raise ValueError("El PDF no contiene páginas.")

    first_page = doc[0]
    page_width = first_page.rect.width
    page_height = first_page.rect.height

    HEADER_FRACTION = 0.14
    FOOTER_FRACTION = 0.08

    elements = collect_page_elements(
        doc,
        header_fraction=HEADER_FRACTION,
        footer_fraction=FOOTER_FRACTION
    )
    repeated_headers = detect_repeated_elements(elements, doc.page_count, "header")
    repeated_footers = detect_repeated_elements(elements, doc.page_count, "footer")
    page_number_pages = detect_page_numbers(elements)
    columns = detect_columns(doc)
    body_top, body_bottom = calculate_body_bounds(
        doc, elements, repeated_headers, repeated_footers
    )

    return DocumentLayoutProfile(
        page_count=doc.page_count,
        page_width=round(page_width, 2),
        page_height=round(page_height, 2),
        header_height=round(page_height * HEADER_FRACTION, 2),
        footer_height=round(page_height * FOOTER_FRACTION, 2),
        repeated_headers=sorted(list(repeated_headers)),
        repeated_footers=sorted(list(repeated_footers)),
        page_numbers=bool(page_number_pages),
        likely_columns=columns,
        body_top=round(body_top, 2),
        body_bottom=round(body_bottom, 2),
        first_page_special=True,
    )


# ============================================================
# LIMPIEZA FÍSICA DEL PDF
# ============================================================

def should_remove_element(element, profile):
    text = element.text.strip()
    if not text:
        return False, ""

    if element.region == "footer" and is_page_number(text):
        return True, "page number"

    if element.region == "header" and element.normalized in profile.repeated_headers:
        if element.page == 1:
            return False, ""
        return True, "repeated header"

    if element.region == "footer" and element.normalized in profile.repeated_footers:
        return True, "repeated footer"

    return False, ""


def clean_pdf_using_layout(doc, profile):
    removed = 0
    for page_number, page in enumerate(doc, start=1):
        rect = page.rect
        page_dict = page.get_text("dict")
        redactions = []

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue
                bbox = line.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x0, y0, x1, y1 = bbox
                line_rect = fitz.Rect(x0, y0, x1, y1)
                element = LayoutElement(
                    text=text,
                    normalized=normalize_editorial_text(text),
                    x0=x0, y0=y0, x1=x1, y1=y1,
                    page=page_number,
                    width=x1 - x0,
                    height=y1 - y0,
                    region=classify_region(
                        line_rect, rect,
                        profile.header_height, profile.footer_height
                    )
                )
                should_remove, reason = should_remove_element(element, profile)
                if should_remove:
                    redactions.append((line_rect, reason, text))

        for image_info in page.get_images(full=True):
            xref = image_info[0]
            for image_rect in page.get_image_rects(xref):
                if image_rect.is_empty:
                    continue
                width = image_rect.width
                height = image_rect.height
                mostly_outside = (
                    image_rect.x1 <= 0 or image_rect.x0 >= rect.width
                    or image_rect.y1 <= 0 or image_rect.y0 >= rect.height
                )
                horizontal_rule = (width >= rect.width * 0.35 and height <= 15)
                vertical_rule = (height >= rect.height * 0.35 and width <= 15)
                in_editorial_header = (image_rect.y1 <= rect.height * 0.14)
                in_editorial_footer = (image_rect.y0 >= rect.height * 0.92)
                if (mostly_outside or horizontal_rule or vertical_rule
                        or in_editorial_header or in_editorial_footer):
                    redactions.append((image_rect, "editorial graphic", f"image xref={xref}"))

        for rect_to_remove, reason, text in redactions:
            page.add_redact_annot(rect_to_remove, fill=(1, 1, 1))
            removed += 1
            log(f"[PDF CLEAN] Página {page_number}: {reason}: {text[:120]}")

        if redactions:
            page.apply_redactions()

    profile.elements_removed_estimate = removed
    return removed


def print_layout_profile(profile):
    print("\n" + "=" * 70)
    print("PERFIL ESTRUCTURAL DEL DOCUMENTO")
    print("=" * 70)
    print(f"Páginas: {profile.page_count}")
    print(f"Tamaño: {profile.page_width} × {profile.page_height} pt")
    log(f"Columnas estimadas: {profile.likely_columns}")
    print(f"Header zone: {profile.header_height} pt")
    print(f"Footer zone: {profile.footer_height} pt")
    print(f"Números de página: {'sí' if profile.page_numbers else 'no'}")
    print(f"Headers repetidos: {len(profile.repeated_headers)}")
    for header in profile.repeated_headers:
        log(f"  HEADER: {header}")
    print(f"Footers repetidos: {len(profile.repeated_footers)}")
    for footer in profile.repeated_footers:
        print(f"  FOOTER: {footer}")
    print("=" * 70 + "\n")


# ============================================================
# DESCARGA DE PDF
# ============================================================

async def download_pdf(url: str) -> bytes:
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=15.0)

    headers = {
        # Muchos publishers bloquean user-agents genéricos.
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content
        content_type = (response.headers.get("content-type") or "").lower()

    # Aceptar si:
    #   - los primeros bytes son %PDF, o
    #   - el content-type es application/pdf, o
    #   - hay %PDF en los primeros 4 KB (BOM/preámbulo)
    looks_like_pdf = (
        data.startswith(b"%PDF")
        or "application/pdf" in content_type
        or b"%PDF" in data[:4096]
    )

    if not looks_like_pdf:
        snippet = data[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"La URL no devuelve un PDF. Content-Type: {content_type!r}. "
            f"Primeros bytes: {snippet!r}"
        )

    # Si el PDF está desplazado (por BOM o preámbulo), recortar hasta %PDF.
    if not data.startswith(b"%PDF"):
        idx = data.find(b"%PDF")
        if 0 < idx < 4096:
            data = data[idx:]

    return data


# ============================================================
# CACHE
# ============================================================

def cache_key(content: bytes, model: str):
    digest = hashlib.sha256(content).hexdigest()
    model_digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:12]
    version_digest = hashlib.sha256(PIPELINE_VERSION.encode("utf-8")).hexdigest()[:12]
    return f"{digest}_{model_digest}_{version_digest}"


def get_cache_path(key: str):
    return CACHE_DIR / f"{key}.json"


def load_cache(key: str):
    path = get_cache_path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(key: str, data: dict):
    path = get_cache_path(key)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    temp_path.replace(path)


# ============================================================
# EXTRACCIÓN MARKDOWN
# ============================================================

def _safe_box_bbox(box):
    bbox = box.get("bbox") if isinstance(box, dict) else None
    if not bbox or len(bbox) != 4:
        return None
    try:
        return tuple(float(value) for value in bbox)
    except (TypeError, ValueError):
        return None


def _safe_box_pos(box, text_length: int):
    pos = box.get("pos") if isinstance(box, dict) else None
    if not pos or len(pos) != 2:
        return None
    try:
        start = max(0, min(int(pos[0]), text_length))
        stop = max(start, min(int(pos[1]), text_length))
    except (TypeError, ValueError):
        return None
    if stop <= start:
        return None
    return start, stop


def _normalize_footnote_markdown(text: str) -> str:
    if not text or not text.strip():
        return ""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    output = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if output and output[-1] != ">":
                output.append(">")
            continue
        stripped = re.sub(r"^>\s*", "", stripped)
        output.append("> " + stripped)
    while output and output[-1] == ">":
        output.pop()
    return "\n".join(output).strip()


def _remove_ranges_from_text(text: str, ranges) -> str:
    if not text or not ranges:
        return text
    result = text
    for start, stop in sorted(ranges, reverse=True):
        result = result[:start] + result[stop:]
    return result


def _footnote_y_key(item) -> float:
    bbox = _safe_box_bbox(item[0])
    if bbox is None:
        return 0.0
    return bbox[1]


def _box_sort_key(box):
    bbox = _safe_box_bbox(box)
    if bbox is None:
        return (0.0, 0.0, 0)
    x0, y0, x1, y1 = bbox
    return (y0, x0, int(box.get("index", 0)))


def _detect_two_columns_from_page_boxes(page: dict, page_width: float) -> bool:
    boxes = page.get("page_boxes") or []
    candidates = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        box_class = str(box.get("class", "")).lower()
        if box_class in {"page-header", "page-footer", "footnote"}:
            continue
        bbox = _safe_box_bbox(box)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        width = x1 - x0
        if width <= 0:
            continue
        if width >= page_width * 0.68:
            continue
        center = (x0 + x1) / 2.0
        if center < page_width * 0.47 or center > page_width * 0.53:
            candidates.append(center)
    if len(candidates) < 4:
        return False
    left = [value for value in candidates if value < page_width * 0.5]
    right = [value for value in candidates if value >= page_width * 0.5]
    if len(left) < 2 or len(right) < 2:
        return False
    left_edge = max(left)
    right_edge = min(right)
    gap = right_edge - left_edge
    return (
        left_edge < page_width * 0.47
        and right_edge > page_width * 0.53
        and gap >= page_width * 0.06
    )


def _reorder_two_column_page(page: dict, page_width: float) -> str:
    text = str(page.get("text", "") or "")
    boxes = page.get("page_boxes") or []
    if not text or not boxes:
        return text

    usable = []
    footnotes = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        pos = _safe_box_pos(box, len(text))
        if pos is None:
            continue
        box_class = str(box.get("class", "")).lower()
        if box_class == "footnote":
            footnotes.append((box, pos))
            continue
        bbox = _safe_box_bbox(box)
        if bbox is None:
            continue
        usable.append((box, pos, bbox))

    original_len = len(text)
    rebuilt = None

    if usable:
        narrow = []
        for box, pos, bbox in usable:
            x0, y0, x1, y1 = bbox
            width = x1 - x0
            if width < page_width * 0.68:
                narrow.append((box, pos, bbox))

        left_boxes = []
        right_boxes = []
        for item in narrow:
            _, _, bbox = item
            x0, y0, x1, y1 = bbox
            center = (x0 + x1) / 2.0
            if center < page_width * 0.5:
                left_boxes.append(item)
            else:
                right_boxes.append(item)

        has_two_columns = (
            len(left_boxes) >= 2
            and len(right_boxes) >= 2
            and max((b[2][0] + b[2][2]) / 2 for b in left_boxes) < page_width * 0.47
            and min((b[2][0] + b[2][2]) / 2 for b in right_boxes) > page_width * 0.53
        )

        if not has_two_columns:
            cleaned = _remove_ranges_from_text(text, [pos for _, pos in footnotes])
        else:
            full_width = []
            column_boxes = []
            for item in usable:
                _, _, bbox = item
                x0, y0, x1, y1 = bbox
                width = x1 - x0
                center = (x0 + x1) / 2.0
                if (width >= page_width * 0.68
                        or (x0 <= page_width * 0.08 and x1 >= page_width * 0.92)):
                    full_width.append(item)
                elif center < page_width * 0.5:
                    column_boxes.append(("left", item))
                else:
                    column_boxes.append(("right", item))

            full_width.sort(key=lambda item: _box_sort_key(item[0]))
            left_items = [item for side, item in column_boxes if side == "left"]
            right_items = [item for side, item in column_boxes if side == "right"]
            left_items.sort(key=_box_sort_key)
            right_items.sort(key=_box_sort_key)

            column_start_y = (
                min(item[2][1] for item in left_items + right_items)
                if left_items and right_items
                else float("inf")
            )

            ordered = []
            leading_full = [i for i in full_width if i[2][3] <= column_start_y + 8]
            middle_full = [i for i in full_width if i not in leading_full]
            ordered.extend(sorted(leading_full, key=_box_sort_key))

            if left_items or right_items:
                if middle_full:
                    remaining_left = list(left_items)
                    remaining_right = list(right_items)
                    for full_item in middle_full:
                        full_bbox = _safe_box_bbox(full_item[0])
                        if full_bbox is None:
                            continue
                        full_y0 = full_bbox[1]
                        left_before = [i for i in remaining_left if i[2][1] < full_y0]
                        right_before = [i for i in remaining_right if i[2][1] < full_y0]
                        if left_before or right_before:
                            ordered.extend(left_before)
                            ordered.extend(right_before)
                            remaining_left = [i for i in remaining_left if i not in left_before]
                            remaining_right = [i for i in remaining_right if i not in right_before]
                        ordered.append(full_item)
                    ordered.extend(remaining_left)
                    ordered.extend(remaining_right)
                else:
                    ordered.extend(left_items)
                    ordered.extend(right_items)
            else:
                ordered.extend(middle_full)

            used_ids = {id(item) for item in ordered}
            leftovers = [i for i in usable if id(i) not in used_ids]
            ordered.extend(sorted(leftovers, key=_box_sort_key))

            segments = []
            seen_ranges = set()
            for box, pos, bbox in ordered:
                if pos in seen_ranges:
                    continue
                seen_ranges.add(pos)
                segment = text[pos[0]:pos[1]]
                if segment:
                    segments.append(segment)

            candidate = ""
            for segment in segments:
                if not candidate:
                    candidate = segment
                    continue
                if not candidate.endswith(("\n", " ", "\t")) and not segment.startswith("\n"):
                    candidate += "\n\n"
                candidate += segment

            if len(candidate) >= int(original_len * 0.80):
                rebuilt = candidate
            else:
                print(
                    f"[LAYOUT] Reordenamiento de dos columnas descartado "
                    f"(pérdida {100 - int(len(candidate) * 100 / max(1, original_len))}% "
                    f"del texto). Se conserva el orden original."
                )
                rebuilt = _remove_ranges_from_text(text, [pos for _, pos in footnotes])

    if rebuilt is None:
        rebuilt = _remove_ranges_from_text(text, [pos for _, pos in footnotes])

    footnote_blocks = []
    for box, pos in sorted(
        footnotes,
        key=lambda item: (_footnote_y_key(item), int(item[0].get("index", 0))),
    ):
        note = _normalize_footnote_markdown(text[pos[0]:pos[1]])
        if note:
            footnote_blocks.append(note)

    cleaned = rebuilt.strip()
    if footnote_blocks:
        cleaned += "\n\n" + "\n\n".join(footnote_blocks)

    return cleaned.strip()


def normalize_page_chunk_layout(page, page_width, page_number):
    if not isinstance(page, dict):
        return {"text": str(page)}

    normalized = dict(page)
    text = str(page.get("text", "") or "")
    boxes = page.get("page_boxes") or []

    if not text or not boxes:
        normalized["text"] = text
        return normalized

    footnote_ranges = []
    footnotes = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        if str(box.get("class", "")).lower() != "footnote":
            continue
        pos = _safe_box_pos(box, len(text))
        if pos is None:
            continue
        footnote_ranges.append(pos)
        footnotes.append((box, pos))

    if _detect_two_columns_from_page_boxes(page, page_width):
        corrected = _reorder_two_column_page(page, page_width)
    else:
        corrected = _remove_ranges_from_text(text, footnote_ranges)
        footnote_blocks = []
        for box, pos in sorted(
            footnotes,
            key=lambda item: (_footnote_y_key(item), int(item[0].get("index", 0))),
        ):
            note = _normalize_footnote_markdown(text[pos[0]:pos[1]])
            if note:
                footnote_blocks.append(note)
        corrected = corrected.strip()
        if footnote_blocks:
            corrected += "\n\n" + "\n\n".join(footnote_blocks)
        corrected = corrected.strip()

    normalized["text"] = corrected
    normalized["_layout_normalized"] = True
    normalized["_page_number"] = page_number
    return normalized


def _markdown_text_signal(markdown: str) -> int:
    """Cuenta texto real del Markdown, ignorando etiquetas de imagen."""
    if not markdown:
        return 0
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"^>\s*\*?Página\s+\d+[^\n]*$", " ", text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r"^@@TABLE_\d+@@\s*$", " ", text, flags=re.MULTILINE)
    return len(re.sub(r"\s+", " ", text).strip())


def _is_deepseek_refusal(text: str) -> bool:
    """Detecta cuando DeepSeek se niega a traducir por falta de contenido."""
    if not text:
        return True
    low = text.lower()[:800]
    patterns = [
        "no hay contenido textual",
        "no hay texto para traducir",
        "no contiene texto extraíble",
        "no contiene texto",
        "no puedo traducir",
        "no se puede traducir",
        "el documento no contiene",
        "las páginas proporcionadas contienen",
        "no hay nada que traducir",
        "el contenido está vacío",
        "no es posible traducir",
        "no hay contenido para traducir",
    ]
    return any(p in low for p in patterns)

# ============================================================
# EXTRACCIÓN POR COLUMNAS (Vision-guided)
# ============================================================

def _block_text_from_pymupdf(block: dict) -> str:
    """Extrae el texto plano de un block de PyMuPDF preservando saltos internos."""
    if block.get("type") != 0:
        return ""
    lines = []
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            parts.append(span.get("text", ""))
        line_text = "".join(parts).rstrip()
        if line_text.strip():
            lines.append(line_text)
    return "\n".join(lines).strip()


def _classify_block_kind(block: dict, page_height: float) -> str:
    """Clasifica un block por su aspecto: heading, párrafo, caption, etc."""
    text = _block_text_from_pymupdf(block)
    if not text:
        return "empty"

    max_size = 0
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            s = span.get("size", 0)
            if s > max_size:
                max_size = s

    bbox = block.get("bbox", [0, 0, 0, 0])
    height = bbox[3] - bbox[1]

    # Heurísticas simples
    if max_size > 14 and len(text) < 100:
        return "heading"
    if re.match(r"^(Figure|Fig\.|Table|Tabla|Figura|TABLE)\s+\d", text, re.I):
        return "caption"
    if re.match(r"^(ABSTRACT|Keywords|References|RESUMEN|Palabras clave|Referencias)\s*$",
                text, re.I):
        return "heading"
    if height < 15 and len(text) < 80:
        return "short_line"
    return "paragraph"


def extract_page_column_aware(
    page,
    layout_hint: Optional[dict] = None,
    header_height: float = 0.0,
    footer_height: float = 0.0,
) -> str:
    """
    Extrae el texto de una página respetando la estructura de columnas.

    Si `layout_hint` viene de Vision, se usa su `column_split_x_pct` y
    `reading_order`. Si no, se detecta geométricamente por mediana de x.
    """
    page_rect = page.rect
    page_width = page_rect.width
    page_height = page_rect.height

    page_dict = page.get_text("dict")
    blocks = []
    for b in page_dict.get("blocks", []):
        if b.get("type") != 0:
            continue
        bbox = b.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        if y1 <= header_height or y0 >= page_height - footer_height:
            continue
        text = _block_text_from_pymupdf(b)
        if not text:
            continue
        blocks.append({
            "bbox": bbox,
            "text": text,
            "x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "x_center": (x0 + x1) / 2,
            "kind": _classify_block_kind(b, page_height),
        })

    if not blocks:
        return ""

    # --- Decidir split vertical ---
    if layout_hint and not layout_hint.get("error"):
        n_cols = int(layout_hint.get("num_columns", 1) or 1)
        split_pct = float(layout_hint.get("column_split_x_pct") or 0.5)
        order = str(layout_hint.get("reading_order", "top_to_bottom"))
        column_split_x = page_width * split_pct
    else:
        n_cols = 1
        split_pct = 0.5
        order = "top_to_bottom"
        column_split_x = page_width * 0.5

    # --- Si Vision dijo 1 columna, ordenar por y y listo ---
    if n_cols == 1:
        blocks.sort(key=lambda b: (b["y0"], b["x0"]))
        return "\n\n".join(b["text"] for b in blocks)

    # --- Si 2 columnas: clasificar bloques ---
    left, right, full = [], [], []
    for b in blocks:
        # Full-width si cruza el split por mucho
        if b["x0"] < column_split_x - page_width * 0.12 and \
           b["x1"] > column_split_x + page_width * 0.12:
            full.append(b)
        elif b["x_center"] < column_split_x:
            left.append(b)
        else:
            right.append(b)

    # Ordenar cada grupo verticalmente
    left.sort(key=lambda b: b["y0"])
    right.sort(key=lambda b: b["y0"])
    full.sort(key=lambda b: b["y0"])

    # Bloques de ancho completo que van antes del inicio de columnas
    # (título de página, abstract heading, etc.)
    column_start_y = min(
        [b["y0"] for b in (left + right)] or [page_height]
    )
    leading_full = [b for b in full if b["y0"] < column_start_y - 5]
    middle_full = [b for b in full if b["y0"] >= column_start_y - 5]

    # Armar orden final
    if order == "left_then_right":
        ordered = leading_full + left + right + middle_full
    elif order == "mixed" and middle_full:
        # Intercalar full-width en medio: aproximación por posición Y
        ordered = leading_full + left + right
        # Insertar middle_full donde corresponda por Y
        for fb in middle_full:
            # Insertar antes del primer bloque cuya y sea mayor
            insert_idx = len(ordered)
            for i, ob in enumerate(ordered):
                if ob["y0"] > fb["y0"]:
                    insert_idx = i
                    break
            ordered.insert(insert_idx, fb)
    else:
        # Fallback conservador
        ordered = leading_full + left + right + middle_full

    # --- Convertir a Markdown ---
    md_parts = []
    for b in ordered:
        text = b["text"]
        if b["kind"] == "heading":
            md_parts.append(f"## {text.strip()}")
        else:
            md_parts.append(text)
    return "\n\n".join(md_parts)

async def extract_markdown_column_aware(
    doc,
    layout_hints: dict[int, dict],
    header_height: float,
    footer_height: float,
) -> list[dict]:
    """
    Extrae Markdown página por página usando la info de layout de Vision.
    Devuelve la misma estructura que `extract_markdown_and_images`.
    """
    pages = []
    for page_number, page in enumerate(doc, start=1):
        hint = layout_hints.get(page_number)
        text = extract_page_column_aware(
            page,
            layout_hint=hint,
            header_height=header_height,
            footer_height=footer_height,
        )
        pages.append({
            "text": text,
            "metadata": {"page_number": page_number},
            "_column_aware": True,
        })
    return pages

def _native_markdown_fallback(doc) -> list[dict]:
    """Fallback duro: reconstruye desde texto nativo de PyMuPDF."""
    pages = []
    for page_number, page in enumerate(doc, start=1):
        blocks = []
        raw_blocks = page.get_text("blocks", sort=True)
        for block in raw_blocks:
            if len(block) < 5:
                continue
            x0, y0, x1, y1, text = block[:5]
            text = str(text or "").strip()
            if not text:
                continue
            if re.fullmatch(r"(?:page\s*)?\d{1,4}", text, re.IGNORECASE):
                continue
            blocks.append((float(x0), float(y0), float(x1), float(y1), text))

        if blocks:
            page_width = page.rect.width
            center = page_width / 2.0
            left = [b for b in blocks if (b[0] + b[2]) / 2.0 < center]
            right = [b for b in blocks if (b[0] + b[2]) / 2.0 >= center]
            if len(left) >= 3 and len(right) >= 3:
                ordered = (
                    sorted(left, key=lambda b: (b[1], b[0]))
                    + sorted(right, key=lambda b: (b[1], b[0]))
                )
            else:
                ordered = sorted(blocks, key=lambda b: (b[1], b[0]))
        else:
            ordered = []

        text_parts = []
        for _, _, _, _, text in ordered:
            cleaned = re.sub(r"[ \t]+", " ", text)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
            if cleaned:
                text_parts.append(cleaned)

        pages.append({
            "text": "\n\n".join(text_parts),
            "metadata": {"page_number": page_number},
            "_native_fallback": True,
        })
    return pages


def extract_markdown_and_images(doc, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    pages = pymupdf4llm.to_markdown(
        doc,
        page_chunks=True,
        write_images=True,
        image_path=str(output_dir)
    )

    if isinstance(pages, str):
        pages = [{"text": pages, "metadata": {"page_number": 1}}]

    native_fallback_pages = _native_markdown_fallback(doc)

    corrected_pages = []
    extracted_chars = 0
    native_chars = 0
    page_fallbacks = 0

    for page_number, page in enumerate(pages, start=1):
        doc_index = page_number - 1
        if doc_index >= len(doc):
            corrected_pages.append(
                page if isinstance(page, dict) else {"text": str(page)}
            )
            continue

        page_width = doc[doc_index].rect.width
        native_page_text = re.sub(
            r"\s+", " ", doc[doc_index].get_text("text")
        ).strip()
        native_page_chars = len(native_page_text)

        normalized = normalize_page_chunk_layout(
            page,
            page_width=page_width,
            page_number=page_number,
        )
        page_text = str(normalized.get("text", ""))
        page_signal = _markdown_text_signal(page_text)

        print(
            f"[EXTRACT] Página {page_number}: "
            f"nativo={native_page_chars} chars | "
            f"pymupdf4llm={page_signal} chars"
        )

        if (
            native_page_chars >= 200
            and page_signal < max(100, int(native_page_chars * 0.20))
        ):
            fallback = (
                native_fallback_pages[doc_index]
                if doc_index < len(native_fallback_pages)
                else None
            )
            if fallback is not None:
                fallback_text = str(fallback.get("text", "") or "")
                fallback_signal = _markdown_text_signal(fallback_text)
                if fallback_signal > page_signal:
                    print(
                        f"[EXTRACT] Página {page_number}: FALLBACK NATIVO "
                        f"({page_signal} → {fallback_signal} chars)."
                    )
                    normalized = {
                        "text": fallback_text,
                        "metadata": {"page_number": page_number},
                        "_native_fallback": True,
                    }
                    page_text = fallback_text
                    page_signal = fallback_signal
                    page_fallbacks += 1

        corrected_pages.append(normalized)
        extracted_chars += page_signal
        native_chars += native_page_chars

    if native_chars >= 500 and extracted_chars < max(500, int(native_chars * 0.20)):
        print(
            f"[EXTRACT] FALLBACK GLOBAL: {extracted_chars} chars extraídos vs "
            f"{native_chars} nativos. Reconstruyendo desde PDF."
        )
        return native_fallback_pages

    if page_fallbacks:
        log(f"[EXTRACT] {page_fallbacks} páginas reemplazadas por texto nativo.")

    return corrected_pages


def add_page_markers(markdown_pages):
    if isinstance(markdown_pages, str):
        return markdown_pages
    output = []
    for page_number, page in enumerate(markdown_pages, start=1):
        if isinstance(page, dict):
            text = page.get("text", "")
        else:
            text = str(page)
        output.append(f"<!-- PAGE:{page_number} -->")
        output.append(text)
    return "\n\n".join(output)

# ============================================================
# TOKENIZACIÓN DE MARKERS PARA TRADUCCIÓN
# ============================================================

# Token que DeepSeek no tiene razón de traducir ni reformatear.
_PAGE_TOKEN_PATTERN = re.compile(r"@@PAGE_(\d+)@@")
_PAGE_MARKER_PATTERN = re.compile(
    r"<!--\s*PAGE\s*:?\s*(\d+)\s*-->",
    re.IGNORECASE,
)


def hide_page_markers_for_translation(markdown: str) -> str:
    """
    Reemplaza `<!-- PAGE:N -->` por `@@PAGE_N@@` antes de enviar a DeepSeek.
    El token es opaco, DeepSeek no tiene razón de traducirlo ni reformatearlo.
    """
    if not markdown:
        return markdown
    return _PAGE_MARKER_PATTERN.sub(
        lambda m: f"@@PAGE_{m.group(1)}@@",
        markdown,
    )


def restore_page_markers_after_translation(markdown: str) -> str:
    """
    Revierte `@@PAGE_N@@` a `<!-- PAGE:N -->`.

    Tolera variantes que DeepSeek pueda haber introducido:
    - `@@ PAGE_N @@` (con espacios)
    - `@@PAGE N@@` (sin guión bajo)
    - `@@page_n@@` (minúsculas)
    """
    if not markdown:
        return markdown

    def _to_marker(match):
        return f"\n\n<!-- PAGE:{match.group(1)} -->\n\n"

    # Variantes toleradas.
    markdown = re.sub(
        r"[ \t]*@@\s*PAGE[_ ]?(\d+)\s*@@[ \t]*",
        _to_marker,
        markdown,
        flags=re.IGNORECASE,
    )
    return markdown

# ============================================================
# LIMPIEZA DEL MARKDOWN
# ============================================================

def remove_obvious_editorial_noise(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue
        if re.fullmatch(r"<!--\s*PAGE:\d+\s*-->", stripped, re.IGNORECASE):
            cleaned.append(line)
            continue
        if is_page_number(stripped):
            continue
        if is_editorial_artifact_line(stripped):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def remove_residual_editorial_lines(text: str, profile) -> str:
    if not text:
        return ""
    repeated = set(profile.repeated_headers + profile.repeated_footers)
    if not repeated:
        return text

    cleaned = []
    page_lines = []

    def flush_page(lines):
        if not lines:
            return []
        result = []
        non_empty_indices = [i for i, line in enumerate(lines) if line.strip()]
        first_indices = set(non_empty_indices[:3])
        last_indices = set(non_empty_indices[-3:])
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                result.append(line)
                continue
            if i in first_indices or i in last_indices:
                normalized = normalize_whitespace(stripped)
                if any(normalized == value for value in repeated):
                    continue
                if is_page_number(stripped):
                    continue
            result.append(line)
        return result

    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"<!--\s*PAGE:\d+\s*-->", stripped, re.IGNORECASE):
            cleaned.extend(flush_page(page_lines))
            page_lines = []
            cleaned.append(line)
            continue
        page_lines.append(line)

    cleaned.extend(flush_page(page_lines))
    return "\n".join(cleaned)


def preprocess_raw_markdown(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def is_affiliation_or_meta(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    patterns = [
        r"\breceived\b", r"\baccepted\b", r"\bsubmitted\b", r"\bdoi\b",
        r"\bcorresponding author\b", r"\bauthor information\b", r"\baffiliation\b",
    ]
    return any(re.search(p, s, re.IGNORECASE) for p in patterns) or looks_like_email(s)


def clean_and_join_broken_paragraphs(
    text: str,
    visual_hints: Optional[dict] = None,
) -> str:
    """
    Reconstrucción moderada de líneas partidas.

    Une guiones de palabra partida dentro de una misma línea, y (si Vision
    lo confirma) une párrafos entre páginas. Nunca une líneas que empiezan
    con #, listas, tablas, blockquotes o imágenes.
    """

    lines = text.splitlines()
    output = []

    for i, line in enumerate(lines):

        current = line.rstrip()

        if not current.strip():
            output.append("")
            continue

        stripped = current.strip()

        if stripped.startswith("<!-- PAGE:"):
            output.append(current)
            continue

        if stripped.startswith("#"):
            output.append(current)
            continue

        if stripped.startswith(("-", "*", ">", "|", "<")):
            output.append(current)
            continue

        # ----------------------------------------------------
        # guión de palabra partido
        # ----------------------------------------------------
        if current.rstrip().endswith("-"):
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and re.match(r"^[a-záéíóúñü]", next_line, re.IGNORECASE):
                    current = current.rstrip()[:-1] + next_line
                    lines[i + 1] = ""

        output.append(current)

    cleaned = "\n".join(output)

    # ----------------------------------------------------
    # Unión inter-página asistida por Vision
    # ----------------------------------------------------
    if visual_hints:
        lines = cleaned.splitlines()
        i = 0
        while i < len(lines):
            marker = re.fullmatch(r"\s*<!-- PAGE:(\d+) -->\s*", lines[i])
            if not marker:
                i += 1
                continue

            page_number = int(marker.group(1))
            hint = visual_hints.get(page_number)
            if not hint or not hint.get("continues_paragraph"):
                i += 1
                continue

            prev = i - 1
            while prev >= 0 and not lines[prev].strip():
                prev -= 1
            nxt = i + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1

            if prev < 0 or nxt >= len(lines):
                i += 1
                continue

            left = lines[prev].rstrip()
            right = lines[nxt].lstrip()
            protected = ("|", ">", "```", "#", "- ", "* ", "![](")
            if (
                not left
                or not right
                or left.startswith(protected)
                or right.startswith(protected)
                or left.endswith((".", ":", ";", "?", "!"))
                or re.match(r"^[A-ZÁÉÍÓÚÑÜ]", right)
            ):
                i += 1
                continue

            # FIX: el marcador NUNCA se mete dentro de la línea del texto.
            # El párrafo unido va a lines[prev]; el marcador queda intacto
            # en su propia línea (lines[i]); el contenido de lines[nxt] se
            # vacía porque ya está en lines[prev].
            if left.endswith("-") and re.match(r"^[a-záéíóúñü]", right, re.I):
                lines[prev] = left[:-1] + right
            else:
                lines[prev] = left + " " + right
            lines[nxt] = ""
            i = nxt + 1

        cleaned = "\n".join(lines)

    return cleaned


def optimize_markdown_for_mobile(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\r\n", "\n")

    # ----------------------------------------------
    # espacios excesivos
    # ----------------------------------------------
    text = re.sub(r"[ \t]+\n", "\n", text)

    # ----------------------------------------------
    # no más de 2 líneas vacías consecutivas
    # ----------------------------------------------
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    # ----------------------------------------------
    # headings
    #
    # FIX: solo al inicio de línea y exigiendo espacio tras los '#'.
    # Evita romper celdas de tabla que empiezan con '#' (ej: "#followers").
    # ----------------------------------------------
    text = re.sub(
        r"\n*(^#{1,6}\s+[^\n]+$)\n*",
        r"\n\n\1\n\n",
        text,
        flags=re.MULTILINE,
    )

    # ----------------------------------------------
    # page markers
    # ----------------------------------------------
    text = re.sub(
        r"\n*(<!-- PAGE:\d+ -->)\n*",
        r"\n\n\1\n\n",
        text,
    )

    # ----------------------------------------------
    # anclas HTML
    # ----------------------------------------------
    text = re.sub(
        r'\n{3,}(<a\s+id="[^"]+"\s*>)',
        r"\n\n\1",
        text,
        flags=re.IGNORECASE,
    )

    # ----------------------------------------------
    # tablas
    # ----------------------------------------------
    text = re.sub(r"\n{3,}(\|)", "\n\n\\1", text)
    text = re.sub(r"(\|[^\n]+)\n{3,}", "\\1\n\n", text)

    return text.strip()

def postprocess_markdown(text: str) -> str:
    return optimize_markdown_for_mobile(text)

def postprocess_markdown(text: str) -> str:
    return optimize_markdown_for_mobile(text)


# ============================================================
# NORMALIZACIÓN DE NOTAS AL PIE
# ============================================================

def normalize_footnote_formatting(markdown: str) -> str:
    """
    Fuerza un formato uniforme para notas al pie: blockquote con número
    en negrita.

    NO toca:
      - referencias bibliográficas (detectadas por heading o por tener año)
      - líneas ya indexadas como referencias (**N.** texto)
      - headings, tablas, listas, blockquotes, imágenes
    """
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    output = []
    in_references = False

    for line in lines:
        stripped = line.strip()

        # Detección robusta de inicio de referencias
        if not in_references and _is_references_heading(stripped):
            in_references = True
            output.append(line)
            continue

        if in_references:
            output.append(line)
            continue

        # Línea ya formateada como referencia indexada → no tocar
        if re.match(r"^\*\*\d+\.\*\*", stripped):
            output.append(line)
            continue

        # Línea con año de 4 dígitos → probablemente referencia, no tocar
        if _looks_like_reference_entry(stripped):
            output.append(line)
            continue

        # Nota al pie: número + espacio + texto corto
        m = re.match(r"^(\d{1,3})[.)]?\s+(.+)$", stripped)
        if (
            m
            and len(stripped) < 300
            and not stripped.startswith(("|", "#", ">", "- ", "* ", "!["))
        ):
            number = m.group(1)
            body = m.group(2).strip()
            output.append(f"> **{number}.** {body}")
            continue

        output.append(line)

    return "\n".join(output)


# ============================================================
# NOTAS DE CORRESPONDENCIA
# ============================================================

CORRESPONDENCE_PATTERNS = [
    r"correspondence\s+(?:should\s+be\s+)?(?:addressed|directed|sent)\s+to",
    r"la\s+correspondencia\s+debe\s+dirigirse\s+a",
    r"corresponding\s+author",
    r"autor\s+correspondiente",
    r"please\s+address\s+correspondence",
    r"address\s+correspondence\s+to",
]

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _looks_like_correspondence_note(text: str) -> bool:
    if not text or len(text) > 600:
        return False
    low = text.lower()
    if any(re.search(p, low, re.IGNORECASE) for p in CORRESPONDENCE_PATTERNS):
        return True
    if EMAIL_RE.search(text):
        if re.search(
            r"\b(?:department|university|institute|college|"
            r"departamento|universidad|instituto|facultad)\b",
            text, re.IGNORECASE,
        ):
            return True
    return False

# ============================================================
# HELPERS DE REFERENCIAS / NOTAS
# ============================================================

def _is_references_heading(text: str) -> bool:
    """Detecta cualquier variante de encabezado de referencias."""
    if not text:
        return False
    # Quitar markdown de heading/bold/italic
    clean = re.sub(r"^\s*[#*_>\s]+", "", text)
    clean = re.sub(r"[#*_\s:]+$", "", clean)
    clean = clean.strip().lower()
    return clean in {
        "references", "referencias", "bibliography", "bibliografía",
        "bibliografia", "reference list", "literature cited",
        "referencias bibliográficas",
    }


def _looks_like_reference_entry(text: str) -> bool:
    """
    Heurística: una referencia típica tiene un año de 4 dígitos y
    formato autor-año. Las notas al pie casi nunca contienen un año.
    """
    if not text or len(text) > 800:
        return False
    s = text.strip().lstrip("> ").strip()
    # Contiene año → muy probablemente referencia
    return bool(re.search(r"\b(?:19|20)\d{2}\b", s))


def strip_page_markers_from_references(markdown: str) -> str:
    """
    Quita todos los `<!-- PAGE:N -->` que caigan dentro de la sección de
    referencias. Los markers ahí solo fragmentan entradas y rompen el
    indexado. La navegación por página no se pierde porque ya hay markers
    en todo el resto del documento.
    """
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    ref_start = None

    for i, line in enumerate(lines):
        if _is_references_heading(line):
            ref_start = i
            break

    if ref_start is None:
        return markdown

    marker_re = re.compile(r"^\s*<!--\s*PAGE:\d+\s*-->\s*$")
    before = lines[:ref_start]
    after = [ln for ln in lines[ref_start:] if not marker_re.match(ln)]

    return "\n".join(before + after)

def extract_correspondence_notes(markdown: str) -> tuple[str, list[str]]:
    """
    Extrae notas de correspondencia del cuerpo del Markdown. Se devuelven
    por separado para reinsertarlas al final del documento, evitando que
    partan un párrafo científico en dos.
    """
    if not markdown:
        return markdown, []

    lines = markdown.splitlines()
    output = []
    notes = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        if _looks_like_correspondence_note(stripped):
            note_lines = [stripped]
            j = i + 1

            while j < len(lines) and lines[j].strip():
                candidate = lines[j].strip()

                if candidate.startswith(("#", "|", "- ", "* ", ">", "<!--", "![")):
                    break

                joined = " ".join(note_lines)
                if EMAIL_RE.search(joined) and re.search(r"\b\d{4,6}\b", joined):
                    break

                if (
                    len(note_lines) >= 2
                    and re.match(r"^[A-ZÁÉÍÓÚÑ][a-záéíóúñü]{3,}\s+\w+", candidate)
                ):
                    break

                note_lines.append(candidate)
                j += 1

            notes.append(" ".join(note_lines))

            while j < len(lines) and not lines[j].strip():
                j += 1

            i = j
            continue

        output.append(lines[i])
        i += 1

    return "\n".join(output), notes


def merge_split_paragraphs(markdown: str) -> str:
    """
    Une párrafos partidos por una línea vacía cuando la segunda mitad
    empieza con minúscula y la primera no termina en puntuación final.
    """
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    output = []
    i = 0
    protected_starts = ("#", "|", "- ", "* ", ">", "<!--", "![", "1.", "2.", "3.")

    while i < len(lines):
        current = lines[i]
        output.append(current)

        if (
            i + 2 < len(lines)
            and current.strip()
            and not lines[i + 1].strip()
            and lines[i + 2].strip()
        ):
            curr_s = current.strip()
            next_s = lines[i + 2].strip()

            if (
                not curr_s.startswith(protected_starts)
                and not next_s.startswith(protected_starts)
                and not curr_s.endswith((".", "!", "?", ":", ";", ")", "»", '"', "”"))
                and re.match(r"^[a-záéíóúñü]", next_s)
            ):
                output.pop()
                output.append(curr_s + " " + next_s)
                i += 3
                continue

        i += 1

    return "\n".join(output)

# ============================================================
# APLICACIÓN DE SEÑALES DE VISION AL MARKDOWN
# ============================================================

def _find_prev_nonblank(lines, start_idx):
    i = start_idx
    while i >= 0 and not lines[i].strip():
        i -= 1
    return i


def _find_next_nonblank(lines, start_idx):
    i = start_idx
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def merge_tables_across_pages(markdown: str, visual_hints: dict) -> str:
    """
    Une tablas Markdown que Vision detectó como continuadas entre páginas.

    Solo actúa cuando:
    - El hint `continues_table=True` con confianza >= 0.80
    - La última línea no vacía antes del marker es fila de tabla (| ... |)
    - La primera línea no vacía después del marker es fila de tabla (| ... |)
    - El número de pipes coincide entre ambas filas

    Efecto: elimina el marker de página y las líneas vacías intermedias
    para que ambas partes queden contiguas. Los marcadores intermedios
    se pierden (trade-off aceptable: preferimos tabla unida a navegación
    exacta en mitad de una tabla).
    """
    if not markdown or not visual_hints:
        return markdown

    lines = markdown.splitlines()
    output = []
    i = 0
    merged = 0

    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)

        if not marker:
            output.append(line)
            i += 1
            continue

        page_number = int(marker.group(1))
        hint = visual_hints.get(page_number)

        if (
            not hint
            or not hint.get("continues_table")
            or hint.get("confidence", 0) < 0.80
        ):
            output.append(line)
            i += 1
            continue

        prev_idx = _find_prev_nonblank(output, len(output) - 1)
        next_idx = _find_next_nonblank(lines, i + 1)

        if prev_idx < 0 or next_idx >= len(lines):
            output.append(line)
            i += 1
            continue

        prev_line = output[prev_idx].strip()
        next_line = lines[next_idx].strip()

        if not (is_markdown_table_row(prev_line) and is_markdown_table_row(next_line)):
            output.append(line)
            i += 1
            continue

        if prev_line.count("|") != next_line.count("|"):
            print(
                f"[VISION MERGE] Tabla NO unida pág {page_number}→{page_number + 1}: "
                f"pipes distintos ({prev_line.count('|')} vs {next_line.count('|')})"
            )
            output.append(line)
            i += 1
            continue

        # Eliminar blank lines al final de output para pegar la próxima fila.
        while output and not output[-1].strip():
            output.pop()

        print(
            f"[VISION MERGE] Tabla unida pág {page_number}→{page_number + 1} "
            f"(conf {hint.get('confidence', 0):.2f})"
        )
        # El marker se descarta. Saltamos hasta next_idx (exclusive) y la
        # próxima iteración procesará la fila de tabla.
        i = next_idx
        merged += 1
        continue

    if merged:
        log(f"[VISION MERGE] Total tablas unidas: {merged}")

    return "\n".join(output)


def merge_lists_across_pages(markdown: str, visual_hints: dict) -> str:
    """
    Une listas Markdown que Vision detectó como continuadas entre páginas.

    Mismo enfoque que tablas, pero verificando items de lista:
    `- `, `* `, `+ `, o `N. `.
    """
    if not markdown or not visual_hints:
        return markdown

    lines = markdown.splitlines()
    output = []
    i = 0
    merged = 0

    def is_list_item(text: str) -> bool:
        return bool(re.match(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+\S", text))

    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)

        if not marker:
            output.append(line)
            i += 1
            continue

        page_number = int(marker.group(1))
        hint = visual_hints.get(page_number)

        if (
            not hint
            or not hint.get("continues_list")
            or hint.get("confidence", 0) < 0.80
        ):
            output.append(line)
            i += 1
            continue

        prev_idx = _find_prev_nonblank(output, len(output) - 1)
        next_idx = _find_next_nonblank(lines, i + 1)

        if prev_idx < 0 or next_idx >= len(lines):
            output.append(line)
            i += 1
            continue

        prev_line = output[prev_idx].strip()
        next_line = lines[next_idx].strip()

        if not (is_list_item(prev_line) and is_list_item(next_line)):
            output.append(line)
            i += 1
            continue

        while output and not output[-1].strip():
            output.pop()

        print(
            f"[VISION MERGE] Lista unida pág {page_number}→{page_number + 1} "
            f"(conf {hint.get('confidence', 0):.2f})"
        )
        i = next_idx
        merged += 1
        continue

    if merged:
        log(f"[VISION MERGE] Total listas unidas: {merged}")

    return "\n".join(output)


def merge_interrupted_citations(markdown: str, visual_hints: dict) -> str:
    """
    Une citas bibliográficas cortadas entre páginas.

    Ejemplos:
      "(Smith, 20" + "20)" → "(Smith, 2020)"
      "[12, 1" + "3]" → "[12, 13]"
    """
    if not markdown or not visual_hints:
        return markdown

    lines = markdown.splitlines()
    output = []
    i = 0
    merged = 0

    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)

        if not marker:
            output.append(line)
            i += 1
            continue

        page_number = int(marker.group(1))
        hint = visual_hints.get(page_number)

        if (
            not hint
            or not hint.get("citation_interrupted")
            or hint.get("confidence", 0) < 0.80
        ):
            output.append(line)
            i += 1
            continue

        prev_idx = _find_prev_nonblank(output, len(output) - 1)
        next_idx = _find_next_nonblank(lines, i + 1)

        if prev_idx < 0 or next_idx >= len(lines):
            output.append(line)
            i += 1
            continue

        prev_line = output[prev_idx].rstrip()
        next_line = lines[next_idx].lstrip()

        # ¿La línea previa deja algo abierto?
        prev_interrupted = (
            re.search(r"\(\s*[^)]*$", prev_line)     # paréntesis abierto
            or re.search(r"\[\s*[^\]]*$", prev_line)  # corchete abierto
            or re.search(r"\b(?:19|20)\d{0,3}$", prev_line)  # año incompleto
        )

        # ¿La línea siguiente cierra algo?
        next_continuation = (
            re.match(r"^[\w\s,;.\-–—]+\s*\)", next_line)  # cierra paréntesis
            or re.match(r"^[\w\s,;.\-–—]+\s*\]", next_line)  # cierra corchete
            or re.match(r"^\d{1,4}\b", next_line)  # completa año
        )

        if not (prev_interrupted and next_continuation):
            output.append(line)
            i += 1
            continue

        # Elegir joiner: sin espacio si estamos completando números,
        # con espacio si estamos uniendo texto.
        if (
            re.search(r"\b(?:19|20)\d{0,3}$", prev_line)
            and re.match(r"^\d{1,4}\b", next_line)
        ):
            joiner = ""
        else:
            joiner = " "

        while output and not output[-1].strip():
            output.pop()

        output[prev_idx] = prev_line + joiner + next_line

        print(
            f"[VISION MERGE] Cita unida pág {page_number}→{page_number + 1} "
            f"(conf {hint.get('confidence', 0):.2f}): "
            f"{prev_line[-30:]!r} + {next_line[:30]!r}"
        )

        i = next_idx + 1
        merged += 1
        continue

    if merged:
        log(f"[VISION MERGE] Total citas unidas: {merged}")

    return "\n".join(output)

# ============================================================
# MERGE GUIADO POR FRAGMENTOS DE VISION
# ============================================================

def _normalize_for_match(text: str) -> str:
    """Normaliza texto para comparación fuzzy de fragmentos."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[«»""''`´]", '"', t)
    t = re.sub(r"[–—]", "-", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s\-áéíóúñü.,;:!?()\[\]\"'-]", "", t)
    return t.strip()


def _find_fragment_position(
    markdown: str,
    fragment: str,
    search_start: int = 0,
    search_end: Optional[int] = None,
) -> Optional[tuple[int, int]]:
    """
    Busca un fragmento en el markdown con tolerancia a espacios/saltos.

    Devuelve (start, end) en el markdown original o None si no lo encuentra.
    """
    if not fragment or not markdown:
        return None

    norm_fragment = _normalize_for_match(fragment)
    if len(norm_fragment) < 5:
        return None

    end_limit = search_end if search_end is not None else len(markdown)
    region = markdown[search_start:end_limit]

    # Estrategia 1: buscar las últimas N palabras del fragmento.
    # Es más robusto que el fragmento completo porque la parte más cercana
    # a la frontera es la que Vision ve mejor.
    words = norm_fragment.split()
    if len(words) >= 4:
        for take in (6, 5, 4):
            if len(words) < take:
                continue
            tail_words = words[-take:]
            pattern = r"\s+".join(re.escape(w) for w in tail_words)
            # Permitir cualquier whitespace o markdown entre palabras.
            loose_pattern = pattern.replace(r"\ ", r"[\s\*_`]+")
            try:
                regex = re.compile(loose_pattern, re.IGNORECASE)
            except re.error:
                continue
            match = regex.search(region)
            if match:
                # Convertir coordenadas relativas a absolutas.
                local_start, local_end = match.span()
                # Expandir para cubrir el fragmento completo desde el inicio
                # de la última porción si es posible.
                return (search_start + local_start, search_start + local_end)

    return None


def apply_vision_fragment_merges(markdown: str, visual_hints: dict) -> str:
    """
    Usa los fragmentos `left_tail` y `right_head` de Vision para unir
    texto cortado entre páginas con altísima precisión.

    Estrategia:
    1. Para cada frontera con boundary_quality="broken" o "suspicious",
       busca `left_tail` (últimas palabras del final de N) y `right_head`
       (primeras palabras del inicio de N+1) en el markdown.
    2. Si ambos se encuentran cerca de un `<!-- PAGE:N+1 -->`, une
       exactamente entre el final del left_tail y el inicio del right_head.
    """
    if not markdown or not visual_hints:
        return markdown

    marker_re = re.compile(r"<!--\s*PAGE\s*:?\s*(\d+)\s*-->", re.IGNORECASE)
    all_markers = list(marker_re.finditer(markdown))

    if not all_markers:
        return markdown

    # Procesar de atrás hacia adelante para no invalidar posiciones.
    applied = 0
    for hint_page in sorted(visual_hints.keys(), reverse=True):
        hint = visual_hints.get(hint_page)
        if not hint:
            continue

        quality = hint.get("boundary_quality", "")
        if quality not in {"broken", "suspicious"}:
            continue

        left_tail = hint.get("left_tail") or ""
        right_head = hint.get("right_head") or ""

        # Necesitamos al menos un lado con texto para localizar.
        if not left_tail and not right_head:
            continue

        # Encontrar el marker de la página N+1 (= hint_page + 1).
        target_page = hint_page + 1
        marker_match = None
        for m in all_markers:
            if int(m.group(1)) == target_page:
                marker_match = m
                break

        if marker_match is None:
            continue

        marker_start, marker_end = marker_match.span()

        # Buscar left_tail ANTES del marker.
        left_pos = _find_fragment_position(
            markdown, left_tail, 0, marker_start
        )

        # Buscar right_head DESPUÉS del marker.
        right_pos = _find_fragment_position(
            markdown, right_head, marker_end, len(markdown)
        )

        if left_pos is None and right_pos is None:
            continue

        # Aplicar merge.
        if left_pos and right_pos:
            # Caso ideal: ambos lados localizados. Eliminamos TODO entre
            # el final del left_tail y el inicio del right_head, y pegamos
            # con un espacio (o nada si parece palabra cortada).
            left_end = left_pos[1]
            right_start = right_pos[0]

            left_text = markdown[left_pos[0]:left_end]
            right_text = markdown[right_start:right_pos[1]]

            # Detectar si hay que unir sin espacio (palabra cortada).
            joiner = " "
            if left_text.rstrip().endswith("-") and re.match(
                r"^[a-záéíóúñü]", right_text.lstrip(), re.I
            ):
                # Eliminar el guión.
                left_text = left_text.rstrip()[:-1]
                joiner = ""
            elif re.match(r"^\d", right_text.lstrip()) and re.search(
                r"\b(?:19|20)\d{0,3}$", left_text.rstrip()
            ):
                # Año cortado: sin espacio.
                joiner = ""

            merged = left_text.rstrip() + joiner + right_text.lstrip()
            new_markdown = (
                markdown[:left_pos[0]]
                + merged
                + markdown[right_pos[1]:]
            )

            print(
                f"[VISION FRAGMENT] Pág {hint_page}→{target_page} "
                f"({quality}, conf {hint.get('confidence', 0):.2f}): "
                f"merge con fragmentos exactos "
                f"({len(left_text)} + {len(right_text)} chars)"
            )
            markdown = new_markdown
            applied += 1

        elif left_pos:
            # Solo left_tail localizado: eliminar el marker y pegar lo que
            # sigue al final del left_tail. Es un merge "a ciegas" del lado
            # derecho, pero con evidencia del izquierdo.
            left_end = left_pos[1]
            # Encontrar el siguiente bloque de texto no vacío tras el marker.
            tail = markdown[marker_end:].lstrip()
            if tail:
                # Cortar hasta el primer cierre de párrafo.
                next_break = re.search(r"\n\s*\n|<!--\s*PAGE", tail)
                if next_break:
                    tail_fragment = tail[:next_break.start()]
                else:
                    tail_fragment = tail[:400]

                left_text = markdown[left_pos[0]:left_end]
                joiner = " "
                if left_text.rstrip().endswith("-"):
                    left_text = left_text.rstrip()[:-1]
                    joiner = ""

                merged = left_text.rstrip() + joiner + tail_fragment.lstrip()
                new_markdown = (
                    markdown[:left_pos[0]]
                    + merged
                    + markdown[marker_end + len(tail_fragment):]
                )
                print(
                    f"[VISION FRAGMENT] Pág {hint_page}→{target_page} "
                    f"({quality}): merge solo con left_tail"
                )
                markdown = new_markdown
                applied += 1

        elif right_pos:
            # Solo right_head localizado: eliminar el marker y pegar lo que
            # precede al inicio del right_head. Más riesgoso, lo aplicamos
            # solo cuando boundary_quality == "broken".
            if quality != "broken":
                continue

            right_start = right_pos[0]
            # Tomar las últimas 200 chars antes del marker.
            before = markdown[:marker_start].rstrip()
            fragment = before[-200:]
            right_text = markdown[right_start:right_pos[1]]

            joiner = " "
            if fragment.rstrip().endswith("-"):
                fragment = fragment.rstrip()[:-1]
                joiner = ""

            merged = fragment + joiner + right_text.lstrip()
            new_markdown = (
                markdown[:marker_start - len(fragment)]
                + merged
                + markdown[right_pos[1]:]
            )
            print(
                f"[VISION FRAGMENT] Pág {hint_page}→{target_page} "
                f"({quality}): merge solo con right_head"
            )
            markdown = new_markdown
            applied += 1

    if applied:
        log(f"[VISION FRAGMENT] Total merges aplicados: {applied}")

    return markdown

# ============================================================
# DETECCIÓN DE IDIOMA
# ============================================================

def detect_language(text: str) -> str:
    sample = text[:8000].lower()
    spanish_markers = [" el ", " la ", " los ", " las ", " de ", " que ",
                       " para ", " una ", " un ", " y "]
    english_markers = [" the ", " of ", " and ", " that ", " for ",
                       " with ", " this ", " are ", " is "]
    spanish_score = sum(sample.count(x) for x in spanish_markers)
    english_score = sum(sample.count(x) for x in english_markers)
    if spanish_score > english_score * 1.2:
        return "Spanish"
    return "English"


# ============================================================
# TABLAS
# ============================================================

def is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return False
    return all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def is_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and stripped.endswith("|")
        and stripped.count("|") >= 2
    )

def repair_broken_tables(markdown: str) -> str:
    """
    Corrige tablas Markdown que DeepSeek dejó mal formadas:
    - `[Header|Col|...]` → `|Header|Col|...|`
    - Filas sin pipe inicial/final
    - Separadores con pocos guiones
    """
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    output = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Caso 1: línea que empieza con '[' y contiene varios '|'
        if (
            stripped.startswith("[")
            and stripped.count("|") >= 2
            and not stripped.startswith("[^")
            and not stripped.startswith("[!")
        ):
            # Quitar corchetes externos y reconstruir pipes
            inner = stripped
            if inner.startswith("["):
                inner = inner[1:]
            if inner.endswith("]"):
                inner = inner[:-1]
            # Si la primera "celda" no tiene pipe al inicio, agregarlo
            if not inner.startswith("|"):
                inner = "|" + inner
            if not inner.endswith("|"):
                inner = inner + "|"
            output.append(inner)
            i += 1
            continue

        # Caso 2: línea con muchos '|' pero sin empezar/terminar en '|'
        if (
            stripped.count("|") >= 3
            and not stripped.startswith("|")
            and not stripped.startswith("#")
            and not stripped.startswith("<!--")
            and not stripped.startswith("-")
            and not stripped.startswith("*")
            and "http" not in stripped
        ):
            rebuilt = stripped
            if not rebuilt.startswith("|"):
                rebuilt = "|" + rebuilt
            if not rebuilt.endswith("|"):
                rebuilt = rebuilt + "|"
            output.append(rebuilt)
            i += 1
            continue

        output.append(line)
        i += 1

    # Normalizar separadores: garantizar al menos 3 guiones por celda.
    def _fix_separator(match):
        cells = match.group(0).split("|")
        fixed = []
        for cell in cells:
            c = cell.strip()
            if c and set(c) <= {"-", ":", " "}:
                # Forzar 3 guiones mínimos conservando ':' de alineación
                left_colon = c.startswith(":")
                right_colon = c.endswith(":")
                core = "---"
                new_c = (":" if left_colon else "") + core + (":" if right_colon else "")
                fixed.append(new_c)
            else:
                fixed.append(c)
        return "|" + "|".join(fixed[1:-1]) + "|" if len(fixed) >= 3 else match.group(0)

    text = "\n".join(output)
    text = re.sub(
        r"^\|[\s\-:|]+\|$",
        _fix_separator,
        text,
        flags=re.MULTILINE,
    )
    return text

def isolate_tables(text: str):
    if not text:
        return text, {}

    tables = {}
    lines = text.splitlines()
    output = []
    counter = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        if (
            i + 1 < len(lines)
            and is_markdown_table_row(line)
            and is_markdown_table_separator(lines[i + 1])
        ):
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines):
                current = lines[i]
                if not is_markdown_table_row(current):
                    break
                table_lines.append(current)
                i += 1
            key = f"@@TABLE_{counter}@@"
            tables[key] = "\n".join(table_lines)
            output.append(key)
            counter += 1
            continue
        output.append(line)
        i += 1

    return "\n".join(output), tables


async def translate_single_table(table_md: str, model: Optional[str] = None) -> str:
    if not table_md.strip():
        return table_md

    instruction = (
        "TRADUCE ESTA TABLA ACADÉMICA AL ESPAÑOL.\n\n"
        "REGLAS OBLIGATORIAS:\n"
        "- Devuelve ÚNICAMENTE la tabla Markdown.\n"
        "- Mantén exactamente el mismo número de columnas y filas.\n"
        "- Mantén intacta la fila separadora Markdown.\n"
        "- Traduce encabezados, categorías y texto de las celdas.\n"
        "- NO traduzcas números, porcentajes, siglas, DSM-IV, nombres propios ni símbolos científicos.\n"
        "- NO agregues explicaciones, introducciones ni comentarios.\n\n"
        + table_md
    )

    translated = await translate_chunk(instruction, 0, 1)
    translated = translated.strip()
    translated = re.sub(
        r"^```(?:markdown)?\s*|\s*```$", "", translated, flags=re.IGNORECASE
    ).strip()

    table_lines = [
        line.strip()
        for line in translated.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]

    original_table_lines = [
        line.strip()
        for line in table_md.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]

    if len(table_lines) < 2:
        print(
            f"[TABLE] Respuesta inválida: no se detectaron filas. "
            f"Preview: {translated[:200]!r}"
        )
        return table_md

    # Validación relajada: aceptamos si la traducción tiene al menos
    # (filas_originales - 1) filas. DeepSeek suele omitir la fila vacía final.
    if len(table_lines) < len(original_table_lines) - 1:
        print(
            f"[TABLE] Traducción incompleta: "
            f"{len(table_lines)} filas vs {len(original_table_lines)} originales. "
            f"Se conserva la original."
        )
        return table_md

    sep_idx = next(
        (i for i, line in enumerate(table_lines) if is_markdown_table_separator(line)),
        None,
    )
    if sep_idx != 1:
        print(
            f"[TABLE] Separador en posición {sep_idx} en vez de 1. "
            f"Preview: {table_lines[:3]!r}"
        )
        return table_md

    header_pipes = table_lines[0].count("|")
    if not any(line.count("|") == header_pipes for line in original_table_lines):
        print(
            f"[TABLE] Header con {header_pipes} pipes, "
            f"originales: {[line.count('|') for line in original_table_lines[:3]]}. "
            f"Preview: {table_lines[0]!r}"
        )
        return table_md

    return "\n".join(table_lines)


async def translate_tables(tables: dict, model: Optional[str] = None) -> dict:
    if not tables:
        return tables

    items = list(tables.items())
    results = {}

    async def translate_one(position: int, key: str, table_md: str):
        print(f"[TABLE] Traduciendo {key} ({position + 1}/{len(items)})...")
        translated = await translate_single_table(table_md, model)
        return key, translated

    pairs = await asyncio.gather(
        *(translate_one(i, key, table_md) for i, (key, table_md) in enumerate(items)),
        return_exceptions=True,
    )

    for (key, original), result in zip(items, pairs):
        if isinstance(result, BaseException):
            print(f"[TABLE] Error en {key}; se conserva la tabla original: {result}")
            results[key] = original
        else:
            result_key, translated = result
            results[result_key] = translated

    return results


def restore_tables(text: str, tables: dict):
    if not tables:
        return text
    for key, value in tables.items():
        text = text.replace(key, "\n\n" + value + "\n\n")
    return text


# ============================================================
# CHUNKING (con filtro de chunks vacíos)
# ============================================================

def _chunk_has_real_text(chunk: str, min_signal: int = 80) -> bool:
    """Determina si un chunk tiene texto real (no solo markers/imágenes/tablas)."""
    return _markdown_text_signal(chunk) >= min_signal


def chunk_markdown(text: str, max_chars: int = MAX_CHARS_PER_CHUNK):
    if not text:
        return []

    if len(text) <= max_chars:
        return [text] if _chunk_has_real_text(text) else []

    sections = re.split(r"(\n\s*\n)", text)
    chunks = []
    current = ""

    for section in sections:
        if len(current) + len(section) <= max_chars:
            current += section
            continue

        if current.strip():
            chunks.append(current.strip())

        if len(section) > max_chars:
            start = 0
            while start < len(section):
                end = start + max_chars
                chunks.append(section[start:end])
                start = end
            current = ""
        else:
            current = section

    if current.strip():
        chunks.append(current.strip())

    # ------------------------------------------------
    # Filtro crítico: nunca enviar a DeepSeek un chunk
    # que solo contiene markers de página e imágenes.
    # ------------------------------------------------
    filtered = []
    dropped = 0
    for idx, chunk in enumerate(chunks):
        if _chunk_has_real_text(chunk):
            filtered.append(chunk)
        else:
            dropped += 1
            print(
                f"[CHUNK] Chunk {idx + 1}/{len(chunks)} descartado: "
                f"sin texto real ({len(chunk)} chars brutos, "
                f"{_markdown_text_signal(chunk)} chars reales)."
            )

    if dropped:
        log(f"[CHUNK] {dropped} chunks vacíos descartados.")

    return filtered


# ============================================================
# PROMPT DE TRADUCCIÓN
# ============================================================

TRANSLATION_SYSTEM_PROMPT = r"""
Eres el traductor académico principal de PsiHub Reader.

Tu tarea es traducir un documento académico completo al español.

REGLA FUNDAMENTAL:
TRADUCE TODO EL CONTENIDO. NO RESUMAS. NO OMITAS. NO SIMPLIFIQUES.

Debes preservar:
- título
- subtítulos
- autores
- afiliaciones
- abstract/resumen
- palabras clave
- cuerpo del artículo
- citas
- referencias
- notas
- tablas
- captions
- DOI
- URLs
- nombres propios
- nombres de instituciones
- fórmulas
- símbolos
- números
- estadísticas
- terminología científica

PRINCIPIOS DE TRADUCCIÓN:
1. Fidelidad semántica máxima.
2. Español académico natural.
3. No hacer traducción palabra por palabra cuando produzca un calco antinatural.
4. No introducir información que no esté en el original.
5. No interpretar los resultados del artículo.
6. No explicar conceptos por cuenta propia.
7. No resumir.
8. No eliminar repeticiones legítimas del autor.
9. Mantener la distinción conceptual entre términos.
10. Utilizar una terminología consistente durante todo el documento.

MARKDOWN:
Debes conservar la estructura Markdown.
Conservar: headings, listas, tablas, links, imágenes, HTML, page markers.

Los marcadores:
<!-- PAGE:N -->
son estructurales.

Los tokens `@@TABLE_N@@` son marcadores internos de tablas.
NO los traduzcas, NO los reformatees y NO los elimines.

IMPORTANTE SOBRE LA PRIMERA PÁGINA:
Si el texto contiene título, autores o afiliaciones,
tradúcelos/conserva su contenido desde la primera palabra.

IMPORTANTE SOBRE REFERENCIAS:
Las referencias bibliográficas forman parte del documento.
No las resumas ni las elimines.
Conserva cada referencia como una unidad independiente.

IMPORTANTE SOBRE CITAS:
No cambies las citas bibliográficas del cuerpo.
Por ejemplo: (Smith, 2020), (Smith et al., 2020), [12], [12, 13]
deben conservar su contenido y formato esencial.

DOI Y URL:
Nunca inventes, modifiques ni traduzcas un DOI o URL.

TABLAS:
No conviertas una tabla en prosa.

RESULTADO:
Devuelve únicamente la traducción.
No agregues comentarios.
No digas "Aquí está la traducción".
"""


# ============================================================
# DEEPSEEK — TRADUCCIÓN ASÍNCRONA
# ============================================================

async def translate_with_deepseek(
    text: str,
    api_key: str,
    first_chunk: bool = False,
    max_retries: int = 3
) -> str:
    if not api_key:
        raise RuntimeError("No hay una API key de DeepSeek disponible.")

    if not _chunk_has_real_text(text, min_signal=40):
        raise RuntimeError(
            "Se intentó traducir un chunk sin texto real. "
            "Esto indica un bug en el filtrado previo."
        )

    user_prompt = ""
    if first_chunk:
        user_prompt += """
ESTE ES EL COMIENZO DEL DOCUMENTO.

Debes comenzar desde la primera palabra disponible.
Incluye título, autores y afiliaciones cuando estén presentes.

"""
    user_prompt += "TRADUCE EL SIGUIENTE CONTENIDO:\n\n" + text

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "stream": False,
        "thinking": {"type": "disabled"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(max_retries):
        started = time.perf_counter()
        try:
            response = await DEEPSEEK_HTTP_CLIENT.post(
                DEEPSEEK_BASE_URL, headers=headers, json=payload,
            )
            elapsed = time.perf_counter() - started
            log(f"[TRANSLATION] DeepSeek response: {elapsed:.2f}s")

            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"DeepSeek HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]

            if not content or not content.strip():
                raise RuntimeError("DeepSeek devolvió una respuesta vacía.")

            # ------------------------------------------------
            # Detectar negativas del modelo: casi siempre significa
            # que el chunk realmente no tenía texto traducible.
            # ------------------------------------------------
            if _is_deepseek_refusal(content):
                log(
                    f"[TRANSLATION] DeepSeek se negó a traducir. "
                    f"Preview chunk: {text[:200]!r}"
                )
                raise RuntimeError(
                    "DeepSeek respondió con una negativa "
                    "(no hay contenido textual para traducir)."
                )

            return content

        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    raise RuntimeError(
        f"Falló la traducción después de {max_retries} intentos: {last_error}"
    )


async def translate_chunk(
    text: str,
    index: int,
    total: int,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    key = api_key or DEEPSEEK_API_KEYS[index % len(DEEPSEEK_API_KEYS)]

    log(
        f"[TRANSLATION] DeepSeek chunk {index + 1}/{total} "
        f"(key {DEEPSEEK_API_KEYS.index(key) + 1})"
    )

    result = await translate_with_deepseek(
        text, api_key=key, first_chunk=(index == 0),
    )

    if TRANSLATION_DELAY > 0:
        await asyncio.sleep(TRANSLATION_DELAY)

    return result


async def _translation_worker(
    worker_id: int,
    queue: asyncio.Queue,
    results: list,
    total: int,
    api_key: str,
):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return

        index, chunk = item
        try:
            log(f"[TRANSLATION] Worker {worker_id} → chunk {index + 1}/{total}")
            results[index] = await translate_chunk(
                chunk, index, total, api_key=api_key,
            )
        except BaseException as exc:
            results[index] = exc
        finally:
            queue.task_done()


async def translate_markdown(markdown: str, model: Optional[str] = None):
    chunks = chunk_markdown(markdown)

    if not chunks:
        print(
            "[TRANSLATION] No hay chunks con texto real para traducir. "
            "Se devuelve el markdown original sin traducción."
        )
        return markdown

    if not DEEPSEEK_API_KEYS:
        raise RuntimeError(
            "No hay ninguna DEEPSEEK_API_KEY_1..._6 configurada en .env."
        )

    worker_count = min(
        TRANSLATION_CONCURRENCY,
        len(DEEPSEEK_API_KEYS),
        len(chunks),
    )

    log(f"[TRANSLATION] {len(chunks)} chunks | {worker_count} workers / API keys")

    queue = asyncio.Queue()
    results: list = [None] * len(chunks)

    for index, chunk in enumerate(chunks):
        await queue.put((index, chunk))

    workers = [
        asyncio.create_task(
            _translation_worker(
                worker_id=i + 1,
                queue=queue,
                results=results,
                total=len(chunks),
                api_key=DEEPSEEK_API_KEYS[i],
            )
        )
        for i in range(worker_count)
    ]

    await queue.join()
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers, return_exceptions=True)

    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise errors[0]

    translated_results = [r for r in results if isinstance(r, str)]
    if len(translated_results) != len(chunks):
        raise RuntimeError("La traducción terminó sin producir todos los chunks.")

    return "\n\n".join(translated_results)


# ============================================================
# IMÁGENES
# ============================================================

def convert_local_images_to_base64(markdown: str, image_dir: Path):
    if not image_dir.exists():
        return markdown

    image_extensions = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }

    for image_path in image_dir.rglob("*"):
        if not image_path.is_file():
            continue
        mime = image_extensions.get(image_path.suffix.lower())
        if not mime:
            continue
        try:
            data = image_path.read_bytes()
            encoded = base64.b64encode(data).decode("ascii")
            data_uri = f"data:{mime};base64,{encoded}"
            markdown = markdown.replace(str(image_path), data_uri)
            markdown = markdown.replace(image_path.name, data_uri)
        except Exception as e:
            log(f"[IMAGE] Error: {image_path}: {e}")

    return markdown


# ============================================================
# REFERENCIAS E ÍNDICE INTERNO
# ============================================================

REFERENCE_HEADINGS = re.compile(
    r"^\s{0,3}"
    r"(?:#{1,6}\s*)?"
    r"(references|bibliography|referencias|bibliografía|"
    r"reference list|literature cited)"
    r"\s*$",
    re.IGNORECASE
)


def find_references_start(lines) -> Optional[int]:
    """
    Encuentra la línea donde empiezan las referencias. Acepta variantes
    de heading (# ## ### ...) y también texto plano.
    """
    for index, line in enumerate(lines):
        if _is_references_heading(line):
            return index
    return None


def looks_like_reference_start(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(r"^(?:\[\d+\]|\d+[.)])\s+", s):
        return True
    if re.search(r"\b(?:19|20)\d{2}[a-z]?\s*\)", s[:300]):
        if re.match(r"^[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñü'’\-]+", s):
            return True
    return False


def split_reference_entries(lines):
    entries = []
    current = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                entries.append("\n".join(current).strip())
                current = []
            continue
        if current and looks_like_reference_start(stripped):
            entries.append("\n".join(current).strip())
            current = [stripped]
            continue
        current.append(stripped)
    if current:
        entries.append("\n".join(current).strip())
    return [entry for entry in entries if entry.strip()]


def number_references(markdown: str) -> str:
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    start = find_references_start(lines)
    if start is None:
        return markdown

    reference_lines = lines[start + 1:]
    references = split_reference_entries(reference_lines)
    if not references:
        return markdown

    numbered = []
    for index, reference in enumerate(references, start=1):
        reference = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s+", "", reference)
        numbered.append(f"[{index}] {reference}")

    output = []
    output.extend(lines[:start + 1])
    output.append("")
    output.extend(numbered)
    return "\n".join(output)


def add_document_top_anchor(markdown: str) -> str:
    if not markdown:
        return markdown
    if '<a id="top"></a>' in markdown:
        return markdown
    return '<a id="top"></a>\n\n' + markdown


def _source_pdf_url(document_id: str, page: Optional[int] = None) -> str:
    url = f"/api/source-pdf/{document_id}"
    if page is not None and page >= 1:
        url += f"#page={page}"
    return url


def _current_page_from_marker(line: str, current_page: int) -> int:
    match = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)
    if match:
        return int(match.group(1))
    return current_page


def _make_visible_page_markers(
    markdown: str,
    page_count: int,
    page_offset: int = 0,
    source_page_count: Optional[int] = None,
) -> str:
    """
    Convierte los marcadores estructurales en indicadores visibles.

    Reglas estrictas:
    - Acepta todas las variantes de `<!-- PAGE:N -->` (case-insensitive).
    - El marker SIEMPRE queda en su propia línea.
    - Se fuerza línea en blanco antes y después.
    - Si la línea previa era un blockquote, se cierra antes del marker.
    - Se detecta también la variante ya "renderizada" `**Página N de M**`
      inline y se separa.
    """
    if not markdown:
        return markdown

    # 1) Normalizar TODOS los markers a su propia línea aislada.
    markdown = re.sub(
        r"[ \t]*(<!--\s*PAGE\s*:?\s*\d+\s*-->)[ \t]*",
        r"\n\n\1\n\n",
        markdown,
        flags=re.IGNORECASE,
    )
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)

    # 2) Si DeepSeek ya dejó `**Página N de M**` inline, separarlo.
    #    Patrón laxo para capturar variantes.
    markdown = re.sub(
        r"[ \t]+(\*\*\s*Página\s+\d+\s+de\s+\d+\s*\*\*)[ \t]*",
        r"\n\n\1\n\n",
        markdown,
        flags=re.IGNORECASE,
    )
    markdown = re.sub(
        r"(\*\*\s*Página\s+\d+\s+de\s+\d+\s*\*\*)[ \t]+",
        r"\1\n\n",
        markdown,
        flags=re.IGNORECASE,
    )

    # 3) Convertir cada marker a su forma visible, siempre con doble
    #    línea en blanco después, para evitar "lazy continuation".
    lines = markdown.splitlines()
    output = []

    for line in lines:
        match = re.match(
            r"^\s*<!--\s*PAGE\s*:?\s*(\d+)\s*-->\s*$",
            line, re.IGNORECASE,
        )
        if not match:
            output.append(line)
            continue

        page = int(match.group(1))
        source_page = page + page_offset
        total = source_page_count or page_count

        # Cerrar cualquier blockquote abierto antes del marker.
        while output and not output[-1].strip():
            output.pop()
        if output and output[-1].lstrip().startswith(">"):
            output.append("")
        if output:
            output.append("")

        output.append(f"> **Página {source_page} de {total}**")
        output.append("")
        output.append("")  # segundo blank: garantiza cierre del blockquote

    while output and not output[-1].strip():
        output.pop()

    return "\n".join(output)


def index_references(
    markdown: str,
    document_id: Optional[str] = None,
    page_count: Optional[int] = None,
    page_offset: int = 0,
) -> str:
    if not markdown:
        return markdown

    markdown = add_document_top_anchor(markdown)
    lines = markdown.splitlines()

    start = find_references_start(lines)
    if start is None:
        return markdown

    before = lines[:start]
    reference_lines = lines[start + 1:]
    references = split_reference_entries(reference_lines)
    if not references:
        return markdown

    normalized_references = []
    for reference in references:
        reference = re.sub(r"^\s*\[(\d+)\]\s+", "", reference)
        reference = re.sub(r"^\s*\d+[.)]\s+", "", reference)
        normalized_references.append(reference)

    body = "\n".join(before)
    first_citation_anchor = {}
    citation_counts = Counter()

    def replace_numeric_citation(match):
        numbers_text = match.group(1)
        numbers = [int(n) for n in re.findall(r"\d+", numbers_text)]
        links = []
        for number in numbers:
            if not 1 <= number <= len(normalized_references):
                links.append(f"[{number}]")
                continue
            citation_counts[number] += 1
            occurrence = citation_counts[number]
            anchor = f"cite-{number}-{occurrence}"
            first_citation_anchor.setdefault(number, anchor)
            links.append(
                f'<a id="{anchor}"></a>'
                f'<a href="#ref-{number}">[{number}]</a>'
            )
        return ", ".join(links)

    body = re.sub(r"\[((?:\d+\s*,?\s*)+)\]", replace_numeric_citation, body)

    output = body.splitlines()
    output.append("")
    reference_heading = lines[start]
    output.append(reference_heading)
    output.append("")

    current_page = 1
    for line in lines[:start]:
        current_page = _current_page_from_marker(line, current_page)
    reference_page = current_page

    for index, reference in enumerate(normalized_references, start=1):
        output.append(f'<a id="ref-{index}"></a>')
        output.append(f"**{index}.** {reference}")
        target = first_citation_anchor.get(index)
        if target:
            output.append(f'<a href="#{target}">↩ Volver al texto</a>')
        else:
            output.append('↩ Sin cita localizada en el texto')
        output.append("")

    result = "\n".join(output)

    if document_id and reference_page:
        result = _link_heading_to_pdf_page(
            result,
            r"references|referencias|bibliography|bibliografía|reference list|literature cited",
            document_id,
            reference_page + page_offset,
        )

    return result


def _link_heading_to_pdf_page(markdown, heading_pattern, document_id, page):
    url = _source_pdf_url(document_id, page)
    pattern = re.compile(
        rf"^(\s*#{{1,6}}\s*)({heading_pattern})\s*$",
        re.IGNORECASE,
    )
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        prefix = match.group(1)
        title = match.group(2)
        lines[i] = f"{prefix}[{title}]({url})"
        break
    return "\n".join(lines)


def add_pdf_heading_links(markdown, document_id, page_offset=0):
    if not markdown or not document_id:
        return markdown
    lines = markdown.splitlines()
    current_page = 1
    output = []
    seen = set()
    for line in lines:
        page_match = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)
        if page_match:
            current_page = int(page_match.group(1))
            output.append(line)
            continue
        heading = re.match(r"^(\s*#{1,6}\s+)(.+?)\s*$", line)
        if not heading:
            output.append(line)
            continue
        title = heading.group(2).strip()
        if title.startswith("[") and title.endswith(")"):
            output.append(line)
            continue
        key = (title.casefold(), current_page)
        if key in seen:
            output.append(line)
            continue
        seen.add(key)
        url = _source_pdf_url(document_id, current_page + page_offset)
        output.append(f"{heading.group(1)}[{title}]({url})")
    return "\n".join(output)

def extract_page_column_aware_sync(
    doc,
    layout_hints,
    header_height,
    footer_height,
) -> list[dict]:
    """Wrapper sync para invocar desde asyncio.to_thread."""
    return [
        {
            "text": extract_page_column_aware(
                page,
                layout_hint=layout_hints.get(page_number),
                header_height=header_height,
                footer_height=footer_height,
            ),
            "metadata": {"page_number": page_number},
            "_column_aware": True,
        }
        for page_number, page in enumerate(doc, start=1)
    ]

# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

async def process_pdf(pdf_bytes: bytes, model: Optional[str] = None):
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("El archivo recibido no parece ser un PDF.")

    effective_model = model or DEEPSEEK_MODEL
    key = cache_key(pdf_bytes, effective_model)

    cached = load_cache(key)
    if cached is not None:
        log("[CACHE] Resultado encontrado.")
        return cached

    temp_dir = Path(tempfile.mkdtemp(prefix="psihub_"))
    pdf_path = temp_dir / "source.pdf"
    image_dir = temp_dir / "images"
    pdf_path.write_bytes(pdf_bytes)

    original_pdf_path = PDF_STORE_DIR / f"{key}.pdf"
    if not original_pdf_path.exists():
        original_pdf_path.write_bytes(pdf_bytes)

    with fitz.open(stream=pdf_bytes, filetype="pdf") as original_doc:
        original_page_count = original_doc.page_count
    source_page_offset = 0

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if remove_publisher_landing_page(doc):
        source_page_offset = 1

    try:
        # ------------------------------------------------
        # 1. LAYOUT
        # ------------------------------------------------
        log("\n[PIPELINE] 1/8 Analizando estructura...")
        layout_profile = analyze_document_layout(doc)

        document_model = build_document_model(doc)
        model_issues = validate_model(document_model)
        visual_candidate_pages = page_visual_candidates(document_model, threshold=0.25)
        visual_router = build_visual_router(
            document_model,
            threshold=float(os.getenv("VISUAL_PAGE_RISK_THRESHOLD", "0.32"))
        )
        model_sequence_issues = audit_block_sequence(document_model)

        print_layout_profile(layout_profile)
        log(
            f"[DOC MODEL] {document_model.page_count} páginas | "
            f"{len(document_model.blocks)} bloques | "
            f"{len(visual_candidate_pages)} páginas de alta complejidad | "
            f"{len(model_issues)} anomalías estructurales"
        )

        # ------------------------------------------------
        # 2. LIMPIEZA FÍSICA
        # ------------------------------------------------
        log("[PIPELINE] 2/8 Limpiando elementos editoriales...")
        removed = clean_pdf_using_layout(doc, layout_profile)
        log(f"[PDF CLEAN] Elementos eliminados: {removed}")

        visual_pdf_path = temp_dir / "visual_source.pdf"
        doc.save(str(visual_pdf_path), garbage=3, deflate=True)

        # ------------------------------------------------
        # 3. EXTRACCIÓN (column-aware + fallback a pymupdf4llm)
        # ------------------------------------------------
        print("[PIPELINE] 3/8 Extrayendo Markdown...")

        # 3.1 Vision analiza layout de todas las páginas (una vez por PDF).
        #     Cacheado dentro de visual_layout_ai.
        page_layout_hints = {}
        if os.getenv("ENABLE_VISION_COLUMN_LAYOUT", "1").strip().lower() \
                not in {"0", "false", "no"}:
            try:
                all_pages = list(range(1, doc.page_count + 1))
                page_layout_hints = await analyze_page_columns_with_vision(
                    str(visual_pdf_path), all_pages
                )
                two_col_pages = sum(
                    1 for h in page_layout_hints.values()
                    if int(h.get("num_columns", 1) or 1) == 2
                )
                print(
                    f"[VISION LAYOUT] {len(page_layout_hints)} páginas analizadas, "
                    f"{two_col_pages} con 2 columnas."
                )
            except Exception as e:
                print(f"[VISION LAYOUT] Error no fatal: {e}. "
                      f"Se usa detección geométrica.")
                page_layout_hints = {}

        # 3.2 Extraer column-aware (usa hints de Vision si están)
        raw_markdown = await asyncio.to_thread(
            extract_page_column_aware_sync,
            doc,
            page_layout_hints,
            layout_profile.header_height,
            layout_profile.footer_height,
        )

        # 3.3 Fallback: si la extracción column-aware quedó muy pobre,
        #     usar pymupdf4llm tradicional.
        column_signal = _markdown_text_signal(
            "\n".join(p["text"] for p in raw_markdown)
        )
        native_signal = sum(
            len(p.get_text("text").strip()) for p in doc
        )
        if native_signal > 500 and column_signal < max(500, native_signal * 0.20):
            print(
                f"[EXTRACT] Column-aware produjo {column_signal} chars "
                f"vs {native_signal} nativos. Fallback a pymupdf4llm."
            )
            raw_markdown = extract_markdown_and_images(doc, image_dir)

        raw_markdown = add_page_markers(raw_markdown)
        raw_markdown = hide_page_markers_for_translation(raw_markdown)
        raw_markdown, correspondence_notes = extract_correspondence_notes(raw_markdown)
        raw_markdown = merge_split_paragraphs(raw_markdown)

        # ------------------------------------------------
        # 3.5 VISION
        # ------------------------------------------------
        visual_diagnostics = []
        visual_hints = {}
        if os.getenv("ENABLE_VISUAL_LAYOUT_AI", "1").strip().lower() not in {"0", "false", "no"}:
            log("[PIPELINE] 3.5/8 Analizando fronteras con visión...")
            try:
                visual_diagnostics = await analyze_pdf_boundaries(str(visual_pdf_path))
                visual_hints = boundary_hints_by_page(visual_diagnostics)
                visual_patterns = collect_editorial_patterns(visual_diagnostics)

                routed_pages = [p for p, route in visual_router.items() if route == "visual"]
                page_payloads = []
                for pno in routed_pages:
                    page = next(
                        (p for p in document_model.pages if p.page == pno), None
                    )
                    if page:
                        page_payloads.append({
                            "page": pno,
                            "blocks": [
                                {
                                    "id": b.id, "kind": b.kind,
                                    "text": b.text[:260],
                                    "bbox": [round(x, 1) for x in b.bbox],
                                    "order": b.reading_order,
                                    "column": b.column,
                                }
                                for b in page.blocks
                            ],
                        })

                visual_page_audits = await audit_pages_with_vision(
                    str(visual_pdf_path), page_payloads
                )
                for audit in visual_page_audits:
                    if (
                        audit.get("reading_order_ok") is False
                        and float(audit.get("confidence", 0) or 0) >= 0.80
                    ):
                        pno = int(audit.get("page", 0))
                        visual_hints.setdefault(pno, {})["reading_order_warning"] = True
                        visual_hints[pno]["visual_audit"] = audit

                log(
                    f"[VISION] {len(visual_diagnostics)} fronteras + "
                    f"{len(visual_page_audits)} auditorías de página"
                )

                for pattern in visual_patterns:
                    if pattern not in layout_profile.repeated_headers:
                        layout_profile.repeated_headers.append(pattern)

                log(
                    f"[VISION] {len(visual_patterns)} patrones editoriales confirmados"
                )
            except Exception as ve:
                log(f"[VISION] Error no fatal: {ve}. Continuando sin hints visuales.")
                visual_hints = {}
        else:
            log("[VISION] Desactivado por ENABLE_VISUAL_LAYOUT_AI")

        # ------------------------------------------------
        # 4. LIMPIEZA MARKDOWN
        # ------------------------------------------------
        log("[PIPELINE] 4/8 Limpiando Markdown...")
        raw_markdown = preprocess_raw_markdown(raw_markdown)
        raw_markdown = remove_obvious_editorial_noise(raw_markdown)
        raw_markdown = remove_residual_editorial_lines(raw_markdown, layout_profile)

        # Aplicar merges guiados por Vision en orden de especificidad:
        # 1) Fragmentos exactos (mayor precisión, gracias a left_tail/right_head).
        # 2) Tablas/listas/citas (por tipo de estructura).
        # 3) Párrafos por heurística (fallback).
        if visual_hints:
            raw_markdown = apply_vision_fragment_merges(raw_markdown, visual_hints)
            raw_markdown = merge_tables_across_pages(raw_markdown, visual_hints)
            raw_markdown = merge_lists_across_pages(raw_markdown, visual_hints)
            raw_markdown = merge_interrupted_citations(raw_markdown, visual_hints)

        raw_markdown = clean_and_join_broken_paragraphs(
            raw_markdown, visual_hints=visual_hints,
        )
        raw_markdown = optimize_markdown_for_mobile(raw_markdown)

        raw_signal = _markdown_text_signal(raw_markdown)
        log(f"[MARKDOWN] Señal de texto tras limpieza: {raw_signal} chars")

        # ------------------------------------------------
        # 5. IDIOMA
        # ------------------------------------------------
        log("[PIPELINE] 5/8 Detectando idioma...")
        source_language = detect_language(raw_markdown)
        log(f"[LANGUAGE] {source_language}")

        # ------------------------------------------------
        # 6. TABLAS + REFERENCIAS
        # ------------------------------------------------
        log("[PIPELINE] 6/8 Preparando tablas y referencias...")
        raw_markdown = number_references(raw_markdown)

        # Reparar tablas mal formadas ANTES de aislarlas.
        raw_markdown = repair_broken_tables(raw_markdown)

        markdown_for_translation, tables = isolate_tables(raw_markdown)

        # ------------------------------------------------
        # GUARD PRE-TRADUCCIÓN
        # ------------------------------------------------
        pre_signal = _markdown_text_signal(markdown_for_translation)
        log(
            f"[GUARD] raw_markdown={raw_signal} chars | "
            f"markdown_for_translation={pre_signal} chars | "
            f"tablas aisladas={len(tables)}"
        )

        if pre_signal < 300:
            preview = markdown_for_translation[:800]
            raise RuntimeError(
                f"El Markdown a traducir contiene solo {pre_signal} caracteres "
                f"de texto real. La extracción falló antes de llegar a DeepSeek.\n\n"
                f"Diagnóstico:\n"
                f"  - Páginas del PDF: {doc.page_count}\n"
                f"  - Señal tras limpieza: {raw_signal}\n"
                f"  - Tablas aisladas: {len(tables)}\n\n"
                f"Preview del Markdown problemático:\n{preview}"
            )

        # ------------------------------------------------
        # 7. TRADUCCIÓN PARALELA
        # ------------------------------------------------
        tables, translated_markdown = await asyncio.gather(
            translate_tables(tables, model),
            translate_markdown(markdown_for_translation, model),
        )

        translated_markdown = restore_page_markers_after_translation(translated_markdown)
        translated_markdown = repair_broken_tables(translated_markdown)

        # Restaurar markers antes de todo el postprocesado.
        translated_markdown = restore_page_markers_after_translation(translated_markdown)

        # ------------------------------------------------
        # RESTAURAR TABLAS
        # ------------------------------------------------
        translated_markdown = restore_tables(translated_markdown, tables)

                # ------------------------------------------------
        # 8. POSTPROCESADO
        # ------------------------------------------------
        log("[PIPELINE] 8/8 Optimizando lectura...")
        translated_markdown = postprocess_markdown(translated_markdown)
        translated_markdown = convert_local_images_to_base64(
            translated_markdown, image_dir
        )

        # Quitar markers dentro de la sección de referencias ANTES de
        # indexar, para que las referencias no queden cortadas.
        translated_markdown = strip_page_markers_from_references(translated_markdown)

        translated_markdown = index_references(
            translated_markdown,
            document_id=key,
            page_count=doc.page_count,
            page_offset=source_page_offset,
        )
        translated_markdown = add_pdf_heading_links(
            translated_markdown,
            document_id=key,
            page_offset=source_page_offset,
        )

        # Reinsertar las notas de correspondencia antes de referencias.
        if correspondence_notes:
            notes_block = "\n\n## Notas de correspondencia\n\n"
            for idx, note in enumerate(correspondence_notes, 1):
                notes_block += f"> **Nota {idx}.** {note}\n\n"

            ref_match = re.search(
                r"\n#{1,6}\s*(?:References|Referencias|Bibliography|Bibliografía)\b",
                translated_markdown,
                re.IGNORECASE,
            )
            if ref_match:
                insert_at = ref_match.start()
                translated_markdown = (
                    translated_markdown[:insert_at]
                    + notes_block
                    + translated_markdown[insert_at:]
                )
            else:
                translated_markdown += notes_block

        # IMPORTANTE: normalize_footnote_formatting corre DESPUÉS de
        # index_references, para que las referencias ya estén formateadas
        # como "**N.** texto" y no sean confundidas con notas al pie.
        translated_markdown = normalize_footnote_formatting(translated_markdown)

        translated_markdown = _make_visible_page_markers(
            translated_markdown,
            page_count=doc.page_count,
            page_offset=source_page_offset,
            source_page_count=original_page_count,
        )

        # ------------------------------------------------
        # GUARD FINAL
        # ------------------------------------------------
        final_signal = _markdown_text_signal(translated_markdown)

        if final_signal < 200:
            raise RuntimeError(
                "La extracción no produjo texto suficiente "
                f"(señal de texto = {final_signal}). "
                "Revisá el PDF de entrada o la configuración de pymupdf4llm."
            )

        if _is_deepseek_refusal(translated_markdown):
            raise RuntimeError(
                "La traducción devolvió una negativa del modelo. "
                "Probablemente la extracción no produjo texto real. "
                "No se guarda en caché."
            )

        # ------------------------------------------------
        # RESULTADO
        # ------------------------------------------------
        result = {
            "success": True,
            "source_language": source_language,
            "model": effective_model,
            "markdown": translated_markdown,
            "original_markdown": raw_markdown,
            "layout_profile": asdict(layout_profile),
            "document_model": document_model.to_dict(),
            "document_model_summary": document_model.summary(),
            "document_model_issues": model_issues,
            "visual_candidate_pages": visual_candidate_pages,
            "visual_diagnostics": visual_diagnostics,
            "page_count": doc.page_count,
            "document_id": key,
            "source_pdf_url": _source_pdf_url(key),
            "source_page_count": original_page_count,
            "source_page_offset": source_page_offset,
        }

        save_cache(key, result)
        return result

    finally:
        doc.close()


# ============================================================
# PROCESAMIENTO DESDE URL
# ============================================================

async def process_url(url: str, model: Optional[str] = None):
    log(f"[DOWNLOAD] {url}")
    pdf_bytes = await download_pdf(url)
    return await process_pdf(pdf_bytes, model)


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/api/source-pdf/{document_id}")
async def api_source_pdf(document_id: str):
    pdf_path = PDF_STORE_DIR / f"{document_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF interno no encontrado.")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename="psihub-source.pdf",
        headers={"Content-Disposition": "inline; filename=psihub-source.pdf"},
    )


@app.post("/api/translate")
async def api_translate(request: TranslateRequest):
    try:
        result = await process_url(request.url, request.model)
        return JSONResponse(content=result)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Error descargando PDF: {e}")
    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/translate-file")
async def api_translate_file(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
):
    try:
        pdf_bytes = await file.read()
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="El archivo no es un PDF válido.")
        result = await process_pdf(pdf_bytes, model)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/check-pdf")
async def api_check_pdf(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="No es un PDF válido.")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            profile = analyze_document_layout(doc)
            return JSONResponse(content={
                "success": True,
                "layout_profile": asdict(profile),
            })
        finally:
            doc.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "PsiHub Reader",
        "deepseek": bool(DEEPSEEK_API_KEYS),
        "deepseek_keys": len(DEEPSEEK_API_KEYS),
        "translation_backend": "DeepSeek",
        "translation_concurrency": TRANSLATION_CONCURRENCY,
    }
# ============================================================
# ENDPOINTS DE DEBUG: LOGS EN VIVO
# ============================================================

@app.get("/api/logs")
async def api_logs(since: float = 0.0, limit: int = 500):
    """Devuelve las últimas entradas del buffer de logs."""
    entries = [e for e in LOG_BUFFER if e["t"] >= since]
    return JSONResponse(content={
        "entries": entries[-limit:],
        "count": len(entries),
    })


@app.get("/api/logs/stream")
async def api_logs_stream():
    """Server-Sent Events: stream de logs en vivo."""
    from fastapi.responses import StreamingResponse
    import json as _json

    async def event_generator():
        last_t = time.time()
        while True:
            await asyncio.sleep(0.5)
            new_entries = [e for e in LOG_BUFFER if e["t"] > last_t]
            if new_entries:
                last_t = new_entries[-1]["t"]
                for entry in new_entries:
                    yield f"data: {_json.dumps(entry)}\n\n"
            else:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )

# ============================================================
# GRADIO
# ============================================================

async def gradio_translate_url(url, model_name=None):
    if not url or not url.strip():
        return ("Introducí una URL de un PDF.", "")
    try:
        result = await process_url(url.strip(), model_name or None)
        profile = result.get("layout_profile", {})
        info = (
            f"**Páginas:** {result.get('page_count', '?')}\n\n"
            f"**Idioma detectado:** {result.get('source_language', '?')}\n\n"
            f"**Columnas estimadas:** {profile.get('likely_columns', '?')}\n\n"
            f"**Elementos editoriales eliminados:** "
            f"{profile.get('elements_removed_estimate', 0)}"
        )
        return (result["markdown"], info)
    except Exception as e:
        return ("", f"Error: {e}")


async def gradio_translate_file(file, model_name=None):
    if file is None:
        return ("Subí un PDF.", "")
    try:
        file_path = file.name if hasattr(file, "name") else str(file)
        pdf_bytes = Path(file_path).read_bytes()
        result = await process_pdf(pdf_bytes, model_name or None)
        profile = result.get("layout_profile", {})
        info = (
            f"**Páginas:** {result.get('page_count', '?')}\n\n"
            f"**Idioma:** {result.get('source_language', '?')}\n\n"
            f"**Columnas:** {profile.get('likely_columns', '?')}\n\n"
            f"**Elementos eliminados:** "
            f"{profile.get('elements_removed_estimate', 0)}"
        )
        return (result["markdown"], info)
    except Exception as e:
        return ("", f"Error: {e}")


# ============================================================
# UI GRADIO
# ============================================================

with gr.Blocks(title="PsiHub Reader") as demo:
    gr.Markdown("""
# PsiHub Reader

### PDF académico → traducción limpia para lectura digital

El documento se analiza estructuralmente antes de traducirse
para evitar encabezados, pies y números de página innecesarios
sin eliminar contenido académico legítimo.
""")

    with gr.Tab("URL"):
        url_input = gr.Textbox(label="URL del PDF", placeholder="https://...")
        translate_url_button = gr.Button("Traducir PDF", variant="primary")
        url_output = gr.Markdown(label="Traducción")
        url_info = gr.Markdown(label="Información")
        translate_url_button.click(
            fn=gradio_translate_url,
            inputs=[url_input],
            outputs=[url_output, url_info],
        )

    with gr.Tab("Archivo"):
        file_input = gr.File(label="PDF", file_types=[".pdf"])
        translate_file_button = gr.Button("Traducir PDF", variant="primary")
        file_output = gr.Markdown(label="Traducción")
        file_info = gr.Markdown(label="Información")
        translate_file_button.click(
            fn=gradio_translate_file,
            inputs=[file_input],
            outputs=[file_output, file_info],
        )


app = gr.mount_gradio_app(app, demo, path="/")

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)