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
#   pymupdf4llm
#    ↓
#   limpieza estructural del Markdown
#    ↓
#   detección de idioma
#    ↓
#   aislamiento de tablas
#    ↓
#   traducción por chunks
#    ↓
#   restauración de tablas/imágenes
#    ↓
#   Markdown optimizado para lectura
#
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


# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv()

DEEPSEEK_API_KEYS = [
    os.getenv(f"DEEPSEEK_API_KEY_{i}", "").strip()
    for i in range(1, 7)
]

# Compatibilidad: si todavía existe la variable antigua, puede usarse
# como única clave. Para el nuevo esquema se recomienda usar _1 ... _6.
_legacy_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
if not any(DEEPSEEK_API_KEYS) and _legacy_key:
    DEEPSEEK_API_KEYS = [_legacy_key]

DEEPSEEK_API_KEYS = [key for key in DEEPSEEK_API_KEYS if key]

# Alias para compatibilidad con partes antiguas del código.
DEEPSEEK_API_KEY = DEEPSEEK_API_KEYS[0] if DEEPSEEK_API_KEYS else ""

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
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

MAX_CHARS_PER_CHUNK = int(
    os.getenv("MAX_CHARS_PER_CHUNK", "18000")
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT", "120")
)

# Cambiar esta versión invalida automáticamente caches generados por
# versiones anteriores del pipeline.
PIPELINE_VERSION = "2026-09-12-reader-layout-v6-parallel6"



# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="PsiHub Reader",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MODELOS
# ============================================================

class TranslateRequest(BaseModel):
    url: str
    model: Optional[str] = None


# ============================================================
# UTILIDADES GENERALES
# ============================================================

def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def normalize_editorial_text(text: str) -> str:
    """
    Normalización para comparar elementos editoriales
    entre páginas.

    No se utiliza para modificar el contenido final.
    """
    text = normalize_whitespace(text)

    # números variables
    text = re.sub(r"\b\d+\b", "#", text)

    # separadores
    text = re.sub(r"\s*[-–—|]\s*", " - ", text)

    return text.lower().strip()


def is_page_number(text: str) -> bool:
    """
    Detecta números de página y variantes comunes.
    """

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
    return bool(
        re.search(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            text,
            re.IGNORECASE
        )
    )


def looks_like_doi(text: str) -> bool:
    return bool(
        re.search(
            r"(?:doi\s*:\s*|https?://doi\.org/)\S+",
            text,
            re.IGNORECASE
        )
    )


def looks_like_url(text: str) -> bool:
    return bool(
        re.search(
            r"https?://\S+",
            text,
            re.IGNORECASE
        )
    )


def is_copyright_line(text: str) -> bool:
    return bool(
        re.search(
            r"(?:copyright|©|\(c\))\s*(?:19|20)\d{2}",
            text,
            re.IGNORECASE
        )
    )


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

def classify_region(
    block_rect: fitz.Rect,
    page_rect: fitz.Rect,
    header_height: float,
    footer_height: float
) -> str:

    if block_rect.y1 <= header_height:
        return "header"

    if block_rect.y0 >= page_rect.height - footer_height:
        return "footer"

    return "body"


def collect_page_elements(
    doc: fitz.Document,
    header_fraction: float = 0.08,
    footer_fraction: float = 0.08
):
    """
    Extrae líneas de texto con coordenadas.

    Se utilizan líneas y no bloques completos porque un PDF puede
    fusionar un header/footer con una línea de contenido académico.

    Ejemplo problemático:

        "...menos del 2 | Alcohol Research | Vol 40 No 1 | 2019"

    Si se analizara por bloques, se podría borrar contenido legítimo.
    Analizando líneas podemos aislar solamente la parte editorial.
    """

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

                text_parts = []

                for span in spans:

                    span_text = span.get(
                        "text",
                        ""
                    )

                    if span_text:
                        text_parts.append(
                            span_text
                        )

                text = "".join(
                    text_parts
                ).strip()

                if not text:
                    continue

                bbox = line.get(
                    "bbox"
                )

                if not bbox or len(bbox) != 4:
                    continue

                x0, y0, x1, y1 = bbox

                block_rect = fitz.Rect(
                    x0,
                    y0,
                    x1,
                    y1
                )

                region = classify_region(
                    block_rect,
                    rect,
                    header_height,
                    footer_height
                )

                elements.append(
                    LayoutElement(
                        text=text,
                        normalized=normalize_editorial_text(
                            text
                        ),
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                        page=page_index + 1,
                        width=x1 - x0,
                        height=y1 - y0,
                        region=region,
                    )
                )

    return elements


def detect_repeated_elements(
    elements,
    page_count: int,
    region: str
):
    """
    Detecta texto que se repite en la misma zona (header/footer)
    a lo largo de varias páginas.

    Se cuenta una sola vez por página para evitar que varias líneas
    idénticas dentro de una misma página inflen artificialmente la
    frecuencia. El umbral es conservador: al menos 2 páginas y,
    cuando el documento es grande, aproximadamente el 40% de las
    páginas.
    """
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
        text
        for text, pages in pages_by_text.items()
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


def detect_columns(doc: fitz.Document):
    """
    Estimación simple de columnas.

    No modifica el documento.
    Sirve únicamente como información del perfil.
    """

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

            # ignorar bloques muy cercanos a los bordes
            if y0 < rect.height * 0.12:
                continue

            if y1 > rect.height * 0.88:
                continue

            blocks.append(
                (x0, x1, y0, y1, text)
            )

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

        if left >= 2 and right >= 2:
            column_scores.append(2)
        else:
            column_scores.append(1)

    if not column_scores:
        return 1

    return 2 if sum(column_scores) / len(column_scores) >= 1.5 else 1


def calculate_body_bounds(
    doc: fitz.Document,
    elements,
    repeated_headers,
    repeated_footers
):
    """
    Estima dónde empieza y termina el contenido real.

    Es una estimación informativa; la eliminación sigue siendo
    extremadamente conservadora.
    """

    if doc.page_count == 0:
        return 0, 0

    page_heights = [
        page.rect.height
        for page in doc
    ]

    average_height = (
        sum(page_heights) / len(page_heights)
    )

    header_positions = []
    footer_positions = []

    for element in elements:

        if element.normalized in repeated_headers:
            header_positions.append(element.y1)

        if element.normalized in repeated_footers:
            footer_positions.append(element.y0)

    if header_positions:
        body_top = max(header_positions) + 5
    else:
        body_top = average_height * 0.08

    if footer_positions:
        body_bottom = min(footer_positions) - 5
    else:
        body_bottom = average_height * 0.92

    return body_top, body_bottom


def is_publisher_landing_page(page: fitz.Page) -> bool:
    """
    Detecta la primera página de portales editoriales como Taylor & Francis.

    Algunas descargas no empiezan directamente con el artículo: primero
    incluyen una página web/editorial con portada, botones, métricas,
    "To cite this article", etc. Esa página no forma parte del contenido
    científico y además suele contener imágenes decorativas.

    La detección exige varias señales simultáneas para no eliminar una
    primera página académica legítima.
    """
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

    hits = sum(
        1
        for marker in markers
        if marker in text
    )

    return (
        hits >= 3
        and (
            "to cite this article" in text
            or "to link to this article" in text
        )
    )


