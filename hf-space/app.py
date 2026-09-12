"""
PsiHub Reader — Servicio de traducción de PDFs académicos
Hugging Face Space (Gradio SDK + FastAPI)

Flujo:
  1. Recibe la URL de un PDF open access
  2. Descarga el PDF
  3. Elimina headers, footers y números de página editoriales
  4. Extrae Markdown estructurado + imágenes
  5. Traduce sección por sección
  6. Traduce tablas de forma aislada
  7. Inserta imágenes en base64
  8. Cachea resultados
"""

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
from typing import Optional

# En Windows CMD asegurar soporte UTF-8
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(
                encoding="utf-8",
                errors="replace"
            )
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(
                encoding="utf-8",
                errors="replace"
            )
    except Exception:
        pass


import httpx
import pymupdf
import pymupdf as fitz
import pymupdf4llm

# pyrefly: ignore [missing-import]
from google import genai
from google.genai import types

from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Form,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# pyrefly: ignore [missing-import]
import gradio as gr

import uvicorn
from dotenv import load_dotenv


# ══════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════

load_dotenv()
load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")


DEEPSEEK_API_KEY = os.environ.get(
    "DEEPSEEK_API_KEY",
    ""
).strip()

print(
    "DEBUG DEEPSEEK_API_KEY cargada: "
    + (
        f"Sí (termina en {DEEPSEEK_API_KEY[-4:]})"
        if DEEPSEEK_API_KEY
        else "NO (VACÍA)"
    )
)


# ══════════════════════════════════════════════════
# MOTOR DE TRADUCCIÓN
# ══════════════════════════════════════════════════

# 0 = DeepSeek
# 1 = Gemini
modelogemini = int(
    os.environ.get(
        "MODELOGEMINI",
        os.environ.get("MODELO_GEMINI", "0")
    )
)


# ──────────────────────────────────────────────────
# DeepSeek
# ──────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get(
    "DEEPSEEK_API_KEY",
    ""
).strip()

DEEPSEEK_MODEL = os.environ.get(
    "DEEPSEEK_MODEL",
    "deepseek-chat"
)

DEEPSEEK_BASE_URL = (
    "https://api.deepseek.com/chat/completions"
)


# ──────────────────────────────────────────────────
# Gemini
# ──────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    ""
).strip()

gemini_client = None

if GEMINI_API_KEY:
    gemini_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"timeout": 120000}
    )


MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]


# Pausa entre chunks
DELAY_BETWEEN_CHUNKS_SEC = 1.0


# ══════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════

CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


api = FastAPI(
    title="PsiHub Reader API",
    version="2.0.0"
)


api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


api.mount(
    "/files",
    StaticFiles(directory="cache"),
    name="files"
)


# ══════════════════════════════════════════════════
# MODELOS
# ══════════════════════════════════════════════════

class TranslateRequest(BaseModel):
    url: str
    paper_id: Optional[str] = None
    id: Optional[str] = None
    force: bool = False

    def get_paper_id(self) -> Optional[str]:
        return self.paper_id or self.id


class CheckRequest(BaseModel):
    url: str


# ══════════════════════════════════════════════════
# UTILIDADES GENERALES
# ══════════════════════════════════════════════════

def clean_paper_id(
    paper_id: Optional[str],
    fallback: str = ""
) -> str:
    """
    Sanitiza y normaliza el ID del paper.
    """

    if not paper_id:
        return fallback

    pid_str = paper_id.strip()

    if not pid_str:
        return fallback

    if pid_str.lower() in (
        "undefined",
        "null",
        "none",
        "0"
    ):
        return fallback

    return pid_str


def safe_id(paper_id: str) -> str:
    """
    Genera un nombre de archivo seguro.
    """

    return hashlib.sha256(
        paper_id.encode()
    ).hexdigest()[:24]


# ══════════════════════════════════════════════════
# DESCARGA DEL PDF
# ══════════════════════════════════════════════════

async def download_pdf(url: str) -> bytes:
    """
    Descarga el PDF siguiendo redirecciones.
    """

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60.0
    ) as client:

        try:
            resp = await client.get(url)

        except httpx.RequestError:
            raise HTTPException(
                status_code=502,
                detail=(
                    "DIRECT_UPLOAD_REQUIRED: "
                    "Este artículo requiere adjuntar "
                    "el archivo PDF directamente."
                )
            )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=(
                    "DIRECT_UPLOAD_REQUIRED: "
                    "Este artículo requiere adjuntar "
                    "el archivo PDF directamente."
                )
            )

        content_type = resp.headers.get(
            "content-type",
            ""
        ).lower()

        if (
            "text/html" in content_type
            and len(resp.content) < 50000
        ):
            raise HTTPException(
                status_code=502,
                detail=(
                    "DIRECT_UPLOAD_REQUIRED: "
                    "Este artículo requiere adjuntar "
                    "el archivo PDF directamente."
                )
            )

        return resp.content


# ══════════════════════════════════════════════════
# LIMPIEZA EDITORIAL DEL PDF
# ══════════════════════════════════════════════════

def normalize_editorial_text(text: str) -> str:
    """
    Normaliza un bloque para poder detectar headers/footers
    repetidos aunque cambien números de página u otros
    detalles menores.

    Ejemplo:

        Proceedings ... 128
        Proceedings ... 129

    se convierten en la misma firma.
    """

    text = text.strip()

    # Espacios
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    # Números -> marcador común
    text = re.sub(
        r"\b\d+\b",
        "#",
        text
    )

    # Separadores
    text = re.sub(
        r"\s*[-–—|]\s*",
        " - ",
        text
    )

    # Espacios alrededor de puntuación
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.lower().strip()


def is_page_number(text: str) -> bool:
    """
    Detecta números de página y variantes comunes.
    """

    s = re.sub(
        r"\s+",
        " ",
        text.strip()
    )

    patterns = [
        r"^\d+$",
        r"^[-–—]?\s*\d+\s*[-–—]?$",
        r"^page\s+\d+$",
        r"^p\.?\s*\d+$",
        r"^\d+\s+(?:of|de)\s+\d+$",
        r"^page\s+\d+\s+(?:of|de)\s+\d+$",
        r"^\d+\s*/\s*\d+$",
        r"^p\.?\s*\d+\s*/\s*\d+$",
    ]

    return any(
        re.fullmatch(
            pattern,
            s,
            re.IGNORECASE
        )
        for pattern in patterns
    )


