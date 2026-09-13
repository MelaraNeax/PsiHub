"""PsiHub Reader - motor visual selectivo para reconstrucción de layout.

Diseño:
- La extracción barata (PyMuPDF/page_boxes) sigue siendo la fuente primaria.
- Vision actúa como árbitro semántico en fronteras ambiguas.
- Una sola imagen compuesta por frontera reduce tokens/costo frente a dos imágenes.
- Thread safety: cada worker abre su PROPIO fitz.Document.
- El PDF se abre una sola vez por worker y el cliente HTTP es persistente.
- Los diagnósticos se cachean para no volver a pagar Vision sobre el mismo PDF.
- La IA nunca modifica directamente el Markdown: devuelve evidencia estructural.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import fitz
import httpx
from PIL import Image, ImageOps, ImageDraw

VISION_API_KEYS = [
    os.getenv(f"DEEPSEEK_VISION_API_KEY_{i}", "").strip()
    for i in range(1, 7)
]
if not any(VISION_API_KEYS):
    VISION_API_KEYS = [
        os.getenv(f"DEEPSEEK_API_KEY_{i}", "").strip()
        for i in range(1, 7)
    ]
VISION_API_KEYS = [k for k in VISION_API_KEYS if k]

VISION_MODEL = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-v4-flash-vision-exp")
VISION_BASE_URL = os.getenv(
    "DEEPSEEK_VISION_BASE_URL",
    "https://api.deepseek.com/chat/completions",
)
VISION_CONCURRENCY = max(
    1,
    min(int(os.getenv("DEEPSEEK_VISION_CONCURRENCY", "4")), len(VISION_API_KEYS) or 1),
)
VISION_TIMEOUT = int(os.getenv("DEEPSEEK_VISION_TIMEOUT", "90"))
VISION_RENDER_SCALE = float(os.getenv("DEEPSEEK_VISION_RENDER_SCALE", "1.35"))
VISION_IMAGE_MAX_PX = int(os.getenv("DEEPSEEK_VISION_IMAGE_MAX_PX", "900"))
VISION_CROP_RATIO = float(os.getenv("DEEPSEEK_VISION_CROP_RATIO", "0.30"))
VISION_MODE = os.getenv("DEEPSEEK_VISION_MODE", "selective").strip().lower()
VISION_MIN_CONFIDENCE = float(os.getenv("DEEPSEEK_VISION_MIN_CONFIDENCE", "0.80"))
VISION_CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache")) / "vision_layout"
VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)

HTTP_LIMITS = httpx.Limits(
    max_connections=max(20, VISION_CONCURRENCY * 3),
    max_keepalive_connections=max(10, VISION_CONCURRENCY * 2),
)
HTTP_CLIENT = httpx.AsyncClient(
    limits=HTTP_LIMITS,
    timeout=httpx.Timeout(VISION_TIMEOUT),
)

SYSTEM_PROMPT = r"""
Eres el árbitro visual de layout de PsiHub Reader para papers académicos.

La imagen contiene DOS zonas de una frontera:
ARRIBA = final de página N.
ABAJO = comienzo de página N+1.

Tu trabajo NO es traducir, resumir ni corregir texto. Determina únicamente
relaciones estructurales que un extractor PDF puede haber interpretado mal,
y transcribe LITERALMENTE los fragmentos de texto visibles en cada borde.

Devuelve JSON válido con exactamente estas claves:
{
  "left_tail": string,
  "right_head": string,
  "boundary_quality": "clean" | "suspicious" | "broken",
  "continues_paragraph": boolean,
  "continues_table": boolean,
  "continues_list": boolean,
  "footnote_relation": boolean,
  "citation_interrupted": boolean,
  "column_flow_risk": boolean,
  "repeated_editorial_pattern": boolean,
  "editorial_text": [string],
  "confidence": number,
  "reason": string
}

REGLAS DE TRANSCRIPCIÓN (left_tail y right_head):
- left_tail = las ÚLTIMAS 10-15 palabras visibles en la mitad superior de la
  imagen (final de la página N). Transcríbelas LITERALMENTE, en el idioma
  original, sin traducir. Si terminan en medio de una palabra, incluye el
  fragmento tal cual (ej: "psico-", "investiga-").
