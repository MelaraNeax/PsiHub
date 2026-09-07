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
    const userEmail = localStorage.getItem('psyhub_user_email');
    if (userEmail && userEmail.includes('@')) return userEmail.trim();
    const saved = localStorage.getItem('psyhub_tr_email');
    if (saved && saved.includes('@')) return saved.trim();
  } catch {}
  return 'psyhub.app.research@gmail.com';
}

function getOpenAlexApiKey() {
  try {
    const key = localStorage.getItem('psyhub_openalex_api_key');
    if (key && key.trim()) return key.trim();
  } catch {}
  return '';
}

// Mapeo inteligente para fallback en caso de que el cluster de búsqueda anónima de OpenAlex arroje 503
const TOPIC_FALLBACKS = {
  neuro:        'C169760540',
  tcc:          'T10853',
  cbt:          'T10853',
  cognit:       'T10853',
  conduct:      'T10853',
  mindfulness:  'T10708',
  act:          'T10708',
  dbt:          'T10708',
  tercera:      'T10708',
  sistem:       'T14295',
  familiar:     'T14295',
  psicoan:      'T12889',
  humanis:      'T10214',
  gestalt:      'T10214',
  existenc:     'T10214',
  fenomen:      'T10214',
  segunda:      'T10853',
  default:      'T10214|T10853|T10708|T12889|T14295'
};

function getFallbackFilterForQuery(query) {
  const q = (query || '').toLowerCase();
  for (const [key, id] of Object.entries(TOPIC_FALLBACKS)) {
    if (key !== 'default' && q.includes(key)) {
      return id.startsWith('C') ? `concepts.id:${id}` : `topics.id:${id}`;
    }
  }
  return `topics.id:${TOPIC_FALLBACKS.default}`;
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
  const apiKey = getOpenAlexApiKey();
  const email  = getPoliteEmail();

  const params = new URLSearchParams({
    filter:     'is_oa:true',
    'per-page': perPage,
    page
  });

  if (fullQuery) {
    params.set('search', fullQuery);
  }

  if (apiKey) {
    params.set('api_key', apiKey);
  } else if (email) {
    params.set('mailto', email);
  }

  if (sort) {
    params.set('sort', sort);
  }

  let res = await fetchWithRetry(`${BASE}?${params}`, { signal });

  // Manejo de contingencia: si el cluster de búsqueda anónima de OpenAlex arroja 503
  if (res && res.status === 503) {
    console.warn('[OpenAlex 503] Búsqueda anónima pausada por sobrecarga en OpenAlex. Activando fallback por topics/filtros...');
    const fallbackFilter = getFallbackFilterForQuery(query);
    const fallbackParams = new URLSearchParams({
      filter:     `${fallbackFilter},is_oa:true`,
      'per-page': perPage,
      page,
      sort:       sort || 'cited_by_count:desc'
    });
    if (apiKey) fallbackParams.set('api_key', apiKey);
    else if (email) fallbackParams.set('mailto', email);

    res = await fetchWithRetry(`${BASE}?${fallbackParams}`, { signal });
  }

  if (!res.ok) {
    if (res.status === 429) {
      throw new Error('Límite temporal alcanzado en OpenAlex. Por favor esperá unos segundos y reintentá.');
    }
    if (res.status === 503) {
      throw new Error('OpenAlex está momentáneamente en mantenimiento. Por favor reintentá en unos momentos.');
    }
    throw new Error(`OpenAlex ${res.status}: ${res.statusText}`);
  }

  const data = await res.json();

  const results = data.results.map(w => {
    const abstract = reconstructAbstract(w.abstract_inverted_index);
    const authors  = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
    const source   = w.primary_location?.source;
    const oaUrl    = w.open_access?.oa_url || null;
    const primaryTopic = w.primary_topic?.display_name;
    const subfield     = w.primary_topic?.subfield?.display_name;
    const topicList    = (w.topics || []).map(t => t.display_name);
    const conceptList  = (w.concepts || []).filter(c => c.score > 0.35).map(c => c.display_name);
    const topics       = Array.from(new Set([primaryTopic, subfield, ...topicList, ...conceptList].filter(Boolean)));

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
      topics,
      type:          w.type || null,
      mesh:          (w.mesh || []).map(m => `${m.descriptor_name || ''} ${m.qualifier_name || ''}`.trim()).filter(Boolean)
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
  const email = getPoliteEmail();
  const apiKey = getOpenAlexApiKey();
  const params = new URLSearchParams();
  if (apiKey) params.set('api_key', apiKey);
  else if (email) params.set('mailto', email);
  const url = `${BASE}/${cleanId}?${params.toString()}`;
  const res = await fetchWithRetry(url, { signal });
  if (!res.ok) throw new Error(`OpenAlex ${res.status}: ${res.statusText}`);
  const w = await res.json();
  const abstract = reconstructAbstract(w.abstract_inverted_index);
  const authors  = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
  const source   = w.primary_location?.source;
  const oaUrl    = w.open_access?.oa_url || null;
  const primaryTopic = w.primary_topic?.display_name;
  const subfield     = w.primary_topic?.subfield?.display_name;
  const topicList    = (w.topics || []).map(t => t.display_name);
  const conceptList  = (w.concepts || []).filter(c => c.score > 0.35).map(c => c.display_name);
  const topics       = Array.from(new Set([primaryTopic, subfield, ...topicList, ...conceptList].filter(Boolean)));

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
    topics,
    type:             w.type || null,
    mesh:             (w.mesh || []).map(m => `${m.descriptor_name || ''} ${m.qualifier_name || ''}`.trim()).filter(Boolean)
  };
}