def remove_headers_footers(doc: fitz.Document):
    """
    Elimina:

      - headers repetidos
      - footers repetidos
      - números de página

    de forma conservadora.

    IMPORTANTE:
    Un texto solo se elimina si además de haber sido
    identificado como editorial se encuentra físicamente
    dentro del margen superior/inferior correspondiente.

    Esto evita que una frase legítima del cuerpo del artículo
    sea eliminada simplemente porque coincide con un header.
    """

    if doc.page_count < 2:
        return

    # Margen relativo a la página.
    #
    # 0.06 = 6% superior/inferior.
    #
    # Para una página de 792 pt:
    # 0.06 ≈ 47.5 pt
    #
    HEADER_MARGIN = 0.06
    FOOTER_MARGIN = 0.06

    # Un texto debe aparecer en al menos 30% de las páginas
    # para ser considerado running header/footer.
    REPEAT_RATIO = 0.30

    threshold = max(
        2,
        int(doc.page_count * REPEAT_RATIO)
    )

    header_candidates: dict[str, int] = {}
    footer_candidates: dict[str, int] = {}

    # ──────────────────────────────────────────────
    # PRIMERA PASADA:
    # encontrar candidatos repetidos
    # ──────────────────────────────────────────────

    for page in doc:
        rect = page.rect

        blocks = page.get_text(
            "blocks",
            sort=True
        )

        for block in blocks:

            block_rect = fitz.Rect(
                block[:4]
            )

            text = block[4].strip()

            if not text:
                continue

            # Los números de página se procesan
            # independientemente.
            if is_page_number(text):
                continue

            normalized = normalize_editorial_text(
                text
            )

            if not normalized:
                continue

            # HEADER
            if (
                block_rect.y1
                <= rect.height * HEADER_MARGIN
            ):
                header_candidates[
                    normalized
                ] = (
                    header_candidates.get(
                        normalized,
                        0
                    ) + 1
                )

            # FOOTER
            elif (
                block_rect.y0
                >= rect.height * (
                    1 - FOOTER_MARGIN
                )
            ):
                footer_candidates[
                    normalized
                ] = (
                    footer_candidates.get(
                        normalized,
                        0
                    ) + 1
                )

    repeated_headers = {
        text
        for text, count
        in header_candidates.items()
        if count >= threshold
    }

    repeated_footers = {
        text
        for text, count
        in footer_candidates.items()
        if count >= threshold
    }

    print(
        f"  🧹 Headers repetidos detectados: "
        f"{len(repeated_headers)} | "
        f"Footers repetidos: "
        f"{len(repeated_footers)}",
        flush=True
    )

    if repeated_headers:
        print(
            "  ↳ Headers:",
            flush=True
        )

        for header in sorted(repeated_headers):
            print(
                f"     • {header}",
                flush=True
            )

    if repeated_footers:
        print(
            "  ↳ Footers:",
            flush=True
        )

        for footer in sorted(repeated_footers):
            print(
                f"     • {footer}",
                flush=True
            )

    # ──────────────────────────────────────────────
    # SEGUNDA PASADA:
    # eliminar únicamente lo inequívoco
    # ──────────────────────────────────────────────

    removed_count = 0

    for page_num, page in enumerate(
        doc,
        start=1
    ):

        rect = page.rect

        blocks = page.get_text(
            "blocks",
            sort=True
        )

        for block in blocks:

            block_rect = fitz.Rect(
                block[:4]
            )

            text = block[4].strip()

            if not text:
                continue

            normalized = normalize_editorial_text(
                text
            )

            should_remove = False
            reason = ""

            # ─────────────────────────────────────
            # 1. NÚMERO DE PÁGINA
            # ─────────────────────────────────────

            if (
                block_rect.y0
                >= rect.height * (
                    1 - FOOTER_MARGIN
                )
                and is_page_number(text)
            ):
                should_remove = True
                reason = "page number"

            # También permitimos número de página
            # en el header si la revista lo coloca arriba.
            elif (
                block_rect.y1
                <= rect.height * HEADER_MARGIN
                and is_page_number(text)
            ):
                should_remove = True
                reason = "page number (header)"

            # ─────────────────────────────────────
            # 2. HEADER REPETIDO
            # ─────────────────────────────────────

            elif (
                block_rect.y1
                <= rect.height * HEADER_MARGIN
                and normalized in repeated_headers
            ):
                should_remove = True
                reason = "repeated header"

            # ─────────────────────────────────────
            # 3. FOOTER REPETIDO
            # ─────────────────────────────────────

            elif (
                block_rect.y0
                >= rect.height * (
                    1 - FOOTER_MARGIN
                )
                and normalized in repeated_footers
            ):
                should_remove = True
                reason = "repeated footer"

            if should_remove:

                page.add_redact_annot(
                    block_rect,
                    fill=(1, 1, 1)
                )

                removed_count += 1

                print(
                    f"    🗑 Página {page_num}: "
                    f"{reason}: "
                    f"{text[:120]!r}",
                    flush=True
                )

        page.apply_redactions()

    print(
        f"  🧹 Elementos editoriales eliminados: "
        f"{removed_count}",
        flush=True
    )


# ══════════════════════════════════════════════════
# LIMPIEZA POST-EXTRACCIÓN
# ══════════════════════════════════════════════════

def remove_obvious_editorial_noise(
    text: str
) -> str:
    """
    Elimina únicamente basura editorial inequívoca
    que haya sobrevivido a la limpieza del PDF.

    NO elimina:

      - autores
      - afiliaciones
      - emails
      - DOI
      - referencias
      - títulos
      - nombres de revistas
    """

    if not text:
        return ""

    lines = text.splitlines()
    cleaned = []

    editorial_patterns = [

        # Copyright
        re.compile(
            r"^\s*copyright\s+"
            r"(?:©|\(c\))?\s*\d{4}",
            re.IGNORECASE
        ),

        re.compile(
            r"^\s*©\s*\d{4}",
            re.IGNORECASE
        ),

        # Algunas variantes editoriales muy evidentes
        re.compile(
            r"^\s*all rights reserved\.?\s*$",
            re.IGNORECASE
        ),
    ]

    for line in lines:

        stripped = line.strip()

        if not stripped:
            cleaned.append(line)
            continue

        # Número de página flotante
        if is_page_number(stripped):
            continue

        # Editorial inequívoco
        if any(
            pattern.search(stripped)
            for pattern in editorial_patterns
        ):
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


# ══════════════════════════════════════════════════
# EXTRACCIÓN MARKDOWN + IMÁGENES
# ══════════════════════════════════════════════════

def extract_markdown_and_images(
    pdf_bytes: bytes
) -> tuple[str, dict[str, str]]:

    """
    Convierte PDF a Markdown estructurado y extrae imágenes.
    """

    import shutil

    doc = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    # IMPORTANTE:
    # esto ocurre ANTES de pymupdf4llm.
    remove_headers_footers(doc)

    img_dir = tempfile.mkdtemp()

    images_b64: dict[str, str] = {}

    try:

        md_chunks = pymupdf4llm.to_markdown(
            doc,
            write_images=True,
            image_path=img_dir,
            page_chunks=True
        )

        full_md = []

        for chunk in md_chunks:

            page_num = (
                chunk
                .get("metadata", {})
                .get("page_number", 1)
            )

            page_text = chunk.get(
                "text",
                ""
            )

            full_md.append(
                f"\n\n<!-- PAGE:{page_num} -->\n\n"
                f"{page_text}"
            )

        md_text = "\n".join(
            full_md
        )

        # ─────────────────────────────────────────
        # IMÁGENES
        # ─────────────────────────────────────────

        for img_file in os.listdir(
            img_dir
        ):

            filepath = os.path.join(
                img_dir,
                img_file
            )

            if not os.path.isfile(filepath):
                continue

            # Ignorar fragmentos diminutos.
            #
            # Esto evita incorporar pequeños elementos
            # vectoriales o residuos como imágenes.
            if (
                os.path.getsize(filepath)
                < 10240
            ):
                continue

            with open(
                filepath,
                "rb"
            ) as f:
                img_data = f.read()

            ext = (
                img_file
                .split(".")[-1]
                .lower()
            )

            mime = (
                "image/jpeg"
                if ext in ("jpg", "jpeg")
                else f"image/{ext}"
            )

            b64 = base64.b64encode(
                img_data
            ).decode("utf-8")

            images_b64[
                img_file
            ] = (
                f"data:{mime};base64,{b64}"
            )

        return md_text, images_b64

    finally:

        doc.close()

        shutil.rmtree(
            img_dir,
            ignore_errors=True
        )


# ══════════════════════════════════════════════════
# IMÁGENES → BASE64
# ══════════════════════════════════════════════════

