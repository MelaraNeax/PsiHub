"""Registro de costos por documento.

El ledger se guarda en GCS cuando COST_BUCKET esta configurado (Cloud Run) y
cae a un archivo JSONL local en desarrollo. Ningun error del ledger debe
interrumpir una traduccion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import httpx

COST_BUCKET = os.getenv('COST_BUCKET', '').strip()
COST_PREFIX = os.getenv('COST_PREFIX', 'costs').strip('/')
# DeepSeek V4.1 Flash: USD por millon de tokens (tarifa estandar).
PRICE_INPUT = float(os.getenv('DEEPSEEK_PRICE_INPUT', '0.15'))
PRICE_OUTPUT = float(os.getenv('DEEPSEEK_PRICE_OUTPUT', '0.60'))
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '').strip()
ADMIN_TOKEN_OBJECT = os.getenv('ADMIN_TOKEN_OBJECT', 'admin/token.txt').strip('/')
# Hash SHA-256 de la frase de acceso; permite abrir /costos sin guardar el texto.
ADMIN_TOKEN_HASH = os.getenv(
    'ADMIN_TOKEN_HASH',
    '31c5e8920ad52900399465aad7c3b3b12598741c91b6244ecbd8c2716fc0809a',
).strip().lower()

_token_value: dict[str, str] = {'value': ADMIN_TOKEN}

CACHE_DIR = Path(os.getenv('CACHE_DIR', './cache')).resolve()
LOCAL_LEDGER = CACHE_DIR / 'costs.jsonl'
METADATA_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'

_token_cache: dict[str, float | str] = {'value': '', 'expires': 0.0}


class LedgerError(RuntimeError):
    pass


def estimate_cost(usage: dict) -> dict:
    """Calcula el costo estimado en USD a partir del usage de DeepSeek."""
    prompt = int(usage.get('prompt_tokens') or 0)
    completion = int(usage.get('completion_tokens') or 0)
    total = int(usage.get('total_tokens') or (prompt + completion))
    cost = (prompt * PRICE_INPUT + completion * PRICE_OUTPUT) / 1_000_000
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'total_tokens': total,
        'cost_usd': round(cost, 6),
        'price_input_per_m': PRICE_INPUT,
        'price_output_per_m': PRICE_OUTPUT,
    }


def _access_token() -> str:
    explicit = os.getenv('COST_GCS_TOKEN', '').strip()
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


def _object_name(record: dict) -> str:
    stamp = record.get('timestamp') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    safe_stamp = stamp.replace(':', '').replace('-', '')
    return f"{COST_PREFIX}/{safe_stamp}-{record.get('doc_id', '')[:16]}.json"


def _gcs_write(record: dict) -> None:
    token = _access_token()
    name = httpx.QueryParams({'name': _object_name(record)})['name']
    url = f'https://storage.googleapis.com/upload/storage/v1/b/{COST_BUCKET}/o'
    response = httpx.post(
        url,
        params={'uploadType': 'media', 'name': name},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        content=json.dumps(record, ensure_ascii=False).encode('utf-8'),
        timeout=20,
    )
    response.raise_for_status()


def _local_write(record: dict) -> None:
    LOCAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_LEDGER.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def record_cost(record: dict) -> None:
    """Guarda una entrada del ledger; nunca lanza excepciones."""
    try:
        if COST_BUCKET:
            try:
                _gcs_write(record)
                return
            except Exception:
                pass
        _local_write(record)
    except Exception:
        pass


def _gcs_list(limit: int) -> list[dict]:
    token = _access_token()
    url = f'https://storage.googleapis.com/storage/v1/b/{COST_BUCKET}/o'
    response = httpx.get(
        url,
        params={'prefix': f'{COST_PREFIX}/', 'maxResults': max(1, min(limit, 1000))},
        headers={'Authorization': f'Bearer {token}'},
        timeout=20,
    )
    response.raise_for_status()
    items = response.json().get('items') or []
    records: list[dict] = []
    for item in items:
        name = item.get('name', '')
        if not name.endswith('.json'):
            continue
        blob = httpx.get(
            f'{url}/{quote(name, safe="")}',
            params={'alt': 'media'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        if blob.status_code == 200:
            try:
                records.append(blob.json())
            except Exception:
                continue
    return records


def _local_list() -> list[dict]:
    if not LOCAL_LEDGER.exists():
        return []
    records: list[dict] = []
    for line in LOCAL_LEDGER.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def list_costs(limit: int = 300) -> list[dict]:
    """Devuelve las entradas mas recientes, ordenadas por fecha descendente."""
    records: Iterable[dict]
    if COST_BUCKET:
        try:
            records = _gcs_list(limit)
        except Exception:
            records = _local_list()
    else:
        records = _local_list()
    ordered = sorted(records, key=lambda r: str(r.get('timestamp') or ''), reverse=True)
    return ordered[:limit]


def summarize(records: Iterable[dict]) -> dict:
    records = list(records)
    return {
        'documents': len(records),
        'pages': sum(int(r.get('pages') or 0) for r in records),
        'prompt_tokens': sum(int(r.get('prompt_tokens') or 0) for r in records),
        'completion_tokens': sum(int(r.get('completion_tokens') or 0) for r in records),
        'total_tokens': sum(int(r.get('total_tokens') or 0) for r in records),
        'cost_usd': round(sum(float(r.get('cost_usd') or 0) for r in records), 6),
    }


def _token_from_bucket() -> str:
    if not COST_BUCKET:
        return ''
    try:
        token = _access_token()
        url = f'https://storage.googleapis.com/storage/v1/b/{COST_BUCKET}/o/{quote(ADMIN_TOKEN_OBJECT, safe="")}'
        response = httpx.get(url, params={'alt': 'media'},
                             headers={'Authorization': f'Bearer {token}'}, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
    except Exception:
        pass
    return ''


def admin_token() -> str:
    """Token de administracion: variable de entorno o archivo privado en GCS."""
    if _token_value['value']:
        return _token_value['value']
    _token_value['value'] = _token_from_bucket()
    return _token_value['value']


def admin_token_matches(token: Optional[str]) -> bool:
    if not token:
        return False
    expected = admin_token()
    if expected:
        return hmac.compare_digest(token, expected)
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    return hmac.compare_digest(digest, ADMIN_TOKEN_HASH)