- right_head = las PRIMERAS 10-15 palabras visibles en la mitad inferior de
  la imagen (comienzo de la página N+1). Transcríbelas LITERALMENTE.
- Si no puedes leer el texto con claridad (borroso, cortado), devuelve
  string vacío "".
- NO inventes texto. NO traduzcas. NO reformatees. Solo transcribe lo que ves.

REGLAS DE boundary_quality:
- "clean": la página N termina en un cierre natural (fin de párrafo con
  puntuación fuerte, fin de sección, fin de tabla) Y la página N+1 empieza
  un párrafo/sección nuevo. Los dos lados NO son la misma unidad de texto.
- "suspicious": parece que el texto continúa pero hay señales mixtas
  (mayúscula donde no correspondería, puntuación que no cierra del todo,
  o el texto parece cortado pero no estás seguro).
- "broken": claramente la última palabra de N y la primera de N+1 pertenecen
  a la misma oración/párrafo/tabla/lista. Esto incluye palabras cortadas
  por guión, oraciones a mitad, filas de tabla que se continúan, items de
  lista cortados.

REGLAS DE LAS DEMÁS SEÑALES:
- continues_paragraph = true SOLO si ambos lados son la continuación literal
  de la misma oración o párrafo.
- continues_table = true si la última fila visible de N y la primera de N+1
  comparten la misma estructura de columnas.
- continues_list = true si el último item de N y el primero de N+1 son
  items de la misma lista.
- citation_interrupted = true si una referencia entre paréntesis o corchetes
  queda cortada entre N y N+1.
- footnote_relation = true si una nota al pie visible en N+1 pertenece al
  párrafo que terminó en N.
- column_flow_risk = true si la frontera sugiere que el orden de lectura
  puede cruzar columnas o una caja de ancho completo.
- repeated_editorial_pattern solo para running headers/footers, nombre de
  revista, autores abreviados, volumen/número u otro texto editorial repetido.
- NO marques como editorial un heading, caption, tabla, cita bibliográfica
  o texto académico normal.

CONFIANZA:
- confidence = 0.0-1.0. Usá >= 0.85 si transcribiste left_tail y right_head
  con claridad y ambos forman una frase coherente al unirse.
- Usá 0.50-0.70 si estás adivinando por contexto.
- Usá < 0.50 si no podés decidir.

