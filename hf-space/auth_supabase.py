"""Verificacion de sesiones de Supabase para el backend.

Hoy la app funciona sin cuentas. Cuando se active el login con Google, el
navegador guarda el `access_token` de Supabase y lo manda como
`Authorization: Bearer …`. Este modulo valida ese token sin obligar a usar el
SDK de Supabase en el servidor:

- proyectos con JWT secret (HS256): se valida con hmac y `SUPABASE_JWT_SECRET`.
- proyectos nuevos con claves asimetricas: si `PyJWT[crypto]` esta instalado y
  `SUPABASE_JWKS_URL` esta definido, se valida contra el JWKS del proyecto.

Nunca se guarda el token. Si no hay configuracion, `user_from_request` devuelve
None y la app sigue siendo anonima/local, como hasta ahora.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

SUPABASE_JWT_SECRET = os.getenv('SUPABASE_JWT_SECRET', '').strip()
SUPABASE_JWKS_URL = os.getenv('SUPABASE_JWKS_URL', '').strip()
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
SUPABASE_ANON_KEY = os.getenv('SUPABASE_ANON_KEY', '').strip()

_jwks_cache: dict[str, object] = {'client': None, 'at': 0.0}


class AuthError(RuntimeError):
    pass


def configured() -> bool:
    return bool(SUPABASE_JWT_SECRET or SUPABASE_JWKS_URL)


def _b64url(value: str) -> bytes:
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _verify_hs256(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split('.')
        header = json.loads(_b64url(header_b64))
        payload = json.loads(_b64url(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise AuthError('Token con formato invalido.') from exc
    if str(header.get('alg')).upper() != 'HS256':
        raise AuthError('Algoritmo de token no soportado sin JWKS.')
    expected = hmac.new(
        SUPABASE_JWT_SECRET.encode('utf-8'),
        f'{header_b64}.{payload_b64}'.encode('ascii'),
        hashlib.sha256,
    ).digest()
    try:
        received = _b64url(signature_b64)
    except Exception as exc:  # noqa: BLE001
        raise AuthError('Firma invalida.') from exc
    if not hmac.compare_digest(expected, received):
        raise AuthError('Firma invalida.')
    exp = payload.get('exp')
    if exp and float(exp) < time.time():
        raise AuthError('Sesion expirada.')
    return payload


def _verify_jwks(token: str) -> dict:
    try:
        import jwt  # type: ignore
        from jwt import PyJWKClient  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise AuthError('Falta PyJWT[crypto] para validar tokens asimetricos.') from exc
    now = time.time()
    client = _jwks_cache.get('client')
    if client is None or now - float(_jwks_cache.get('at') or 0) > 3600:
        client = PyJWKClient(SUPABASE_JWKS_URL, cache_keys=True)
        _jwks_cache['client'] = client
        _jwks_cache['at'] = now
    try:
        signing_key = client.get_signing_key_from_jwt(token)  # type: ignore[union-attr]
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=['RS256', 'ES256', 'HS256'],
            options={'verify_aud': False},
        )
    except Exception as exc:  # noqa: BLE001
        raise AuthError('No se pudo validar el token.') from exc


def verify_token(token: str) -> dict:
    if not token:
        raise AuthError('Falta el token.')
    if SUPABASE_JWT_SECRET:
        return _verify_hs256(token)
    if SUPABASE_JWKS_URL:
        return _verify_jwks(token)
    raise AuthError('Supabase no esta configurado en el servidor.')


def bearer_token(authorization: str) -> str:
    raw = (authorization or '').strip()
    if not raw.lower().startswith('bearer '):
        return ''
    return raw[7:].strip()


def user_from_authorization(authorization: str) -> Optional[dict]:
    """Devuelve {id, email, role} o None si no hay sesion valida."""
    token = bearer_token(authorization)
    if not token:
        return None
    try:
        claims = verify_token(token)
    except AuthError:
        return None
    user_id = str(claims.get('sub') or '')
    if not user_id:
        return None
    return {
        'id': user_id,
        'email': claims.get('email'),
        'role': claims.get('role'),
    }


def require_user(authorization: str) -> dict:
    """Para endpoints que en el futuro exijan cuenta."""
    token = bearer_token(authorization)
    if not token:
        raise AuthError('Se necesita iniciar sesion.')
    claims = verify_token(token)
    user_id = str(claims.get('sub') or '')
    if not user_id:
        raise AuthError('Token sin usuario.')
    return {'id': user_id, 'email': claims.get('email'), 'role': claims.get('role')}
