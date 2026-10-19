"""Almacenamiento de artefactos en GCS (con caida silenciosa a disco local).

Cloud Run no garantiza disco persistente entre peticiones ni instancias, asi
que el PDF fuente, el Markdown, el PDF traducido, el JSON, las imagenes y la
cache se respaldan en el bucket configurado.
"""
from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import quote

import httpx

BUCKET = (os.getenv('ARTIFACT_BUCKET', '') or os.getenv('COST_BUCKET', '')).strip()
METADATA_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'

_token_cache: dict[str, float | str] = {'value': '', 'expires': 0.0}


def enabled() -> bool:
    return bool(BUCKET)


def access_token() -> str:
    explicit = os.getenv('GCS_TOKEN', '').strip()
    if explicit:
        return explicit
    now = time.time()
    if _token_cache['value'] and float(_token_cache['expires']) > now + 60:
        return str(_token_cache['value'])
    response = httpx.get(METADATA_URL, headers={'Metadata-Flavor': 'Google'}, timeout=5)
    response.raise_for_status()
    payload = response.json()
    _token_cache['value'] = payload.get('access_token', '')
    _token_cache['expires'] = now + float(payload.get('expires_in') or 0)
    return str(_token_cache['value'])


def _object_url(key: str) -> str:
    return f'https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{quote(key, safe="")}'


def put(key: str, data: bytes, content_type: str = 'application/octet-stream') -> bool:
    """Sube un objeto; devuelve False si no hay bucket o falla."""
    if not enabled():
        return False
    try:
        token = access_token()
        response = httpx.post(
            f'https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o',
            params={'uploadType': 'media', 'name': key},
            headers={'Authorization': f'Bearer {token}', 'Content-Type': content_type},
            content=data,
            timeout=60,
        )
        return response.status_code < 300
    except Exception:
        return False


def get(key: str) -> Optional[bytes]:
    """Descarga un objeto; None si no existe o falla."""
    if not enabled():
        return None
    try:
        token = access_token()
        response = httpx.get(_object_url(key), params={'alt': 'media'},
                             headers={'Authorization': f'Bearer {token}'}, timeout=60)
        if response.status_code == 200:
            return response.content
    except Exception:
        pass
    return None


def list_keys(prefix: str) -> list[str]:
    """Nombres de objetos bajo un prefijo; lista vacia si falla."""
    if not enabled():
        return []
    try:
        token = access_token()
        response = httpx.get(
            f'https://storage.googleapis.com/storage/v1/b/{BUCKET}/o',
            params={'prefix': prefix, 'maxResults': 1000},
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        if response.status_code == 200:
            return [item.get('name', '') for item in response.json().get('items') or []]
    except Exception:
        pass
    return []


def exists(key: str) -> bool:
    if not enabled():
        return False
    try:
        token = access_token()
        response = httpx.get(_object_url(key), headers={'Authorization': f'Bearer {token}'}, timeout=15)
        return response.status_code == 200
    except Exception:
        return False
