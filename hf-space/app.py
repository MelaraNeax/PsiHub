"""
PsiHub Reader — Servicio de traducción de PDFs académicos
Hugging Face Space (Gradio SDK + FastAPI)

Flujo:
  1. Recibe la URL de un PDF open access (desde PsiHub o desde la web)
  2. Descarga el PDF
  3. Extrae Markdown estructurado + imágenes con pymupdf4llm / PyMuPDF
  4. Traduce sección por sección con Groq (openai/gpt-oss-20b con fallback a 120b)
  5. Pausa de 20s entre chunks para respetar límites TPM
  6. Devuelve el Markdown traducido con imágenes embebidas en base64
  7. Cachea resultados en disco para evitar reprocesamiento
"""

import os
import re
import time
import json
import hashlib
import tempfile
import base64
import asyncio
from pathlib import Path

import httpx
import pymupdf
import pymupdf as fitz
import pymupdf4llm
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from groq import Groq
# pyrefly: ignore [missing-import]
import gradio as gr
import uvicorn

# ══════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"
DELAY_BETWEEN_CHUNKS_SEC = 2
CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

api = FastAPI(title="PsiHub Reader API", version="1.0.0")

# CORS abierto para que PsiHub (GitHub Pages / Capacitor) pueda consumir la API
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TranslateRequest(BaseModel):
    url: str   # URL del PDF open access
    id: str    # ID del paper (para caché)


# ══════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════

def safe_id(paper_id: str) -> str:
    """Genera un nombre de archivo seguro a partir del ID del paper."""
    return hashlib.sha256(paper_id.encode()).hexdigest()[:24]


async def download_pdf(url: str) -> bytes:
    """Descarga el PDF siguiendo redirecciones."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"No se pudo descargar el PDF (HTTP {resp.status_code})"
            )
        content_type = resp.headers.get("content-type", "")
        # Algunos servidores devuelven HTML en vez de PDF
        if "text/html" in content_type and len(resp.content) < 50000:
            raise HTTPException(
                status_code=502,
                detail="El servidor devolvió HTML en vez de un PDF. Es posible que el acceso al paper esté restringido."
            )
        return resp.content


def extract_images_from_pdf(pdf_bytes: bytes) -> dict[str, str]:
    """
    Extrae todas las imágenes del PDF y las devuelve como 
    dict[nombre_referencia] = data:image/...;base64,...
    """
    images = {}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        img_counter = 0
        for page_num in range(len(doc)):
            page = doc[page_num]
            image_list = page.get_images(full=True)
            for img_info in image_list:
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    if base_image and base_image.get("image"):
                        img_data = base_image["image"]
                        ext = base_image.get("ext", "png")
                        mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
                        b64 = base64.b64encode(img_data).decode("utf-8")
                        ref_name = f"img-{page_num:03d}-{img_counter:03d}.{ext}"
                        images[ref_name] = f"data:{mime};base64,{b64}"
                        img_counter += 1
                except Exception:
                    continue
        doc.close()
    except Exception as e:
        print(f"[WARN] Error extracting images: {e}")
    return images


def pdf_to_markdown(pdf_bytes: bytes) -> str:
    """Convierte PDF a Markdown estructurado con pymupdf4llm."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        md_text = pymupdf4llm.to_markdown(tmp_path)
        return md_text
    finally:
        os.unlink(tmp_path)