def replace_image_refs_with_base64(
    markdown: str,
    images: dict[str, str],
    final_pdf_url: str = ""
) -> str:

    """
    Reemplaza referencias de imágenes Markdown
    por Base64.

    Se ejecuta DESPUÉS de traducir para evitar enviar
    grandes cadenas Base64 a la IA.
    """

    used_images = set()

    def get_img_page(
        filename: str
    ) -> int:

        # Formatos habituales de pymupdf4llm
        patterns = [
            r"-(\d+)-\d+\.[^.]+$",
            r"-(\d+)\.[^.]+$",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                filename
            )

            if match:

                try:
                    return int(
                        match.group(1)
                    )
                except (ValueError, TypeError):
                    pass

        return 1

    def make_figure_block(
        alt: str,
        uri: str,
        filename: str
    ) -> str:

        p_num = get_img_page(
            filename
        )

        if final_pdf_url:

            pdf_target = (
                f"{final_pdf_url}"
                f"#page={p_num}"
            )

        else:

            pdf_target = (
                f"#page={p_num}"
            )

        btn_html = (
            '<a href="#" '
            'class="internal-pdf-link '
            'reader-pdf-page-btn" '
            f'data-url="{pdf_target}">'
            f'Ver en PDF original — Pág. {p_num}'
            '</a>'
        )

        return (
            "\n\n"
            f"![{alt}]({uri})"
            "\n\n"
            f"{btn_html}"
            "\n\n"
        )

    def replace_img_ref(match):

        alt = (
            match.group(1)
            or "Figura"
        )

        ref = match.group(2)

        for name, data_uri in images.items():

            if (
                ref in name
                or name in ref
                or os.path.basename(ref) == name
            ):

                used_images.add(name)

                return make_figure_block(
                    alt,
                    data_uri,
                    name
                )

        # Si la referencia no corresponde a una
        # imagen existente, no dejamos un enlace roto.
        return ""

    markdown = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        replace_img_ref,
        markdown
    )

    # ──────────────────────────────────────────────
    # Imágenes no referenciadas
    # ──────────────────────────────────────────────

    unreferenced = [
        (name, uri)
        for name, uri in images.items()
        if name not in used_images
    ]

    if unreferenced:

        markdown += (
            "\n\n---\n\n"
            "## Figuras del artículo\n\n"
        )

        for i, (name, uri) in enumerate(
            unreferenced,
            1
        ):

            markdown += make_figure_block(
                f"Figura {i}",
                uri,
                name
            )

    return markdown


# ══════════════════════════════════════════════════
# TABLAS
# ══════════════════════════════════════════════════

TABLE_SYSTEM_INSTRUCTION = (
    "Eres un traductor académico especializado "
    "en textos científicos. "
    "Tu única tarea es traducir el contenido "
    "de esta tabla Markdown al español.\n\n"

    "REGLAS OBLIGATORIAS:\n"

    "1. Mantén EXACTAMENTE la estructura "
    "Markdown de la tabla.\n"

    "2. No añadas texto antes ni después "
    "de la tabla.\n"

    "3. Traduce el contenido de las celdas "
    "con precisión académica.\n"

    "4. Conserva exactamente números, "
    "porcentajes, valores estadísticos, "
    "símbolos, unidades, referencias y siglas.\n"

    "5. Mantén una terminología consistente "
    "con el significado del contexto.\n"

    "6. Utiliza español académico natural "
    "y evita calcos innecesarios.\n"

    "7. No resumas, interpretes, expliques "
    "ni modifiques los datos.\n\n"

    "Devuelve ÚNICAMENTE la tabla traducida."
)


