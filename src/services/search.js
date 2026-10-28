/**
 * Buscador de PsiHub — Europe PMC (OpenAlex como respaldo).
 *
 * Europe PMC no manda cabeceras CORS, así que el navegador no puede llamarlo
 * directamente: todo pasa por el proxy `/api/search` del backend, que además
 * aprovecha su key. El respaldo es OpenAlex directo desde el cliente, para que
 * una caída del proxy deje la pantalla funcionando.
 *
 * Paginación: Europe PMC no acepta `page`, solo un cursor opaco. Este módulo
 * traduce páginas a cursores encadenados, así que el resto de la app sigue
 * pidiendo `page: 2` como antes.
 */

import { fetchPapers as openalexFetchPapers, fetchWorkById as openalexFetchWorkById } from './openalex.js';

// Mismo override local que usa el resto de la app: permite probar contra
// `http://127.0.0.1:8011` con localStorage.setItem('psyhub_api_base', …).
const HF_SPACE = (() => {
  const fallback = 'https://mi-servidor-api-42204102300.southamerica-east1.run.app';
  try {
    return (localStorage.getItem('psyhub_api_base') || fallback).replace(/\/$/, '');
  } catch { return fallback; }
})();
const SEARCH_URL = `${HF_SPACE}/api/search`;
const WORK_URL = `${HF_SPACE}/api/work`;
const REQUEST_TIMEOUT_MS = 25000;
const CACHE_TTL_MS = 3 * 60 * 1000;

// Términos adicionales por subtipo, igual que en el buscador anterior.
const SUBTYPE_TERMS = {
  all: '',
  teorico: 'theory theoretical framework conceptual epistemology review',
  evidencia: 'randomized controlled trial meta-analysis systematic review efficacy outcome empirical',
  clinico: 'clinical case study psychotherapy intervention treatment patient counseling'
};

// Una página encadenada por consulta. `next` es el cursor que devolvió el
// servidor; sin él no hay página siguiente.
const pageCache = new Map();

function cacheKey({ query, subtype, sort, perPage, access, scope, yearFrom, yearTo, study, precise }) {
  return [query, subtype || 'all', sort || 'relevance', perPage, access || 'all', scope || 'psy',
    yearFrom || '', yearTo || '', study || 'all', precise ? 'precise' : 'broad']
    .join('|')
    .toLowerCase();
}

