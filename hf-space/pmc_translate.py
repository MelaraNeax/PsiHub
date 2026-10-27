"""Traduccion al espanol de un articulo PMC ya estructurado (JATS -> documento).

El pipeline historico manda paginas renderizadas a un modelo de vision. Con
Europe PMC no hace falta: el articulo llega como texto estructurado, asi que se
traduce por bloques. Sale mas barato, mas fiel (no se pierden tablas ni
referencias por OCR) y permite mantener la estructura para el lector y el PDF.

Reglas:
- Nunca se pierde contenido: si un bloque no vuelve traducido, se deja el
  original en vez de borrarlo.
- Los marcadores viajan con el texto, de modo que el orden no se puede romper.
- Se traducen titulo, subtitulo, resumen, palabras clave, cuerpos, listas,
  celdas de tabla y epigrafes; no se tocan DOIs, anios ni citas numericas.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Callable, Optional

import httpx

DEEPSEEK_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')
TRANSLATE_MODEL = os.getenv(
    'PMC_TRANSLATE_MODEL',
    os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
)
CHUNK_CHARS = max(1200, int(os.getenv('PMC_TRANSLATE_CHUNK_CHARS', '3200')))
MAX_OUTPUT = max(800, int(os.getenv('PMC_TRANSLATE_MAX_TOKENS', '4000')))
CONCURRENCY = max(1, int(os.getenv('PMC_TRANSLATE_CONCURRENCY', '3')))
ATTEMPTS = max(1, int(os.getenv('PMC_TRANSLATE_ATTEMPTS', '2')))

SYSTEM_PROMPT = """Eres traductor científico de PsiHub. Traducís al español textos académicos de psicología, psiquiatría y medicina.

Reglas:
- Traducción completa y fiel: no resumas, no omitas oraciones, no agregues nada.
- Conservá el Markdown (negritas, cursivas, encabezados), los números, porcentajes, tamaños de efecto, valores p, nombres de instrumentos y citas numéricas tal como aparecen.
- Los nombres propios de autores, revistas y escalas quedan en su idioma original.
- Usá el español académico de Latinoamérica, con tildes y eñes, en presente y voz activa cuando el original lo permite.
- Devolvés SOLO los bloques traducidos, cada uno precedido por su marcador exacto <<<n>>> en una línea propia. Si un bloque no necesita traducción (por ejemplo, una fórmula), devolvé ese mismo bloque sin cambios.

