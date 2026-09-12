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

from google import genai
from google.genai import types

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

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com/chat/completions"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

MODEL_SWITCH = int(os.getenv("MODELOGEMINI", "0"))

TRANSLATION_DELAY = float(os.getenv("TRANSLATION_DELAY", "1"))

CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_CHARS_PER_CHUNK = int(
    os.getenv("MAX_CHARS_PER_CHUNK", "18000")
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT", "120")
)


# ============================================================
# CLIENTE GEMINI
# ============================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={
                "timeout": REQUEST_TIMEOUT * 1000
            }
        )
    except Exception as e:
        print(f"[Gemini] Error inicializando cliente: {e}")


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

    Se utilizan líneas y no bloques completos para evitar que
    un bloque que contenga simultáneamente contenido académico
    y elementos editoriales termine siendo eliminado completo.
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

                line_rect = fitz.Rect(
                    x0,
                    y0,
                    x1,
                    y1
                )

                region = classify_region(
                    line_rect,
                    rect,
                    header_height,
                    footer_height
                )

                elements.append(
                    LayoutElement(
                        text=text,
                        normalized=normalize_editorial_text(text),
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
    Detecta elementos repetidos entre páginas.

    La repetición se calcula por cantidad de páginas distintas,
    no por cantidad de apariciones.

    Para documentos de más de dos páginas se exige que el elemento
    aparezca en al menos el 30% de las páginas, con un mínimo de 2.
    """

    pages_by_text = defaultdict(set)

    for element in elements:

        if element.region != region:
            continue

        normalized = element.normalized

        if not normalized:
            continue

        pages_by_text[normalized].add(
            element.page
        )

    if page_count <= 2:
        minimum_pages = 2
    else:
        minimum_pages = max(
            2,
            int(page_count * 0.30)
        )

    repeated = set()

    for normalized, pages in pages_by_text.items():

        if len(pages) >= minimum_pages:
            repeated.add(normalized)

    return repeated


def detect_page_numbers(elements):

    pages_with_numbers = set()

    for element in elements:

        if element.region != "footer":
            continue

        if is_page_number(element.text):
            pages_with_numbers.add(
                element.page
            )

    return pages_with_numbers


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

    HEADER_FRACTION = 0.08
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
    Limpieza física conservadora basada en líneas.

    Nunca elimina un bloque completo si dentro puede existir
    contenido académico legítimo.

    Se eliminan únicamente:
      - números de página claramente identificados
      - headers repetidos
      - footers repetidos

    La primera página queda protegida frente a headers repetidos.
    """

    removed = 0

    for page_number, page in enumerate(
        doc,
        start=1
    ):

        rect = page.rect

        page_dict = page.get_text("dict")

        redactions = []

        for block in page_dict.get(
            "blocks",
            []
        ):

            if block.get("type") != 0:
                continue

            for line in block.get(
                "lines",
                []
            ):

                spans = line.get(
                    "spans",
                    []
                )

                if not spans:
                    continue

                text = "".join(
                    span.get(
                        "text",
                        ""
                    )
                    for span in spans
                ).strip()

                if not text:
                    continue

                bbox = line.get(
                    "bbox"
                )

                if not bbox or len(bbox) != 4:
                    continue

                x0, y0, x1, y1 = bbox

                line_rect = fitz.Rect(
                    x0,
                    y0,
                    x1,
                    y1
                )

                region = classify_region(
                    line_rect,
                    rect,
                    profile.header_height,
                    profile.footer_height
                )

                element = LayoutElement(
                    text=text,
                    normalized=normalize_editorial_text(
                        text
                    ),
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    page=page_number,
                    width=x1 - x0,
                    height=y1 - y0,
                    region=region
                )

                should_remove, reason = (
                    should_remove_element(
                        element,
                        profile
                    )
                )

                if not should_remove:
                    continue

                redactions.append(
                    (
                        line_rect,
                        reason,
                        text
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

    return f"{digest}_{model_digest}"


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

def extract_markdown_and_images(
    doc: fitz.Document,
    output_dir: Path
):

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    markdown = pymupdf4llm.to_markdown(
        doc,
        page_chunks=True,
        write_images=True,
        image_path=str(output_dir)
    )

    return markdown


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

        # Nunca tocar page markers.
        if re.fullmatch(
            r"<!--\s*PAGE:\d+\s*-->",
            stripped,
            re.IGNORECASE
        ):
            cleaned.append(line)
            continue

        # Solo eliminar números de página aislados.
        if is_page_number(stripped):
            continue

        # NO eliminar copyright aquí.
        #
        # Puede formar parte de información editorial
        # legítima del documento y ya no tenemos coordenadas
        # para determinar si estaba realmente en el footer.

        cleaned.append(line)

    return "\n".join(
        cleaned
    )

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


def restore_tables(
    text: str,
    tables: dict
):

    if not tables:
        return text

    for key, value in tables.items():

        text = text.replace(
            key,
            value
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
# DEEPSEEK
# ============================================================

async def translate_with_deepseek(
    text: str,
    first_chunk: bool = False
) -> str:

    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY no está configurada."
        )

    user_prompt = ""

    if first_chunk:
        user_prompt += """
ESTE ES EL COMIENZO DEL DOCUMENTO.

Debes comenzar desde la primera palabra disponible.
Incluye título, autores y afiliaciones cuando estén presentes.

"""

    user_prompt += (
        "TRADUCE EL SIGUIENTE CONTENIDO:\n\n"
        + text
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": TRANSLATION_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "temperature": 0.1,
        "stream": False,
    }

    headers = {
        "Authorization": (
            f"Bearer {DEEPSEEK_API_KEY}"
        ),
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(
        REQUEST_TIMEOUT
    )

    async with httpx.AsyncClient(
        timeout=timeout
    ) as client:

        response = await client.post(
            DEEPSEEK_BASE_URL,
            headers=headers,
            json=payload,
        )

        response.raise_for_status()

        data = response.json()

    return (
        data["choices"][0]
        ["message"]
        ["content"]
    )


# ============================================================
# GEMINI
# ============================================================

async def translate_with_gemini(
    text: str,
    model: str,
    first_chunk: bool = False
) -> str:

    if gemini_client is None:
        raise RuntimeError(
            "GEMINI_API_KEY no está configurada "
            "o el cliente Gemini no pudo inicializarse."
        )

    user_prompt = ""

    if first_chunk:
        user_prompt += """
ESTE ES EL COMIENZO DEL DOCUMENTO.

Comienza desde la primera palabra disponible.
Incluye título, autores y afiliaciones.

"""

    user_prompt += (
        "TRADUCE EL SIGUIENTE CONTENIDO:\n\n"
        + text
    )

    def call_gemini():
        response = gemini_client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=TRANSLATION_SYSTEM_PROMPT,
                temperature=0.1,
            )
        )

        if not response or not response.text:
            raise RuntimeError(
                "Gemini devolvió una respuesta vacía."
            )

        return response.text

    return await asyncio.to_thread(
        call_gemini
    )


# ============================================================
# SELECTOR DE MODELO
# ============================================================

async def translate_chunk(
    text: str,
    index: int,
    total: int,
    model: Optional[str] = None
):

    first_chunk = index == 0

    if MODEL_SWITCH == 0:

        print(
            f"[TRANSLATION] "
            f"DeepSeek chunk {index + 1}/{total}"
        )

        result = await translate_with_deepseek(
            text,
            first_chunk=first_chunk
        )

    else:

        selected_model = (
            model
            or MODELS[
                min(
                    MODEL_SWITCH - 1,
                    len(MODELS) - 1
                )
            ]
        )

        print(
            f"[TRANSLATION] "
            f"Gemini {selected_model} "
            f"chunk {index + 1}/{total}"
        )

        result = await translate_with_gemini(
            text,
            selected_model,
            first_chunk=first_chunk
        )

    if TRANSLATION_DELAY > 0:
        await asyncio.sleep(
            TRANSLATION_DELAY
        )

    return result


# ============================================================
# TRADUCCIÓN COMPLETA
# ============================================================

async def translate_markdown(
    markdown: str,
    model: Optional[str] = None
):

    chunks = chunk_markdown(
        markdown
    )

    print(
        f"[TRANSLATION] "
        f"{len(chunks)} chunks"
    )

    translated_chunks = []

    for index, chunk in enumerate(
        chunks
    ):

        translated = await translate_chunk(
            chunk,
            index,
            len(chunks),
            model
        )

        translated_chunks.append(
            translated
        )

    return "\n\n".join(
        translated_chunks
    )


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

    effective_model = (
        model
        or (
            DEEPSEEK_MODEL
            if MODEL_SWITCH == 0
            else MODELS[
                min(
                    MODEL_SWITCH - 1,
                    len(MODELS) - 1
                )
            ]
        )
    )

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
        "deepseek": bool(
            DEEPSEEK_API_KEY
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "translation_backend": (
            "DeepSeek"
            if MODEL_SWITCH == 0
            else "Gemini"
        ),
    }


# ============================================================
# GRADIO
# ============================================================

async def gradio_translate_url(
    url,
    model_name
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
    model_name
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

        model_dropdown = gr.Dropdown(
            choices=MODELS,
            value=(
                MODELS[0]
                if MODEL_SWITCH != 0
                else None
            ),
            label="Modelo Gemini",
            allow_custom_value=False
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
            inputs=[
                url_input,
                model_dropdown
            ],
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

        file_model_dropdown = gr.Dropdown(
            choices=MODELS,
            value=(
                MODELS[0]
                if MODEL_SWITCH != 0
                else None
            ),
            label="Modelo Gemini"
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
            inputs=[
                file_input,
                file_model_dropdown
            ],
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
