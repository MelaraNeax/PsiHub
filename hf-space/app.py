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
from collections import Counter, defaultdict
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
PIPELINE_VERSION = "2026-09-12-reader-master-v16"

PDF_STORE_DIR = CACHE_DIR / "source_pdfs"
PDF_STORE_DIR.mkdir(parents=True, exist_ok=True)


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
            print(f"[PDF CLEAN] Página {page_number}: {reason}: {text[:120]}")

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
    print(f"Columnas estimadas: {profile.likely_columns}")
    print(f"Header zone: {profile.header_height} pt")
    print(f"Footer zone: {profile.footer_height} pt")
    print(f"Números de página: {'sí' if profile.page_numbers else 'no'}")
    print(f"Headers repetidos: {len(profile.repeated_headers)}")
    for header in profile.repeated_headers:
        print(f"  HEADER: {header}")
    print(f"Footers repetidos: {len(profile.repeated_footers)}")
    for footer in profile.repeated_footers:
        print(f"  FOOTER: {footer}")
    print("=" * 70 + "\n")


# ============================================================
# DESCARGA DE PDF
# ============================================================

async def download_pdf(url: str) -> bytes:
    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.content
        if not data.startswith(b"%PDF"):
            raise ValueError("La URL no parece devolver un PDF válido.")
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
        print(f"[EXTRACT] {page_fallbacks} páginas reemplazadas por texto nativo.")

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


