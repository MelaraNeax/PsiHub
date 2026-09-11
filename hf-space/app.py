"""
PsiHub Reader — Servicio de traducción de PDFs académicos
Hugging Face Space (Gradio SDK + FastAPI)

Flujo:
  1. Recibe la URL de un PDF open access (desde PsiHub o desde la web)
  2. Descarga el PDF
  3. Extrae Markdown estructurado + imágenes con pymupdf4llm / PyMuPDF
  4. Traduce sección por sección (en bloques grandes) con Gemini 1.5 Flash
  5. Pausa de 2s entre chunks para respetar límites gratuitos (15 RPM)
  6. Devuelve el Markdown traducido e incrusta las imágenes en base64
  7. Cachea resultados en disco para evitar reprocesamiento
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

# En Windows CMD asegurar soporte UTF-8 sin errores de charmap
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
import pymupdf
import pymupdf as fitz
import pymupdf4llm
# pyrefly: ignore [missing-import]
# pyrefly: ignore [missing-import]
from google import genai
from google.genai import types
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
# pyrefly: ignore [missing-import]
import gradio as gr
import uvicorn
from dotenv import load_dotenv

# Cargar variables de entorno desde .env local o raíz
load_dotenv()
load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

# ══════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY, http_options={'timeout': 120000}) # 2 mins max per chunk

# Sistema de Respaldo en Cascada (Fallback)
# Si uno falla por cuota (Rate Limit), saltará instantáneamente al siguiente.
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite", 
    "gemini-3-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash"
]

# Al usar chunks gigantes (25000 chars), haremos muy pocas peticiones (ej: 4 en lugar de 26)
# por lo que el rate limit de 15 RPM no será un problema.
DELAY_BETWEEN_CHUNKS_SEC = 2.0

CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

api = FastAPI(title="PsiHub Reader API", version="2.0.0")

# CORS abierto para que PsiHub (GitHub Pages / Capacitor) pueda consumir la API
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api.mount("/files", StaticFiles(directory="cache"), name="files")


class TranslateRequest(BaseModel):
    url: str   # URL del PDF open access
    paper_id: Optional[str] = None  # ID del paper (para caché)
    id: Optional[str] = None        # Alias alternativo para compatibilidad
    force: bool = False             # Forzar re-traducción ignorando caché

    def get_paper_id(self) -> Optional[str]:
        return self.paper_id or self.id

class CheckRequest(BaseModel):
    url: str


# ══════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════

def clean_paper_id(paper_id: Optional[str], fallback: str = "") -> str:
    """Sanitiza y normaliza el ID del paper eliminando valores nulos o strings vacíos."""
    if not paper_id:
        return fallback
    pid_str = paper_id.strip()
    if not pid_str or pid_str.lower() in ("undefined", "null", "none", "0"):
        return fallback
    return pid_str


def safe_id(paper_id: str) -> str:
    """Genera un nombre de archivo seguro a partir del ID del paper."""
    return hashlib.sha256(paper_id.encode()).hexdigest()[:24]


async def download_pdf(url: str) -> bytes:
    """Descarga el PDF siguiendo redirecciones."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        try:
            resp = await client.get(url)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"No se pudo descargar el PDF automáticamente (bot protection o timeout). Por favor descárgalo y súbelo manualmente."
            )
        
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"El servidor origen bloqueó la descarga automática (HTTP {resp.status_code}). Por favor descárgalo y súbelo manualmente."
            )
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type and len(resp.content) < 50000:
            raise HTTPException(
                status_code=502,
                detail="El servidor devolvió HTML o captcha en vez de un PDF. Por favor descárgalo y súbelo manualmente."
            )
        return resp.content


def remove_headers_footers(doc: fitz.Document):
    """Detecta y remueve encabezados y pies de página repetitivos."""
    if doc.page_count < 3:
        return
    
    header_texts = {}
    footer_texts = {}
    
    for page in doc:
        rect = page.rect
        for b in page.get_text("blocks"):
            b_rect = fitz.Rect(b[:4])
            text = b[4].strip()
            if not text or len(text) < 4: continue 
            
            # 10% superior o inferior
            if b_rect.y1 < rect.height * 0.10:
                header_texts[text] = header_texts.get(text, 0) + 1
            elif b_rect.y0 > rect.height * 0.90:
                footer_texts[text] = footer_texts.get(text, 0) + 1

    threshold = max(2, int(doc.page_count * 0.35))
    bad_texts = {k for k, v in header_texts.items() if v >= threshold} | {k for k, v in footer_texts.items() if v >= threshold}
    
    if bad_texts:
        for page in doc:
            for b in page.get_text("blocks"):
                if b[4].strip() in bad_texts:
                    page.add_redact_annot(fitz.Rect(b[:4]), fill=(1, 1, 1))
            page.apply_redactions()


