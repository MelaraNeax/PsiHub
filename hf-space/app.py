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


def extract_markdown_and_images(pdf_bytes: bytes) -> tuple[str, dict[str, str]]:
    """Convierte PDF a Markdown estructurado y extrae imágenes usando pymupdf4llm para preservar la ubicación exacta."""
    import shutil
    
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    img_dir = tempfile.mkdtemp()
    images_b64 = {}
    
    try:
        # Extraemos por páginas para inyectar marcadores de paginación
        md_chunks = pymupdf4llm.to_markdown(tmp_path, write_images=True, image_path=img_dir, page_chunks=True)
        
        full_md = []
        for chunk in md_chunks:
            page_num = chunk.get("metadata", {}).get("page", 0) + 1
            page_text = chunk.get("text", "")
            
            # Forzamos la inserción de un enlace a la página exacta original bajo CADA imagen,
            # así garantizamos que se preserve el link (y la IA solo traduce el pie de página).
            page_text = re.sub(
                r'(!\[[^\]]*\]\([^)]+\))', 
                r'\1\n\n[Ver imagen en PDF original - pág. ' + str(page_num) + r'](#page=' + str(page_num) + r')\n\n', 
                page_text
            )
            
            full_md.append(f"\n\n<!-- PAGE:{page_num} -->\n\n{page_text}")
        
        md_text = "\n".join(full_md)
        
        # Cargar las imágenes extraídas a base64 (Filtrando las < 10KB)
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
        os.unlink(tmp_path)
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
# TRADUCCIÓN CON GEMINI FLASH
# ══════════════════════════════════════════════════

def chunk_markdown(markdown: str, max_chars: int = 15000) -> list[str]:
    """
    Divide el Markdown en chunks muy grandes (~15000 caracteres, aprox 3000-4000 tokens).
    Gemini 1.5 Flash soporta hasta 1M tokens de entrada, pero lo dividimos para asegurar
    que la respuesta no supere el límite de salida (8192 tokens).
    """
    paragraphs = markdown.split('\n\n')
    chunks = []
    current = ""
    last_seen_page = 1

    for p in paragraphs:
        match = re.search(r'<!-- PAGE:(\d+) -->', p)
        if match:
            last_seen_page = int(match.group(1))

        if len(current) + len(p) > max_chars and current:
            chunks.append(current.strip())
            # Al iniciar un nuevo chunk, inyectar el último marcador de página
            # para que la IA no pierda el contexto (sino asumirá la pág 1)
            current = f"<!-- PAGE:{last_seen_page} -->\n\n"
            
        current += p + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks


async def translate_chunk_gemini(chunk: str, client: genai.Client, active_models: list[str] | None = None, max_retries: int = 3) -> str:
    """Traduce un bloque de Markdown usando Gemini, con timeout y reintentos en cascada entre varios modelos."""
    if not chunk or not chunk.strip():
        return ""

    if active_models is None:
        active_models = list(MODELS)

    system_instruction = (
        "Eres un traductor académico experto. Traduce TODO el texto científico al español de forma fiel, rigurosa y fluida. No deben haber frases/parrafos enteros en inglés.\n\n"
        "REGLAS CRÍTICAS DE LIMPIEZA Y FORMATO:\n"
        "1. FORMATO DE TÍTULOS: Usa estrictamente sintaxis Markdown estándar para los encabezados (`# Título`, `## Subtítulo`, etc). NUNCA uses etiquetas literales como `[H1]`, `[H2]`, etc.\n"
        "2. MARCADORES DE PÁGINA: El texto contiene marcadores ocultos `<!-- PAGE:X -->` que indican la página original del PDF.\n"
        "3. SECCIONES A OMITIR (Referencias, Bibliografía, Agradecimientos): NO las traduzcas. Sustitúyelas por `[Ver sección en PDF original - pág. N](#page=N)` (reemplazando N por el número exacto del último marcador PAGE visto) y CONTINÚA traduciendo el resto del documento.\n"
        "4. ARREGLA ERRORES DE OCR: Corrige consistencia en letras y mayúsculas en títulos. Elimina símbolos o letras rotas.\n"
        "5. PRESERVA EXACTAMENTE las tablas e imágenes (las que queden en el texto) y sus enlaces en su posición original.\n"
        "6. Al inicio, aclara título, autor, fecha (si incluye), revista (si incluye) y cualquier información relevante como presentación del documento, si un titulo se repite constantemente a lo largo del documento, solo aclaralo al inicio.\n"
        "7. En cuanto a la calidad de redacción, asegurate de que el texto sea fluído y natural, evita repeticiones y redundancias, asegúrate de que el texto sea coherente y tenga sentido, corrige errores gramaticales y ortográficos.\n"
        "8. Si encuentras tablas, asegurate de preservar el formato de tablas sin errores ni desorganización, mantenla humanamente legible.\n"
        "NO resumas el contenido. NO agregues notas adicionales al inicio o final de tu respuesta."
    )

    for attempt in range(1, max_retries + 1):
        models_to_try = list(active_models)
        for model_name in models_to_try:
            try:
                print(f"      ↳ Intento {attempt}/{max_retries} usando [{model_name}]...", flush=True)
                
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=chunk,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1
                        # Se eliminó thinking_config para evitar el Error 400 en modelos Flash
                    )
                )
                
                if response.text and response.text.strip():
                    # Si tuvo éxito, lo aseguramos al principio de la lista para el próximo chunk
                    if model_name in active_models:
                        active_models.remove(model_name)
                        active_models.insert(0, model_name)
                    return response.text.strip()
                    
                print(f"      [WARN] Respuesta vacía de {model_name}.", flush=True)
                
            except Exception as err:
                err_str = str(err).lower()
                print(f"      [WARN] Error con {model_name}: {err}", flush=True)
                
                # Si el servidor rechaza la petición o da 504, lo movemos al final de la cola (menor prioridad)
                if "504" in str(err) or "deadline" in err_str or "429" in str(err) or "resource_exhausted" in err_str or "rate" in err_str or "503" in str(err) or "400" in str(err):
                    print(f"      🔄 Cambiando al siguiente modelo... (Moviendo [{model_name}] al final de la cola)", flush=True)
                    if model_name in active_models:
                        active_models.remove(model_name)
                        active_models.append(model_name)
                    continue
                
                # Para otros errores, también probamos el siguiente modelo
                continue
                
        # Si todos los modelos de la lista fallaron en este intento, esperar antes de reiniciar el ciclo
        wait = 20 * attempt
        print(f"      ⏸ Todos los modelos fallaron. Esperando {wait}s antes del próximo intento general...", flush=True)
        await asyncio.sleep(wait)

    print("      [WARN] Todos los intentos fallaron; conservando original.", flush=True)
    return chunk