def remove_publisher_landing_page(doc: fitz.Document) -> bool:
    """Elimina únicamente una portada editorial detectada en la primera página."""
    if doc.page_count <= 1:
        return False

    if not is_publisher_landing_page(doc[0]):
        return False

    print(
        "[PDF CLEAN] Primera página detectada como portada/página editorial; "
        "se elimina antes de la extracción."
    )
    doc.delete_page(0)
    return True


def is_editorial_artifact_line(text: str) -> bool:
    """Detecta artefactos editoriales de producción/cabecera claramente aislados."""
    s = normalize_whitespace(text)
    low = s.lower()

    if re.search(
        r"pages_[^\s]+\.qxd",
        s,
        re.IGNORECASE
    ):
        return True

    if re.search(
        r"^dialogues\s+clin\s+neurosci\.\s*\d{4};\d+:\d+[-–]\d+\.?$",
        s,
        re.IGNORECASE
    ):
        return True

    return False


def analyze_document_layout(
    doc: fitz.Document
) -> DocumentLayoutProfile:

    if doc.page_count == 0:
        raise ValueError(
            "El PDF no contiene páginas."
        )

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

    repeated_headers = detect_repeated_elements(
        elements,
        doc.page_count,
        "header"
    )

    repeated_footers = detect_repeated_elements(
        elements,
        doc.page_count,
        "footer"
    )

    page_number_pages = detect_page_numbers(
        elements
    )

    columns = detect_columns(
        doc
    )

    body_top, body_bottom = (
        calculate_body_bounds(
            doc,
            elements,
            repeated_headers,
            repeated_footers
        )
    )

    profile = DocumentLayoutProfile(
        page_count=doc.page_count,

        page_width=round(
            page_width,
            2
        ),

        page_height=round(
            page_height,
            2
        ),

        header_height=round(
            page_height * HEADER_FRACTION,
            2
        ),

        footer_height=round(
            page_height * FOOTER_FRACTION,
            2
        ),

        repeated_headers=sorted(
            list(repeated_headers)
        ),

        repeated_footers=sorted(
            list(repeated_footers)
        ),

        page_numbers=bool(
            page_number_pages
        ),

        likely_columns=columns,

        body_top=round(
            body_top,
            2
        ),

        body_bottom=round(
            body_bottom,
            2
        ),

        first_page_special=True,
    )

    return profile


# ============================================================
# LIMPIEZA FÍSICA DEL PDF
# ============================================================

def should_remove_element(
    element: LayoutElement,
    profile: DocumentLayoutProfile
):
    """
    Regla central de seguridad.

    Un elemento NO se elimina simplemente porque se repita.

    Tiene que cumplir además con:
      - estar en header/footer
      - o ser número de página
      - o ser un elemento editorial extremadamente evidente.

    La primera página recibe protección especial.
    """

    text = element.text.strip()

    if not text:
        return False, ""

    # --------------------------------------------------------
    # NÚMEROS DE PÁGINA
    # --------------------------------------------------------

    if (
        element.region == "footer"
        and is_page_number(text)
    ):
        return True, "page number"

    # --------------------------------------------------------
    # HEADER REPETIDO
    # --------------------------------------------------------

    if (
        element.region == "header"
        and element.normalized in profile.repeated_headers
    ):

        # Nunca borrar automáticamente el header de la
        # primera página.
        if element.page == 1:
            return False, ""

        return True, "repeated header"

    # --------------------------------------------------------
    # FOOTER REPETIDO
    # --------------------------------------------------------

    if (
        element.region == "footer"
        and element.normalized in profile.repeated_footers
    ):

        return True, "repeated footer"

    return False, ""


def clean_pdf_using_layout(
    doc: fitz.Document,
    profile: DocumentLayoutProfile
):
    """
    Limpieza física conservadora basada en líneas de texto + artefactos
    gráficos claramente editoriales.

    Nunca elimina figuras científicas normales. Solo elimina: 
      - números de página claramente identificados
      - headers/footers repetidos
      - reglas gráficas horizontales/verticales muy finas
        que son decoración editorial
      - imágenes que quedan prácticamente fuera de la página
    """

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

                text = "".join(
                    span.get("text", "")
                    for span in spans
                ).strip()

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
                        line_rect,
                        rect,
                        profile.header_height,
                        profile.footer_height
                    )
                )

                should_remove, reason = should_remove_element(
                    element,
                    profile
                )

                if should_remove:
                    redactions.append((line_rect, reason, text))

        # --------------------------------------------------------
        # Artefactos gráficos editoriales
        # --------------------------------------------------------
        for image_info in page.get_images(full=True):
            xref = image_info[0]

            for image_rect in page.get_image_rects(xref):
                if image_rect.is_empty:
                    continue

                width = image_rect.width
                height = image_rect.height

                outside_left = image_rect.x1 <= 0
                outside_right = image_rect.x0 >= rect.width
                outside_top = image_rect.y1 <= 0
                outside_bottom = image_rect.y0 >= rect.height

                mostly_outside = (
                    outside_left
                    or outside_right
                    or outside_top
                    or outside_bottom
                )

                horizontal_rule = (
                    width >= rect.width * 0.35
                    and height <= 15
                )

                vertical_rule = (
                    height >= rect.height * 0.35
                    and width <= 15
                )

                in_editorial_header = (
                    image_rect.y1 <= rect.height * 0.14
                )

                in_editorial_footer = (
                    image_rect.y0 >= rect.height * 0.92
                )

                if (
                    mostly_outside
                    or horizontal_rule
                    or vertical_rule
                    or in_editorial_header
                    or in_editorial_footer
                ):
                    reason = "editorial graphic"
                    redactions.append(
                        (
                            image_rect,
                            reason,
                            f"image xref={xref}"
                        )
                    )

        for rect_to_remove, reason, text in redactions:
            page.add_redact_annot(
                rect_to_remove,
                fill=(1, 1, 1)
            )

            removed += 1
            print(
                f"[PDF CLEAN] Página {page_number}: "
                f"{reason}: {text[:120]}"
            )

        if redactions:
            page.apply_redactions()

    profile.elements_removed_estimate = removed
    return removed


# ============================================================
# DIAGNÓSTICO DEL PERFIL
# ============================================================