def extract_markdown_and_images(pdf_bytes: bytes) -> tuple[str, dict[str, str]]:
    """Convierte PDF a Markdown estructurado y extrae imágenes usando pymupdf4llm para preservar la ubicación exacta."""
    import shutil
    
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    remove_headers_footers(doc)

    img_dir = tempfile.mkdtemp()
    images_b64 = {}
    
    try:
        # Extraemos por páginas para inyectar marcadores de paginación
        md_chunks = pymupdf4llm.to_markdown(doc, write_images=True, image_path=img_dir, page_chunks=True)
        
        full_md = []
        for chunk in md_chunks:
            page_num = chunk.get("metadata", {}).get("page_number", 1)
            page_text = chunk.get("text", "")
            
            # Forzamos la inserción de un enlace a la página exacta original bajo CADA imagen
            page_text = re.sub(
                r'(!\[[^\]]*\]\([^)]+\))', 
                r'\1\n\n[Ver imagen en PDF original - pág. ' + str(page_num) + r'](#page=' + str(page_num) + r')\n\n', 
                page_text
            )
            
            full_md.append(f"\n\n<!-- PAGE:{page_num} -->\n\n{page_text}")
        
        md_text = "\n".join(full_md)
        
        # Cargar las imágenes extraídas a base64
        for img_file in os.listdir(img_dir):
            filepath = os.path.join(img_dir, img_file)
            if not os.path.isfile(filepath): continue
            
            # Filtro de tamaño: ignorar fragmentos de gráficos vectoriales (< 10 KB)
            if os.path.getsize(filepath) < 10240:
                continue
            
            with open(filepath, "rb") as f:
                img_data = f.read()
                
            ext = img_file.split(".")[-1].lower()
            mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
            b64 = base64.b64encode(img_data).decode("utf-8")
            images_b64[img_file] = f"data:{mime};base64,{b64}"
            
        return md_text, images_b64
    finally:
        doc.close()
        shutil.rmtree(img_dir, ignore_errors=True)


def replace_image_refs_with_base64(markdown: str, images: dict[str, str]) -> str:
    """
    Reemplaza las referencias a imágenes en el Markdown por las imágenes base64.
    Debe llamarse DESPUÉS de traducir para no enviar enormes Base64 a la IA.
    """
    used_images = set()

    def replace_img_ref(match):
        alt = match.group(1)
        ref = match.group(2)
        for name, data_uri in images.items():
            if ref in name or name in ref or os.path.basename(ref) == name:
                used_images.add(name)
                return f"![{alt}]({data_uri})"
        return ""  # Eliminar fragmentos filtrados o no resueltos

    markdown = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_img_ref, markdown)

    # Si hay imágenes no referenciadas, insertarlas al final
    unreferenced = [
        (name, uri) for name, uri in images.items() if name not in used_images
    ]
    if unreferenced:
        markdown += "\n\n---\n\n## Figuras del artículo\n\n"
        for i, (name, uri) in enumerate(unreferenced, 1):
            markdown += f"![Figura {i}]({uri})\n\n"

    return markdown


# ══════════════════════════════════════════════════
# TRADUCCIÓN DE TABLAS (Aislada)
# ══════════════════════════════════════════════════

async def translate_table_gemini(table_md: str, client: genai.Client) -> str:
    """Traduce una tabla en formato Markdown de manera aislada."""
    system_instruction = (
        "Eres un traductor académico. Tu única tarea es traducir el contenido de esta tabla Markdown al español. "
        "MANTÉN LA ESTRUCTURA TABULAR EXACTA (`| col | col |`). NO añadas texto fuera de la tabla. "
        "Traduce las celdas con precisión. "
        "GLOSARIO Y CONSISTENCIA: Mantén un criterio unificado para la traducción de siglas y términos técnicos a lo largo de todo el documento. En textos de psicología, aplica convenciones estándar si aparecen (ej. MBIs -> Intervenciones basadas en Mindfulness (IBM), TFA -> Marco Teórico de Aceptabilidad). "
        "PROHIBICIÓN DE CALCOS: Evita anglicismos innecesarios (ej. usa 'versus' en lugar de forzar 'frente a' en comparaciones)."
    )
    for attempt in range(2):
        try:
            resp = await client.aio.models.generate_content(
                model=MODELS[0],
                contents=table_md,
                config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.1)
            )
            if resp.text: return resp.text.strip()
        except:
            pass
    return table_md

