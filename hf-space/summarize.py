"""Resumen de papers con DeepSeek (texto, sin vision).

Dos niveles:
- basico: usa el abstract y los metadatos ya recuperados por OpenAlex/Crossref.
- avanzado: extrae el texto del PDF completo con pymupdf y resume resultados y
  conclusion reales, no solo lo que anuncia el resumen.

Nunca inventa datos ausentes: si el abstract no trae conclusion, lo dice.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx
import pymupdf

from costs import log_usage

DEEPSEEK_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')
SUMMARY_MODEL = os.getenv('SUMMARY_MODEL', os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'))
MAX_OUTPUT_TOKENS = max(300, int(os.getenv('SUMMARY_MAX_TOKENS', '900')))
MAX_PDF_CHARS = max(8000, int(os.getenv('SUMMARY_PDF_CHARS', '60000')))

BASIC_PROMPT = """Eres el asistente de PsiHub, una app de psicología basada en evidencia.
Resumís un artículo académico en español claro para estudiantes y profesionales.

Reglas:
- Usá SOLO la información del abstract y de los metadatos que recibís. No inventes
  resultados, muestras, autores ni conclusiones que no estén explícitos.
- Si el abstract no informa la conclusión o el tamaño de muestra, decilo.
- No prometas más de lo que el estudio permite: calibrá "muestra", "se asocia",
  "sugiere". Nada de "demuestra" si no es experimental.
- 250 a 450 palabras, en tercera persona, sin repetir el título.
- Secciones: "## De qué trata", "## Hallazgos principales", "## Conclusión".
  Agregá "## Límites" solo si el abstract los menciona.
- Sin tablas salvo que haya datos numéricos comparables. Sin URLs ni referencias."""

FULL_PROMPT = """Eres el asistente de PsiHub, una app de psicología basada en evidencia.
Resumís el TEXTO COMPLETO de un artículo académico en español claro para estudiantes
y profesionales.

Reglas:
- Usá SOLO el texto provisto. No inventes resultados, muestras ni conclusiones.
- Distinguí lo que el estudio encontró de lo que discute o especula.
- Calibrá el verbo según el diseño (experimental/meta-análisis: "muestra";
  correlacional: "se asocia"; teoría o muestra pequeña: "sugiere").
- Ignorá referencias, agradecimientos y datos editoriales.
- 350 a 600 palabras, en tercera persona.
- Secciones: "## De qué trata", "## Método", "## Hallazgos principales",
  "## Conclusión", "## Límites".
- Si el texto no alcanza para una sección, escribí en una línea que no consta.
- Sin URLs ni lista de referencias."""


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = MAX_PDF_CHARS) -> tuple[str, int]:
    """Extrae texto seleccionable del PDF, recortado para controlar el costo."""
    document = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        chunks: list[str] = []
        total = 0
        for page in document:
            text = page.get_text('text') or ''
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            if not text:
                continue
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        joined = '\n\n'.join(chunks)
        return joined[:max_chars], len(document)
    finally:
        document.close()


def _paper_header(data: dict) -> str:
    authors = data.get('authors') or []
    if isinstance(authors, list):
        authors_text = ', '.join(str(a) for a in authors[:6] if a)
    else:
        authors_text = str(authors)
    lines = [
        f"Titulo: {data.get('title') or 'Sin titulo'}",
        f"Autores: {authors_text or 'No informados'}",
        f"Año: {data.get('year') or 'No informado'}",
        f"Revista: {data.get('journal') or 'No informada'}",
        f"DOI: {data.get('doi') or 'No informado'}",
        f"Tipo: {data.get('type') or 'No informado'}",
        f"Diseno declarado: {data.get('design') or 'No informado'}",
        f"Muestra declarada: {data.get('sample') or 'No informada'}",
    ]
    return '\n'.join(lines)


async def _request(messages: list[dict], api_keys: list[str],
                   max_tokens: int = MAX_OUTPUT_TOKENS,
                   temperature: float = 0.2) -> tuple[str, dict]:
    if not api_keys:
        raise RuntimeError('No hay API key de DeepSeek configurada.')
    payload = {
        'model': SUMMARY_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': False,
        'thinking': {'type': 'disabled'},
        'max_tokens': max_tokens,
    }
    last_error: Optional[BaseException] = None
    for attempt in range(min(len(api_keys), 3)):
        key = api_keys[attempt % len(api_keys)]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
                response = await client.post(
                    DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                )
            if response.status_code >= 400:
                raise RuntimeError(f'DeepSeek HTTP {response.status_code}: {response.text[:200]}')
            data = response.json()
            choice = (data.get('choices') or [{}])[0]
            content = ((choice.get('message') or {}).get('content') or '').strip()
            if not content:
                raise RuntimeError('DeepSeek devolvio una respuesta vacia.')
            return content, (data.get('usage') or {})
        except Exception as exc:  # noqa: BLE001 - rota de key
            last_error = exc
            await asyncio.sleep(1.0)
    raise RuntimeError(f'No se pudo generar el resumen: {last_error}')


def _record(usage: dict, title: str, mode: str, input_chars: int,
            elapsed_sec: float = 0.0) -> dict:
    record = log_usage(
        'summary', usage,
        model=SUMMARY_MODEL,
        elapsed_sec=elapsed_sec,
        title=title,
        doc_id=f'summary-{int(time.time() * 1000)}',
        source='summary',
        mode=mode,
        input_chars=input_chars,
    )
    return {key: record[key] for key in (
        'prompt_tokens', 'completion_tokens', 'total_tokens',
        'cache_hit_tokens', 'cost_usd',
    ) if key in record}


async def summarize_basic(data: dict, api_keys: list[str]) -> dict:
    started = time.time()
    abstract = str(data.get('abstract') or '').strip()
    if len(abstract) < 200:
        raise ValueError('Este artículo no tiene un resumen disponible. Usá "Resumen avanzado" para leer el PDF.')
    user = (
        f"{_paper_header(data)}\n\n"
        f"ABSTRACT:\n{abstract[:6000]}\n\n"
        'Escribí el resumen en español con las secciones indicadas.'
    )
    content, usage = await _request(
        [{'role': 'system', 'content': BASIC_PROMPT}, {'role': 'user', 'content': user}],
        api_keys,
    )
    cost = _record(usage, str(data.get('title') or ''), 'basic', len(user),
                   elapsed_sec=time.time() - started)
    return {'summary': content, 'mode': 'basic', 'usage': usage, 'cost': cost}


async def summarize_full_text(data: dict, pdf_bytes: bytes, api_keys: list[str]) -> dict:
    started = time.time()
    text, pages = extract_pdf_text(pdf_bytes)
    if len(text) < 1500:
        raise ValueError('El PDF no tiene texto seleccionable suficiente para resumir.')
    user = (
        f"{_paper_header(data)}\n"
        f"Paginas del PDF: {pages}\n\n"
        f"TEXTO DEL ARTICULO (recortado a {len(text)} caracteres):\n{text}\n\n"
        'Escribí el resumen en español con las secciones indicadas.'
    )
    content, usage = await _request(
        [{'role': 'system', 'content': FULL_PROMPT}, {'role': 'user', 'content': user}],
        api_keys,
    )
    cost = _record(usage, str(data.get('title') or ''), 'advanced', len(user),
                   elapsed_sec=time.time() - started)
    return {'summary': content, 'mode': 'advanced', 'pages': pages,
            'usage': usage, 'cost': cost}