def print_layout_profile(
    profile: DocumentLayoutProfile
):

    print("\n" + "=" * 70)
    print("PERFIL ESTRUCTURAL DEL DOCUMENTO")
    print("=" * 70)

    print(
        f"Páginas: {profile.page_count}"
    )

    print(
        f"Tamaño: "
        f"{profile.page_width} × "
        f"{profile.page_height} pt"
    )

    print(
        f"Columnas estimadas: "
        f"{profile.likely_columns}"
    )

    print(
        f"Header zone: "
        f"{profile.header_height} pt"
    )

    print(
        f"Footer zone: "
        f"{profile.footer_height} pt"
    )

    print(
        f"Números de página: "
        f"{'sí' if profile.page_numbers else 'no'}"
    )

    print(
        f"Headers repetidos: "
        f"{len(profile.repeated_headers)}"
    )

    for header in profile.repeated_headers:
        print(f"  HEADER: {header}")

    print(
        f"Footers repetidos: "
        f"{len(profile.repeated_footers)}"
    )

    for footer in profile.repeated_footers:
        print(f"  FOOTER: {footer}")

    print("=" * 70 + "\n")


# ============================================================
# DESCARGA DE PDF
# ============================================================

async def download_pdf(url: str) -> bytes:

    timeout = httpx.Timeout(
        REQUEST_TIMEOUT
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True
    ) as client:

        response = await client.get(url)

        response.raise_for_status()

        content_type = (
            response.headers
            .get("content-type", "")
            .lower()
        )

        data = response.content

        if not data.startswith(b"%PDF"):
            raise ValueError(
                "La URL no parece devolver un PDF válido."
            )

        return data


# ============================================================
# CACHE
# ============================================================

def cache_key(
    content: bytes,
    model: str
):

    digest = hashlib.sha256(
        content
    ).hexdigest()

    model_digest = hashlib.sha256(
        model.encode("utf-8")
    ).hexdigest()[:12]

    version_digest = hashlib.sha256(
        PIPELINE_VERSION.encode("utf-8")
    ).hexdigest()[:12]

    return f"{digest}_{model_digest}_{version_digest}"


def get_cache_path(key: str):

    return CACHE_DIR / f"{key}.json"


def load_cache(key: str):

    path = get_cache_path(key)

    if not path.exists():
        return None

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:
        return None


