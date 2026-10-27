// Autenticación opcional con Supabase (Google OAuth).
//
// La app funciona sin cuenta: todo se guarda en localStorage. Cuando el
// proyecto configura Supabase, este módulo agrega el inicio de sesión real y
// deja el token disponible para el backend (`Authorization: Bearer …`), que a
// su vez lo valida en /api/auth/me y en las tablas con RLS.
//
// Configuración (cualquiera de las dos, en index.html):
//   <meta name="psyhub-supabase-url" content="https://xxxx.supabase.co">
//   <meta name="psyhub-supabase-anon" content="eyJ...">
// o antes de cargar app.js:
//   window.PSYHUB_SUPABASE = { url: '…', anonKey: '…' };

const SESSION_KEY = 'psyhub_auth_session';
const USER_KEY = 'psyhub_auth_user';

function readConfig() {
  const fromWindow = typeof window !== 'undefined' ? window.PSYHUB_SUPABASE : null;
  const meta = name => {
    if (typeof document === 'undefined') return '';
    return document.querySelector(`meta[name="${name}"]`)?.getAttribute('content')?.trim() || '';
  };
  const url = (fromWindow?.url || meta('psyhub-supabase-url')).replace(/\/+$/, '');
  const anonKey = fromWindow?.anonKey || meta('psyhub-supabase-anon') || '';
  return { url, anonKey };
}

function loadJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
}

function saveJson(key, value) {
  try {
    if (value) localStorage.setItem(key, JSON.stringify(value));
    else localStorage.removeItem(key);
  } catch { }
}

function decodeJwt(token) {
  try {
    const payload = token.split('.')[1];
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(
      atob(padded)
        .split('')
        .map(c => `%${c.charCodeAt(0).toString(16).padStart(2, '0')}`)
        .join('')
    );
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function userFromClaims(claims) {
  if (!claims?.sub) return null;
  return {
    id: String(claims.sub),
    email: claims.email || '',
    name: claims.user_metadata?.full_name
      || claims.user_metadata?.name
      || claims.email?.split('@')[0]
      || '',
    avatar: claims.user_metadata?.avatar_url || '',
    provider: claims.app_metadata?.provider || 'google'
  };
}

let cachedUser = loadJson(USER_KEY);

export const auth = {
  isConfigured() {
    const { url, anonKey } = readConfig();
    return Boolean(url && anonKey);
  },

  /** Sesión guardada: { access_token, refresh_token, expires_at }. */
  session() {
    const value = loadJson(SESSION_KEY);
    if (!value?.access_token) return null;
    if (value.expires_at && value.expires_at * 1000 < Date.now()) return value;
    return value;
  },

  /** Usuario actual (sin llamar a la red). */
  currentUser() {
    const session = this.session();
    if (session?.access_token) {
      const user = userFromClaims(decodeJwt(session.access_token));
      if (user) {
        cachedUser = user;
        saveJson(USER_KEY, user);
        return user;
      }
    }
    return cachedUser;
  },

  /** Encabezados para llamadas al backend que aceptan sesión. */
  headers() {
    const session = this.session();
    return session?.access_token
      ? { Authorization: `Bearer ${session.access_token}` }
      : {};
  },

  /**
   * Inicia el flujo OAuth de Google. Supabase redirige de vuelta a la app con
   * los tokens en el hash (implicit) o con ?code= (PKCE). Este cliente usa el
   * flujo implícito, que no requiere un intercambio adicional.
   */
  async signInWithGoogle({ redirectTo } = {}) {
    const { url, anonKey } = readConfig();
    if (!url || !anonKey) return false;
    const redirect = redirectTo || `${location.origin}${location.pathname}`;
    const params = new URLSearchParams({ provider: 'google', redirect_to: redirect });
    // Supabase devuelve la URL de Google cuando se le pasa la apikey por
    // header; si el navegador no lo permite, se navega con la key en la URL.
    try {
      const res = await fetch(`${url}/auth/v1/authorize?${params}`, {
        headers: { apikey: anonKey }
      });
      const data = await res.json();
      if (data?.url) {
        location.assign(data.url);
        return true;
      }
    } catch { }
    params.set('apikey', anonKey);
    location.assign(`${url}/auth/v1/authorize?${params}`);
    return true;
  },

  /** Captura los tokens cuando Supabase vuelve a la app. Devuelve true si hubo sesión. */
  handleRedirect() {
    if (typeof location === 'undefined') return false;
    const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
    const accessToken = hash.get('access_token');
    if (!accessToken) return false;
    const expiresIn = parseInt(hash.get('expires_in') || '3600', 10);
    saveJson(SESSION_KEY, {
      access_token: accessToken,
      refresh_token: hash.get('refresh_token') || '',
      expires_at: Math.floor(Date.now() / 1000) + (Number.isFinite(expiresIn) ? expiresIn : 3600)
    });
    const user = userFromClaims(decodeJwt(accessToken));
    cachedUser = user;
    saveJson(USER_KEY, user);
    // Limpia los tokens de la barra de direcciones.
    history.replaceState(null, '', location.pathname + location.search);
    return true;
  },

  /** Renueva el access token con el refresh token. */
  async refresh() {
    const { url, anonKey } = readConfig();
    const session = this.session();
    if (!url || !anonKey || !session?.refresh_token) return false;
    try {
      const res = await fetch(`${url}/auth/v1/token?grant_type=refresh_token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', apikey: anonKey },
        body: JSON.stringify({ refresh_token: session.refresh_token })
      });
      if (!res.ok) return false;
      const data = await res.json();
      if (!data?.access_token) return false;
      saveJson(SESSION_KEY, {
        access_token: data.access_token,
        refresh_token: data.refresh_token || session.refresh_token,
        expires_at: Math.floor(Date.now() / 1000) + (data.expires_in || 3600)
      });
      const user = userFromClaims(decodeJwt(data.access_token));
      cachedUser = user;
      saveJson(USER_KEY, user);
      return true;
    } catch {
      return false;
    }
  },

  async signOut() {
    const { url, anonKey } = readConfig();
    const session = this.session();
    try {
      if (url && anonKey && session?.access_token) {
        await fetch(`${url}/auth/v1/logout`, {
          method: 'POST',
          headers: { apikey: anonKey, Authorization: `Bearer ${session.access_token}` }
        });
      }
    } catch { }
    cachedUser = null;
    saveJson(SESSION_KEY, null);
    saveJson(USER_KEY, null);
  },

  /** Verifica la sesión contra el backend y devuelve el usuario o null. */
  async verifyWithServer(apiBase) {
    if (!apiBase) return null;
    const headers = this.headers();
    if (!headers.Authorization) return null;
    try {
      const res = await fetch(`${apiBase}/api/auth/me`, { headers, cache: 'no-store' });
      if (res.status === 401) {
        const refreshed = await this.refresh();
        if (!refreshed) return null;
        const retry = await fetch(`${apiBase}/api/auth/me`, {
          headers: this.headers(), cache: 'no-store'
        });
        if (!retry.ok) return null;
        const data = await retry.json();
        return data?.user || this.currentUser();
      }
      if (!res.ok) return this.currentUser();
      const data = await res.json();
      return data?.user || this.currentUser();
    } catch {
      return this.currentUser();
    }
  }
};
