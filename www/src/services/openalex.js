/**
 * OpenAlex API Service — PsyHub
 */

const BASE = 'https://api.openalex.org/works';

// Términos adicionales por subtipo para modificar la query
const SUBTYPE_TERMS = {
  all:       '',
  teorico:   'theory theoretical framework conceptual epistemology review',
  evidencia: 'randomized controlled trial meta-analysis systematic review efficacy outcome empirical',
  clinico:   'clinical case study psychotherapy intervention treatment patient counseling'
};

function getPoliteEmail() {
  try {
    const saved = localStorage.getItem('psyhub_tr_email');
    if (saved && saved.includes('@')) return saved.trim();
  } catch {}
  return 'psyhub.app.research@gmail.com';
}

/**
 * Fetch con reintentos automáticos para evitar errores 429 de límite de frecuencia.
 */
async function fetchWithRetry(url, options = {}, retries = 2, delayMs = 1000) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, options);
      if (res.status === 429 && attempt < retries) {
        const retryAfter = parseInt(res.headers.get('Retry-After'), 10);
        const wait = !isNaN(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : delayMs * (attempt + 1);
        console.warn(`[OpenAlex 429] Rate limit temporal. Reintentando en ${wait}ms (intento ${attempt + 1}/${retries})...`);
        await new Promise(r => setTimeout(r, wait));
        continue;
      }
      return res;
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      if (attempt === retries) throw err;
      await new Promise(r => setTimeout(r, delayMs));
    }
  }
}

/**
 * Reconstruye el abstract desde abstract_inverted_index de OpenAlex.
 */
export function reconstructAbstract(invertedIndex) {
  if (!invertedIndex || typeof invertedIndex !== 'object') return '';
  const words = [];
  for (const [word, positions] of Object.entries(invertedIndex)) {
    if (Array.isArray(positions)) {
      positions.forEach(pos => { words[pos] = word; });
    }
  }
  return words.filter(Boolean).join(' ');
}

/**
 * Busca papers en OpenAlex con Polite Pool y reintentos automáticos.
 * @param {Object} opts
 * @param {string} opts.query      - Término base de búsqueda
 * @param {string} opts.subtype    - 'all' | 'teorico' | 'evidencia' | 'clinico'
 * @param {string} opts.sort       - ej. 'cited_by_count:desc'
 * @param {number} opts.perPage    - resultados por página
 * @param {number} opts.page       - número de página (1-based)
 * @param {AbortSignal} opts.signal - para cancelar peticiones obsoletas
 */
export async function fetchPapers({
  query    = 'psychotherapy',
  subtype  = 'all',
  sort     = null,
  perPage  = 20,
  page     = 1,
  signal   = null
} = {}) {
  const extras = SUBTYPE_TERMS[subtype] || '';
  const fullQuery = extras ? `${query} ${extras}` : query;

  const params = new URLSearchParams({
    search:     fullQuery,
    filter:     'is_oa:true',
    'per-page': perPage,
    page,
    mailto:     getPoliteEmail()
  });

  if (sort) {
    params.set('sort', sort);
  }

  const res = await fetchWithRetry(`${BASE}?${params}`, { signal });
  if (!res.ok) {
    if (res.status === 429) {
      throw new Error('Límite temporal alcanzado en OpenAlex. Por favor esperá unos segundos y reintentá.');
    }
    throw new Error(`OpenAlex ${res.status}: ${res.statusText}`);
  }

  const data = await res.json();

  const results = data.results.map(w => {
    const abstract = reconstructAbstract(w.abstract_inverted_index);
    const authors  = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
    const source   = w.primary_location?.source;
    const oaUrl    = w.open_access?.oa_url || null;
    const topics   = (w.concepts || []).filter(c => c.score > 0.4).slice(0, 5).map(c => c.display_name);

    return {
      id:            w.id,
      doi:           w.doi || null,
      title:         w.title || 'Sin título',
      authors,
      firstInstitution: w.authorships?.[0]?.institutions?.[0]?.display_name || null,
      journal:       source?.display_name || null,
      year:          w.publication_year || null,
      isOa:          w.open_access?.is_oa ?? false,
      oaUrl,
      citations:     w.cited_by_count ?? 0,
      abstract,
      topics
    };
  });

  return {
    results,
    meta: {
      count:   data.meta?.count ?? 0,
      page:    data.meta?.page  ?? page,
      perPage: data.meta?.per_page ?? perPage
    }
  };
}

/**
 * Obtiene los detalles completos de un paper por su ID de OpenAlex.
 * @param {string} id
 * @param {AbortSignal} signal
 */
export async function fetchWorkById(id, signal = null) {
  if (!id) return null;
  const cleanId = id.startsWith('http') ? id.split('/').pop() : id;
  const email = getPoliteEmail();
  const url = `${BASE}/${cleanId}?mailto=${encodeURIComponent(email)}`;
  const res = await fetchWithRetry(url, { signal });
  if (!res.ok) throw new Error(`OpenAlex ${res.status}: ${res.statusText}`);
  const w = await res.json();
  const abstract = reconstructAbstract(w.abstract_inverted_index);
  const authors  = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
  const source   = w.primary_location?.source;
  const oaUrl    = w.open_access?.oa_url || null;
  const topics   = (w.concepts || []).filter(c => c.score > 0.4).slice(0, 5).map(c => c.display_name);

  return {
    id:               w.id,
    doi:              w.doi || null,
    title:            w.title || 'Sin título',
    authors,
    firstInstitution: w.authorships?.[0]?.institutions?.[0]?.display_name || null,
    journal:          source?.display_name || null,
    year:             w.publication_year || null,
    isOa:             w.open_access?.is_oa ?? false,
    oaUrl,
    citations:        w.cited_by_count ?? 0,
    abstract,
    topics
  };
}