def embed_images_in_markdown(markdown: str, images: dict[str, str]) -> str:
    """
    Reemplaza las referencias a imágenes en el Markdown por las imágenes base64.
    pymupdf4llm genera referencias tipo ![imagen](img-000-000.png).
    También inserta las imágenes más relevantes entre secciones si no fueron referenciadas.
    """
    used_images = set()

    # Reemplazar referencias existentes
    def replace_img_ref(match):
        alt = match.group(1)
        ref = match.group(2)
        # Buscar coincidencia parcial (el nombre puede variar)
        for name, data_uri in images.items():
            if ref in name or name in ref or os.path.basename(ref) == name:
                used_images.add(name)
                return f"![{alt}]({data_uri})"
        return match.group(0)

    markdown = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_img_ref, markdown)

    # Si hay imágenes no referenciadas, insertarlas al final como figuras
    unreferenced = [
        (name, uri) for name, uri in images.items() if name not in used_images
    ]
    if unreferenced:
        markdown += "\n\n---\n\n## Figuras del artículo\n\n"
        for i, (name, uri) in enumerate(unreferenced, 1):
            markdown += f"![Figura {i}]({uri})\n\n"

    return markdown


# ══════════════════════════════════════════════════
# TRADUCCIÓN CON GROQ
# ══════════════════════════════════════════════════

def chunk_markdown(markdown: str, max_chars: int = 10000) -> list[str]:
    """
    Divide el Markdown en chunks por párrafos dobles, 
    respetando límites de tamaño para el modelo.
    """
    paragraphs = markdown.split('\n\n')
    chunks = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += p + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks


def translate_chunk(client: Groq, chunk: str) -> str:
    """Traduce un chunk de Markdown académico con Groq (20b con fallback a 120b)."""
    system_prompt = """Eres un traductor académico especializado en psicología y psicoterapia.
Tu tarea es traducir textos científicos del inglés al español.

REGLAS CRÍTICAS:
1. PRESERVA EXACTAMENTE la estructura Markdown: encabezados (#, ##, ###), listas, tablas, negritas (**), cursivas (*), links e imágenes (![...](...)). NO modifiques ninguna referencia a imágenes.
2. FIDELIDAD DE DATOS: NUNCA inventes, aproximes ni extrapoles números, porcentajes, años o tamaños de muestra. Solo menciona cifras si aparecen EXPLÍCITAMENTE en el texto.
3. TERMINOLOGÍA ACADÉMICA: Usa terminología correcta de psicología en español. Ejemplos:
   - "burnout" → "burnout" o "síndrome de desgaste profesional" (NUNCA "quemado")
   - "attachment" → "apego"
   - "mindfulness" → "mindfulness" (se mantiene en inglés por convención)
   - "coping" → "afrontamiento"
   - "self-efficacy" → "autoeficacia"
   - "well-being" → "bienestar"
4. Si un término técnico se usa habitualmente en inglés en la literatura hispana, mantenlo en inglés.
5. Devuelve ÚNICAMENTE la traducción, sin notas ni comentarios adicionales."""

    for model_name in [MODEL, FALLBACK_MODEL]:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Traduce el siguiente fragmento al español:\n\n{chunk}"}
                ],
                temperature=0.1,
                max_tokens=4096
            )
            return response.choices[0].message.content
        except Exception as err:
            print(f"    [WARN] Falló con {model_name}: {err}. Intentando fallback...")

    raise RuntimeError("No se pudo traducir el fragmento con ninguno de los modelos.")


def translate_full_markdown(markdown: str) -> str:
    """Traduce todo el Markdown por chunks con pausas para no saturar TPM."""
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY no configurada en el servidor."
        )

    client = Groq(api_key=GROQ_API_KEY)
    chunks = chunk_markdown(markdown, max_chars=10000)
    translated_chunks = []

    for i, chunk in enumerate(chunks):
        if i > 0:
            print(f"  ⏳ Pausa de {DELAY_BETWEEN_CHUNKS_SEC}s entre chunks para respetar límite TPM de Groq...")
            time.sleep(DELAY_BETWEEN_CHUNKS_SEC)

        print(f"  Traduciendo chunk {i+1}/{len(chunks)}...")
        try:
            translated = translate_chunk(client, chunk)
            translated_chunks.append(translated)
        except Exception as e:
            print(f"  [WARN] Error en chunk {i+1}: {e}")
            # Si falla un chunk, incluir el original
            translated_chunks.append(chunk)

    return "\n\n".join(translated_chunks)