async def extract_and_translate_tables(markdown: str) -> tuple[str, dict[str, str]]:
    """Encuentra tablas Markdown, las reemplaza por marcadores, y las traduce."""
    table_pattern = re.compile(r'(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}', re.MULTILINE)
    tables_map = {}
    
    def replacer(match):
        t_id = f"TABLE_{len(tables_map) + 1}"
        table_content = match.group(0).strip()
        tables_map[t_id] = table_content
        return f"\n\n<!-- {t_id} -->\n\n"
        
    modified_markdown = table_pattern.sub(replacer, markdown)
    
    translated_tables = {}
    if tables_map and gemini_client:
        print(f"  📊 Detectadas {len(tables_map)} tablas. Traducción aislada en progreso...", flush=True)
        tasks = [translate_table_gemini(content, gemini_client) for content in tables_map.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for (t_id, _), res in zip(tables_map.items(), results):
            if isinstance(res, str):
                translated_tables[t_id] = res
            else:
                translated_tables[t_id] = tables_map[t_id]
                
    return modified_markdown, translated_tables

def restore_tables(markdown: str, tables_map: dict[str, str]) -> str:
    """Reinserta las tablas traducidas en su posición original."""
    for t_id, content in tables_map.items():
        markdown = markdown.replace(f"<!-- {t_id} -->", f"\n\n{content}\n\n")
    return markdown

# ══════════════════════════════════════════════════
# TRADUCCIÓN CON GEMINI FLASH
# ══════════════════════════════════════════════════

def chunk_markdown(markdown: str, max_chars: int = 12000) -> list[str]:
    """
    Divide el Markdown en chunks controlados (~12000 caracteres) respetando párrafos, páginas 
    y preferentemente agrupando por encabezados lógicos (#, ##) para preservar el contexto semántico.
    """
    paragraphs = markdown.split('\n\n')
    chunks = []
    current = ""
    last_seen_page = 1

    for p in paragraphs:
        match = re.search(r'<!-- PAGE:(\d+) -->', p)
        if match:
            last_seen_page = int(match.group(1))

        is_heading = re.match(r'^#{1,3}\s+', p.strip()) is not None

        if (len(current) + len(p) > max_chars and current) or (is_heading and len(current) > max_chars * 0.7):
            chunks.append(current.strip())
            current = f"<!-- PAGE:{last_seen_page} -->\n\n"
            
        current += p + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks

async def detect_document_language(first_page_text: str, client: genai.Client) -> str:
    """Detecta el idioma original del documento usando el primer fragmento."""
    if not first_page_text.strip(): return "Inglés"
    try:
        resp = await client.aio.models.generate_content(
            model=MODELS[0],
            contents=f"Detect the primary language of this academic text. Return ONLY the language name (e.g. English, French, Portuguese, German). Do not return anything else.\n\n{first_page_text[:1500]}",
            config=types.GenerateContentConfig(temperature=0.0)
        )
        return resp.text.strip() if resp.text else "Inglés"
    except:
        return "Inglés"

async def translate_chunk_gemini(chunk: str, client: genai.Client, active_models: list[str] | None = None, max_retries: int = 3, chunk_num: int = 1, total_chunks: int = 1, doc_lang: str = "Inglés") -> str:
    """Traduce un bloque de Markdown usando Gemini, con timeout y reintentos en cascada entre varios modelos."""
    if not chunk or not chunk.strip():
        return ""

    if active_models is None:
        active_models = list(MODELS)

    system_instruction = (
        f"Eres un traductor académico profesional y exhaustivo. El idioma origen es {doc_lang}. "
        "Tu misión es traducir TODO el texto científico al español de forma fiel, rigurosa, completa y palabra por palabra.\n\n"
        "REGLAS CRÍTICAS E INQUEBRANTABLES:\n"
        "1. INTEGRIDAD TOTAL: Está TERMINANTEMENTE PROHIBIDO saltarse páginas o recortar contenido. Traduce TODO.\n"
        "2. NUNCA RESUMAS: No hagas síntesis, resúmenes ejecutivos ni recortes.\n"
        "3. FORMATO DE TÍTULOS: Usa estrictamente sintaxis Markdown estándar para los encabezados (`# Título`, `## Subtítulo`, `### Sección`). NUNCA uses etiquetas literales como `[H1]`.\n"
        "4. MARCADORES DE PÁGINA: Si aparecen marcas de página, NUNCA partas una oración o párrafo en dos por culpa del salto de página. Mantén la oración unida fluidamente.\n"
        "5. CONSISTENCIA TERMINOLÓGICA: Mantén un criterio unificado para la traducción de siglas y conceptos técnicos en todo el texto. Si el documento es de psicología, aplica convenciones estándar cuando aparezcan (ej. MBIs -> Intervenciones basadas en Mindfulness (IBM), TFA -> Marco Teórico de Aceptabilidad).\n"
        "6. PROHIBICIÓN DE CALCOS LITERALES: Evita anglicismos innecesarios. Por ejemplo, usa 'versus' en lugar de forzar 'frente a' en títulos o comparaciones científicas.\n"
        "7. FLUIDEZ Y PRECISIÓN ACADÉMICA: Asegura un español científico impecable, natural y riguroso, corrigiendo posibles errores de OCR.\n"
        "8. UNIFICACIÓN DE PÁRRAFOS Y ORACIONES CORTADAS: En los PDFs las oraciones con frecuencia quedan cortadas por saltos de página o columnas (por ejemplo, terminando una línea con 'en', '(', etc., y continuando en la siguiente con minúscula o paréntesis de cierre). ESTÁ ESTRICTAMENTE PROHIBIDO dejar oraciones partidas en párrafos separados. Debes unir el texto para que forme un párrafo continuo y natural, sin saltos de línea injustificados en medio de una frase.\n"
        "9. BLOQUES DE AFILIACIONES, AUTORES Y NOTAS AL PIE: Si encuentras datos de afiliaciones o correspondencia en medio del texto, TRADÚCELOS COMO UN PÁRRAFO SEPARADO y no los unas al texto narrativo principal.\n"
        "NO agregues prefacios, introducciones ni notas adicionales al final."
    )

    # Inyección de directiva para el primer chunk para evitar que se salte la primera página
    if chunk_num == 1:
        chunk = "ESTE ES EL COMIENZO DEL DOCUMENTO (Portada/Abstract). DEBES TRADUCIR DESDE LA PRIMERA PALABRA, incluyendo título, autores y afiliaciones.\n\n" + chunk

    for attempt in range(1, max_retries + 1):
        models_to_try = list(active_models)
        for model_name in models_to_try:
            try:
                print(f"      ↳ [Chunk {chunk_num}/{total_chunks}] Intento {attempt}/{max_retries} usando [{model_name}]...", flush=True)
                
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=chunk,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1
                    )
                )
                
                if response.text and response.text.strip():
                    if model_name in active_models:
                        active_models.remove(model_name)
                        active_models.insert(0, model_name)
                    return response.text.strip()
                    
                print(f"      [WARN] Respuesta vacía de {model_name}.", flush=True)
                
            except Exception as err:
                err_str = str(err).lower()
                print(f"      [WARN] Error con {model_name}: {err}", flush=True)
                
                if "504" in str(err) or "deadline" in err_str or "429" in str(err) or "resource_exhausted" in err_str or "rate" in err_str or "503" in str(err) or "400" in str(err):
                    print(f"      🔄 Cambiando al siguiente modelo... (Moviendo [{model_name}] al final de la cola)", flush=True)
                    if model_name in active_models:
                        active_models.remove(model_name)
                        active_models.append(model_name)
                    continue
                
                continue
                
        wait = 15 * attempt
        print(f"      ⏸ Todos los modelos fallaron en intento {attempt}. Esperando {wait}s...", flush=True)
        await asyncio.sleep(wait)

    print("      [WARN] Todos los intentos fallaron; conservando original.", flush=True)
    return chunk


