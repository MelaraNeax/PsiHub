"""Selector de motor LLM: DeepSeek (por defecto) o Claude.

Una sola variable decide quien responde:

    LLM_PROVIDER=0   -> DeepSeek (comportamiento historico)
    LLM_PROVIDER=1   -> Claude (Anthropic)

Cada motor tiene sus propias claves:

    DeepSeek: DEEPSEEK_API_CHAT, DEEPSEEK_API_KEY_1..6
    Claude:   CLAUDE_CHAT_API, CLAUDE_TRAD_API_1..3

"Trad" son las claves de traduccion: se reparten en circulo entre los lotes de
un mismo documento, igual que las de DeepSeek. El modelo de Claude se cambia
con CLAUDE_MODEL (por defecto claude-haiku-5-5).

La vision (PDF escaneado) sigue en DeepSeek: el plan es de 4 claves de texto.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional

PROVIDER = 'claude' if os.getenv('LLM_PROVIDER', '0').strip() == '1' else 'deepseek'
USE_CLAUDE = PROVIDER == 'claude'

CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-haiku-5-5').strip() or 'claude-haiku-5-5'

# Reintentos por clave dentro de una misma llamada.
KEY_ATTEMPTS = 3


def _split(value: Optional[str]) -> list[str]:
    return [piece.strip() for piece in (value or '').split(',') if piece.strip()]


def chat_keys(deepseek_default: Optional[list[str]] = None) -> list[str]:
    """Claves del chat y de los resumenes.

    En modo Claude nunca se devuelven claves de DeepSeek: una key ajena solo
    produciria un 401 en bucle.
    """
    if not USE_CLAUDE:
        return [k for k in (deepseek_default or []) if k]
    return _split(os.getenv('CLAUDE_CHAT_API', ''))


def trad_keys(deepseek_default: Optional[list[str]] = None) -> list[str]:
    """Claves de traduccion (PMC, resumen de articulo)."""
    if not USE_CLAUDE:
        return [k for k in (deepseek_default or []) if k]
    keys: list[str] = []
    for index in (1, 2, 3):
        keys.extend(_split(os.getenv(f'CLAUDE_TRAD_API_{index}', '')))
    keys.extend(_split(os.getenv('CLAUDE_TRAD_API', '')))
    if not keys:
        # Sin claves "Trad" dedicadas se usa la del chat: mejor eso que un 503.
        keys = _split(os.getenv('CLAUDE_CHAT_API', ''))
    return keys


def model_for(deepseek_model: str) -> str:
    return CLAUDE_MODEL if USE_CLAUDE else deepseek_model


def describe() -> dict:
    """Resumen para /health."""
    return {
        'provider': PROVIDER,
        'model': CLAUDE_MODEL if USE_CLAUDE else None,
    }


# ---------------------------------------------------------------------------
# Claude (Anthropic)
# ---------------------------------------------------------------------------

def _map_usage(raw) -> dict:
    """Traduce el usage de Anthropic al formato que ya consume costs.py."""
    inp = int(getattr(raw, 'input_tokens', 0) or 0)
    out = int(getattr(raw, 'output_tokens', 0) or 0)
    cache_read = int(getattr(raw, 'cache_read_input_tokens', 0) or 0)
    cache_write = int(getattr(raw, 'cache_creation_input_tokens', 0) or 0)
    prompt = inp + cache_read + cache_write
    return {
        'prompt_tokens': prompt,
        'completion_tokens': out,
        'total_tokens': prompt + out,
        'prompt_cache_hit_tokens': cache_read,
        'prompt_cache_miss_tokens': inp + cache_write,
        'prompt_tokens_details': {'cached_tokens': cache_read},
    }


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic recibe el system aparte y solo turnos user/assistant."""
    system_parts: list[str] = []
    turns: list[dict] = []
    for message in messages:
        role = (message.get('role') or 'user').strip()
        content = message.get('content')
        if not isinstance(content, str):
            content = str(content or '')
        if role == 'system':
            if content.strip():
                system_parts.append(content)
        elif role in ('user', 'assistant'):
            turns.append({'role': role, 'content': content})
        else:
            turns.append({'role': 'user', 'content': content})
    if not turns:
        turns = [{'role': 'user', 'content': '(sin contenido)'}]
    if turns[0]['role'] != 'user':
        turns.insert(0, {'role': 'user', 'content': '(continuar)'})
    return '\n\n'.join(system_parts), turns