def clean_and_join_broken_paragraphs(text: str, visual_hints: Optional[dict] = None) -> str:
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

        if current.rstrip().endswith("-"):
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and re.match(r"^[a-záéíóúñü]", next_line, re.IGNORECASE):
                    current = current.rstrip()[:-1] + next_line
                    lines[i + 1] = ""

        output.append(current)

    cleaned = "\n".join(output)

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
                not left or not right
                or left.startswith(protected) or right.startswith(protected)
                or left.endswith((".", ":", ";", "?", "!"))
                or re.match(r"^[A-ZÁÉÍÓÚÑÜ]", right)
            ):
                i += 1
                continue

            # --- FIX: el marcador NUNCA se mete en la línea del texto ---
            # El párrafo unido va a lines[prev]. El marcador se queda
            # intacto, solo en su propia línea, en lines[i].
            # lines[nxt] se vacía (su contenido ya está en lines[prev]).
            if left.endswith("-") and re.match(r"^[a-záéíóúñü]", right, re.I):
                lines[prev] = left[:-1] + right
            else:
                lines[prev] = left + " " + right
            lines[nxt] = ""
            # lines[i] queda tal cual: <!-- PAGE:N+1 -->, solo en su línea.
            i = nxt + 1

        cleaned = "\n".join(lines)

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
    # FIX: solo aplicar al inicio de línea y exigir un espacio después
    # de los '#'. Esto evita romper celdas de tabla que empiezan con
    # '#', como "#seguidores/enlaces entrantes".
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
    #
    # No introducir espacios arbitrarios dentro de ellas.
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

    if len(table_lines) < 2:
        print("[TABLE] Respuesta inválida; se conserva la tabla original.")
        return table_md

    separator_index = next(
        (i for i, line in enumerate(table_lines) if is_markdown_table_separator(line)),
        None
    )
    if separator_index != 1:
        print("[TABLE] Estructura alterada; se conserva la tabla original.")
        return table_md

    header_columns = table_lines[0].count("|")
    if header_columns < 3:
        return table_md

    if any(line.count("|") != header_columns for line in table_lines):
        print("[TABLE] Número de columnas alterado; se conserva la tabla original.")
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
        print(f"[CHUNK] {dropped} chunks vacíos descartados.")

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
            print(f"[TRANSLATION] DeepSeek response: {elapsed:.2f}s")

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
                print(
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

    print(
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
            print(f"[TRANSLATION] Worker {worker_id} → chunk {index + 1}/{total}")
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

    print(f"[TRANSLATION] {len(chunks)} chunks | {worker_count} workers / API keys")

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
            print(f"[IMAGE] Error: {image_path}: {e}")

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


def find_references_start(lines):
    for index, line in enumerate(lines):
        if REFERENCE_HEADINGS.match(line.strip()):
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
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    output = []
    for line in lines:
        match = re.match(r"^\s*<!--\s*PAGE:(\d+)\s*-->\s*$", line)
        if not match:
            output.append(line)
            continue
        page = int(match.group(1))
        source_page = page + page_offset
        total = source_page_count or page_count
        output.append("")
        output.append(f"> **Página {source_page} de {total}**")
        output.append("")
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
        print("[CACHE] Resultado encontrado.")
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
        print("\n[PIPELINE] 1/8 Analizando estructura...")
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
        print(
            f"[DOC MODEL] {document_model.page_count} páginas | "
            f"{len(document_model.blocks)} bloques | "
            f"{len(visual_candidate_pages)} páginas de alta complejidad | "
            f"{len(model_issues)} anomalías estructurales"
        )

        # ------------------------------------------------
        # 2. LIMPIEZA FÍSICA
        # ------------------------------------------------
        print("[PIPELINE] 2/8 Limpiando elementos editoriales...")
        removed = clean_pdf_using_layout(doc, layout_profile)
        print(f"[PDF CLEAN] Elementos eliminados: {removed}")

        visual_pdf_path = temp_dir / "visual_source.pdf"
        doc.save(str(visual_pdf_path), garbage=3, deflate=True)

        # ------------------------------------------------
        # 3. EXTRACCIÓN
        # ------------------------------------------------
        print("[PIPELINE] 3/8 Extrayendo Markdown...")
        raw_markdown = extract_markdown_and_images(doc, image_dir)
        raw_markdown = add_page_markers(raw_markdown)

        # ------------------------------------------------
        # 3.5 VISION
        # ------------------------------------------------
        visual_diagnostics = []
        visual_hints = {}
        if os.getenv("ENABLE_VISUAL_LAYOUT_AI", "1").strip().lower() not in {"0", "false", "no"}:
            print("[PIPELINE] 3.5/8 Analizando fronteras con visión...")
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

                print(
                    f"[VISION] {len(visual_diagnostics)} fronteras + "
                    f"{len(visual_page_audits)} auditorías de página"
                )

                for pattern in visual_patterns:
                    if pattern not in layout_profile.repeated_headers:
                        layout_profile.repeated_headers.append(pattern)

                print(
                    f"[VISION] {len(visual_patterns)} patrones editoriales confirmados"
                )
            except Exception as ve:
                print(f"[VISION] Error no fatal: {ve}. Continuando sin hints visuales.")
                visual_hints = {}
        else:
            print("[VISION] Desactivado por ENABLE_VISUAL_LAYOUT_AI")

        # ------------------------------------------------
        # 4. LIMPIEZA MARKDOWN
        # ------------------------------------------------
        print("[PIPELINE] 4/8 Limpiando Markdown...")
        raw_markdown = preprocess_raw_markdown(raw_markdown)
        raw_markdown = remove_obvious_editorial_noise(raw_markdown)
        raw_markdown = remove_residual_editorial_lines(raw_markdown, layout_profile)
        raw_markdown = clean_and_join_broken_paragraphs(
            raw_markdown, visual_hints=visual_hints,
        )
        raw_markdown = optimize_markdown_for_mobile(raw_markdown)

        raw_signal = _markdown_text_signal(raw_markdown)
        print(f"[MARKDOWN] Señal de texto tras limpieza: {raw_signal} chars")

        # ------------------------------------------------
        # 5. IDIOMA
        # ------------------------------------------------
        print("[PIPELINE] 5/8 Detectando idioma...")
        source_language = detect_language(raw_markdown)
        print(f"[LANGUAGE] {source_language}")

        # ------------------------------------------------
        # 6. TABLAS + REFERENCIAS
        # ------------------------------------------------
        print("[PIPELINE] 6/8 Preparando tablas y referencias...")
        raw_markdown = number_references(raw_markdown)
        markdown_for_translation, tables = isolate_tables(raw_markdown)

        # ------------------------------------------------
        # GUARD PRE-TRADUCCIÓN
        # ------------------------------------------------
        pre_signal = _markdown_text_signal(markdown_for_translation)
        print(
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
        print("[PIPELINE] 7/8 Traduciendo cuerpo + tablas en paralelo...")
        tables, translated_markdown = await asyncio.gather(
            translate_tables(tables, model),
            translate_markdown(markdown_for_translation, model),
        )

        # ------------------------------------------------
        # RESTAURAR TABLAS
        # ------------------------------------------------
        translated_markdown = restore_tables(translated_markdown, tables)

        # ------------------------------------------------
        # 8. POSTPROCESADO
        # ------------------------------------------------
        print("[PIPELINE] 8/8 Optimizando lectura...")
        translated_markdown = postprocess_markdown(translated_markdown)
        translated_markdown = convert_local_images_to_base64(
            translated_markdown, image_dir
        )
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
    print(f"[DOWNLOAD] {url}")
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
        print(f"[ERROR] {type(e).__name__}: {e}")
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
        print(f"[ERROR] {type(e).__name__}: {e}")
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