async def translate_table_deepseek(
    table_md: str
) -> str:

    if not DEEPSEEK_API_KEY:
        return table_md

    headers = {
        "Authorization": (
            f"Bearer {DEEPSEEK_API_KEY}"
        ),
        "Content-Type": "application/json"
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": TABLE_SYSTEM_INSTRUCTION
            },
            {
                "role": "user",
                "content": table_md
            }
        ],
        "temperature": 0.1,
        "stream": False
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        for _ in range(2):

            try:

                resp = await client.post(
                    DEEPSEEK_BASE_URL,
                    headers=headers,
                    json=payload
                )

                if resp.status_code == 200:

                    data = resp.json()

                    content = (
                        data
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if content and content.strip():
                        return content.strip()

            except Exception as err:

                print(
                    f"  [WARN] Error traduciendo tabla "
                    f"con DeepSeek: {err}",
                    flush=True
                )

    return table_md


async def translate_table_gemini(
    table_md: str,
    client: genai.Client
) -> str:

    for attempt in range(2):

        try:

            resp = await client.aio.models.generate_content(
                model=MODELS[0],
                contents=table_md,
                config=types.GenerateContentConfig(
                    system_instruction=TABLE_SYSTEM_INSTRUCTION,
                    temperature=0.1
                )
            )

            if resp.text:
                return resp.text.strip()

        except Exception as err:

            print(
                f"  [WARN] Error traduciendo tabla "
                f"con Gemini: {err}",
                flush=True
            )

    return table_md


async def translate_single_table(
    table_md: str
) -> str:

    if modelogemini == 0:

        return await translate_table_deepseek(
            table_md
        )

    if gemini_client:

        return await translate_table_gemini(
            table_md,
            gemini_client
        )

    return table_md


async def extract_and_translate_tables(
    markdown: str
) -> tuple[str, dict[str, str]]:

    """
    Encuentra tablas Markdown, las sustituye
    por marcadores y las traduce por separado.
    """

    table_pattern = re.compile(
        r"(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}",
        re.MULTILINE
    )

    tables_map: dict[str, str] = {}

    def replacer(match):

        t_id = (
            f"TABLE_{len(tables_map) + 1}"
        )

        table_content = (
            match.group(0).strip()
        )

        tables_map[t_id] = table_content

        return (
            f"\n\n<!-- {t_id} -->\n\n"
        )

    modified_markdown = table_pattern.sub(
        replacer,
        markdown
    )

    translated_tables: dict[str, str] = {}

    is_engine_ready = (
        bool(DEEPSEEK_API_KEY)
        if modelogemini == 0
        else bool(gemini_client)
    )

    if tables_map and is_engine_ready:

        engine_label = (
            f"DeepSeek ({DEEPSEEK_MODEL})"
            if modelogemini == 0
            else "Gemini Flash"
        )

        print(
            f"  📊 Detectadas {len(tables_map)} tablas. "
            f"Traducción aislada en progreso "
            f"con [{engine_label}]...",
            flush=True
        )

        tasks = [
            translate_single_table(content)
            for content in tables_map.values()
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

        for (t_id, _), result in zip(
            tables_map.items(),
            results
        ):

            if isinstance(result, str):

                translated_tables[
                    t_id
                ] = result

            else:

                translated_tables[
                    t_id
                ] = tables_map[t_id]

    else:

        # Si no hay motor disponible, conservamos
        # las tablas originales.
        translated_tables = dict(
            tables_map
        )

    return (
        modified_markdown,
        translated_tables
    )


def restore_tables(
    markdown: str,
    tables_map: dict[str, str]
) -> str:

    for t_id, content in tables_map.items():

        markdown = markdown.replace(
            f"<!-- {t_id} -->",
            f"\n\n{content}\n\n"
        )

    return markdown


# ══════════════════════════════════════════════════
# CHUNKING
# ══════════════════════════════════════════════════

def chunk_markdown(
    markdown: str,
    max_chars: int = 12000
) -> list[str]:

    """
    Divide el Markdown respetando párrafos
    y encabezados.
    """

    paragraphs = markdown.split(
        "\n\n"
    )

    chunks = []
    current = ""

    last_seen_page = 1

    for p in paragraphs:

        match = re.search(
            r"<!-- PAGE:(\d+) -->",
            p
        )

        if match:

            last_seen_page = int(
                match.group(1)
            )

        is_heading = (
            re.match(
                r"^#{1,3}\s+",
                p.strip()
            )
            is not None
        )

        if (
            (
                len(current) + len(p)
                > max_chars
                and current
            )
            or (
                is_heading
                and len(current)
                > max_chars * 0.7
            )
        ):

            chunks.append(
                current.strip()
            )

            current = (
                f"<!-- PAGE:{last_seen_page} -->"
                "\n\n"
            )

        current += (
            p + "\n\n"
        )

    if current.strip():

        chunks.append(
            current.strip()
        )

    return chunks


# ══════════════════════════════════════════════════
# INSTRUCCIONES DEL TRADUCTOR
# ══════════════════════════════════════════════════

def get_system_instruction(
    doc_lang: str
) -> str:

    return (
        "Eres un traductor académico profesional. "
        f"El idioma de origen del texto es {doc_lang}. "
        "Tu tarea es traducir el texto científico "
        "proporcionado al ESPAÑOL.\n\n"

        "REGLAS OBLIGATORIAS:\n\n"

        "1. INTEGRIDAD: Traduce TODO el contenido "
        "proporcionado. No omitas, resumas, "
        "simplifiques ni agregues información.\n\n"

        "2. FIDELIDAD: Conserva exactamente "
        "el significado, los matices, las relaciones "
        "lógicas, las afirmaciones, las referencias, "
        "las cifras, los nombres propios y los "
        "términos técnicos del original.\n\n"

        "3. ESPAÑOL ACADÉMICO: Utiliza un español "
        "académico natural, claro, preciso y formal. "
        "No traduzcas mecánicamente palabra por palabra "
        "cuando eso produzca una construcción antinatural.\n\n"

        "4. TERMINOLOGÍA: Mantén una terminología "
        "consistente a lo largo de todo el texto. "
        "Cuando exista una traducción académica estándar "
        "en español, utilízala.\n\n"

        "5. PROHIBICIÓN DE CALCOS: Evita traducciones "
        "literales que produzcan anglicismos o "
        "construcciones incorrectas.\n\n"

        "6. SIGLAS: Conserva las siglas originales "
        "cuando sean necesarias. Si desarrollas una "
        "sigla, conserva la sigla correspondiente.\n\n"

        "7. FORMATO MARKDOWN: Conserva la estructura "
        "Markdown proporcionada. Mantén encabezados, "
        "listas, tablas, negritas, cursivas, enlaces, "
        "referencias y demás elementos de formato.\n\n"

        "8. PÁGINAS: Los marcadores PAGE son únicamente "
        "referencias estructurales. No los traduzcas "
        "ni los conviertas en contenido.\n\n"

        "9. PÁRRAFOS: Une únicamente saltos de línea "
        "que correspondan a una misma oración o párrafo. "
        "No combines párrafos independientes.\n\n"

        "10. CONTENIDO CIENTÍFICO: No interpretes, "
        "critiques, actualices, corrijas ni reformules "
        "las afirmaciones científicas.\n\n"

        "11. ERRORES DE EXTRACCIÓN: Si existe un error "
        "evidente de extracción y la corrección es "
        "inequívoca por el contexto inmediato, puedes "
        "reconstruirlo. Si no es inequívoco, conserva "
        "el original.\n\n"

        "12. REFERENCIAS Y CITAS: Conserva autores, "
        "años, números de referencia, DOI, URLs, "
        "nombres de revistas y títulos bibliográficos. "
        "No traduzcas datos bibliográficos salvo cuando "
        "corresponda explícitamente.\n\n"

        "NO agregues introducciones, explicaciones, "
        "comentarios, advertencias ni conclusiones. "
        "Devuelve únicamente la traducción."
    )


# ══════════════════════════════════════════════════
# DETECCIÓN DE IDIOMA
# ══════════════════════════════════════════════════

async def detect_document_language(
    first_page_text: str,
    client: Optional[genai.Client] = None
) -> str:

    if not first_page_text.strip():
        return "Inglés"

    prompt = (
        "Detect the primary language of this academic text. "
        "Return ONLY the language name "
        "(e.g. English, French, Portuguese, German). "
        "Do not return anything else.\n\n"
        f"{first_page_text[:1500]}"
    )

    # DeepSeek
    if (
        modelogemini == 0
        and DEEPSEEK_API_KEY
    ):

        try:

            headers = {
                "Authorization": (
                    f"Bearer {DEEPSEEK_API_KEY}"
                ),
                "Content-Type": "application/json"
            }

            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a language detection tool. "
                            "Output only the language name."
                        )
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.0
            }

            async with httpx.AsyncClient(
                timeout=15.0
            ) as http_client:

                resp = await http_client.post(
                    DEEPSEEK_BASE_URL,
                    headers=headers,
                    json=payload
                )

                if resp.status_code == 200:

                    ans = (
                        resp.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )

                    if ans:
                        return ans

        except Exception as err:

            print(
                f"  [WARN] Error detectando idioma: "
                f"{err}",
                flush=True
            )

        return "Inglés"

    # Gemini
    c = client or gemini_client

    if not c:
        return "Inglés"

    try:

        resp = await c.aio.models.generate_content(
            model=MODELS[0],
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0
            )
        )

        if resp.text:
            return resp.text.strip()

    except Exception as err:

        print(
            f"  [WARN] Error detectando idioma "
            f"con Gemini: {err}",
            flush=True
        )

    return "Inglés"


# ══════════════════════════════════════════════════
# TRADUCCIÓN DE CHUNK — DEEPSEEK
# ══════════════════════════════════════════════════

