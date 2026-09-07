/**
 * generate_stories.js — Automatización diaria de historias para Psi-hub
 * 
 * 1. Consulta OpenAlex para obtener los papers más recientes y relevantes de 8 tópicos.
 * 2. Envía un prompt ultraligero a Groq (openai/gpt-oss-120b con fallback a openai/gpt-oss-20b)
 *    para seleccionar y sintetizar la placa snackable.
 * 3. Cuenta con control automático de rate-limits (429), reintentos con backoff y preservación de historias previas.
 * 4. Escribe el resultado en data/stories.json (y www/data/stories.json si existe).
 */

const fs = require('fs');
const path = require('path');

const GROQ_API_KEY = process.env.GROQ_API_KEY || '';
const PRIMARY_MODEL = process.env.GROQ_MODEL || 'openai/gpt-oss-120b';
const FALLBACK_MODEL = 'openai/gpt-oss-20b';

// 8 Tópicos definidos para Psi-hub
const TOPICS = [
  {
    id: 'neurociencia',
    name: 'Neurociencia',
    color: '#38bdf8',
    icon: 'ph-brain',
    query: 'neuroscience neuroplasticity synaptogenesis brain functional connectivity'
  },
  {
    id: 'tcc',
    name: 'TCC & Conductual',
    color: '#f59e0b',
    icon: 'ph-lightning',
    query: 'cognitive behavioral therapy CBT acceptance commitment therapy behavioral activation'
  },
  {
    id: 'clinica',
    name: 'Clínica & Psicoterapia',
    color: '#10b981',
    icon: 'ph-heartbeat',
    query: 'psychotherapy clinical trial therapeutic alliance psychological intervention efficacy'
  },
  {
    id: 'psicoanalisis',
    name: 'Psicoanálisis & Dinámica',
    color: '#ec4899',
    icon: 'ph-spiral',
    query: 'psychoanalysis psychodynamic attachment theory transference defense mechanisms unconscious'
  },
  {
    id: 'social',
    name: 'Psicología Social',
    color: '#8b5cf6',
    icon: 'ph-users-three',
    query: 'social psychology cognitive bias heuristics decision making collective behavior'
  },
  {
    id: 'neuropsicologia',
    name: 'Neuropsicología',
    color: '#06b6d4',
    icon: 'ph-eye',
    query: 'neuropsychology executive functions working memory cognitive aging neurodevelopment'
  },
  {
    id: 'desarrollo',
    name: 'Desarrollo & Infantil',
    color: '#f43f5e',
    icon: 'ph-baby',
    query: 'developmental psychology child development adolescence attachment parenting autism'
  },
  {
    id: 'organizacional',
    name: 'Organizacional',
    color: '#84cc16',
    icon: 'ph-briefcase',
    query: 'organizational psychology occupational health leadership burnout employee well-being'
  }
];

/**
 * Reconstruye el abstract a partir del índice invertido de OpenAlex
 */