function cleanQuery(text) {
  return String(text || '')
    .replace(/[,|"'()?*¿¡!;:]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function withTimeout(signal, timeoutMs = REQUEST_TIMEOUT_MS) {
  if (typeof AbortSignal === 'undefined' || !AbortSignal.timeout) return signal;
  const timeout = AbortSignal.timeout(timeoutMs);
  if (!signal) return timeout;
  return AbortSignal.any ? AbortSignal.any([signal, timeout]) : signal;
}

async function requestPage(params, cursor, signal) {
  const qs = new URLSearchParams({
    query: params.query,
    cursor: cursor || '*',
    page_size: String(params.perPage),
    access: params.access || 'all',
    scope: params.scope || 'psy',
    mode: params.precise ? 'precise' : 'broad'
  });
  if (params.sort) qs.set('sort', params.sort);
  if (params.yearFrom) qs.set('year_from', String(params.yearFrom));
  if (params.yearTo) qs.set('year_to', String(params.yearTo));
  if (params.study && params.study !== 'all') qs.set('study', params.study);

  const res = await fetch(`${SEARCH_URL}?${qs}`, { signal: withTimeout(signal) });
  if (res.status === 503) {
    throw new Error('Europe PMC no está disponible en este momento.');
  }
  if (res.status === 429) {
    throw new Error('El buscador alcanzó su límite de consultas. Reintentá en un minuto.');
  }
  if (!res.ok) {
    throw new Error(`Buscador ${res.status}: ${res.statusText}`);
  }
  const data = await res.json();
  return {
    results: Array.isArray(data?.results) ? data.results : [],
    meta: {
      count: data?.meta?.count ?? 0,
      cursor: data?.meta?.cursor || '',
      pageSize: data?.meta?.pageSize ?? params.perPage,
      engine: data?.meta?.engine || 'europepmc'
    }
  };
}

async function loadPage(params, page, signal, { fresh = false } = {}) {
  const key = cacheKey(params);
  let chain = pageCache.get(key);
  if (!chain) {
    chain = { pages: new Map(), at: Date.now() };
    pageCache.set(key, chain);
  }
  const now = Date.now();
  const cached = chain.pages.get(page);
  if (cached && !fresh && now - cached.at < CACHE_TTL_MS) return cached.data;

  // Se arranca desde la página cacheada más cercana hacia atrás: pedir dos o
  // tres páginas seguidas no repite las anteriores.
  let cursor = '*';
  let start = 1;
  if (page > 1) {
    for (let p = page - 1; p >= 1; p--) {
      const prev = chain.pages.get(p);
      if (prev && now - prev.at < CACHE_TTL_MS && prev.next) {
        cursor = prev.next;
        start = p + 1;
        break;
      }
    }
  }

  let data = null;
  for (let p = start; p <= page; p++) {
    data = await requestPage(params, cursor, signal);
    chain.pages.set(p, { at: Date.now(), data, next: data.meta.cursor });
    cursor = data.meta.cursor;
    if (!cursor) break;
  }

  const result = chain.pages.get(page)?.data;
  if (!result) {
    // Se pidió una página posterior al final: devolver vacío, no la anterior.
    return { results: [], meta: { count: 0, cursor: '', pageSize: params.perPage, engine: 'europepmc' } };
  }
  // Sin resultados no se cachea: un hipo temporal quedaría pegado tres minutos.
  if (!result.results.length) chain.pages.delete(page);
  if (pageCache.size > 40) pageCache.delete(pageCache.keys().next().value);
  return result;
}

/**
 * Busca papers. Misma firma y misma forma de resultado que el servicio anterior,
 * para que la interfaz no cambie.
 */
export async function fetchPapers({
  query = 'psychotherapy',
  subtype = 'all',
  sort = null,
  perPage = 10,
  page = 1,
  precise = true,
  access = 'all',
  scope = 'psy',
  yearFrom = null,
  yearTo = null,
  study = null,
  signal = null
} = {}) {
  const extras = SUBTYPE_TERMS[subtype] || '';
  const params = {
    query: cleanQuery(extras ? `${query} ${extras}` : query),
    perPage,
    access,
    scope,
    sort,
    precise,
    yearFrom,
    yearTo,
    study
  };

  try {
    let data = await loadPage(params, page, signal);

    // Si la búsqueda precisa devuelve muy poco, se repite en modo amplio: el
    // motor anterior hacía lo mismo y evita una pantalla vacía.
    if (precise && page === 1 && (data.meta?.count ?? 0) < Math.max(perPage, 5)) {
      const broad = await loadPage({ ...params, precise: false }, 1, signal);
      if ((broad.meta?.count ?? 0) > (data.meta?.count ?? 0)) data = broad;
    }

    return {
      results: data.results,
      meta: {
        count: data.meta?.count ?? 0,
        page,
        perPage: data.meta?.pageSize ?? perPage,
        cursor: data.meta?.cursor || '',
        engine: data.meta?.engine || 'europepmc'
      }
    };
  } catch (err) {
    if (signal?.aborted) throw err;
    // Respaldo: OpenAlex directo. Mantiene la app usable si el proxy falla.
    console.warn('[Buscador] Europe PMC no respondió, usando OpenAlex:', err?.message || err);
    return openalexFetchPapers({ query, subtype, sort, perPage, page, precise, access, yearFrom, yearTo, study, signal });
  }
}

/**
 * Ficha de un paper por clave (`med:123`, `pmc:456`), DOI o id de OpenAlex.
 */
export async function fetchWorkById(id, signal = null) {
  // Acepta ids de OpenAlex, claves de Europe PMC (`med:`, `pmc:`) y DOIs,
  // tanto sueltos como en forma de URL.
  const raw = String(id || '').trim()
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '');
  if (!raw) return null;

  const asOpenAlex = raw.replace(/^https?:\/\/openalex\.org\//i, '');
  const isDoi = /^10\.\d{4,9}\//.test(raw);
  const isEpmcKey = /^(med|pmc):/i.test(raw);

  if (isEpmcKey || isDoi) {
    const qs = new URLSearchParams();
    if (isDoi && !isEpmcKey) qs.set('doi', raw);
    else qs.set('key', raw);
    try {
      const res = await fetch(`${WORK_URL}?${qs}`, { signal: withTimeout(signal) });
      if (res.ok) return await res.json();
    } catch (err) {
      if (signal?.aborted) throw err;
      console.warn('[Buscador] ficha no disponible en el proxy:', err?.message || err);
    }
    // El endpoint de OpenAlex es por id, no por DOI ni por clave de Europe PMC.
    return null;
  }

  return openalexFetchWorkById(asOpenAlex, signal);
}