async def translate_chunk_deepseek(
    chunk: str,
    system_instruction: str,
    chunk_num: int = 1,
    total_chunks: int = 1,
    max_retries: int = 3
) -> str:

    if not chunk or not chunk.strip():
        return ""

    headers = {
        "Authorization": (
            f"Bearer {DEEPSEEK_API_KEY}"
        ),
        "Content-Type": "application/json"
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_instruction
            },
            {
                "role": "user",
                "content": chunk
            }
        ],
        "temperature": 0.1,
        "stream": False
    }

    async with httpx.AsyncClient(
        timeout=120.0
    ) as client:

        for attempt in range(
            1,
            max_retries + 1
        ):

            try:

                print(
                    f"      ↳ [Chunk "
                    f"{chunk_num}/{total_chunks}] "
                    f"Intento {attempt}/{max_retries} "
                    f"usando DeepSeek "
                    f"[{DEEPSEEK_MODEL}]...",
                    flush=True
                )

                resp = await client.post(
                    DEEPSEEK_BASE_URL,
                    headers=headers,
                    json=payload
                )

                if resp.status_code == 200:

                    data = resp.json()

                    content = (
                        data
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if (
                        content
                        and content.strip()
                    ):

                        return content.strip()

                    print(
                        "      [WARN] Respuesta "
                        "vacía de DeepSeek.",
                        flush=True
                    )

                elif resp.status_code == 402:

                    try:
                        err_msg = (
                            resp.json()
                            .get("error", {})
                            .get(
                                "message",
                                "Insufficient Balance"
                            )
                        )
                    except Exception:
                        err_msg = (
                            "Insufficient Balance"
                        )

                    print(
                        f"      [ERROR] DeepSeek "
                        f"HTTP 402: {err_msg}",
                        flush=True
                    )

                    raise RuntimeError(
                        f"DeepSeek 402: {err_msg}"
                    )

                elif resp.status_code == 429:

                    print(
                        "      [WARN] DeepSeek "
                        "Rate Limit (429).",
                        flush=True
                    )

                else:

                    print(
                        f"      [WARN] DeepSeek "
                        f"HTTP {resp.status_code}: "
                        f"{resp.text}",
                        flush=True
                    )

            except RuntimeError:
                raise

            except Exception as err:

                print(
                    f"      [WARN] Error de conexión "
                    f"con DeepSeek: {err}",
                    flush=True
                )

            if attempt < max_retries:

                wait = 4 * attempt

                print(
                    f"      ⏸ Esperando {wait}s "
                    f"antes de reintentar...",
                    flush=True
                )

                await asyncio.sleep(
                    wait
                )

    print(
        "      [WARN] Fallaron los intentos "
        "con DeepSeek; conservando original.",
        flush=True
    )

    return chunk


# ══════════════════════════════════════════════════
# TRADUCCIÓN DE CHUNK — GEMINI
# ══════════════════════════════════════════════════

async def translate_chunk_gemini(
    chunk: str,
    client: genai.Client,
    active_models: Optional[list[str]] = None,
    max_retries: int = 3,
    chunk_num: int = 1,
    total_chunks: int = 1,
    doc_lang: str = "Inglés",
    system_instruction: Optional[str] = None
) -> str:

    if not chunk or not chunk.strip():
        return ""

    if active_models is None:
        active_models = list(MODELS)

    if system_instruction is None:
        system_instruction = get_system_instruction(
            doc_lang
        )

    for attempt in range(
        1,
        max_retries + 1
    ):

        models_to_try = list(
            active_models
        )

        for model_name in models_to_try:

            try:

                print(
                    f"      ↳ [Chunk "
                    f"{chunk_num}/{total_chunks}] "
                    f"Intento {attempt}/{max_retries} "
                    f"usando [{model_name}]...",
                    flush=True
                )

                response = (
                    await client.aio.models.generate_content(
                        model=model_name,
                        contents=chunk,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.1
                        )
                    )
                )

                if (
                    response.text
                    and response.text.strip()
                ):

                    if model_name in active_models:

                        active_models.remove(
                            model_name
                        )

                        active_models.insert(
                            0,
                            model_name
                        )

                    return response.text.strip()

                print(
                    f"      [WARN] Respuesta "
                    f"vacía de {model_name}.",
                    flush=True
                )

            except Exception as err:

                err_str = str(err).lower()

                print(
                    f"      [WARN] Error con "
                    f"{model_name}: {err}",
                    flush=True
                )

                if (
                    "504" in str(err)
                    or "deadline" in err_str
                    or "429" in str(err)
                    or "resource_exhausted" in err_str
                    or "rate" in err_str
                    or "503" in str(err)
                    or "400" in str(err)
                ):

                    print(
                        f"      🔄 Cambiando "
                        f"al siguiente modelo...",
                        flush=True
                    )

                    if model_name in active_models:

                        active_models.remove(
                            model_name
                        )

                        active_models.append(
                            model_name
                        )

                    continue

        wait = 15 * attempt

        print(
            f"      ⏸ Todos los modelos "
            f"fallaron en intento {attempt}. "
            f"Esperando {wait}s...",
            flush=True
        )

        await asyncio.sleep(
            wait
        )

    print(
        "      [WARN] Todos los intentos "
        "fallaron; conservando original.",
        flush=True
    )

    return chunk


# ══════════════════════════════════════════════════
# TRADUCCIÓN DE CHUNK
# ══════════════════════════════════════════════════

async def translate_chunk(
    chunk: str,
    doc_lang: str = "Inglés",
    chunk_num: int = 1,
    total_chunks: int = 1,
    active_models: Optional[list[str]] = None
) -> str:

    if not chunk or not chunk.strip():
        return ""

    system_instruction = (
        get_system_instruction(
            doc_lang
        )
    )

    # Directiva adicional únicamente para el primer chunk.
    #
    # No se agrega a todos los chunks porque sería ruido
    # innecesario para el modelo.
    processed_chunk = chunk

    if chunk_num == 1:

        processed_chunk = (
            "ESTE ES EL COMIENZO DEL DOCUMENTO "
            "(PORTADA/ABSTRACT). DEBES TRADUCIR "
            "DESDE LA PRIMERA PALABRA, incluyendo "
            "título, autores y afiliaciones.\n\n"
            + chunk
        )

    if modelogemini == 0:

        return await translate_chunk_deepseek(
            processed_chunk,
            system_instruction,
            chunk_num=chunk_num,
            total_chunks=total_chunks
        )

    if not gemini_client:

        print(
            "  [ERROR] gemini_client "
            "no inicializado.",
            flush=True
        )

        return processed_chunk

    return await translate_chunk_gemini(
        processed_chunk,
        gemini_client,
        active_models=active_models,
        chunk_num=chunk_num,
        total_chunks=total_chunks,
        doc_lang=doc_lang,
        system_instruction=system_instruction
    )


# ══════════════════════════════════════════════════
# TRADUCCIÓN COMPLETA
# ══════════════════════════════════════════════════

async def translate_full_markdown(
    markdown: str,
    doc_lang: str
) -> str:

    if modelogemini == 0:

        if not DEEPSEEK_API_KEY:

            print(
                "  [ERROR] No se encontró "
                "DEEPSEEK_API_KEY. "
                "Devolviendo texto original."
            )

            return markdown

        engine_label = (
            f"DeepSeek ({DEEPSEEK_MODEL})"
        )

    else:

        if not gemini_client:

            print(
                "  [ERROR] No se encontró "
                "GEMINI_API_KEY. "
                "Devolviendo texto original."
            )

            return markdown

        engine_label = (
            "Gemini Flash (cascada)"
        )

    chunks = chunk_markdown(
        markdown,
        max_chars=12000
    )

    total_chunks = len(chunks)

    translated_chunks = []

    active_models = list(
        MODELS
    )

    print(
        f"\n  🚀 Iniciando traducción "
        f"exhaustiva "
        f"(total {total_chunks} fragmentos) "
        f"con motor [{engine_label}]...",
        flush=True
    )

    t0 = time.time()

    for i, chunk in enumerate(
        chunks
    ):

        page_matches = re.findall(
            r"<!-- PAGE:(\d+) -->",
            chunk
        )

        if page_matches:

            pages_info = (
                "(Páginas: "
                + ", ".join(
                    sorted(
                        set(page_matches),
                        key=int
                    )
                )
                + ")"
            )

        else:

            pages_info = ""

        print(
            f"  ⏳ Procesando fragmento "
            f"{i + 1}/{total_chunks} "
            f"{pages_info} "
            f"({len(chunk)} chars)...",
            flush=True
        )

        t_chunk = time.time()

        translated_text = await translate_chunk(
            chunk,
            doc_lang=doc_lang,
            chunk_num=i + 1,
            total_chunks=total_chunks,
            active_models=active_models
        )

        translated_chunks.append(
            translated_text
        )

        elapsed = round(
            time.time() - t_chunk,
            1
        )

        print(
            f"  ✓ Fragmento "
            f"{i + 1}/{total_chunks} "
            f"completado en {elapsed}s.",
            flush=True
        )

        if i < total_chunks - 1:

            await asyncio.sleep(
                DELAY_BETWEEN_CHUNKS_SEC
            )

    elapsed_total = round(
        time.time() - t0,
        1
    )

    print(
        f"  Traducción 100% completada "
        f"en {elapsed_total}s.\n",
        flush=True
    )

    return "\n\n".join(
        translated_chunks
    )


# ══════════════════════════════════════════════════
# METADATOS / AFILIACIONES
# ══════════════════════════════════════════════════