async def translate_full_markdown(markdown: str) -> str:
    """
    Traduce el Markdown iterando secuencialmente sobre bloques grandes.
    """
    if not gemini_client:
        print("  [ERROR] No se encontró GEMINI_API_KEY o el cliente no se inicializó. Devolviendo texto original.")
        return markdown

    # Usamos chunks enormes (25000 chars) porque Gemini Flash puede generar hasta 8192 tokens de salida.
    # 25000 chars ~ 5000 tokens, por lo que entra perfectamente en el límite de salida sin cortarse.
    # Esto reduce un paper de 96k chars a solo 4 peticiones en lugar de 26, evitando el Error 503 y bloqueos.
    chunks = chunk_markdown(markdown, max_chars=25000)
    total_chunks = len(chunks)
    translated_chunks = []

    # Copia local de los modelos disponibles para priorizarlos dinámicamente en este procesamiento
    active_models = list(MODELS)

    print(f"\n  🚀 Iniciando traducción con sistema Multi-Modelo de Respaldo (total {total_chunks} fragmentos grandes)...", flush=True)
    t0 = time.time()

    for i, chunk in enumerate(chunks):
        print(f"  ⏳ Procesando fragmento {i+1}/{total_chunks} ({len(chunk)} chars)...", flush=True)
        t_chunk = time.time()
        
        translated_text = await translate_chunk_gemini(chunk, gemini_client, active_models=active_models)
        translated_chunks.append(translated_text)
        
        elapsed = round(time.time() - t_chunk, 1)
        print(f"  ✓ Fragmento {i+1} completado en {elapsed}s.", flush=True)

        if i < total_chunks - 1:
            # Pausa para no saturar los límites gratuitos de Gemini (15 RPM)
            await asyncio.sleep(DELAY_BETWEEN_CHUNKS_SEC)

    elapsed_total = round(time.time() - t0, 1)
    print(f"  ✨ Traducción 100% completada en {elapsed_total}s.\n", flush=True)
    return "\n\n".join(translated_chunks)


def preprocess_raw_markdown(md_text: str) -> str:
    """Aplica expresiones regulares para limpiar ruido del PDF antes de traducir."""
    # 1. Eliminar números de línea sueltos (típicos de márgenes)
    md_text = re.sub(r'(?m)^\s*\d+\s*$\n?', '', md_text)
    
    # 2. Unir palabras cortadas por guión al final de la línea (hyphenation)
    md_text = re.sub(r'(\w+)-\n(\w+)', r'\1\2', md_text)
    
    # 3. Limpiar saltos de línea redundantes
    md_text = re.sub(r'\n{3,}', '\n\n', md_text)
    
    return md_text


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

    # 3. Traducir el Markdown (sin el peso del Base64)
    translated = await translate_full_markdown(raw_markdown)
    print(f"  Traducción completada: {len(translated)} caracteres", flush=True)

    # 4. Embedir imágenes en el Markdown ya traducido
    final_markdown = replace_image_refs_with_base64(translated, images)

    # 5. Inyectar la URL final del PDF en los enlaces #page= (reemplaza #page= con http...#page=)
    final_pdf_url = source_url if (source_url and source_url.startswith("http")) else f"/files/{file_id}.pdf"
    final_markdown = final_markdown.replace("](#page=", f"]({final_pdf_url}#page=")

    # 6. Convertir los enlaces #page= a etiquetas HTML <a target="_blank"> para asegurar que el navegador abra el PDF y salte a la página correcta
    final_markdown = re.sub(
        r'\[([^\]]+)\]\(([^)]+#page=\d+)\)',
        r'<a href="\2" target="_blank">\1</a>',
        final_markdown
    )

    # 7. Guardar en caché solo si la traducción fue completa
    result = {"markdown": final_markdown}
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
