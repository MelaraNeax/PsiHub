/**
 * PsyHub — Servicio de Traducción de Alto Rendimiento
 * Motor Principal: Google Translate GTX (100% gratuito, sin límite de 10k palabras/día, alta calidad)
 * Motor de Respaldo: MyMemory API
 * Incluye: Detección inteligente de idioma, caché persistente en LocalStorage y procesamiento por lotes.
 */

const CACHE = new Map();
const STORE_KEY = 'psyhub_tr_cache_v2';
const MAX_CACHE_ENTRIES = 1000;

// Cargar caché persistente desde localStorage
try {
  const stored = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
  Object.entries(stored).forEach(([k, v]) => CACHE.set(k, v));
} catch {}

function persistCache() {
  try {
    const entries = [...CACHE.entries()].slice(-MAX_CACHE_ENTRIES);
    localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch {}
}

/**
 * Traduce usando Google Translate GTX API (gratuito, sin api key, rápido y sin límite diario)
 */
async function translateGoogle(text, from = 'auto', to = 'es') {
  const url = `https://translate.googleapis.com/translate_a/single?client=gtx&sl=${from}&tl=${to}&dt=t&q=${encodeURIComponent(text)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Google Translate HTTP ${res.status}`);
  const data = await res.json();
  if (!Array.isArray(data) || !Array.isArray(data[0])) {
    throw new Error('Respuesta inválida de Google Translate');
  }
  const translated = data[0].map(item => (item && item[0]) ? item[0] : '').join('');
  return translated || text;
}

/**
 * Motor de respaldo: MyMemory API
 */
async function translateMyMemory(text, langpair) {
  const email = localStorage.getItem('psyhub_tr_email') || '';
  const params = new URLSearchParams({
    q: text.slice(0, 500),
    langpair,
    ...(email ? { de: email } : {})
  });
  const res = await fetch(`https://api.mymemory.translated.net/get?${params}`);
  if (!res.ok) throw new Error(`MyMemory HTTP ${res.status}`);
  const data = await res.json();
  const tr = data?.responseData?.translatedText;
  if (!tr || tr.includes('MYMEMORY WARNING')) throw new Error('MyMemory quota reached');
  return tr;
}

/**
 * Heurística para saber si un texto ya está en español
 */
function isAlreadySpanish(text) {
  if (!text) return false;
  // Letras características del español
  if (/[áéíóúüñÁÉÍÓÚÜÑ]/.test(text)) return true;
  // Palabras comunes en español vs inglés
  const lower = text.toLowerCase();
  const spanishWords = [' de ', ' la ', ' el ', ' en ', ' y ', ' los ', ' las ', ' del ', ' por ', ' una ', ' con ', ' para ', ' psicoterapia ', ' estudio ', ' salud '];
  const count = spanishWords.filter(w => lower.includes(w)).length;
  return count >= 2;
}

/**
 * Heurística para saber si una query de búsqueda ya está en inglés
 */
function isAlreadyEnglish(text) {
  if (!text) return true;
  if (/[áéíóúüñÁÉÍÓÚÜÑ¿¡]/.test(text)) return false;
  const lower = text.toLowerCase();
  const englishIndicators = ['therapy', 'treatment', 'psychotherapy', 'depression', 'anxiety', 'trial', 'review', 'clinical', 'outcome', 'efficacy', 'evidence', 'patient'];
  return englishIndicators.some(w => lower.includes(w));
}

/**
 * Traduce texto de inglés (u otro) → español (para títulos, snippets y abstracts)
 * @param {string} text
 * @returns {Promise<string>}
 */
export async function translateToSpanish(text) {
  if (!text || typeof text !== 'string') return '';
  const clean = text.trim();
  if (!clean) return '';

  // Si ya está en español, evitar llamada innecesaria
  if (isAlreadySpanish(clean)) return clean;

  const cacheKey = `es:${clean}`;
  if (CACHE.has(cacheKey)) return CACHE.get(cacheKey);

  let result = clean;

  // 1. Intentar con Google Translate (motor principal)
  try {
    result = await translateGoogle(clean, 'auto', 'es');
  } catch (errGoogle) {
    // 2. Respaldo MyMemory si Google falla
    try {
      result = await translateMyMemory(clean, 'en|es');
    } catch {
      result = clean; // Fallback al original en caso extremo
    }
  }

  if (result && result !== clean) {
    CACHE.set(cacheKey, result);
    persistCache();
  }

  return result;
}

/**
 * Traduce de español → inglés (para queries de búsqueda en OpenAlex)
 * @param {string} text
 * @returns {Promise<string>}
 */
export async function translateToEnglish(text) {
  if (!text || typeof text !== 'string') return '';
  const clean = text.trim();
  if (!clean) return '';

  // Si ya es inglés y no tiene tildes ni eñes, devolver directo
  if (isAlreadyEnglish(clean)) return clean;

  const cacheKey = `en:${clean}`;
  if (CACHE.has(cacheKey)) return CACHE.get(cacheKey);

  let result = clean;

  try {
    result = await translateGoogle(clean, 'auto', 'en');
  } catch (errGoogle) {
    try {
      result = await translateMyMemory(clean, 'es|en');
    } catch {
      result = clean;
    }
  }

  if (result && result !== clean) {
    CACHE.set(cacheKey, result);
    persistCache();
  }

  return result;
}

/**
 * Traduce en paralelo un array de textos con límite de concurrencia y tolerancia a fallos.
 * @param {string[]} texts
 * @returns {Promise<string[]>}
 */
export async function translateBatch(texts) {
  if (!Array.isArray(texts) || texts.length === 0) return [];

  const results = new Array(texts.length).fill('');
  const concurrency = 4;

  for (let i = 0; i < texts.length; i += concurrency) {
    const batch = texts.slice(i, i + concurrency);
    const batchPromises = batch.map((txt) => {
      if (!txt) return Promise.resolve('');
      return translateToSpanish(txt).catch(() => txt);
    });

    const batchResults = await Promise.all(batchPromises);
    batchResults.forEach((r, j) => {
      results[i + j] = r;
    });

    // Pequeño respiro de 30ms entre lotes para no saturar conexiones simultáneas
    if (i + concurrency < texts.length) {
      await new Promise(r => setTimeout(r, 30));
    }
  }

  return results;
}