def is_affiliation_or_meta(
    block: str
) -> bool:

    s = block.strip()

    if not s:
        return False

    if (
        s.startswith("#")
        or s.startswith("!")
        or s.startswith("|")
    ):
        return False

    has_email = bool(
        re.search(
            r"[\w.\-+]+@[\w.\-]+\.\w+"
            r"|e-mail:"
            r"|email:"
            r"|correo electrónico:",
            s,
            re.I
        )
    )

    affil_keywords = [
        "department of",
        "departamento de",
        "division of",
        "división de",
        "section on",
        "sección de",
        "institute of",
        "instituto de",
        "university",
        "universidad",
        "school of",
        "escuela de",
        "faculty of",
        "facultad de",
        "hospital",
        "laboratory of",
        "laboratorio de",
        "center for",
        "centro de",
        "clinic",
        "clínica",
        "unit",
        "unidad de",
    ]

    keyword_hits = sum(
        1
        for keyword in affil_keywords
        if re.search(
            r"\b"
            + re.escape(keyword)
            + r"\b",
            s,
            re.I
        )
    )

    has_author_sym = bool(
        re.search(
            r"\(&\)"
            r"|\bcorrespondence\b"
            r"|\bcorresponding author\b"
            r"|\bautor de correspondencia\b"
            r"|\baddress correspondence\b",
            s,
            re.I
        )
    )

    has_address = bool(
        re.search(
            r"\b(?:USA|UK|Spain|France|Germany|"
            r"Bethesda|MD\s*\d{5}|"
            r"MO\s*\d{5}|Room\s*\d+|"
            r"Box\s*\d+|P\.?\s*O\.?\s*Box)\b",
            s,
            re.I
        )
    )

    has_editorial = bool(
        re.search(
            r"\b(?:received:\s*\d|"
            r"accepted:\s*\d|"
            r"published online:|"
            r"doi:\s*10\.)\b"
            r"|copyright\s*©"
            r"|©\s*\d{4}",
            s,
            re.I
        )
    )

    if has_editorial:
        return True

    if has_email:
        return True

    if (
        keyword_hits >= 1
        and (
            has_author_sym
            or has_address
        )
    ):
        return True

    if keyword_hits >= 2:
        return True

    return False


# ══════════════════════════════════════════════════
# LIMPIEZA DE TEXTO EXTRAÍDO
# ══════════════════════════════════════════════════

def clean_and_join_broken_paragraphs(
    text: str
) -> str:

    """
    Limpieza conservadora del Markdown extraído.

    IMPORTANTE:
    No utiliza is_affiliation_or_meta() para eliminar
    contenido. Esa función únicamente ayuda a decidir
    cuándo NO unir bloques.
    """

    if not text:
        return ""

    # ──────────────────────────────────────────────
    # 1. Números de página flotantes
    # ──────────────────────────────────────────────

    text = re.sub(
        r"(?m)^\s*(?:"
        r"\d+\s+de\s+\d+"
        r"|\d+\s+of\s+\d+"
        r"|page\s+\d+"
        r"|p\.?\s*\d+"
        r"|\d+"
        r")\s*$",
        "",
        text,
        flags=re.I
    )

    # ──────────────────────────────────────────────
    # 2. Marcas editoriales muy específicas
    # ──────────────────────────────────────────────

    text = re.sub(
        r"(?mi)^\s*"
        r"(?:wileyonlinelibrary\.com.*"
        r"|JCPP Advances.*)"
        r"\s*$",
        "",
        text
    )

    # ──────────────────────────────────────────────
    # 3. Copyright inequívoco
    # ──────────────────────────────────────────────

    text = re.sub(
        r"(?mi)^\s*"
        r"(?:copyright\s+©?\s*\d{4}|"
        r"©\s*\d{4}|"
        r"all rights reserved\.?)"
        r"\s*$",
        "",
        text
    )

    # ──────────────────────────────────────────────
    # 4. PAGE markers
    # ──────────────────────────────────────────────
    #
    # Se conservan temporalmente como espacios para
    # no cortar oraciones artificialmente.

    text = re.sub(
        r"\s*<!-- PAGE:\d+ -->\s*",
        " ",
        text
    )

    # ──────────────────────────────────────────────
    # 5. Encabezados Markdown
    # ──────────────────────────────────────────────

    text = re.sub(
        r"(?m)^([#]+)\s*"
        r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]+)"
        r"\s*$",
        r"\n\n\1 \2\n\n",
        text
    )

    # ──────────────────────────────────────────────
    # 6. Palabras cortadas por guión de línea
    # ──────────────────────────────────────────────

    text = re.sub(
        r"(\b[\wáéíóúñÁÉÍÓÚÑ]+)"
        r"-\s*\n+\s*"
        r"([\wáéíóúñÁÉÍÓÚÑ]+\b)",
        r"\1\2",
        text
    )

    # ──────────────────────────────────────────────
    # 7. Paréntesis/corchetes cortados
    # ──────────────────────────────────────────────

    text = re.sub(
        r"([\(\[\{])\s*\n+\s*",
        r"\1",
        text
    )

    # ──────────────────────────────────────────────
    # 8. Unir líneas rotas
    # ──────────────────────────────────────────────

    lines = text.split("\n")

    joined_lines = []

    i = 0

    while i < len(lines):

        line = lines[i]

        while i + 1 < len(lines):

            next_line = lines[i + 1]

            next_stripped = (
                next_line.lstrip()
            )

            curr_stripped = (
                line.rstrip()
            )

            # No unir contra elementos Markdown
            if (
                next_stripped
                and not next_stripped.startswith(
                    (
                        "#",
                        "*",
                        "-",
                        "|",
                        ">",
                        "<",
                        "!",
                        "`",
                    )
                )
                and not re.match(
                    r"^\d+\.\s+",
                    next_stripped
                )
            ):

                if (
                    curr_stripped
                    and not curr_stripped.endswith(
                        (
                            ".",
                            "!",
                            "?",
                            ":",
                            "#",
                            "---",
                            "***",
                            ">",
                            "</a>",
                        )
                    )
                    and not curr_stripped.startswith(
                        (
                            "#",
                            "*",
                            "-",
                            "|",
                            ">",
                            "<",
                            "!",
                            "`",
                        )
                    )
                ):

                    # Evitar tocar metadatos/afiliaciones.
                    if not is_affiliation_or_meta(
                        next_stripped
                    ):

                        is_curr_cut = bool(
                            re.search(
                                r"[-–—(¿¡]$"
                                r"|(?:\b(?:"
                                r"en|de|del|la|el|"
                                r"los|las|un|una|"
                                r"con|por|para|y|"
                                r"o|que|a|al|su|"
                                r"sus|como"
                                r")\s*)$",
                                curr_stripped,
                                re.I
                            )
                        )

                        is_next_cont = bool(
                            re.match(
                                r"^[a-záéíóúñ("
                                r"),;\]]",
                                next_stripped
                            )
                        )

                        if (
                            is_next_cont
                            or is_curr_cut
                        ):

                            line = (
                                curr_stripped
                                + " "
                                + next_stripped
                            )

                            i += 1
                            continue

            # ─────────────────────────────────────
            # Caso:
            #
            # línea
            #
            # siguiente línea
            # ─────────────────────────────────────

            if (
                not next_line.strip()
                and i + 2 < len(lines)
            ):

                after_empty = lines[
                    i + 2
                ]

                after_stripped = (
                    after_empty.lstrip()
                )

                curr_stripped = (
                    line.rstrip()
                )

                if (
                    curr_stripped
                    and not curr_stripped.endswith(
                        (
                            ".",
                            "!",
                            "?",
                            ":",
                            "#",
                            "---",
                            "***",
                            ">",
                            "</a>",
                        )
                    )
                    and not curr_stripped.startswith(
                        (
                            "#",
                            "*",
                            "-",
                            "|",
                            ">",
                            "<",
                            "!",
                            "`",
                        )
                    )
                    and after_stripped
                    and not after_stripped.startswith(
                        (
                            "#",
                            "*",
                            "-",
                            "|",
                            ">",
                            "<",
                            "!",
                            "`",
                        )
                    )
                    and not re.match(
                        r"^\d+\.\s+",
                        after_stripped
                    )
                    and not is_affiliation_or_meta(
                        after_stripped
                    )
                ):

                    is_curr_cut = bool(
                        re.search(
                            r"[-–—(¿¡]$"
                            r"|(?:\b(?:"
                            r"en|de|del|la|el|"
                            r"los|las|un|una|"
                            r"con|por|para|y|"
                            r"o|que|a|al|su|"
                            r"sus|como"
                            r")\s*)$",
                            curr_stripped,
                            re.I
                        )
                    )

                    is_after_cont = bool(
                        re.match(
                            r"^[a-záéíóúñ("
                            r"),;\]]",
                            after_stripped
                        )
                    )

                    if (
                        is_after_cont
                        or is_curr_cut
                    ):

                        line = (
                            curr_stripped
                            + " "
                            + after_stripped
                        )

                        i += 2
                        continue

            break

        joined_lines.append(
            line
        )

        i += 1

    text = "\n".join(
        joined_lines
    )

    # Normalizar exceso de saltos
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def preprocess_raw_markdown(
    md_text: str
) -> str:

    md_text = remove_obvious_editorial_noise(
        md_text
    )

    md_text = clean_and_join_broken_paragraphs(
        md_text
    )

    return md_text