async def process_pdf_translation(url: str, paper_id: str) -> dict:
    """Función central que orquesta la descarga, extracción y traducción."""
    file_id = safe_id(paper_id)
    cache_file = CACHE_DIR / f"{file_id}.json"

    # 1. Verificar caché
    if cache_file.exists():
        print(f"[CACHE HIT] {paper_id}")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    print(f"[PROCESSING] {paper_id} — {url}")

    # 2. Descargar PDF
    pdf_bytes = await download_pdf(url)
    print(f"  PDF descargado: {len(pdf_bytes)} bytes")

    # 3. Extraer imágenes del PDF
    images = extract_images_from_pdf(pdf_bytes)
    print(f"  Imágenes extraídas: {len(images)}")

    # 4. Convertir a Markdown
    raw_markdown = pdf_to_markdown(pdf_bytes)
    print(f"  Markdown generado: {len(raw_markdown)} caracteres")

    # 5. Embedir imágenes en el Markdown
    markdown_with_images = embed_images_in_markdown(raw_markdown, images)

    # 6. Traducir
    translated = translate_full_markdown(markdown_with_images)
    print(f"  Traducción completada: {len(translated)} caracteres")

    # 7. Guardar en caché
    result = {"markdown": translated}
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


# ══════════════════════════════════════════════════
# ENDPOINTS REST (Para la App PsiHub)
# ══════════════════════════════════════════════════

@api.post("/api/translate")
async def api_translate(req: TranslateRequest):
    """Endpoint consumido por PsiHub desde app.js."""
    data = await process_pdf_translation(req.url, req.id)
    return JSONResponse(content=data)


@api.get("/health")
async def health():
    return {
        "status": "ok",
        "groq_configured": bool(GROQ_API_KEY),
        "primary_model": MODEL,
        "fallback_model": FALLBACK_MODEL,
        "cache_entries": len(list(CACHE_DIR.glob("*.json")))
    }


# ══════════════════════════════════════════════════
# INTERFAZ WEB GRADIO (Para pruebas manuales)
# ══════════════════════════════════════════════════

try:
    # pyrefly: ignore [missing-import]
    import spaces
    @spaces.GPU
    def gradio_translate(pdf_url: str):
        if not pdf_url or not pdf_url.strip():
            return "Por favor ingresa una URL de PDF válida."
        try:
            res = asyncio.run(process_pdf_translation(pdf_url.strip(), pdf_url.strip()))
            return res.get("markdown", "Sin contenido traducido.")
        except Exception as e:
            return f"❌ Error al traducir el PDF: {str(e)}"
except ImportError:
    async def gradio_translate(pdf_url: str):
        if not pdf_url or not pdf_url.strip():
            return "Por favor ingresa una URL de PDF válida."
        try:
            res = await process_pdf_translation(pdf_url.strip(), pdf_url.strip())
            return res.get("markdown", "Sin contenido traducido.")
        except Exception as e:
            return f"❌ Error al traducir el PDF: {str(e)}"


with gr.Blocks(title="PsiHub Reader") as demo:
    gr.Markdown("# 📖 PsiHub Reader API")
    gr.Markdown("Servicio de traducción académica de papers científicos para **PsiHub**.")
    
    with gr.Row():
        url_input = gr.Textbox(
            label="URL del PDF Open Access", 
            placeholder="https://.../paper.pdf", 
            scale=4
        )
        btn_translate = gr.Button("Traducir Paper", variant="primary", scale=1)
        
    output_md = gr.Markdown(label="Traducción en Modo Lectura")
    btn_translate.click(fn=gradio_translate, inputs=url_input, outputs=output_md)


# Integrar endpoints FastAPI y middleware CORS directamente en Gradio
demo.app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
demo.app.include_router(api.router)

# Exportar 'app' por si Hugging Face o Uvicorn lo cargan como módulo (app:app)
app = demo.app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.queue().launch(server_name="0.0.0.0", server_port=port)
