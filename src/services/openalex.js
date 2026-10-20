/**
 * OpenAlex API Service — PsyHub
 */

const BASE = 'https://api.openalex.org/works';

// Términos adicionales por subtipo para modificar la query
const SUBTYPE_TERMS = {
  all: '',
  teorico: 'theory theoretical framework conceptual epistemology review',
  evidencia: 'randomized controlled trial meta-analysis systematic review efficacy outcome empirical',
  clinico: 'clinical case study psychotherapy intervention treatment patient counseling'
};

function getPoliteEmail() {
  try {
    const userEmail = localStorage.getItem('psyhub_user_email');
    if (userEmail && userEmail.includes('@')) return userEmail.trim();
    const saved = localStorage.getItem('psyhub_tr_email');
    if (saved && saved.includes('@')) return saved.trim();
  } catch { }
  return 'psyhub.app.research@gmail.com';
}

function getOpenAlexApiKey() {
  try {
    const key = localStorage.getItem('psyhub_openalex_api_key');
    if (key && key.trim()) return key.trim();
  } catch { }
  return '';
}

// Eliminado blacklist y validaciones manuales.
// Ahora se valida el PDF asincrónicamente contra el backend.

// Mapeo inteligente para fallback en caso de que el cluster de búsqueda anónima de OpenAlex arroje 503
const TOPIC_FALLBACKS = {
  neuro: 'C169760540',
  tcc: 'T10853',
  cbt: 'T10853',
  cognit: 'T10853',
  conduct: 'T10853',
  mindfulness: 'T10708',
  act: 'T10708',
  dbt: 'T10708',
  tercera: 'T10708',
  sistem: 'T14295',
  familiar: 'T14295',
  psicoan: 'T12889',
  humanis: 'T10214',
  gestalt: 'T10214',
  existenc: 'T10214',
  fenomen: 'T10214',
  segunda: 'T10853',
  default: 'T10214|T10853|T10708|T12889|T14295'
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
 * Extrae la mejor URL de PDF de un work de OpenAlex.
 *
 * Prioridad:
 *   1. best_oa_location.pdf_url (es la ubicación OA "ganadora")
 *   2. Cualquier location.pdf_url que sea OA
 *   3. primary_location.pdf_url
 *   4. oa_url si termina en .pdf o matchea patrones típicos de descarga
 *
 * Devuelve null si no hay ninguna URL con forma de PDF.
 */
function extractPdfUrl(w) {
  const candidates = [];

  if (w.best_oa_location?.pdf_url) {
    candidates.push(w.best_oa_location.pdf_url);
  }

  for (const loc of w.locations || []) {
    if (loc?.pdf_url) {
      candidates.push(loc.pdf_url);
    }
  }

  if (w.primary_location?.pdf_url) {
    candidates.push(w.primary_location.pdf_url);
  }

  // Filtrar duplicados preservando orden.
  const seen = new Set();
  const unique = candidates.filter(u => u && !seen.has(u) && (seen.add(u), true));

  if (unique.length > 0) {
    return unique[0];
  }

  // Fallback: oa_url si tiene pinta de ser un PDF directo.
  const oaUrl = w.open_access?.oa_url;
  if (oaUrl) {
    if (
      oaUrl.toLowerCase().endsWith('.pdf') ||
      /\/(?:pdf|download|fulltext|full-text|article\/[^/]+\/pdf)/i.test(oaUrl)
    ) {
      return oaUrl;
    }
  }

  return null;
}

/**
 * Determina si un paper es "auto-descargable": OA + tiene URL de PDF.
 */
function isAutoDownloadable(w) {
  if (!w?.open_access?.is_oa) return false;
  return Boolean(extractPdfUrl(w));
}

const PSY_FIELDS = 'primary_topic.field.id:32|27';
const WORK_TYPES = 'type:article|review';
const SELECT_FIELDS = [
  'id', 'doi', 'title', 'publication_year', 'cited_by_count', 'authorships',
  'primary_location', 'best_oa_location', 'locations', 'open_access',
  'abstract_inverted_index', 'primary_topic', 'topics', 'concepts', 'type', 'mesh'
].join(',');
const CACHE_TTL_MS = 3 * 60 * 1000;
const responseCache = new Map();

function sanitizeQuery(text) {
  return String(text || '')
    .replace(/[,|"'()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function buildWorksParams({ query, subtype, sort, perPage, page, precise, apiKey, email }) {
  const extras = SUBTYPE_TERMS[subtype] || '';
  const fullQuery = sanitizeQuery(extras ? `${query} ${extras}` : query);
  const filters = ['is_oa:true', PSY_FIELDS, WORK_TYPES];

  if (precise && fullQuery) filters.push(`title_and_abstract.search:${fullQuery}`);

  const params = new URLSearchParams({
    filter: filters.join(','),
    'per-page': String(perPage),
    page: String(page),
    select: SELECT_FIELDS
  });

  if (!precise && fullQuery) params.set('search', fullQuery);
  if (apiKey) params.set('api_key', apiKey);
  else if (email) params.set('mailto', email);
  if (sort) params.set('sort', sort);

  return params;
}

async function fetchWorksPage(params, signal) {
  const url = `${BASE}?${params}`;
  const cached = responseCache.get(url);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  let res = await fetchWithRetry(url, { signal });

  // Contingencia: si la búsqueda anónima devuelve 503, caemos a topics.
  if (res && res.status === 503) {
    console.warn('[OpenAlex 503] Activando fallback por topics…');
    const fallbackParams = new URLSearchParams(params);
    const originalFilter = fallbackParams.get('filter') || '';
    const cleanFilter = originalFilter
      .split(',')
      .filter(part => !part.startsWith('title_and_abstract.search:'))
      .join(',');
    const topicFilter = getFallbackFilterForQuery(fallbackParams.get('search') || originalFilter);
    fallbackParams.set('filter', `${cleanFilter},${topicFilter}`);
    fallbackParams.delete('search');
    if (!fallbackParams.get('sort')) fallbackParams.set('sort', 'cited_by_count:desc');
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
  if (!signal?.aborted) {
    if (responseCache.size > 40) {
      responseCache.delete(responseCache.keys().next().value);
    }
    responseCache.set(url, { at: Date.now(), data });
  }
  return data;
}

/**
 * Busca papers en OpenAlex con Polite Pool y reintentos automáticos.
 *
 * Estrategia de precisión:
 *  1. Filtra a Psicología/Psiquiatría y a artículos/revisiones.
 *  2. Busca en título + abstract (title_and_abstract.search).
 *  3. Si el resultado es demasiado estrecho, repite con búsqueda amplia.
 */
export async function fetchPapers({
  query = 'psychotherapy',
  subtype = 'all',
  sort = null,
  perPage = 10,
  page = 1,
  precise = true,
  signal = null
} = {}) {
  const apiKey = getOpenAlexApiKey();
  const email = getPoliteEmail();
  const base = { query, subtype, sort, perPage, page, precise, apiKey, email };

  let data = await fetchWorksPage(buildWorksParams(base), signal);

  // Fallback de precisión: si title/abstract devuelve muy poco, ampliamos.
  if (precise && page === 1 && (data.meta?.count ?? 0) < Math.max(perPage, 5)) {
    const broad = await fetchWorksPage(
      buildWorksParams({ ...base, precise: false }),
      signal
    );
    if ((broad.meta?.count ?? 0) > (data.meta?.count ?? 0)) data = broad;
  }

  const results = (data.results || []).map(w => {
    const abstract = reconstructAbstract(w.abstract_inverted_index);
    const authors = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
    const source = w.primary_location?.source;
    const oaUrl = w.open_access?.oa_url || null;
    const primaryTopic = w.primary_topic?.display_name;
    const subfield = w.primary_topic?.subfield?.display_name;
    const topicList = (w.topics || []).map(t => t.display_name);
    const conceptList = (w.concepts || []).filter(c => c.score > 0.35).map(c => c.display_name);
    const topics = Array.from(new Set([primaryTopic, subfield, ...topicList, ...conceptList].filter(Boolean)));

    return {
      id: w.id,
      doi: w.doi || null,
      title: w.title || 'Sin título',
      authors,
      firstInstitution: w.authorships?.[0]?.institutions?.[0]?.display_name || null,
      journal: source?.display_name || null,
      year: w.publication_year || null,
      isOa: w.open_access?.is_oa ?? false,
      oaStatus: w.open_access?.oa_status || null,
      oaUrl,
      pdfUrl: extractPdfUrl(w),
      isAutoDownloadable: isAutoDownloadable(w),
      citations: w.cited_by_count ?? 0,
      abstract,
      topics,
      type: w.type || null,
      mesh: (w.mesh || []).map(m => `${m.descriptor_name || ''} ${m.qualifier_name || ''}`.trim()).filter(Boolean)
    };
  });

  return {
    results,
    meta: {
      count: data.meta?.count ?? 0,
      page: data.meta?.page ?? page,
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
  let cleanId = String(id).trim();
  if (cleanId.startsWith('https://openalex.org/')) {
    cleanId = cleanId.replace('https://openalex.org/', '');
  }
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
  const authors = (w.authorships || []).map(a => a.author?.display_name).filter(Boolean);
  const source = w.primary_location?.source;
  const oaUrl = w.open_access?.oa_url || null;
  const primaryTopic = w.primary_topic?.display_name;
  const subfield = w.primary_topic?.subfield?.display_name;
  const topicList = (w.topics || []).map(t => t.display_name);
  const conceptList = (w.concepts || []).filter(c => c.score > 0.35).map(c => c.display_name);
  const topics = Array.from(new Set([primaryTopic, subfield, ...topicList, ...conceptList].filter(Boolean)));

  return {
    id: w.id,
    doi: w.doi || null,
    title: w.title || 'Sin título',
    authors,
    firstInstitution: w.authorships?.[0]?.institutions?.[0]?.display_name || null,
    journal: source?.display_name || null,
    year: w.publication_year || null,
    isOa: w.open_access?.is_oa ?? false,
    oaStatus: w.open_access?.oa_status || null,
    oaUrl,
    pdfUrl: extractPdfUrl(w),
    isAutoDownloadable: isAutoDownloadable(w),
    citations: w.cited_by_count ?? 0,
    abstract,
    topics,
    type: w.type || null,
    mesh: (w.mesh || []).map(m => `${m.descriptor_name || ''} ${m.qualifier_name || ''}`.trim()).filter(Boolean)
  };
}