def _stop_reason(value: Optional[str]) -> str:
    if value == 'max_tokens':
        return 'length'
    if value == 'refusal':
        return 'refusal'
    return 'stop'


def _client(api_key: str, timeout: float):
    import anthropic  # import perezoso: modo DeepSeek no necesita el paquete

    return anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)


# Si el modelo rechaza `thinking: disabled`, se deja de enviarlo en este
# proceso en vez de fallar en cada llamada.
_thinking_disabled_ok = True


def _params(messages: list[dict], max_tokens: int,
            think: Optional[bool] = None) -> dict:
    system, turns = _split_system(messages)
    params: dict = {
        'model': CLAUDE_MODEL,
        'max_tokens': max_tokens,
        'messages': turns,
    }
    if system:
        params['system'] = system
    # En traduccion el razonamiento solo gasta tokens de salida: se apaga.
    if think is not None and _thinking_disabled_ok:
        params['thinking'] = {'type': 'adaptive' if think else 'disabled'}
    return params


async def complete(
    messages: list[dict],
    api_keys: list[str],
    max_tokens: int,
    temperature: Optional[float] = None,
    timeout: float = 120.0,
    think: bool = False,
) -> tuple[str, dict]:
    """Respuesta sin streaming. `temperature` se ignora: Haiku 5.5 rechaza
    valores no estandar de muestreo."""
    global _thinking_disabled_ok
    keys = [k for k in (api_keys or []) if k]
    if not keys:
        raise RuntimeError('No hay API key de Claude configurada.')

    last_error: Optional[BaseException] = None
    for key in keys[:KEY_ATTEMPTS]:
        try:
            async with _client(key, timeout) as client:
                try:
                    response = await client.messages.create(
                        **_params(messages, max_tokens, think)
                    )
                except Exception:
                    if not _thinking_disabled_ok or think:
                        raise
                    # El modelo no acepta apagar thinking: se reintenta sin el.
                    _thinking_disabled_ok = False
                    response = await client.messages.create(
                        **_params(messages, max_tokens, None)
                    )
            text = ''.join(
                block.text for block in response.content if block.type == 'text'
            ).strip()
            return text, _map_usage(response.usage)
        except Exception as exc:  # noqa: BLE001 - se rota de clave
            last_error = exc
            await asyncio.sleep(0.8)
    raise RuntimeError(f'Claude no respondio: {last_error}')


async def stream(
    messages: list[dict],
    api_keys: list[str],
    max_tokens: int,
    temperature: Optional[float] = None,
    think: bool = False,
    timeout: float = 180.0,
) -> AsyncIterator[tuple[str, object]]:
    """Streaming con los mismos eventos que el chat ya consume:
    ('delta', texto), ('usage', dict), ('finish', motivo)."""
    keys = [k for k in (api_keys or []) if k]
    if not keys:
        raise RuntimeError('No hay API key de Claude configurada.')

    last_error: Optional[BaseException] = None
    for key in keys[:KEY_ATTEMPTS]:
        produced = False
        try:
            async with _client(key, timeout) as client:
                async with client.messages.stream(**_params(messages, max_tokens, think)) as response_stream:
                    async for chunk in response_stream.text_stream:
                        if not chunk:
                            continue
                        produced = True
                        yield ('delta', chunk)
                    final = await response_stream.get_final_message()
            yield ('usage', _map_usage(final.usage))
            yield ('finish', _stop_reason(final.stop_reason))
            return
        except Exception as exc:  # noqa: BLE001
            if produced:
                raise
            last_error = exc
            await asyncio.sleep(min(2 ** KEY_ATTEMPTS, 8) / 2)
    raise RuntimeError(f'Claude no respondio: {last_error}')