def save_cache(
    key: str,
    data: dict
):

    path = get_cache_path(key)

    temp_path = path.with_suffix(
        ".tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

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
    """Asegura que una nota extraída como footnote quede siempre como blockquote."""
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

        # PyMuPDF4LLM normalmente ya entrega '> '. No duplicarlo.
        stripped = re.sub(r"^>\s*", "", stripped)
        output.append("> " + stripped)

    while output and output[-1] == ">":
        output.pop()

    return "\n".join(output).strip()


def _remove_ranges_from_text(text: str, ranges) -> str:
    """Elimina rangos de caracteres sin alterar el resto del Markdown."""
    if not text or not ranges:
        return text

    result = text
    for start, stop in sorted(ranges, reverse=True):
        result = result[:start] + result[stop:]
    return result


def _footnote_y_key(item) -> float:
    """Devuelve la coordenada Y de una nota al pie sin subscriptar un Optional."""
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
    """
    Detecta únicamente columnas claramente separadas.

    No fuerza una lectura en columnas ante páginas de una sola columna,
    títulos de ancho completo, tablas o layouts ambiguos.
    """
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

        # Las cajas que cruzan claramente toda la página no sirven para
        # decidir si existe una estructura de dos columnas.
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

    # Debe existir un hueco central real entre ambas masas de texto.
    left_edge = max(left)
    right_edge = min(right)
    gap = right_edge - left_edge

    return (
        left_edge < page_width * 0.47
        and right_edge > page_width * 0.53
        and gap >= page_width * 0.06
    )


def _reorder_two_column_page(page: dict, page_width: float) -> str:
    """
    Reconstruye conservadoramente el orden de lectura de una página de dos
    columnas usando los page_boxes de PyMuPDF4LLM.

    La unidad de reordenamiento es la caja completa: nunca se reconstruyen
    palabras ni se reescribe el Markdown interno de una caja.
    """
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

    if not usable:
        cleaned = text
    else:
        # Detectar cajas que realmente pertenecen a las dos columnas.
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
            elif center >= page_width * 0.5:
                right_boxes.append(item)

        has_two_columns = (
            len(left_boxes) >= 2
            and len(right_boxes) >= 2
            and max((b[2][0] + b[2][2]) / 2 for b in left_boxes)
            < page_width * 0.47
            and min((b[2][0] + b[2][2]) / 2 for b in right_boxes)
            > page_width * 0.53
        )

        if not has_two_columns:
            cleaned = _remove_ranges_from_text(
                text,
                [pos for _, pos in footnotes]
            )
        else:
            # Cajas de ancho completo funcionan como anclas entre bloques de
            # columnas: título, figura, tabla, caption, etc.
            full_width = []
            column_boxes = []
            for item in usable:
                _, _, bbox = item
                x0, y0, x1, y1 = bbox
                width = x1 - x0
                center = (x0 + x1) / 2.0

                if (
                    width >= page_width * 0.68
                    or (x0 <= page_width * 0.08 and x1 >= page_width * 0.92)
                ):
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

            # El inicio de la zona de columnas evita mandar un título de ancho
            # completo al final de la página.
            if left_items and right_items:
                column_start_y = min(
                    item[2][1]
                    for item in left_items + right_items
                )
            else:
                column_start_y = float("inf")

            ordered = []

            # Todo lo que precede claramente al comienzo de las columnas.
            leading_full = [
                item for item in full_width
                if item[2][3] <= column_start_y + 8
            ]
            middle_full = [
                item for item in full_width
                if item not in leading_full
            ]

            ordered.extend(sorted(leading_full, key=_box_sort_key))

            if left_items or right_items:
                # Para el cuerpo académico estándar: columna izquierda completa
                # y luego columna derecha completa.
                if middle_full:
                    # Si hay una figura/tabla de ancho completo en medio, usarla
                    # como ancla y dividir el contenido de columnas por su y0.
                    remaining_left = list(left_items)
                    remaining_right = list(right_items)

                    for full_item in middle_full:
                        full_bbox = _safe_box_bbox(full_item[0])
                        if full_bbox is None:
                            continue
                        full_y0 = full_bbox[1]

                        left_before = [
                            item for item in remaining_left
                            if item[2][1] < full_y0
                        ]
                        right_before = [
                            item for item in remaining_right
                            if item[2][1] < full_y0
                        ]

                        # No se consume el contenido de una columna antes de
                        # tiempo si todavía no existe suficiente evidencia de
                        # que el ancla corta ambas columnas.
                        if left_before or right_before:
                            ordered.extend(left_before)
                            ordered.extend(right_before)
                            remaining_left = [
                                item for item in remaining_left
                                if item not in left_before
                            ]
                            remaining_right = [
                                item for item in remaining_right
                                if item not in right_before
                            ]

                        ordered.append(full_item)

                    ordered.extend(remaining_left)
                    ordered.extend(remaining_right)
                else:
                    ordered.extend(left_items)
                    ordered.extend(right_items)
            else:
                ordered.extend(middle_full)

            # Cualquier caja no cubierta por la clasificación anterior se añade
            # al final en su orden espacial, evitando pérdida de contenido.
            used_ids = {id(item) for item in ordered}
            leftovers = [
                item for item in usable
                if id(item) not in used_ids
            ]
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

            # Las cajas de page_boxes normalmente ya contienen sus separadores
            # Markdown. Solo añadimos un salto cuando el segmento anterior no
            # termina en whitespace.
            rebuilt = ""
            for segment in segments:
                if not rebuilt:
                    rebuilt = segment
                    continue
                if not rebuilt.endswith(("\n", " ", "\t")) and not segment.startswith("\n"):
                    rebuilt += "\n\n"
                rebuilt += segment
            cleaned = rebuilt

    # Footnotes: se eliminan de su posición física y se colocan al final de la
    # página, nunca en medio de un párrafo o entre dos columnas.
    footnote_blocks = []
    for box, pos in sorted(
        footnotes,
        key=lambda item: (
            _footnote_y_key(item),
            int(item[0].get("index", 0)),
        )
    ):
        note = _normalize_footnote_markdown(text[pos[0]:pos[1]])
        if note:
            footnote_blocks.append(note)

    cleaned = cleaned.strip()
    if footnote_blocks:
        cleaned += "\n\n" + "\n\n".join(footnote_blocks)

    return cleaned.strip()


def normalize_page_chunk_layout(
    page: dict,
    page_width: float,
    page_number: int,
) -> dict:
    """
    Normaliza una página manteniendo su frontera física.

    Esto es deliberadamente anterior a la unión de páginas: una nota al pie
    no puede saltar a la mitad del párrafo de la página siguiente.
    """
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

    # Primero se intenta corregir el orden de columnas. Si no hay dos columnas,
    # solo se retiran las notas al pie de su posición original.
    if _detect_two_columns_from_page_boxes(page, page_width):
        corrected = _reorder_two_column_page(page, page_width)
    else:
        corrected = _remove_ranges_from_text(text, footnote_ranges)

        footnote_blocks = []
        for box, pos in sorted(
            footnotes,
            key=lambda item: (
                _footnote_y_key(item),
                int(item[0].get("index", 0)),
            )
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


def extract_markdown_and_images(
    doc: fitz.Document,
    output_dir: Path
):
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    pages = pymupdf4llm.to_markdown(
        doc,
        page_chunks=True,
        write_images=True,
        image_path=str(output_dir)
    )

    if isinstance(pages, str):
        return pages

    corrected_pages = []
    for page_number, page in enumerate(pages, start=1):
        corrected_pages.append(
            normalize_page_chunk_layout(
                page,
                page_width=doc[page_number - 1].rect.width,
                page_number=page_number,
            )
        )

    return corrected_pages


def add_page_markers(
    markdown_pages
):

    if isinstance(markdown_pages, str):
        return markdown_pages

    output = []

    for page_number, page in enumerate(
        markdown_pages,
        start=1
    ):

        if isinstance(page, dict):

            text = page.get(
                "text",
                ""
            )

        else:

            text = str(page)

        output.append(
            f"<!-- PAGE:{page_number} -->"
        )

        output.append(text)

    return "\n\n".join(output)


# ============================================================
# LIMPIEZA DEL MARKDOWN
# ============================================================

def remove_obvious_editorial_noise(
    text: str
) -> str:

    if not text:
        return ""

    lines = text.splitlines()
    cleaned = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            cleaned.append(line)
            continue

        if re.fullmatch(
            r"<!--\s*PAGE:\d+\s*-->",
            stripped,
            re.IGNORECASE
        ):
            cleaned.append(line)
            continue

        # Números de página aislados.
        if is_page_number(stripped):
            continue

        # Artefactos de producción/editoriales que no pertenecen al texto.
        if is_editorial_artifact_line(stripped):
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


def remove_residual_editorial_lines(
    text: str,
    profile: DocumentLayoutProfile
) -> str:
    """
    Segunda barrera de seguridad después de pymupdf4llm.

    Solo elimina un header/footer repetitivo cuando aparece
    como una línea prácticamente independiente dentro de su página.

    NUNCA elimina un fragmento editorial incrustado dentro de
    una línea que también contiene contenido académico.
    """

    if not text:
        return ""

    repeated = set(
        profile.repeated_headers
        + profile.repeated_footers
    )

    if not repeated:
        return text

    cleaned = []

    current_page = 1
    page_lines = []

    def flush_page(lines):

        if not lines:
            return []

        result = []

        non_empty_indices = [
            i
            for i, line in enumerate(lines)
            if line.strip()
        ]

        first_indices = set(
            non_empty_indices[:3]
        )

        last_indices = set(
            non_empty_indices[-3:]
        )

        for i, line in enumerate(lines):

            stripped = line.strip()

            if not stripped:
                result.append(line)
                continue

            if (
                i in first_indices
                or i in last_indices
            ):

                normalized = normalize_whitespace(
                    stripped
                )

                if any(
                    normalized == value
                    for value in repeated
                ):
                    continue

                if is_page_number(
                    stripped
                ):
                    continue

            result.append(line)

        return result

    for line in text.splitlines():

        stripped = line.strip()

        if re.fullmatch(
            r"<!--\s*PAGE:\d+\s*-->",
            stripped,
            re.IGNORECASE
        ):

            cleaned.extend(
                flush_page(
                    page_lines
                )
            )

            page_lines = []

            cleaned.append(line)

            continue

        page_lines.append(line)

    cleaned.extend(
        flush_page(
            page_lines
        )
    )

    return "\n".join(
        cleaned
    )

def preprocess_raw_markdown(
    text: str
) -> str:

    if not text:
        return ""

    # normalizar saltos Windows
    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # eliminar espacios al final
    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text
    )

    # excesivos saltos
    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text
    )

    return text.strip()


def is_affiliation_or_meta(
    text: str
) -> bool:

    s = text.strip()

    if not s:
        return False

    patterns = [
        r"\breceived\b",
        r"\baccepted\b",
        r"\bsubmitted\b",
        r"\bdoi\b",
        r"\bcorresponding author\b",
        r"\bauthor information\b",
        r"\baffiliation\b",
    ]

    return any(
        re.search(
            pattern,
            s,
            re.IGNORECASE
        )
        for pattern in patterns
    ) or looks_like_email(s)


def clean_and_join_broken_paragraphs(
    text: str
) -> str:

    """
    Reconstrucción moderada de líneas partidas.

    NO intenta "entender" el artículo.

    Su objetivo es eliminar artefactos típicos de extracción
    como:

        This is a para-
        graph that was
        broken across
        several lines.

    sin destruir headings, tablas o Markdown.
    """

    lines = text.splitlines()

    output = []

    for i, line in enumerate(lines):

        current = line.rstrip()

        if not current.strip():
            output.append("")
            continue

        # ----------------------------------------------------
        # Nunca modificar:
        #   - page markers
        #   - headings
        #   - listas
        #   - tablas
        #   - HTML
        #   - imágenes
        # ----------------------------------------------------

        stripped = current.strip()

        if stripped.startswith(
            "<!-- PAGE:"
        ):
            output.append(current)
            continue

        if stripped.startswith("#"):
            output.append(current)
            continue

        if stripped.startswith(
            ("-", "*", ">", "|", "<")
        ):
            output.append(current)
            continue

        # ----------------------------------------------------
        # guión de palabra partido
        # ----------------------------------------------------

        if current.rstrip().endswith("-"):

            if i + 1 < len(lines):

                next_line = lines[i + 1].strip()

                if (
                    next_line
                    and re.match(
                        r"^[a-záéíóúñü]",
                        next_line,
                        re.IGNORECASE
                    )
                ):

                    current = (
                        current.rstrip()[:-1]
                        + next_line
                    )

                    lines[i + 1] = ""

        output.append(current)

    return "\n".join(output)