async def translate_full_markdown(markdown: str, doc_lang: str) -> str:
    """
    Traduce el Markdown iterando secuencialmente sobre bloques asegurando la totalidad del texto.
    """
    if not gemini_client:
        print("  [ERROR] No se encontró GEMINI_API_KEY o el cliente no se inicializó. Devolviendo texto original.")
        return markdown

    chunks = chunk_markdown(markdown, max_chars=12000)
    total_chunks = len(chunks)
    translated_chunks = []

    active_models = list(MODELS)

    print(f"\n  🚀 Iniciando traducción exhaustiva (total {total_chunks} fragmentos)...", flush=True)
    t0 = time.time()

    for i, chunk in enumerate(chunks):
        page_matches = re.findall(r'<!-- PAGE:(\d+) -->', chunk)
        pages_info = f"(Páginas: {', '.join(sorted(set(page_matches), key=int))})" if page_matches else ""
        print(f"  ⏳ Procesando fragmento {i+1}/{total_chunks} {pages_info} ({len(chunk)} chars)...", flush=True)
        t_chunk = time.time()
        
        translated_text = await translate_chunk_gemini(
            chunk, 
            gemini_client, 
            active_models=active_models,
            chunk_num=i+1,
            total_chunks=total_chunks,
            doc_lang=doc_lang
        )
        translated_chunks.append(translated_text)
        
        elapsed = round(time.time() - t_chunk, 1)
        print(f"  ✓ Fragmento {i+1}/{total_chunks} completado en {elapsed}s.", flush=True)

        if i < total_chunks - 1:
            await asyncio.sleep(DELAY_BETWEEN_CHUNKS_SEC)

    elapsed_total = round(time.time() - t0, 1)
    print(f"  ✨ Traducción 100% completada en {elapsed_total}s.\n", flush=True)
    return "\n\n".join(translated_chunks)


