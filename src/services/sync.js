// Sincronización de datos del usuario con Supabase (PostgREST + RLS).
//
// La app guarda todo en localStorage. Cuando hay sesión, este módulo espeja el
// estado persistible en una fila por usuario (`user_sync.data` jsonb) y, al
// iniciar sesión, la vuelca de vuelta para que el historial viaje entre
// dispositivos. RLS garantiza que cada usuario solo lee/escribe su fila.
//
// No depende del backend: usa la anon key pública y el access_token del usuario.
import { auth } from './auth.js';

const TABLE = 'user_sync';
const DEBOUNCE_MS = 800;

function readMeta(name) {
  if (typeof document === 'undefined') return '';
  return document.querySelector(`meta[name="${name}"]`)?.getAttribute('content')?.trim() || '';
}

function baseUrl() {
  return readMeta('psyhub-supabase-url').replace(/\/+$/, '');
}

function anonKey() {
  return readMeta('psyhub-supabase-anon');
}

function requestHeaders() {
  const headers = {
    apikey: anonKey(),
    'Content-Type': 'application/json',
  };
  const token = auth.session()?.access_token;
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

let _timer = null;
let _pending = null;

export const sync = {
  enabled() {
    return Boolean(baseUrl() && anonKey() && auth.session()?.access_token);
  },

  /** Trae la fila del usuario (o null si no hay datos todavía). */
  async pull() {
    if (!this.enabled()) return null;
    try {
      const res = await fetch(
        `${baseUrl()}/rest/v1/${TABLE}?select=data&limit=1`,
        { headers: requestHeaders() }
      );
      if (!res.ok) return null;
      const rows = await res.json();
      return (rows && rows[0] && rows[0].data) || null;
    } catch {
      return null;
    }
  },

  /** Programa una subida (debounced). Reemplaza la fila completa del usuario. */
  push(data) {
    if (!this.enabled()) return;
    _pending = data;
    clearTimeout(_timer);
    _timer = setTimeout(() => this._flush(), DEBOUNCE_MS);
  },

  async _flush() {
    const data = _pending;
    _pending = null;
    if (!data || !this.enabled()) return;
    const userId = auth.currentUser()?.id;
    if (!userId) return;
    try {
      const res = await fetch(
        `${baseUrl()}/rest/v1/${TABLE}?on_conflict=user_id`,
        {
          method: 'POST',
          headers: {
            ...requestHeaders(),
            Prefer: 'resolution=merge-duplicates,return=minimal',
          },
          body: JSON.stringify({
            user_id: userId,
            data,
            updated_at: new Date().toISOString(),
          }),
        }
      );
      if (!res.ok) console.warn('[sync] subida falló', res.status);
    } catch (err) {
      console.warn('[sync] error de red', err);
    }
  },

  /** Vuelca una subida pendiente antes de cerrar sesión. */
  async flushNow() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    if (_pending) { const d = _pending; _pending = null; await this._pushData(d); }
  },

  async _pushData(data) {
    if (!data || !this.enabled()) return;
    const userId = auth.currentUser()?.id;
    if (!userId) return;
    try {
      await fetch(`${baseUrl()}/rest/v1/${TABLE}?on_conflict=user_id`, {
        method: 'POST',
        headers: { ...requestHeaders(), Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: JSON.stringify({ user_id: userId, data, updated_at: new Date().toISOString() }),
      });
    } catch { }
  },
};