def postprocess_markdown(
    md_text: str
) -> str:

    # Solo limpieza estructural ligera al final.
    #
    # No volver a ejecutar toda la limpieza agresiva
    # después de insertar HTML e imágenes.

    md_text = remove_obvious_editorial_noise(
        md_text
    )

    md_text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        md_text
    )

    return md_text.strip()


# ══════════════════════════════════════════════════
# PROCESAMIENTO PRINCIPAL
# ══════════════════════════════════════════════════

async def process_pdf_bytes_translation(
    pdf_bytes: bytes,
    paper_id: Optional[str] = None,
    force_retranslate: bool = False,
    source_url: str = ""
) -> dict:

    """
    Procesa bytes de PDF:
      PDF
       ↓
      limpieza editorial
       ↓
      Markdown
       ↓
      tablas
       ↓
      traducción
       ↓
      imágenes
       ↓
      resultado
    """

    engine_name = (
        "gemini"
        if modelogemini == 1
        else "deepseek"
    )

    clean_pid = clean_paper_id(
        paper_id,
        fallback=(
            source_url
            or hashlib.sha256(
                pdf_bytes
            ).hexdigest()[:16]
        )
    )

    file_id = safe_id(
        f"{clean_pid}_{engine_name}"
    )

    cache_file = (
        CACHE_DIR
        / f"{file_id}.json"
    )

    pdf_file_path = (
        CACHE_DIR
        / f"{file_id}.pdf"
    )

    # ──────────────────────────────────────────────
    # Guardar PDF original
    # ──────────────────────────────────────────────

    if (
        not pdf_file_path.exists()
        or force_retranslate
    ):

        pdf_file_path.write_bytes(
            pdf_bytes
        )

    # ──────────────────────────────────────────────
    # Cache
    # ──────────────────────────────────────────────

    if (
        cache_file.exists()
        and not force_retranslate
    ):

        print(
            f"[CACHE HIT] "
            f"{clean_pid} "
            f"({engine_name.capitalize()})",
            flush=True
        )

        return json.loads(
            cache_file.read_text(
                encoding="utf-8"
            )
        )

    engine_label = (
        "Gemini Flash (cascada)"
        if modelogemini == 1
        else f"DeepSeek ({DEEPSEEK_MODEL})"
    )

    print(
        f"[PROCESSING] {clean_pid} "
        f"({len(pdf_bytes)} bytes) "
        f"[Motor: {engine_label}]",
        flush=True
    )

    # ──────────────────────────────────────────────
    # 1. EXTRAER
    # ──────────────────────────────────────────────

    raw_markdown, images = (
        extract_markdown_and_images(
            pdf_bytes
        )
    )

    # ──────────────────────────────────────────────
    # 2. LIMPIAR
    # ──────────────────────────────────────────────

    raw_markdown = (
        preprocess_raw_markdown(
            raw_markdown
        )
    )

    print(
        f"  Markdown generado: "
        f"{len(raw_markdown)} caracteres. "
        f"Imágenes extraídas: "
        f"{len(images)}",
        flush=True
    )

    # ──────────────────────────────────────────────
    # 3. IDIOMA
    # ──────────────────────────────────────────────

    doc_lang = (
        await detect_document_language(
            raw_markdown[:1500]
        )
    )

    print(
        f"  🌐 Idioma detectado: "
        f"{doc_lang}",
        flush=True
    )

    # ──────────────────────────────────────────────
    # 4. TRADUCCIÓN
    # ──────────────────────────────────────────────

    if (
        "español"
        in doc_lang.lower()
        or "spanish"
        in doc_lang.lower()
        or doc_lang.lower().strip() == "es"
    ):

        print(
            "  ✅ El documento ya está "
            "en español. Omitiendo traducción.",
            flush=True
        )

        translated = raw_markdown

    else:

        # ─────────────────────────────────────────
        # Tablas aisladas
        # ─────────────────────────────────────────

        (
            raw_markdown_no_tables,
            translated_tables
        ) = await extract_and_translate_tables(
            raw_markdown
        )

        # ─────────────────────────────────────────
        # Traducción del cuerpo
        # ─────────────────────────────────────────

        translated = (
            await translate_full_markdown(
                raw_markdown_no_tables,
                doc_lang
            )
        )

        print(
            f"  Traducción completada: "
            f"{len(translated)} caracteres",
            flush=True
        )

        # ─────────────────────────────────────────
        # Restaurar tablas
        # ─────────────────────────────────────────

        translated = restore_tables(
            translated,
            translated_tables
        )

    # ──────────────────────────────────────────────
    # 5. Imágenes
    # ──────────────────────────────────────────────

    final_pdf_url = (
        f"/files/{file_id}.pdf"
    )

    final_markdown = (
        replace_image_refs_with_base64(
            translated,
            images,
            final_pdf_url=final_pdf_url
        )
    )

    # ──────────────────────────────────────────────
    # 6. Enlaces PDF
    # ──────────────────────────────────────────────

    final_markdown = final_markdown.replace(
        "](#page=",
        f"]({final_pdf_url}#page="
    )

    # ──────────────────────────────────────────────
    # 7. Convertir enlaces internos
    # ──────────────────────────────────────────────

    final_markdown = re.sub(
        r"\[([^\]]+)\]"
        r"\(([^)]*"
        r"\.pdf(?:#[^)]*)?"
        r"|#page=\d+"
        r")\)",
        r'<a href="#" '
        r'class="internal-pdf-link" '
        r'data-url="\2">\1</a>',
        final_markdown
    )

    # ──────────────────────────────────────────────
    # 8. Limpieza final ligera
    # ──────────────────────────────────────────────

    final_markdown = (
        postprocess_markdown(
            final_markdown
        )
    )

    # ──────────────────────────────────────────────
    # 9. Resultado
    # ──────────────────────────────────────────────

    result = {
        "markdown": final_markdown,
        "file_id": file_id,
        "pdf_url": final_pdf_url
    }

    # Guardar cache únicamente si no hubo
    # indicación explícita de traducción parcial.
    if (
        "> [!NOTE]\n"
        "> **Traducción parcial**"
        not in final_markdown
    ):

        cache_file.write_text(
            json.dumps(
                result,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

    return result


# ══════════════════════════════════════════════════
# PROCESAMIENTO DESDE URL
# ══════════════════════════════════════════════════

async def process_pdf_translation(
    url: str,
    paper_id: Optional[str] = None,
    force_retranslate: bool = False
) -> dict:

    engine_name = (
        "gemini"
        if modelogemini == 1
        else "deepseek"
    )

    clean_pid = clean_paper_id(
        paper_id,
        fallback=url
    )

    file_id = safe_id(
        f"{clean_pid}_{engine_name}"
    )

    cache_file = (
        CACHE_DIR
        / f"{file_id}.json"
    )

    if (
        cache_file.exists()
        and not force_retranslate
    ):

        print(
            f"[CACHE HIT] "
            f"{clean_pid} "
            f"({engine_name.capitalize()})",
            flush=True
        )

        return json.loads(
            cache_file.read_text(
                encoding="utf-8"
            )
        )

    pdf_bytes = await download_pdf(
        url
    )

    return await process_pdf_bytes_translation(
        pdf_bytes,
        paper_id=clean_pid,
        force_retranslate=force_retranslate,
        source_url=url
    )


# ══════════════════════════════════════════════════
# ENDPOINTS REST
# ══════════════════════════════════════════════════

@api.post("/api/translate")
async def api_translate(
    req: TranslateRequest
):
    """
    Endpoint consumido por PsiHub
    cuando existe una URL directa.
    """

    target_id = req.get_paper_id()

    data = await process_pdf_translation(
        req.url,
        paper_id=target_id,
        force_retranslate=req.force
    )

    return JSONResponse(
        content=data
    )


@api.post("/api/translate-file")
async def api_translate_file(
    file: UploadFile = File(...),
    paper_id: Optional[str] = Form(None),
    id: Optional[str] = Form(None),
    force: bool = Form(False)
):

    """
    Endpoint para subir PDF directamente.
    """

    pdf_bytes = await file.read()

    if (
        not pdf_bytes
        or len(pdf_bytes) < 100
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Archivo PDF inválido "
                "o vacío."
            )
        )

    target_id = (
        paper_id
        or id
    )

    clean_pid = clean_paper_id(
        target_id,
        fallback=(
            file.filename
            or "uploaded.pdf"
        )
    )

    data = await process_pdf_bytes_translation(
        pdf_bytes,
        paper_id=clean_pid,
        force_retranslate=force,
        source_url=""
    )

    return JSONResponse(
        content=data
    )


@api.post("/api/check-pdf")
async def api_check_pdf(
    req: CheckRequest
):

    """
    Verifica si una URL parece descargable
    directamente.
    """

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=5.0
    ) as client:

        try:

            async with client.stream(
                "GET",
                req.url
            ) as resp:

                if resp.status_code == 200:

                    content_type = (
                        resp.headers
                        .get(
                            "content-type",
                            ""
                        )
                        .lower()
                    )

                    if (
                        "text/html"
                        not in content_type
                    ):

                        return {
                            "status": "ok"
                        }

        except Exception:
            pass

    return {
        "status": "blocked"
    }