def is_affiliation_or_meta(b: str) -> bool:
    s = b.strip()
    if not s or s.startswith('#') or s.startswith('!') or s.startswith('|'):
        return False
    has_email = bool(re.search(r'[\w\.-]+@[\w\.-]+\.\w+|e-mail:|email:|correo electrónico:', s, re.I))
    affil_keywords = [
        'department of', 'departamento de', 'division of', 'división de', 
        'section on', 'sección de', 'institute of', 'instituto de', 
        'university', 'universidad', 'school of', 'escuela de', 
        'faculty of', 'facultad de', 'hospital', 'laboratory of', 
        'laboratorio de', 'center for', 'centro de', 'dirp', 'nih', 'nimh', 
        'clinic', 'clínica', 'unit', 'unidad de'
    ]
    keyword_hits = sum(1 for kw in affil_keywords if re.search(r'\b' + kw + r'\b', s, re.I))
    has_author_sym = bool(re.search(r'\(&\)|\bcorrespondence\b|\bcorresponding author\b|\bautor de correspondencia\b|\baddress correspondence\b', s, re.I))
    has_address = bool(re.search(r'\b(?:USA|UK|Spain|France|Germany|Bethesda|MD\s*\d{5}|MO\s*\d{5}|Room\s*\d+|Box\s*\d+|P\.?O\.?\s*Box)\b', s, re.I))
    has_editorial = bool(re.search(r'\b(?:received:\s*\d|accepted:\s*\d|published online:|doi:\s*10\.|copyright\s*©|©\s*\d{4})\b', s, re.I))
    
    if has_editorial: return True
    if has_email: return True
    if keyword_hits >= 1 and (has_author_sym or has_address): return True
    if keyword_hits >= 2: return True
    return False

def relocate_affiliations_and_meta(text: str) -> str:
    """Extrae bloques de afiliaciones/autores/notas al pie que hayan quedado en medio del texto y los mueve a la cabecera."""
    if not text:
        return ""
    paragraphs = text.split('\n\n')
    if len(paragraphs) < 3:
        return text

    body_paragraphs = []
    extracted_affils = []
    
    for idx, p in enumerate(paragraphs):
        if idx < 2:
            body_paragraphs.append(p)
            continue
        if is_affiliation_or_meta(p):
            extracted_affils.append(p.strip())
        else:
            body_paragraphs.append(p)

    if not extracted_affils:
        return text

    # Formatear las afiliaciones extraídas en un bloque limpio de front-matter
    cleaned_affils = [' '.join(line.strip() for line in aff.split('\n') if line.strip()) for aff in extracted_affils]
    affil_section = '> **Afiliaciones y Correspondencia:**\n> ' + '\n>\n> '.join(cleaned_affils)

    insert_pos = min(2, len(body_paragraphs))
    body_paragraphs.insert(insert_pos, affil_section)
    return '\n\n'.join(body_paragraphs)