No agregues comentarios, títulos ni explicaciones fuera de los bloques."""


class TranslationError(RuntimeError):
    pass


def _blocks(document: dict):
    """Itera (getter, setter, texto) de cada unidad traducible del documento."""
    meta = document.get('document') or {}
    for key in ('title', 'subtitle', 'abstract_original'):
        # (kind, target, key): las claves de la ficha no tienen nodo contenedor.
        yield ('meta', None, key)

    for block in document.get('blocks') or []:
        btype = block.get('type')
        if btype in ('paragraph', 'heading', 'quote', 'equation'):
            yield ('block', block, 'text')
        elif btype == 'list':
            for index, _item in enumerate(block.get('items') or []):
                yield ('list', block, index)
        elif btype == 'table':
            yield ('block', block, 'caption')
            for index, _header in enumerate(block.get('headers') or []):
                yield ('table_head', block, index)
            for row_index, row in enumerate(block.get('rows') or []):
                for cell_index, _cell in enumerate(row):
                    yield ('table_cell', block, (row_index, cell_index))
        elif btype == 'figure':
            yield ('block', block, 'caption')
            yield ('block', block, 'label')
    _ = meta


def _get(document: dict, kind: str, target, key) -> str:
    if kind == 'meta':
        return str((document.get('document') or {}).get(key) or '')
    if kind == 'block':
        return str(target.get(key) or '')
    if kind == 'list':
        items = target.get('items') or []
        return str(items[key]) if 0 <= key < len(items) else ''
    if kind == 'table_head':
        headers = target.get('headers') or []
        return str(headers[key]) if 0 <= key < len(headers) else ''
    if kind == 'table_cell':
        row_index, cell_index = key
        rows = target.get('rows') or []
        if 0 <= row_index < len(rows) and 0 <= cell_index < len(rows[row_index]):
            return str(rows[row_index][cell_index])
    return ''


def _set(document: dict, kind: str, target, key, value: str) -> None:
    if kind == 'meta':
        document.setdefault('document', {})[key] = value
    elif kind == 'block':
        target[key] = value
    elif kind == 'list':
        items = target.get('items') or []
        if 0 <= key < len(items):
            items[key] = value
    elif kind == 'table_head':
        headers = target.get('headers') or []
        if 0 <= key < len(headers):
            headers[key] = value
    elif kind == 'table_cell':
        row_index, cell_index = key
        rows = target.get('rows') or []
        if 0 <= row_index < len(rows) and 0 <= cell_index < len(rows[row_index]):
            rows[row_index][cell_index] = value


def _needs_translation(text: str) -> bool:
    clean = (text or '').strip()
    if len(clean) < 2:
        return False
    # Cifras, URLs y formulas quedan como estan.
    if re.fullmatch(r'[\d\s.,;:+\-–—%()\[\]/=<>±°º*#|_~^]+', clean):
        return False
    letters = re.findall(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]', clean)
    if not letters:
        return False
    has_spanish = bool(re.search(r'[áéíóúüñ¿¡]', clean)) or bool(re.search(
        r'\b(el|la|los|las|de|del|que|con|para|por|una|un)\b', clean, re.I))
    has_english = bool(re.search(
        r'\b(the|and|of|with|for|this|that|was|were|are|is|from|between|patients|study|results)\b',
        clean, re.I))
    return has_english or not has_spanish


def _units(document: dict) -> list[tuple[str, object, object, str]]:
    out: list[tuple[str, object, object, str]] = []
    for kind, target, key in _blocks(document):
        text = _get(document, kind, target, key)
        if _needs_translation(text):
            out.append((kind, target, key, text))
    return out


def _chunks(units: list[tuple[str, object, object, str]]) -> list[list[tuple[int, str]]]:
    """Agrupa unidades en lotes por tamano, conservando el indice global."""
    groups: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for index, (_kind, _target, _key, text) in enumerate(units):
        if current and size + len(text) > CHUNK_CHARS:
            groups.append(current)
            current = []
            size = 0
        current.append((index, text))
        size += len(text)
    if current:
        groups.append(current)
    return groups


_MARKER_RE = re.compile(r'<<<\s*(\d+)\s*>>>')


def _parse(content: str, expected: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    matches = list(_MARKER_RE.finditer(content or ''))
    for position, match in enumerate(matches):
        index = int(match.group(1))
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(content)
        text = content[start:end].strip()
        if index in expected and text:
            out[index] = text
    return out


async def _call(
    client: httpx.AsyncClient,
    api_key: str,
    group: list[tuple[int, str]],
    model: str,
) -> tuple[dict[int, str], dict]:
    payload_lines = []
    for index, text in group:
        payload_lines.append(f'<<<{index}>>>\n{text}')
    user_content = '\n\n'.join(payload_lines)
    body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.0,
        'stream': False,
        'thinking': {'type': 'disabled'},
        'max_tokens': MAX_OUTPUT,
    }
    response = await client.post(
        DEEPSEEK_URL,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json=body,
    )
    if response.status_code >= 400:
        raise TranslationError(f'DeepSeek HTTP {response.status_code}: {response.text[:200]}')
    data = response.json()
    choice = (data.get('choices') or [{}])[0]
    content = (choice.get('message') or {}).get('content') or ''
    usage = data.get('usage') or {}
    parsed = _parse(content, [index for index, _ in group])
    return parsed, usage


async def translate_document(
    document: dict,
    api_keys: list[str],
    model: Optional[str] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[dict, dict]:
    """Traduce el documento en el lugar y devuelve (documento, uso de tokens)."""
    log = log_fn or (lambda _msg: None)
    keys = [k for k in (api_keys or []) if k]
    if not keys:
        raise TranslationError('No hay API key de DeepSeek configurada.')
    started = time.time()
    model_name = (model or TRANSLATE_MODEL).strip()
    # Se guarda el original antes de tocar nada: la ficha muestra el titulo en
    # los dos idiomas y el JSON conserva el texto de origen.
    meta_original = {
        key: str((document.get('document') or {}).get(key) or '')
        for key in ('title', 'subtitle', 'abstract_original')
    }
    units = _units(document)
    usage_total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'calls': 0}
    if not units:
        return document, usage_total

    groups = _chunks(units)
    log(f'[PMC] Traduciendo {len(units)} bloques en {len(groups)} lotes ({model_name})')
    semaphore = asyncio.Semaphore(CONCURRENCY)
    translated: dict[int, str] = {}
    failures = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async def run(group: list[tuple[int, str]], attempt: int = 0) -> None:
            nonlocal failures
            async with semaphore:
                key = keys[(attempt + len(group)) % len(keys)]
                try:
                    parsed, usage = await _call(client, key, group, model_name)
                except Exception as exc:  # noqa: BLE001 - se reintenta
                    if attempt + 1 < ATTEMPTS:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        await run(group, attempt + 1)
                        return
                    failures += 1
                    log(f'[PMC] Lote de {len(group)} bloques fallo: {type(exc).__name__}: {exc}')
                    return
                for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    usage_total[name] += int(usage.get(name) or 0)
                usage_total['calls'] += 1
                missing = [index for index, _ in group if index not in parsed]
                if missing and attempt + 1 < ATTEMPTS:
                    # Segundo intento solo con lo que no volvio.
                    retry_group = [(i, t) for i, t in group if i in missing]
                    await asyncio.sleep(0.8)
                    await run(retry_group, attempt + 1)
                translated.update(parsed)

        await asyncio.gather(*(run(group) for group in groups))

    applied = 0
    for index, (kind, target, key, original) in enumerate(units):
        value = translated.get(index)
        if not value:
            continue
        _set(document, kind, target, key, value)
        applied += 1

    meta = document.setdefault('document', {})
    # `_set` deja el texto traducido en los mismos campos, asi que primero se
    # leen y despues se restauran los originales en `title`/`abstract_original`.
    translated_title = str(meta.get('title') or '')
    translated_subtitle = str(meta.get('subtitle') or '')
    translated_abstract = str(meta.get('abstract_original') or '')
    if meta_original.get('title'):
        meta['title'] = meta_original['title']
        meta['title_es'] = translated_title or meta_original['title']
    if meta_original.get('subtitle'):
        meta['subtitle'] = meta_original['subtitle']
        meta['subtitle_es'] = translated_subtitle or None
    if meta_original.get('abstract_original'):
        meta['abstract_original'] = meta_original['abstract_original']
        meta['abstract_es'] = translated_abstract or meta_original['abstract_original']
    if meta.get('keywords'):
        meta.setdefault('keywords_es', meta['keywords'])

    stats = {
        'blocks': len(units),
        'translated': applied,
        'failed_batches': failures,
        'usage': usage_total,
        'model': model_name,
        'elapsed_sec': round(time.time() - started, 1),
    }
    log(f'[PMC] Traduccion aplicada en {applied}/{len(units)} bloques')
    return document, stats


def usage_json(stats: dict) -> str:
    return json.dumps(stats.get('usage') or {}, ensure_ascii=False)