@api.get("/health")
async def health():

    return {
        "status": "ok",
        "engine": (
            "deepseek"
            if modelogemini == 0
            else "gemini"
        ),
        "modelogemini": modelogemini,
        "deepseek_configured": bool(
            DEEPSEEK_API_KEY
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "active_model": (
            DEEPSEEK_MODEL
            if modelogemini == 0
            else (
                MODELS[0]
                if MODELS
                else None
            )
        ),
        "cache_entries": len(
            list(
                CACHE_DIR.glob(
                    "*.json"
                )
            )
        )
    }


# ══════════════════════════════════════════════════
# GRADIO
# ══════════════════════════════════════════════════

def gradio_translate(
    pdf_url: str,
    pdf_file,
    force: bool
):

    md = ""

    # ──────────────────────────────────────────────
    # Archivo local
    # ──────────────────────────────────────────────

    if pdf_file is not None:

        try:

            with open(
                pdf_file,
                "rb"
            ) as f:

                pdf_bytes = f.read()

            file_name = Path(
                pdf_file
            ).name

            res = asyncio.run(
                process_pdf_bytes_translation(
                    pdf_bytes,
                    file_name,
                    force,
                    source_url=""
                )
            )

            md = res.get(
                "markdown",
                "Sin contenido traducido."
            )

        except Exception as e:

            md = (
                "❌ Error al procesar "
                "archivo PDF subido: "
                f"{str(e)}"
            )

    # ──────────────────────────────────────────────
    # URL
    # ──────────────────────────────────────────────

    elif (
        pdf_url
        and pdf_url.strip()
    ):

        try:

            url = pdf_url.strip()

            res = asyncio.run(
                process_pdf_translation(
                    url,
                    url,
                    force
                )
            )

            md = res.get(
                "markdown",
                "Sin contenido traducido."
            )

        except Exception as e:

            md = (
                "❌ Error al traducir "
                f"el PDF: {str(e)}"
            )

    else:

        md = (
            "⚠️ Por favor ingresa una "
            "URL de PDF válida o arrastra "
            "un archivo PDF."
        )

    # ──────────────────────────────────────────────
    # Archivo Markdown descargable
    # ──────────────────────────────────────────────

    with tempfile.NamedTemporaryFile(
        suffix=".md",
        delete=False,
        mode="w",
        encoding="utf-8"
    ) as f:

        f.write(md)

        tmp_path = f.name

    return (
        md,
        tmp_path
    )


# ══════════════════════════════════════════════════
# INTERFAZ GRADIO
# ══════════════════════════════════════════════════

with gr.Blocks(
    title="PsiHub Reader"
) as demo:

    gr.Markdown(
        "# 📖 PsiHub Reader API"
    )

    engine_display = (
        f"DeepSeek ({DEEPSEEK_MODEL})"
        if modelogemini == 0
        else "Google Gemini Flash"
    )

    gr.Markdown(
        "Servicio de traducción académica "
        f"mediante **{engine_display}**. "
        "Traduce por URL o subiendo tu archivo PDF."
    )

    with gr.Row():

        with gr.Column(
            scale=1
        ):

            url_input = gr.Textbox(
                label=(
                    "Opción 1: URL del "
                    "PDF Open Access"
                ),
                placeholder=(
                    "https://.../paper.pdf"
                )
            )

            file_input = gr.File(
                label=(
                    "Opción 2: O sube tu PDF "
                    "aquí directamente "
                    "(Drag & Drop)"
                ),
                file_types=[".pdf"],
                type="filepath"
            )

            force_checkbox = gr.Checkbox(
                label=(
                    "🔄 Forzar nueva traducción "
                    "(ignorar caché)"
                ),
                value=False
            )

            btn_translate = gr.Button(
                "Traducir Paper Completo",
                variant="primary"
            )

        with gr.Column(
            scale=2
        ):

            output_md = gr.Markdown(
                label="Traducción en Modo Lectura"
            )

            output_file = gr.File(
                label=(
                    "Descargar Documento Markdown"
                )
            )

    btn_translate.click(
        fn=gradio_translate,
        inputs=[
            url_input,
            file_input,
            force_checkbox
        ],
        outputs=[
            output_md,
            output_file
        ]
    )


# ══════════════════════════════════════════════════
# MONTAR GRADIO EN FASTAPI
# ══════════════════════════════════════════════════

app = gr.mount_gradio_app(
    api,
    demo,
    path="/"
)


# ══════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            7860
        )
    )

    print(
        "\n🚀 Servidor de traducción iniciado:"
    )

    print(
        f"👉 Interfaz web: "
        f"http://localhost:{port}"
    )

    print(
        f"👉 API Endpoint: "
        f"http://localhost:{port}/api/translate\n"
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