def clean_and_join_broken_paragraphs(text: str) -> str:
    """Limpia ruido del PDF y une oraciones cortadas por saltos de página o columna."""
    if not text:
        return ""
    
    # 0. Reubicar bloques de autores, afiliaciones y metadatos fuera del cuerpo del texto
    text = relocate_affiliations_and_meta(text)
    
    # 1. Eliminar números de página flotantes aislados
    text = re.sub(r'(?m)^\s*\d+\s*$\n?', '', text)
    
    # 2. Eliminar marcadores <!-- PAGE:X --> para evitar que rompan párrafos
    text = re.sub(r'\s*<!-- PAGE:\d+ -->\s*', ' ', text)
    
    # 3. Unir palabras separadas por guión de fin de línea
    text = re.sub(r'(\b[\wáéíóúñÁÉÍÓÚÑ]+)-\s*\n+\s*([\wáéíóúñÁÉÍÓÚÑ]+\b)', r'\1\2', text)
    
    # 4. Unir paréntesis o corchetes cortados antes de una línea siguiente
    text = re.sub(r'([(\[{])\s*\n+\s*', r'\1', text)
    
    # 5. Unir líneas y párrafos rotos donde la primera no termina en signo terminal
    lines = text.split('\n')
    joined_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        while i + 1 < len(lines):
            next_line = lines[i + 1]
            
            # Caso 1: Salto simple (\n)
            if next_line.strip() and not next_line.strip().startswith(('#', '*', '-', '|', '>', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', '`')):
                curr_stripped = line.rstrip()
                next_stripped = next_line.lstrip()
                
                if curr_stripped and not curr_stripped.endswith(('.', '!', '?', ':', '#', '---', '***')) and not curr_stripped.startswith(('#', '*', '-', '|', '>', '`')):
                    if not is_affiliation_or_meta(next_stripped):
                        is_curr_cut = bool(re.search(r'[-–—(¿¡]$|(?:\b(?:en|de|del|la|el|los|las|un|una|con|por|para|y|o|que|a|al|su|sus|como)\s*)$', curr_stripped, re.I))
                        is_next_cont = bool(re.match(r'^[a-záéíóúñ\(\),;\]]', next_stripped))
                        if is_next_cont or is_curr_cut:
                            line = curr_stripped + ' ' + next_stripped
                            i += 1
                            continue
                        
            # Caso 2: Salto doble (\n\n) accidental
            if not next_line.strip() and i + 2 < len(lines):
                after_empty = lines[i + 2]
                curr_stripped = line.rstrip()
                after_stripped = after_empty.lstrip()
                
                if curr_stripped and not curr_stripped.endswith(('.', '!', '?', ':', '#', '---', '***')) and not curr_stripped.startswith(('#', '*', '-', '|', '>', '`')):
                    if not after_stripped.startswith(('#', '*', '-', '|', '>', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', '`')):
                        if not is_affiliation_or_meta(after_stripped):
                            is_curr_cut = bool(re.search(r'[-–—(¿¡]$|(?:\b(?:en|de|del|la|el|los|las|un|una|con|por|para|y|o|que|a|al|su|sus|como)\s*)$', curr_stripped, re.I))
                            is_after_cont = bool(re.match(r'^[a-záéíóúñ\(\),;\]]', after_stripped))
                            if is_after_cont or is_curr_cut:
                                line = curr_stripped + ' ' + after_stripped
                                i += 2
                                continue
            break
            
        joined_lines.append(line)
        i += 1
        
    text = '\n'.join(joined_lines)
    # 6. Reducir saltos de línea excesivos
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text

def preprocess_raw_markdown(md_text: str) -> str:
    return clean_and_join_broken_paragraphs(md_text)

def postprocess_markdown(md_text: str) -> str:
    return clean_and_join_broken_paragraphs(md_text)


async def process_pdf_bytes_translation(pdf_bytes: bytes, paper_id: Optional[str] = None, force_retranslate: bool = False, source_url: str = "") -> dict:
    """Procesa los bytes de un PDF: Extrae, traduce, inyecta base64."""
    clean_pid = clean_paper_id(paper_id, fallback=source_url or hashlib.sha256(pdf_bytes).hexdigest()[:16])
    file_id = safe_id(f"{clean_pid}_gemini")
    cache_file = CACHE_DIR / f"{file_id}.json"
    pdf_file_path = CACHE_DIR / f"{file_id}.pdf"
    
    # Guardar el PDF original en caché para poder servirlo a través de /files/
    if not pdf_file_path.exists() or force_retranslate:
        pdf_file_path.write_bytes(pdf_bytes)

    if cache_file.exists() and not force_retranslate:
        print(f"[CACHE HIT] {clean_pid} (Gemini)", flush=True)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    print(f"[PROCESSING] {clean_pid} ({len(pdf_bytes)} bytes) [Motor: Gemini Flash]", flush=True)

    # 1 y 2. Extraer Markdown y las imágenes manteniendo el orden y referencias (pymupdf4llm)
    raw_markdown, images = extract_markdown_and_images(pdf_bytes)
    raw_markdown = preprocess_raw_markdown(raw_markdown)
    print(f"  Markdown generado: {len(raw_markdown)} caracteres. Imágenes extraídas: {len(images)}", flush=True)

    # Detectar Idioma ANTES de procesar tablas
    doc_lang = await detect_document_language(raw_markdown[:1500], gemini_client) if gemini_client else "Inglés"
    print(f"  🌐 Idioma detectado: {doc_lang}", flush=True)

    if "español" in doc_lang.lower() or "spanish" in doc_lang.lower() or doc_lang.lower().strip() == "es":
        print("  ✅ El documento ya está en español. Omitiendo traducción.", flush=True)
        translated = raw_markdown
    else:
        # 2.5 Extraer y traducir tablas aisladas
        raw_markdown_no_tables, translated_tables = await extract_and_translate_tables(raw_markdown)

        # 3. Traducir el Markdown (sin el peso del Base64 y sin las tablas que se traducen solas)
        translated = await translate_full_markdown(raw_markdown_no_tables, doc_lang)
        print(f"  Traducción completada: {len(translated)} caracteres", flush=True)

        # 3.5 Restaurar Tablas Traducidas
        translated = restore_tables(translated, translated_tables)

    # 4. Embedir imágenes en el Markdown ya traducido
    final_markdown = replace_image_refs_with_base64(translated, images)

    # 5. Inyectar la URL final del PDF en los enlaces #page=
    final_pdf_url = f"/files/{file_id}.pdf"
    final_markdown = final_markdown.replace("](#page=", f"]({final_pdf_url}#page=")

    # 6. Convertir los enlaces al PDF / #page= a etiquetas HTML para el visualizador interno
    final_markdown = re.sub(
        r'\[([^\]]+)\]\(([^)]*(?:\.pdf(?:#[^)]*)?|#page=\d+))\)',
        r'<a href="#" class="internal-pdf-link" data-url="\2">\1</a>',
        final_markdown
    )

    # 6.5 Limpiar párrafos rotos residuales y marcadores de página del Markdown final
    final_markdown = postprocess_markdown(final_markdown)

    # 7. Guardar en caché solo si la traducción fue completa
    result = {
        "markdown": final_markdown,
        "file_id": file_id,
        "pdf_url": final_pdf_url
    }
    if "> [!NOTE]\n> **Traducción parcial**" not in final_markdown:
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        
    return result


async def process_pdf_translation(url: str, paper_id: Optional[str] = None, force_retranslate: bool = False) -> dict:
    """Descarga el PDF desde la URL y lo traduce."""
    clean_pid = clean_paper_id(paper_id, fallback=url)
    file_id = safe_id(f"{clean_pid}_gemini")
    cache_file = CACHE_DIR / f"{file_id}.json"
    if cache_file.exists() and not force_retranslate:
        print(f"[CACHE HIT] {clean_pid} (Gemini)", flush=True)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    pdf_bytes = await download_pdf(url)
    return await process_pdf_bytes_translation(pdf_bytes, paper_id=clean_pid, force_retranslate=force_retranslate, source_url=url)


# ══════════════════════════════════════════════════
# ENDPOINTS REST (Para la App PsiHub)
# ══════════════════════════════════════════════════

@api.post("/api/translate")
async def api_translate(req: TranslateRequest):
    """Endpoint consumido por PsiHub cuando hay URL directa."""
    target_id = req.get_paper_id()
    data = await process_pdf_translation(req.url, paper_id=target_id, force_retranslate=req.force)
    return JSONResponse(content=data)


@api.post("/api/translate-file")
async def api_translate_file(
    file: UploadFile = File(...),
    paper_id: Optional[str] = Form(None),
    id: Optional[str] = Form(None),
    force: bool = Form(False)
):
    """Endpoint para subir un archivo PDF directamente."""
    pdf_bytes = await file.read()
    if not pdf_bytes or len(pdf_bytes) < 100:
        raise HTTPException(status_code=400, detail="Archivo PDF inválido o vacío.")
    target_id = paper_id or id
    clean_pid = clean_paper_id(target_id, fallback=file.filename or "uploaded.pdf")
    data = await process_pdf_bytes_translation(pdf_bytes, paper_id=clean_pid, force_retranslate=force, source_url="")
    return JSONResponse(content=data)

@api.post("/api/check-pdf")
async def api_check_pdf(req: CheckRequest):
    """Verifica si la URL es descargable sin protecciones."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
        try:
            async with client.stream("GET", req.url) as resp:
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" not in content_type:
                        return {"status": "ok"}
        except Exception:
            pass
    return {"status": "blocked"}


@api.get("/health")
async def health():
    return {
        "status": "ok",
        "gemini_configured": bool(GEMINI_API_KEY),
        "primary_model": MODELS[0] if MODELS else None,
        "cache_entries": len(list(CACHE_DIR.glob("*.json")))
    }


# ══════════════════════════════════════════════════
# INTERFAZ WEB GRADIO (Pruebas manuales: URL o Archivo)
# ══════════════════════════════════════════════════

def gradio_translate(pdf_url: str, pdf_file, force: bool):
    md = ""
    # 1. Si el usuario subió un archivo PDF local
    if pdf_file is not None:
        try:
            with open(pdf_file, "rb") as f:
                pdf_bytes = f.read()
            file_name = Path(pdf_file).name
            res = asyncio.run(process_pdf_bytes_translation(pdf_bytes, file_name, force, source_url=""))
            md = res.get("markdown", "Sin contenido traducido.")
        except Exception as e:
            md = f"❌ Error al procesar archivo PDF subido: {str(e)}"

    # 2. Si el usuario ingresó una URL
    elif pdf_url and pdf_url.strip():
        try:
            res = asyncio.run(process_pdf_translation(pdf_url.strip(), pdf_url.strip(), force))
            md = res.get("markdown", "Sin contenido traducido.")
        except Exception as e:
            md = f"❌ Error al traducir el PDF: {str(e)}"

    else:
        md = "⚠️ Por favor ingresa una URL de PDF válida o arrastra un archivo PDF."
        
    # Guardar en archivo temporal para descargar
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write(md)
        tmp_path = f.name
        
    return md, tmp_path


with gr.Blocks(title="PsiHub Reader") as demo:
    gr.Markdown("# 📖 PsiHub Reader API")
    gr.Markdown("Servicio de traducción académica mediante **Gemini 1.5 Flash**. Traduce por URL o subiendo tu archivo PDF.")
    
    with gr.Row():
        with gr.Column(scale=1):
            url_input = gr.Textbox(
                label="Opción 1: URL del PDF Open Access", 
                placeholder="https://.../paper.pdf"
            )
            file_input = gr.File(
                label="Opción 2: O sube tu PDF aquí directamente (Drag & Drop)",
                file_types=[".pdf"],
                type="filepath"
            )
            force_checkbox = gr.Checkbox(label="🔄 Forzar nueva traducción (ignorar caché)", value=False)
            btn_translate = gr.Button("Traducir Paper Completo", variant="primary")
            
        with gr.Column(scale=2):
            output_md = gr.Markdown(label="Traducción en Modo Lectura")
            output_file = gr.File(label="Descargar Documento Markdown")

    btn_translate.click(
        fn=gradio_translate, 
        inputs=[url_input, file_input, force_checkbox], 
        outputs=[output_md, output_file]
    )


# Montar la interfaz Gradio sobre la aplicación FastAPI
app = gr.mount_gradio_app(api, demo, path="/")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print("\n🚀 Servidor de traducción iniciado:")
    print(f"👉 Interfaz web: http://localhost:{port}")
    print(f"👉 API Endpoint: http://localhost:{port}/api/translate\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