function reconstructAbstract(invertedIndex) {
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
 * Busca hasta 3 papers destacados con abstract en OpenAlex (optimizado para no sobrecargar tokens)
 */
async function fetchTopCandidatePapers(topicQuery) {
  const currentYear = new Date().getFullYear();
  const fromYear = currentYear - 1; // Último año y año corriente
  
  const url = new URL('https://api.openalex.org/works');
  url.searchParams.set('search', topicQuery);
  url.searchParams.set('filter', `is_oa:true,from_publication_date:${fromYear}-01-01`);
  url.searchParams.set('sort', 'cited_by_count:desc');
  url.searchParams.set('per-page', '8');
  url.searchParams.set('mailto', 'psyhub.app.research@gmail.com');

  let res;
  let retries = 3;
  while (retries > 0) {
    res = await fetch(url.toString(), {
      headers: { 'User-Agent': 'PsiHubDailyStoriesBot/1.0 (psyhub.app.research@gmail.com)' }
    });
    if (res.status === 429) {
      console.warn(`    ⚠️ OpenAlex 429. Esperando ${4 - retries}x segundos...`);
      await new Promise(r => setTimeout(r, (4 - retries) * 3000));
      retries--;
    } else if (!res.ok) {
      throw new Error(`OpenAlex error ${res.status}: ${res.statusText}`);
    } else {
      break;
    }
  }

  if (!res || !res.ok) {
    throw new Error('OpenAlex error: Excedido el límite de reintentos 429.');
  }

  const data = await res.json();
  const validPapers = [];

  for (const item of (data.results || [])) {
    const abstract = reconstructAbstract(item.abstract_inverted_index);
    if (abstract && abstract.length > 80 && item.title) {
      validPapers.push({
        id: item.id,
        title: item.title,
        abstract: abstract.substring(0, 240), // Breve para consumir mínimos tokens por minuto (<300 tokens por prompt)
        journal: item.primary_location?.source?.display_name || 'Journal científico',
        year: item.publication_year || currentYear,
        citations: item.cited_by_count || 0,
        doi: item.doi ? item.doi.replace('https://doi.org/', '') : '',
        url: item.doi || item.primary_location?.landing_page_url || item.id,
        pdfUrl: item.open_access?.oa_url || item.primary_location?.pdf_url || item.doi || item.id
      });
    }
    // 3 candidatos son ideales para tener diversidad y no saturar el límite TPM de Groq
    if (validPapers.length >= 3) break;
  }

  return validPapers;
}

/**
 * Realiza la petición a Groq con reintentos automáticos y backoff en caso de 429
 */
async function callGroqWithRetry(messages, maxRetries = 3) {
  let modelToUse = PRIMARY_MODEL;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    const response = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${GROQ_API_KEY}`
      },
      body: JSON.stringify({
        model: modelToUse,
        messages,
        response_format: { type: 'json_object' },
        temperature: 0.3
      })
    });

    if (response.status === 429) {
      const errJson = await response.json().catch(() => ({}));
      const msg = errJson.error?.message || '';
      // Extraer segundos sugeridos por Groq (ej: "Please try again in 15.06s")
      const waitMatch = msg.match(/in (\d+(\.\d+)?)s/i);
      const waitSec = waitMatch ? Math.ceil(parseFloat(waitMatch[1])) + 2 : 15;
      
      console.warn(`    ⚠️ [Groq 429 TPM Limit] Esperando ${waitSec}s para liberar cupo (intento ${attempt}/${maxRetries})...`);
      await new Promise(r => setTimeout(r, waitSec * 1000));
      
      if (attempt === 2 && modelToUse !== FALLBACK_MODEL) {
        console.warn(`    🔄 Conmutando a modelo fallback ${FALLBACK_MODEL}...`);
        modelToUse = FALLBACK_MODEL;
      }
      continue;
    }

    if (!response.ok) {
      const errText = await response.text();
      if (attempt < maxRetries && modelToUse !== FALLBACK_MODEL) {
        console.warn(`    ⚠️ Error con ${modelToUse}: ${errText}. Reintentando con ${FALLBACK_MODEL}...`);
        modelToUse = FALLBACK_MODEL;
        await new Promise(r => setTimeout(r, 2000));
        continue;
      }
      throw new Error(`Groq API error (${response.status}): ${errText}`);
    }

    const resultData = await response.json();
    return resultData.choices[0]?.message?.content || '{}';
  }

  throw new Error('Groq API: Se superó el límite de reintentos tras 429.');
}

/**
 * Consulta a Groq para elegir el mejor paper y armar la placa "snackable"
 */
async function summarizeWithGroq(topic, papers, existingStory = null) {
  if (!GROQ_API_KEY) {
    console.warn(`[!] No se proporcionó GROQ_API_KEY. Conservando historia de respaldo para "${topic.name}".`);
    if (existingStory) return existingStory;
    throw new Error('No hay API Key ni historia previa para respaldar.');
  }

  const promptPapers = papers.map((p, idx) => `
[CANDIDATO ${idx + 1}]
Título: ${p.title}
Revista: ${p.journal} (${p.year})
Abstract: ${p.abstract}
`).join('\n---\n');

  const systemPrompt = `Eres un divulgador de élite en psicología científica, neurociencias y psicoterapia.
Tu misión es seleccionar de una lista de papers el hallazgo MÁS sorprendente o clínicamente relevante para profesionales de la salud mental.

Debes responder ÚNICAMENTE en formato JSON con la siguiente estructura estricta:
{
  "selectedCandidateIndex": 1,
  "hook": "Pregunta intrigante en español (máximo 12 palabras, ej: '¿El café antes o después de estudiar?')",
  "headline": "Titular del hallazgo en una sola frase potente en español (máx 15 palabras)",
  "finding": "Explicación del hallazgo en 2 oraciones claras y atractivas en español (máx 45 palabras)",
  "takeaway": "Por qué importa este dato o su aplicación clínica en español (máx 40 palabras)",
  "tags": ["3 etiquetas cortas en español"]
}

Reglas:
- Escribe SIEMPRE en español neutro, riguroso y atractivo. NUNCA dejes texto en inglés.
- Devuelve SOLO el objeto JSON.`;

  const userPrompt = `Tópico: "${topic.name}".
Aquí tienes los candidatos:\n${promptPapers}\n\nSelecciona el mejor y genera el JSON en español.`;

  const rawContent = await callGroqWithRetry([
    { role: 'system', content: systemPrompt },
    { role: 'user', content: userPrompt }
  ]);

  const parsed = JSON.parse(rawContent);
  const idx = Math.max(0, Math.min(papers.length - 1, (parsed.selectedCandidateIndex || 1) - 1));
  const chosenPaper = papers[idx];

  return {
    id: `${topic.id}-${Date.now().toString(36)}`,
    topicId: topic.id,
    topicName: topic.name,
    topicColor: topic.color,
    topicIcon: topic.icon,
    hook: parsed.hook || (existingStory ? existingStory.hook : `¿Qué revela lo último en ${topic.name}?`),
    headline: parsed.headline || (existingStory ? existingStory.headline : chosenPaper.title),
    finding: parsed.finding || (existingStory ? existingStory.finding : chosenPaper.abstract),
    takeaway: parsed.takeaway || (existingStory ? existingStory.takeaway : 'Aporta evidencia significativa para la práctica clínica.'),
    paperTitle: chosenPaper.title,
    journal: chosenPaper.journal,
    year: chosenPaper.year,
    citations: chosenPaper.citations !== undefined ? chosenPaper.citations : (existingStory?.citations || 0),
    doi: chosenPaper.doi,
    url: chosenPaper.url,
    pdfUrl: chosenPaper.pdfUrl,
    tags: Array.isArray(parsed.tags) && parsed.tags.length ? parsed.tags : (existingStory ? existingStory.tags : [topic.name, 'Investigación'])
  };
}

async function main() {
  console.log('🚀 Iniciando curaduría diaria de Historias para Psi-hub...');
  console.log(`📅 Fecha: ${new Date().toISOString()}`);
  console.log(`🤖 Modelo Groq Principal: ${PRIMARY_MODEL} (Fallback: ${FALLBACK_MODEL})`);
  console.log(`🔑 Groq API Key: ${GROQ_API_KEY ? 'Presente ✓' : 'No provista (se mantendrán historias previas)'}\n`);

  const projectRoot = path.resolve(__dirname, '..');
  const targetPath = path.join(projectRoot, 'data', 'stories.json');
  const wwwTargetPath = path.join(projectRoot, 'www', 'data', 'stories.json');

  // Cargar historias previas para garantizar que si un tópico falla temporalmente, nunca se pierda
  const existingStoriesMap = new Map();
  try {
    if (fs.existsSync(targetPath)) {
      const currentData = JSON.parse(fs.readFileSync(targetPath, 'utf8'));
      if (Array.isArray(currentData.stories)) {
        currentData.stories.forEach(s => existingStoriesMap.set(s.topicId, s));
      }
    }
  } catch (errRead) {
    console.warn('  ⚠️ No se pudieron leer historias previas para respaldo:', errRead.message);
  }

  const stories = [];

  for (const topic of TOPICS) {
    const existingStory = existingStoriesMap.get(topic.id) || null;
    console.log(`🔍 [${topic.name}] Buscando papers en OpenAlex...`);
    try {
      const papers = await fetchTopCandidatePapers(topic.query);
      if (papers.length === 0) {
        console.warn(`  ⚠️ No se encontraron papers para "${topic.name}". Conservando respaldo previo.`);
        if (existingStory) stories.push(existingStory);
        await new Promise(r => setTimeout(r, 2000));
        continue;
      }

      console.log(`  ✓ Encontrados ${papers.length} candidatos. Analizando con Groq...`);
      const story = await summarizeWithGroq(topic, papers, existingStory);
      stories.push(story);
      console.log(`  ✨ Historia generada: "${story.hook}"`);
      
      // Pausa estratégica de 20s entre tópicos para respetar TPM (Tokens Per Minute) en la cuota gratuita de Groq
      await new Promise(r => setTimeout(r, 20000));
    } catch (err) {
      console.error(`  ❌ Error en tópico "${topic.name}":`, err.message);
      if (existingStory) {
        console.log(`  🛡️ Conservando historia de alta calidad previa para "${topic.name}".`);
        stories.push(existingStory);
      }
      await new Promise(r => setTimeout(r, 3000));
    }
  }

  if (stories.length === 0) {
    console.error('❌ No se pudo generar ni conservar ninguna historia. Abortando.');
    process.exit(1);
  }

  const outputData = {
    updatedAt: new Date().toISOString(),
    version: '1.0',
    stories
  };

  // Guardar en data/stories.json
  fs.mkdirSync(path.dirname(targetPath), { recursive: true });
  fs.writeFileSync(targetPath, JSON.stringify(outputData, null, 2), 'utf8');
  console.log(`\n💾 Guardado exitosamente (${stories.length} historias) en: ${targetPath}`);

  // Copiar a www/ si existe
  if (fs.existsSync(path.join(projectRoot, 'www'))) {
    fs.mkdirSync(path.dirname(wwwTargetPath), { recursive: true });
    fs.writeFileSync(wwwTargetPath, JSON.stringify(outputData, null, 2), 'utf8');
    console.log(`💾 Copiado a www: ${wwwTargetPath}`);
  }

  console.log('\n🎉 ¡Curaduría diaria completada con éxito!');
}

main().catch(err => {
  console.error('Fatal error en automatización:', err);
  process.exit(1);
});