def optimize_markdown_for_mobile(
    text: str
) -> str:

    if not text:
        return ""

    text = text.replace(
        "\r\n",
        "\n"
    )

    # ----------------------------------------------
    # espacios excesivos
    # ----------------------------------------------

    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text
    )

    # ----------------------------------------------
    # no más de 2 líneas vacías consecutivas
    # ----------------------------------------------

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text
    )

    # ----------------------------------------------
    # headings
    # ----------------------------------------------

    text = re.sub(
        r"\n*(#{1,6}[^\n]+)\n*",
        r"\n\n\1\n\n",
        text
    )

    # ----------------------------------------------
    # page markers
    # ----------------------------------------------

    text = re.sub(
        r"\n*(<!-- PAGE:\d+ -->)\n*",
        r"\n\n\1\n\n",
        text
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
    flags=re.IGNORECASE
)

    # ----------------------------------------------
    # tablas
    # ----------------------------------------------

    text = re.sub(
        r"\n{3,}(\|)",
        "\n\n\\1",
        text
    )

    text = re.sub(
        r"(\|[^\n]+)\n{3,}",
        "\\1\n\n",
        text
    )

    return text.strip()


def postprocess_markdown(
    text: str
) -> str:

    text = optimize_markdown_for_mobile(
        text
    )

    return text


# ============================================================
# DETECCIÓN DE IDIOMA
# ============================================================

def detect_language(
    text: str
) -> str:

    sample = text[:8000].lower()

    spanish_markers = [
        " el ",
        " la ",
        " los ",
        " las ",
        " de ",
        " que ",
        " para ",
        " una ",
        " un ",
        " y ",
    ]

    english_markers = [
        " the ",
        " of ",
        " and ",
        " that ",
        " for ",
        " with ",
        " this ",
        " are ",
        " is ",
    ]

    spanish_score = sum(
        sample.count(x)
        for x in spanish_markers
    )

    english_score = sum(
        sample.count(x)
        for x in english_markers
    )

    if spanish_score > english_score * 1.2:
        return "Spanish"

    return "English"


# ============================================================
# TABLAS
# ============================================================

def is_markdown_table_separator(
    line: str
) -> bool:

    stripped = line.strip()

    if "|" not in stripped:
        return False

    cells = [
        cell.strip()
        for cell in stripped.strip("|").split("|")
    ]

    if len(cells) < 2:
        return False

    return all(
        re.fullmatch(
            r":?-{2,}:?",
            cell
        )
        for cell in cells
    )


def is_markdown_table_row(
    line: str
) -> bool:

    stripped = line.strip()

    return (
        stripped.startswith("|")
        and stripped.endswith("|")
        and stripped.count("|") >= 2
    )


def isolate_tables(
    text: str
):

    if not text:
        return text, {}

    tables = {}

    lines = text.splitlines()

    output = []

    counter = 0
    i = 0

    while i < len(lines):

        line = lines[i]

        # ----------------------------------------------------
        # Posible inicio de tabla
        # ----------------------------------------------------

        if (
            i + 1 < len(lines)
            and is_markdown_table_row(line)
            and is_markdown_table_separator(
                lines[i + 1]
            )
        ):

            table_lines = [
                line,
                lines[i + 1]
            ]

            i += 2

            while i < len(lines):

                current = lines[i]

                if not is_markdown_table_row(
                    current
                ):
                    break

                table_lines.append(
                    current
                )

                i += 1

            key = (
                f"@@TABLE_{counter}@@"
            )

            tables[key] = "\n".join(
                table_lines
            )

            output.append(
                key
            )

            counter += 1

            continue

        output.append(line)

        i += 1

    return (
        "\n".join(output),
        tables
    )


async def translate_single_table(
    table_md: str,
    model: Optional[str] = None
) -> str:
    """Traduce una tabla completa sin permitir que el modelo la convierta en prosa."""
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

    translated = await translate_chunk(
        instruction,
        0,
        1,
    )

    translated = translated.strip()
    translated = re.sub(
        r"^```(?:markdown)?\s*|\s*```$",
        "",
        translated,
        flags=re.IGNORECASE
    ).strip()

    table_lines = [
        line.strip()
        for line in translated.splitlines()
        if line.strip().startswith("|")
        and line.strip().endswith("|")
    ]

    if len(table_lines) < 2:
        print("[TABLE] Respuesta inválida; se conserva la tabla original.")
        return table_md

    separator_index = next(
        (
            i for i, line in enumerate(table_lines)
            if is_markdown_table_separator(line)
        ),
        None
    )

    if separator_index != 1:
        print("[TABLE] Estructura alterada; se conserva la tabla original.")
        return table_md

    header_columns = table_lines[0].count("|")
    if header_columns < 3:
        return table_md

    if any(
        line.count("|") != header_columns
        for line in table_lines
    ):
        print("[TABLE] Número de columnas alterado; se conserva la tabla original.")
        return table_md

    return "\n".join(table_lines)


async def translate_tables(
    tables: dict,
    model: Optional[str] = None
) -> dict:
    """Traduce las tablas aisladas una por una y conserva las fallidas."""
    if not tables:
        return tables

    translated = {}

    for key, table_md in tables.items():
        print(f"[TABLE] Traduciendo {key}...")
        translated[key] = await translate_single_table(
            table_md,
            model
        )

    return translated


def restore_tables(
    text: str,
    tables: dict
):

    if not tables:
        return text

    for key, value in tables.items():
        text = text.replace(
            key,
            "\n\n" + value + "\n\n"
        )

    return text


# ============================================================
# CHUNKING
# ============================================================

def chunk_markdown(
    text: str,
    max_chars: int = MAX_CHARS_PER_CHUNK
):

    if len(text) <= max_chars:
        return [text]

    sections = re.split(
        r"(\n\s*\n)",
        text
    )

    chunks = []
    current = ""

    for section in sections:

        if (
            len(current)
            + len(section)
            <= max_chars
        ):

            current += section

            continue

        if current.strip():
            chunks.append(
                current.strip()
            )

        # seção individual maior que limite
        if len(section) > max_chars:

            start = 0

            while start < len(section):

                end = start + max_chars

                chunks.append(
                    section[start:end]
                )

                start = end

            current = ""

        else:

            current = section

    if current.strip():
        chunks.append(
            current.strip()
        )

    return chunks


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

Conservar:

# headings
## headings
### headings
listas
tablas
links
imágenes
HTML
page markers

Los marcadores:

<!-- PAGE:N -->

son estructurales.

Los tokens `@@TABLE_N@@` son marcadores internos de tablas.
NO los traduzcas, NO los reformatees y NO los elimines.

NO los traduzcas.
NO los elimines.
NO los dupliques.
NO los cambies.

IMPORTANTE SOBRE LA PRIMERA PÁGINA:

Si el texto contiene título, autores o afiliaciones,
tradúcelos/conserva su contenido desde la primera palabra.

No asumas que son basura editorial simplemente porque aparecen
antes del abstract.

IMPORTANTE SOBRE REFERENCIAS:

Las referencias bibliográficas forman parte del documento.
No las resumas ni las elimines.

Conserva cada referencia como una unidad independiente.

NO combines dos referencias.
NO dividas una referencia en varias.
NO cambies el orden de las referencias.
NO inventes numeración.
NO elimines autores, años, títulos, revistas, volúmenes,
páginas, DOI ni URLs.

Si aparecen marcadores HTML, anchors o enlaces internos
relacionados con las referencias, consérvalos exactamente.

IMPORTANTE SOBRE CITAS:

No cambies las citas bibliográficas del cuerpo.

Por ejemplo:

(Smith, 2020)
(Smith et al., 2020)
[12]
[12, 13]

deben conservar su contenido y formato esencial.

No conviertas citas en explicaciones.
No elimines citas.

Los nombres de autores y títulos bibliográficos deben conservarse
según corresponda al original, salvo que el contexto exija
traducir un título para mantener coherencia con la traducción.

DOI Y URL:

Nunca inventes, modifiques ni traduzcas un DOI o URL.

TABLAS:

No conviertas una tabla en prosa.
Mantén su estructura.

RESULTADO:

Devuelve únicamente la traducción.
No agregues comentarios.
No agregues introducciones.
No digas "Aquí está la traducción".
"""


# ============================================================
# DEEPSEEK — TRADUCCIÓN ASÍNCRONA CON POOL DE 6 CLAVES
# ============================================================

async def translate_with_deepseek(
    text: str,
    api_key: str,
    first_chunk: bool = False,
    max_retries: int = 3
) -> str:
    """Traduce un bloque usando una API key concreta."""
    if not api_key:
        raise RuntimeError("No hay una API key de DeepSeek disponible.")

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
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    last_error = None

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    DEEPSEEK_BASE_URL,
                    headers=headers,
                    json=payload,
                )

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
    """Traduce un chunk. model se conserva solo por compatibilidad."""
    key = api_key or DEEPSEEK_API_KEYS[index % len(DEEPSEEK_API_KEYS)]

    print(
        f"[TRANSLATION] DeepSeek chunk {index + 1}/{total} "
        f"(key {DEEPSEEK_API_KEYS.index(key) + 1})"
    )

    result = await translate_with_deepseek(
        text,
        api_key=key,
        first_chunk=(index == 0),
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
    """Worker persistente: cuando termina un chunk toma el siguiente."""
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return

        index, chunk = item
        try:
            print(
                f"[TRANSLATION] Worker {worker_id} → "
                f"chunk {index + 1}/{total}"
            )
            results[index] = await translate_chunk(
                chunk,
                index,
                total,
                api_key=api_key,
            )
        except Exception as exc:
            results[index] = exc
        finally:
            queue.task_done()


async def translate_markdown(
    markdown: str,
    model: Optional[str] = None,
):
    """
    Traduce todos los chunks en paralelo usando hasta 6 API keys.

    El orden final siempre coincide con el orden original del Markdown,
    independientemente de qué request termine primero.
    """
    chunks = chunk_markdown(markdown)

    if not chunks:
        return ""

    if not DEEPSEEK_API_KEYS:
        raise RuntimeError(
            "No hay ninguna DEEPSEEK_API_KEY_1..._6 configurada en .env."
        )

    worker_count = min(
        TRANSLATION_CONCURRENCY,
        len(DEEPSEEK_API_KEYS),
        len(chunks),
    )

    print(
        f"[TRANSLATION] {len(chunks)} chunks | "
        f"{worker_count} workers / API keys"
    )

    queue = asyncio.Queue()
    results: list[Optional[str] | Exception] = [None] * len(chunks)

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

    await asyncio.gather(*workers)

    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise errors[0]

    translated_results = [
        result
        for result in results
        if isinstance(result, str)
    ]

    if len(translated_results) != len(chunks):
        raise RuntimeError(
            "La traducción terminó sin producir todos los chunks."
        )

    return "\n\n".join(translated_results)


# ============================================================
# IMÁGENES
# ============================================================

def convert_local_images_to_base64(
    markdown: str,
    image_dir: Path
):

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

        mime = image_extensions.get(
            image_path.suffix.lower()
        )

        if not mime:
            continue

        try:

            data = image_path.read_bytes()

            encoded = base64.b64encode(
                data
            ).decode("ascii")

            data_uri = (
                f"data:{mime};base64,{encoded}"
            )

            markdown = markdown.replace(
                str(image_path),
                data_uri
            )

            markdown = markdown.replace(
                image_path.name,
                data_uri
            )

        except Exception as e:

            print(
                f"[IMAGE] Error: "
                f"{image_path}: {e}"
            )

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


def find_references_start(
    lines: list[str]
) -> Optional[int]:

    for index, line in enumerate(lines):

        if REFERENCE_HEADINGS.match(
            line.strip()
        ):
            return index

    return None


def looks_like_reference_start(
    line: str
) -> bool:

    s = line.strip()

    if not s:
        return False

    # --------------------------------------------------------
    # Referencias numeradas
    # --------------------------------------------------------

    if re.match(
        r"^(?:\[\d+\]|\d+[.)])\s+",
        s
    ):
        return True

    # --------------------------------------------------------
    # Autor + año
    # --------------------------------------------------------

    if re.search(
        r"\b(?:19|20)\d{2}[a-z]?\s*\)",
        s[:300]
    ):

        if re.match(
            r"^[A-ZÁÉÍÓÚÑ]"
            r"[A-Za-zÁÉÍÓÚÑáéíóúñü'’\-]+",
            s
        ):
            return True

    return False


def split_reference_entries(
    lines: list[str]
) -> list[str]:

    entries = []

    current = []

    for line in lines:

        stripped = line.strip()

        if not stripped:

            if current:
                entries.append(
                    "\n".join(
                        current
                    ).strip()
                )

                current = []

            continue

        if (
            current
            and looks_like_reference_start(
                stripped
            )
        ):

            entries.append(
                "\n".join(
                    current
                ).strip()
            )

            current = [
                stripped
            ]

            continue

        current.append(
            stripped
        )

    if current:

        entries.append(
            "\n".join(
                current
            ).strip()
        )

    return [
        entry
        for entry in entries
        if entry.strip()
    ]


def detect_existing_reference_number(
    reference: str
):

    match = re.match(
        r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s+",
        reference
    )

    if not match:
        return None

    return int(
        match.group(1)
        or match.group(2)
    )


def number_references(
    markdown: str
) -> str:
    """
    Numera las referencias bibliográficas sin crear todavía
    HTML ni enlaces.

    Esto se ejecuta ANTES de la traducción.
    """

    if not markdown:
        return markdown

    lines = markdown.splitlines()

    start = find_references_start(
        lines
    )

    if start is None:
        return markdown

    reference_lines = lines[
        start + 1:
    ]

    references = split_reference_entries(
        reference_lines
    )

    if not references:
        return markdown

    numbered = []

    for index, reference in enumerate(
        references,
        start=1
    ):

        reference = re.sub(
            r"^\s*(?:\[\d+\]|\d+[.)])\s+",
            "",
            reference
        )

        numbered.append(
            f"[{index}] {reference}"
        )

    output = []

    output.extend(
        lines[:start + 1]
    )

    output.append("")

    output.extend(
        numbered
    )

    return "\n".join(
        output
    )


def add_document_top_anchor(
    markdown: str
) -> str:

    if not markdown:
        return markdown

    if '<a id="top"></a>' in markdown:
        return markdown

    return (
        '<a id="top"></a>\n\n'
        + markdown
    )


def index_references(
    markdown: str
) -> str:
    """
    Después de la traducción:

      - convierte las referencias en anchors
      - convierte citas [n] en links
      - mantiene la numeración
      - agrega retorno al inicio

    No modifica citas autor-año.
    """

    if not markdown:
        return markdown

    markdown = add_document_top_anchor(
        markdown
    )

    lines = markdown.splitlines()

    start = find_references_start(
        lines
    )

    if start is None:
        return markdown

    before = lines[:start]

    reference_lines = lines[
        start + 1:
    ]

    references = split_reference_entries(
        reference_lines
    )

    if not references:
        return markdown

    normalized_references = []

    for index, reference in enumerate(
        references,
        start=1
    ):

        reference = re.sub(
            r"^\s*\[(\d+)\]\s+",
            "",
            reference
        )

        reference = re.sub(
            r"^\s*\d+[.)]\s+",
            "",
            reference
        )

        normalized_references.append(
            reference
        )

    # --------------------------------------------------------
    # Citas numéricas en el cuerpo
    # --------------------------------------------------------

    body = "\n".join(
        before
    )

    def replace_numeric_citation(
        match
    ):

        numbers_text = match.group(1)

        numbers = re.findall(
            r"\d+",
            numbers_text
        )

        links = []

        for number in numbers:

            number_int = int(
                number
            )

            if not (
                1
                <= number_int
                <= len(normalized_references)
            ):
                links.append(
                    f"[{number_int}]"
                )
                continue

            links.append(
                f'<a href="#ref-{number_int}">'
                f'[{number_int}]'
                f'</a>'
            )

        return ", ".join(
            links
        )

    body = re.sub(
        r"\[((?:\d+\s*,?\s*)+)\]",
        replace_numeric_citation,
        body
    )

    # --------------------------------------------------------
    # Reconstrucción
    # --------------------------------------------------------

    output = list(
        body.splitlines()
    )

    output.append("")
    output.append(
        lines[start]
    )
    output.append("")

    for index, reference in enumerate(
        normalized_references,
        start=1
    ):

        output.append(
            f'<a id="ref-{index}"></a>'
        )

        output.append(
            f"**{index}.** {reference}"
        )

        output.append(
            '<a href="#top">↩ Volver al texto</a>'
        )

        output.append("")

    return "\n".join(
        output
    )


def inject_internal_pdf_links(
    markdown: str
) -> str:

    """
    Compatibilidad con el pipeline.

    El índice real se genera mediante index_references().
    """

    return index_references(
        markdown
    )

# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

async def process_pdf(
    pdf_bytes: bytes,
    model: Optional[str] = None
):

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError(
            "El archivo recibido no parece ser un PDF."
        )

    # --------------------------------------------------------
    # HASH
    # --------------------------------------------------------

    effective_model = model or DEEPSEEK_MODEL

    key = cache_key(
        pdf_bytes,
        effective_model
    )

    cached = load_cache(key)

    if cached is not None:

        print(
            "[CACHE] Resultado encontrado."
        )

        return cached

    # --------------------------------------------------------
    # DIRECTORIO TEMPORAL
    # --------------------------------------------------------

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="psihub_"
        )
    )

    pdf_path = temp_dir / "source.pdf"

    image_dir = temp_dir / "images"

    pdf_path.write_bytes(
        pdf_bytes
    )

    # --------------------------------------------------------
    # ABRIR PDF
    # --------------------------------------------------------

    doc = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    # Algunos PDFs de editoriales incluyen una página web/editorial
    # antes del artículo. Eliminarla aquí evita que entren portada,
    # logos, iconos, métricas y metadatos en la traducción.
    remove_publisher_landing_page(doc)

    try:

        # ----------------------------------------------------
        # 1. ANALIZAR LAYOUT
        # ----------------------------------------------------

        print(
            "\n[PIPELINE] "
            "1/8 Analizando estructura..."
        )

        layout_profile = (
            analyze_document_layout(
                doc
            )
        )

        print_layout_profile(
            layout_profile
        )

        # ----------------------------------------------------
        # 2. LIMPIEZA FÍSICA
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "2/8 Limpiando elementos editoriales..."
        )

        removed = clean_pdf_using_layout(
            doc,
            layout_profile
        )

        print(
            f"[PDF CLEAN] "
            f"Elementos eliminados: {removed}"
        )

        # ----------------------------------------------------
        # 3. EXTRACCIÓN MARKDOWN
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "3/8 Extrayendo Markdown..."
        )

        raw_markdown = (
            extract_markdown_and_images(
                doc,
                image_dir
            )
        )

        raw_markdown = add_page_markers(
            raw_markdown
        )

        # ----------------------------------------------------
        # 4. LIMPIEZA MARKDOWN
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "4/8 Limpiando Markdown..."
        )

        raw_markdown = (
    preprocess_raw_markdown(
        raw_markdown
    )
)

        raw_markdown = (
            remove_obvious_editorial_noise(
                raw_markdown
            )
        )

        raw_markdown = (
            remove_residual_editorial_lines(
                raw_markdown,
                layout_profile
            )
        )

        raw_markdown = (
            clean_and_join_broken_paragraphs(
                raw_markdown
            )
        )

        raw_markdown = (
            optimize_markdown_for_mobile(
                raw_markdown
            )
        )

        # ----------------------------------------------------
        # 5. IDIOMA
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "5/8 Detectando idioma..."
        )

        source_language = detect_language(
            raw_markdown
        )

        print(
            f"[LANGUAGE] "
            f"{source_language}"
        )

        # --------------------------------------------------------
        # 6. TABLAS + REFERENCIAS
        # --------------------------------------------------------

        print(
            "[PIPELINE] "
            "6/8 Preparando tablas y referencias..."
        )

        raw_markdown = number_references(
            raw_markdown
        )

        markdown_for_translation, tables = (
            isolate_tables(
                raw_markdown
            )
        )

        tables = await translate_tables(
            tables,
            model
        )
        # ----------------------------------------------------
        # 7. TRADUCCIÓN
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "7/8 Traduciendo..."
        )

        translated_markdown = (
            await translate_markdown(
                markdown_for_translation,
                model
            )
        )

        # ----------------------------------------------------
        # RESTAURAR TABLAS
        # ----------------------------------------------------

        translated_markdown = (
            restore_tables(
                translated_markdown,
                tables
            )
        )

        # ----------------------------------------------------
        # 8. POSTPROCESADO
        # ----------------------------------------------------

        print(
            "[PIPELINE] "
            "8/8 Optimizando lectura..."
        )

        translated_markdown = (
            postprocess_markdown(
                translated_markdown
            )
        )

        translated_markdown = (
            convert_local_images_to_base64(
                translated_markdown,
                image_dir
            )
        )

        translated_markdown = index_references(
    translated_markdown
)

        # ----------------------------------------------------
        # RESULTADO
        # ----------------------------------------------------

        result = {
            "success": True,
            "source_language": source_language,
            "model": effective_model,
            "markdown": translated_markdown,
            "original_markdown": raw_markdown,
            "layout_profile": asdict(
                layout_profile
            ),
            "page_count": doc.page_count,
        }

        save_cache(
            key,
            result
        )

        return result

    finally:

        doc.close()


# ============================================================
# PROCESAMIENTO DESDE URL
# ============================================================

async def process_url(
    url: str,
    model: Optional[str] = None
):

    print(
        f"[DOWNLOAD] {url}"
    )

    pdf_bytes = await download_pdf(
        url
    )

    return await process_pdf(
        pdf_bytes,
        model
    )


# ============================================================
# ENDPOINT: TRANSLATE
# ============================================================

@app.post(
    "/api/translate"
)
async def api_translate(
    request: TranslateRequest
):

    try:

        result = await process_url(
            request.url,
            request.model
        )

        return JSONResponse(
            content=result
        )

    except httpx.HTTPError as e:

        raise HTTPException(
            status_code=400,
            detail=f"Error descargando PDF: {e}"
        )

    except Exception as e:

        print(
            f"[ERROR] {type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# ENDPOINT: TRANSLATE FILE
# ============================================================

@app.post(
    "/api/translate-file"
)
async def api_translate_file(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None)
):

    try:

        pdf_bytes = await file.read()

        if not pdf_bytes.startswith(
            b"%PDF"
        ):
            raise HTTPException(
                status_code=400,
                detail="El archivo no es un PDF válido."
            )

        result = await process_pdf(
            pdf_bytes,
            model
        )

        return JSONResponse(
            content=result
        )

    except HTTPException:
        raise

    except Exception as e:

        print(
            f"[ERROR] {type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# ENDPOINT: CHECK PDF
# ============================================================

@app.post(
    "/api/check-pdf"
)
async def api_check_pdf(
    file: UploadFile = File(...)
):

    pdf_bytes = await file.read()

    if not pdf_bytes.startswith(
        b"%PDF"
    ):
        raise HTTPException(
            status_code=400,
            detail="No es un PDF válido."
        )

    try:

        doc = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        try:

            profile = (
                analyze_document_layout(
                    doc
                )
            )

            return JSONResponse(
                content={
                    "success": True,
                    "layout_profile": asdict(
                        profile
                    ),
                }
            )

        finally:

            doc.close()

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
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

async def gradio_translate_url(
    url,
    model_name=None
):

    if not url or not url.strip():

        return (
            "Introducí una URL de un PDF.",
            ""
        )

    try:

        selected_model = (
            model_name
            if model_name
            else None
        )

        result = await process_url(
            url.strip(),
            selected_model
        )

        profile = result.get(
            "layout_profile",
            {}
        )

        info = (
            f"**Páginas:** "
            f"{result.get('page_count', '?')}\n\n"
            f"**Idioma detectado:** "
            f"{result.get('source_language', '?')}\n\n"
            f"**Columnas estimadas:** "
            f"{profile.get('likely_columns', '?')}\n\n"
            f"**Elementos editoriales eliminados:** "
            f"{profile.get('elements_removed_estimate', 0)}"
        )

        return (
            result["markdown"],
            info
        )

    except Exception as e:

        return (
            "",
            f"Error: {e}"
        )


async def gradio_translate_file(
    file,
    model_name=None
):

    if file is None:

        return (
            "Subí un PDF.",
            ""
        )

    try:

        # Gradio puede entregar un objeto con .name
        # o directamente una ruta.

        if hasattr(file, "name"):
            file_path = file.name
        else:
            file_path = str(file)

        pdf_bytes = Path(
            file_path
        ).read_bytes()

        result = await process_pdf(
            pdf_bytes,
            model_name or None
        )

        profile = result.get(
            "layout_profile",
            {}
        )

        info = (
            f"**Páginas:** "
            f"{result.get('page_count', '?')}\n\n"
            f"**Idioma:** "
            f"{result.get('source_language', '?')}\n\n"
            f"**Columnas:** "
            f"{profile.get('likely_columns', '?')}\n\n"
            f"**Elementos eliminados:** "
            f"{profile.get('elements_removed_estimate', 0)}"
        )

        return (
            result["markdown"],
            info
        )

    except Exception as e:

        return (
            "",
            f"Error: {e}"
        )


# ============================================================
# UI GRADIO
# ============================================================

with gr.Blocks(
    title="PsiHub Reader"
) as demo:

    gr.Markdown(
        """