Si no hay evidencia suficiente, usá false y confidence baja. No inventes.
"""


# ---------------------------------------------------------------
# Utilidades de render (thread-safe: cada worker tiene su propio doc)
# ---------------------------------------------------------------

def _page_excerpt(page: fitz.Page, top: bool) -> str:
    h = page.rect.height * VISION_CROP_RATIO
    if top:
        rect = fitz.Rect(0, 0, page.rect.width, h)
    else:
        rect = fitz.Rect(0, page.rect.height - h, page.rect.width, page.rect.height)
    return re.sub(r"\s+", " ", page.get_text("text", clip=rect)).strip()[:2200]


def _render_strip(page: fitz.Page, top: bool) -> Image.Image:
    h = page.rect.height * VISION_CROP_RATIO
    if top:
        clip = fitz.Rect(0, 0, page.rect.width, h)
    else:
        clip = fitz.Rect(0, page.rect.height - h, page.rect.width, page.rect.height)
    pix = page.get_pixmap(
        matrix=fitz.Matrix(VISION_RENDER_SCALE, VISION_RENDER_SCALE),
        clip=clip,
        alpha=False,
    )
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def _compose_boundary(page_a: fitz.Page, page_b: fitz.Page) -> str:
    """Una sola imagen vertical: final N arriba + inicio N+1 abajo."""
    top = _render_strip(page_a, False)
    bottom = _render_strip(page_b, True)
    width = max(top.width, bottom.width)
    top = ImageOps.contain(top, (width, VISION_IMAGE_MAX_PX // 2))
    bottom = ImageOps.contain(bottom, (width, VISION_IMAGE_MAX_PX // 2))
    canvas = Image.new("RGB", (width, top.height + bottom.height + 50), "white")
    canvas.paste(top, ((width - top.width) // 2, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, top.height, width, top.height + 50), fill="white")
    draw.text((12, top.height + 12), "FINAL PÁGINA N  ↓  INICIO PÁGINA N+1", fill="black")
    canvas.paste(bottom, ((width - bottom.width) // 2, top.height + 50))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _local_boundary_score(page_a: fitz.Page, page_b: fitz.Page) -> float:
    """Score barato de ambigüedad. No decide el resultado; solo puede ahorrar Vision."""
    a = _page_excerpt(page_a, False)
    b = _page_excerpt(page_b, True)
    score = 0.0
    if a and b:
        if not re.search(r"[.!?:;)]$", a):
            score += 0.35
        if re.match(r"^[a-záéíóúñü0-9(]", b, re.I):
            score += 0.35
        if re.search(r"(?:Table|Tabla|Figure|Figura|Fig\.?|Table\.)\s*\d", a, re.I) or re.search(
            r"(?:Table|Tabla|Figure|Figura|Fig\.?|Table\.)\s*\d", b, re.I
        ):
            score += 0.20
        if re.search(r"\b(?:doi|et al\.|19\d{2}|20\d{2})\b", a + " " + b, re.I):
            score += 0.10
    return min(score, 1.0)


def _cache_key(pdf_path: str) -> str:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    meta = f"{VISION_MODEL}|{VISION_MODE}|{VISION_CROP_RATIO}|{VISION_IMAGE_MAX_PX}|v10"
    h.update(meta.encode())
    return h.hexdigest()


def _cache_path(pdf_path: str) -> Path:
    return VISION_CACHE_DIR / f"{_cache_key(pdf_path)}.json"


def _load_cache(pdf_path: str) -> Optional[list[dict[str, Any]]]:
    path = _cache_path(pdf_path)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # Descartar cache si contiene errores.
                if any(item.get("error") for item in data):
                    print("[VISION] Cache contiene errores; se descarta.")
                    return None
                print(f"[VISION] Cache visual encontrado: {len(data)} diagnósticos")
                return data
    except Exception as exc:
        print(f"[VISION] Cache ilegible: {exc}")
    return None


def _save_cache(pdf_path: str, diagnostics: list[dict[str, Any]]) -> None:
    # No guardar si hay errores: la próxima ejecución debería reintentar.
    if any(item.get("error") for item in diagnostics):
        return
    try:
        _cache_path(pdf_path).write_text(
            json.dumps(diagnostics, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[VISION] No se pudo guardar cache: {exc}")


# ---------------------------------------------------------------
# Boundary Vision
# ---------------------------------------------------------------

async def _analyze_boundary(
    doc: fitz.Document, page_index: int, api_key: str
) -> dict[str, Any]:
    page_a, page_b = doc[page_index], doc[page_index + 1]
    image = _compose_boundary(page_a, page_b)
    excerpt_a = _page_excerpt(page_a, False)
    excerpt_b = _page_excerpt(page_b, True)
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Frontera {page_index + 1} → {page_index + 2}.\n"
                            f"Texto extraído del final de la página {page_index + 1}:\n"
                            f"{excerpt_a}\n\n"
                            f"Texto extraído del inicio de la página {page_index + 2}:\n"
                            f"{excerpt_b}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 700,
        "stream": False,
    }
    started = time.perf_counter()
    response = await HTTP_CLIENT.post(
        VISION_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    elapsed = time.perf_counter() - started
    print(f"[VISION] {page_index + 1}→{page_index + 2}: {elapsed:.2f}s")
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    result = json.loads(content)
    result["page"] = page_index + 1
    result["next_page"] = page_index + 2
    result["excerpt_a"] = excerpt_a[:800]
    result["excerpt_b"] = excerpt_b[:800]
    return result


async def analyze_pdf_boundaries(pdf_path: str) -> list[dict[str, Any]]:
    if not VISION_API_KEYS:
        print("[VISION] Sin API keys; se omite análisis visual.")
        return []
    cached = _load_cache(pdf_path)
    if cached is not None:
        return cached

    # Abrir sólo para medir y puntuar (rápido, secuencial, sin threads).
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        if page_count < 2:
            return []

        candidates = list(range(page_count - 1))

        if VISION_MODE == "selective":
            scored = [(_local_boundary_score(doc[i], doc[i + 1]), i) for i in candidates]
            candidates = [i for score, i in scored if score >= 0.30]
            if not candidates:
                candidates = [i for _, i in sorted(scored, reverse=True)[: min(3, len(scored))]]
            print(f"[VISION] Modo selectivo: {len(candidates)}/{page_count - 1} fronteras")

        elif VISION_MODE == "aggressive":
            # En modo agresivo analizamos TODAS las fronteras, pero primero
            # calculamos el score para priorizarlas y ver en logs cuáles
            # son las más sospechosas.
            scored = [(_local_boundary_score(doc[i], doc[i + 1]), i) for i in candidates]
            scored.sort(reverse=True)
            top_scores = [(i, round(s, 2)) for s, i in scored[:5]]
            print(f"[VISION] Modo agresivo: analizando TODAS las {len(candidates)} fronteras. "
                  f"Top sospechosas: {top_scores}")

        else:  # "all" (compatibilidad hacia atrás)
            print(f"[VISION] Modo all: {len(candidates)} fronteras")

    if not candidates:
        _save_cache(pdf_path, [])
        return []

    queue: asyncio.Queue[Optional[int]] = asyncio.Queue()
    for i in candidates:
        await queue.put(i)
    results: dict[int, dict[str, Any]] = {}
    worker_count = min(VISION_CONCURRENCY, len(VISION_API_KEYS), len(candidates))

    async def worker(worker_id: int, key: str) -> None:
        # Documento PROPIO del worker: PyMuPDF no es thread-safe.
        worker_doc = fitz.open(pdf_path)
        try:
            while True:
                index = await queue.get()
                if index is None:
                    queue.task_done()
                    return
                try:
                    print(f"[VISION] Worker {worker_id} → {index + 1}→{index + 2}")
                    results[index] = await _analyze_boundary(worker_doc, index, key)
                except Exception as exc:
                    print(f"[VISION] Error {index + 1}→{index + 2}: {exc}")
                    results[index] = {
                        "page": index + 1,
                        "next_page": index + 2,
                        "error": str(exc),
                    }
                finally:
                    queue.task_done()
        finally:
            worker_doc.close()

    workers = [
        asyncio.create_task(worker(i + 1, VISION_API_KEYS[i]))
        for i in range(worker_count)
    ]
    await queue.join()
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers, return_exceptions=True)

    diagnostics = [results[i] for i in sorted(results) if i in results]
    _save_cache(pdf_path, diagnostics)
    return diagnostics


# ---------------------------------------------------------------
# Señales exportables
# ---------------------------------------------------------------

def collect_editorial_patterns(diagnostics: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        if item.get("error"):
            continue
        if not item.get("repeated_editorial_pattern"):
            continue
        texts = item.get("editorial_text") or []
        if isinstance(texts, str):
            texts = [texts]
        for text in texts:
            value = re.sub(r"\s+", " ", str(text)).strip()
            if len(value) >= 4:
                counts[value] = counts.get(value, 0) + 1
    return [text for text, count in counts.items() if count >= 2]


def boundary_hints_by_page(diagnostics: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Convierte diagnósticos en señales que app.py puede aplicar directamente."""
    hints: dict[int, dict[str, Any]] = {}
    for item in diagnostics:
        if item.get("error"):
            continue
        confidence = float(item.get("confidence", 0) or 0)
        page = int(item.get("page", 0) or 0)
        if page <= 0:
            continue

        # Umbral más bajo cuando boundary_quality == "broken": son casos
        # en los que Vision tiene evidencia visual fuerte, aunque su
        # confidence declarada sea media.
        quality = str(item.get("boundary_quality", "")).lower()
        min_conf = 0.85 if quality == "broken" else VISION_MIN_CONFIDENCE
        if confidence < min_conf:
            continue

        hints[page] = {
            "boundary_quality": quality,
            "left_tail": str(item.get("left_tail", "") or "")[:400],
            "right_head": str(item.get("right_head", "") or "")[:400],
            "continues_paragraph": bool(item.get("continues_paragraph")),
            "continues_table": bool(item.get("continues_table")),
            "continues_list": bool(item.get("continues_list")),
            "footnote_relation": bool(item.get("footnote_relation")),
            "citation_interrupted": bool(item.get("citation_interrupted")),
            "column_flow_risk": bool(item.get("column_flow_risk")),
            "confidence": confidence,
            "reason": str(item.get("reason", "")),
            "excerpt_a": str(item.get("excerpt_a", "")),
            "excerpt_b": str(item.get("excerpt_b", "")),
        }
    return hints