# PsiHub Reader

### PDF académico → traducción limpia para lectura digital

El documento se analiza estructuralmente antes de traducirse
para evitar encabezados, pies y números de página innecesarios
sin eliminar contenido académico legítimo.
"""
    )

    with gr.Tab("URL"):

        url_input = gr.Textbox(
            label="URL del PDF",
            placeholder="https://..."
        )

        translate_url_button = gr.Button(
            "Traducir PDF",
            variant="primary"
        )

        url_output = gr.Markdown(
            label="Traducción"
        )

        url_info = gr.Markdown(
            label="Información"
        )

        translate_url_button.click(
            fn=gradio_translate_url,
            inputs=[url_input],
            outputs=[
                url_output,
                url_info
            ]
        )

    with gr.Tab("Archivo"):

        file_input = gr.File(
            label="PDF",
            file_types=[".pdf"]
        )

        translate_file_button = gr.Button(
            "Traducir PDF",
            variant="primary"
        )

        file_output = gr.Markdown(
            label="Traducción"
        )

        file_info = gr.Markdown(
            label="Información"
        )

        translate_file_button.click(
            fn=gradio_translate_file,
            inputs=[file_input],
            outputs=[
                file_output,
                file_info
            ]
        )


# ============================================================
# MONTAR GRADIO EN FASTAPI
# ============================================================

app = gr.mount_gradio_app(
    app,
    demo,
    path="/"
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8000"
        )
    )

    host = os.getenv(
        "HOST",
        "0.0.0.0"
    )

    uvicorn.run(
        app,
        host=host,
        port=port
    )