# ---------------------------------------------------------------
# Full-page audit
# ---------------------------------------------------------------

FULL_PAGE_SYSTEM_PROMPT = r"""
Eres el auditor visual estructural de PsiHub Reader.
Recibes una página completa de un paper y un catálogo de bloques extraídos
localmente. NO traduzcas ni reescribas. Evalúa si la extracción nativa respeta
la geometría visible y el orden humano de lectura.

Devuelve JSON:
{
  "confidence": 0.0,
  "reading_order_ok": true,
  "column_structure_ok": true,
  "blocks": [
    {"id":"p1b3", "kind":"paragraph|heading|table|figure|caption|equation|footnote|list|header|footer|unknown", "visible":true, "confidence":0.0}
  ],
  "recommended_order": ["p1b0", "p1b2"],
  "issues": ["..."],
  "reason": "..."
}

Solo usa IDs del catálogo. No inventes IDs. Si no puedes establecer el orden
con seguridad, devuelve reading_order_ok=false y una confidence baja.
"""


def _render_full_page(page: fitz.Page, max_px: int = 1500) -> str:
    scale = min(2.0, max_px / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def audit_pages_with_vision(
    pdf_path: str, page_payloads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Auditoría visual por página en páginas de riesgo alto."""
    if not VISION_API_KEYS or not page_payloads:
        return []
    results: dict[int, dict[str, Any]] = {}

    queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
    for item in page_payloads:
        await queue.put(item)
    workers = min(VISION_CONCURRENCY, len(VISION_API_KEYS), len(page_payloads))

    async def worker(wid: int, key: str) -> None:
        # Documento propio del worker.
        worker_doc = fitz.open(pdf_path)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return
                page_no = int(item["page"])
                try:
                    page = worker_doc[page_no - 1]
                    catalog = item.get("blocks", [])
                    prompt = (
                        f"Página {page_no}. Catálogo de bloques nativos:\n"
                        + json.dumps(catalog, ensure_ascii=False)
                    )
                    payload = {
                        "model": VISION_MODEL,
                        "messages": [
                            {"role": "system", "content": FULL_PAGE_SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": _render_full_page(page)},
                                    },
                                ],
                            },
                        ],
                        "temperature": 0,
                        "thinking": {"type": "disabled"},
                        "response_format": {"type": "json_object"},
                        "max_tokens": 1200,
                        "stream": False,
                    }
                    response = await HTTP_CLIENT.post(
                        VISION_BASE_URL,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    data = json.loads(response.json()["choices"][0]["message"]["content"])
                    data["page"] = page_no
                    results[page_no] = data
                except Exception as exc:
                    results[page_no] = {
                        "page": page_no,
                        "error": str(exc),
                        "confidence": 0,
                    }
                finally:
                    queue.task_done()
        finally:
            worker_doc.close()

    tasks = [
        asyncio.create_task(worker(i + 1, VISION_API_KEYS[i]))
        for i in range(workers)
    ]
    await queue.join()
    for _ in tasks:
        await queue.put(None)
    await asyncio.gather(*tasks, return_exceptions=True)
    return [results[k] for k in sorted(results)]
