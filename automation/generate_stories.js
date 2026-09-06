/**
 * generate_stories.js — Automatización diaria de historias para PsyHub
 * 
 * 1. Consulta OpenAlex para obtener los 10 papers más recientes y relevantes de 6 tópicos de psicología/neurociencias.
 * 2. Envía los abstracts a la API de Groq (Llama 3.3 70B) para que seleccione el más impactante y lo sintetice en formato "snackable".
 * 3. Escribe el resultado en data/stories.json.
 * 
 * Uso local:
 *   $env:GROQ_API_KEY="tu-api-key"
 *   node automation/generate_stories.js
 */

const fs = require('fs');
const path = require('path');

const GROQ_API_KEY = process.env.GROQ_API_KEY || '';
const GROQ_MODEL = 'llama3-70b-8192'; // Modelo gratuito, ultra rápido y de alta capacidad en Groq

// 6 Tópicos definidos para PsyHub
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
 * Busca hasta 10 papers recientes con abstract en OpenAlex
 */
async function fetchTopCandidatePapers(topicQuery) {
  const currentYear = new Date().getFullYear();
  const fromYear = currentYear - 1; // Último año y año corriente
  
  const url = new URL('https://api.openalex.org/works');
  url.searchParams.set('search', topicQuery);
  url.searchParams.set('filter', `is_oa:true,from_publication_date:${fromYear}-01-01`);
  url.searchParams.set('sort', 'cited_by_count:desc');
  url.searchParams.set('per-page', '15');
  url.searchParams.set('mailto', 'psyhub.app.research@gmail.com');

  const res = await fetch(url.toString(), {
    headers: { 'User-Agent': 'PsyHubDailyStoriesBot/1.0' }
  });

  if (!res.ok) {
    throw new Error(`OpenAlex error ${res.status}: ${res.statusText}`);
  }

  const data = await res.json();
  const validPapers = [];

  for (const item of (data.results || [])) {
    const abstract = reconstructAbstract(item.abstract_inverted_index);
    if (abstract && abstract.length > 120 && item.title) {
      validPapers.push({
        id: item.id,
        title: item.title,
        abstract: abstract.substring(0, 800), // Primeras líneas sustanciales
        journal: item.primary_location?.source?.display_name || 'Journal científico',
        year: item.publication_year || currentYear,
        doi: item.doi ? item.doi.replace('https://doi.org/', '') : '',
        url: item.doi || item.primary_location?.landing_page_url || item.id,
        pdfUrl: item.open_access?.oa_url || item.primary_location?.pdf_url || item.doi || item.id
      });
    }
    if (validPapers.length >= 10) break;
  }

  return validPapers;
}

/**
 * Consulta a Groq para elegir el mejor paper y armar la placa "snackable"
 */
async function summarizeWithGroq(topic, papers) {
  if (!GROQ_API_KEY) {
    console.warn(`[!] No se proporcionó GROQ_API_KEY. Usando fallback de selección directa para "${topic.name}".`);
    const p = papers[0];
    return {
      id: `${topic.id}-${Date.now()}`,
      topicId: topic.id,
      topicName: topic.name,
      topicColor: topic.color,
      topicIcon: topic.icon,
      hook: `¿Qué nos enseña la investigación reciente en ${topic.name}?`,
      headline: p.title,
      finding: p.abstract.substring(0, 180) + '...',
      takeaway: 'La evidencia actual resalta la relevancia de este mecanismo en la práctica clínica y el estudio cognitivo.',
      paperTitle: p.title,
      journal: p.journal,
      year: p.year,
      doi: p.doi,
      url: p.url,
      pdfUrl: p.pdfUrl,
      tags: [topic.name, 'Evidencia', 'Reciente']
    };
  }

  const promptPapers = papers.map((p, idx) => `
[CANDIDATO ${idx + 1}]
Título: ${p.title}
Revista: ${p.journal} (${p.year})
DOI: ${p.doi}
URL: ${p.url}
PDF: ${p.pdfUrl}
Abstract: ${p.abstract}
`).join('\n---\n');

  const systemPrompt = `Eres un divulgador de élite en psicología científica, neurociencias y psicoterapia.
Tu misión es seleccionar de una lista de papers el hallazgo MÁS sorprendente, contraintuitivo o clínicamente relevante para profesionales de la salud mental.

Debes responder ÚNICAMENTE en formato JSON con la siguiente estructura estricta:
{
  "selectedCandidateIndex": 1,
  "hook": "Una pregunta o afirmación breve e intrigante (máximo 12 palabras, ej: '¿El café antes o después de estudiar?')",
  "headline": "El titular del hallazgo en una sola frase potente (máx 15 palabras)",
  "finding": "Explicación del hallazgo en 2 o 3 oraciones claras, atractivas y sin jerga incomprensible (máx 50 palabras)",
  "takeaway": "Por qué importa este dato o cuál es su aplicación práctica/mecanismo (máx 45 palabras)",
  "tags": ["3 etiquetas cortas sobre el tema"]
}

Reglas:
- Escribe en español neutro, impecable y riguroso.
- No uses rodeos como "según este estudio". Ve directo al dato contundente.
- Devuelve SOLO el objeto JSON, sin comentarios ni backticks Markdown si es posible.`;

  const userPrompt = `Tópico: "${topic.name}".
Aquí tienes los candidatos:\n${promptPapers}\n\nSelecciona el mejor y genera el JSON.`;

  const response = await fetch('https://api.groq.com/openai/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${GROQ_API_KEY}`
    },
    body: JSON.stringify({
      model: GROQ_MODEL,
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userPrompt }
      ],
      response_format: { type: 'json_object' },
      temperature: 0.4
    })
  });

  if (!response.ok) {
    const errText = await response.text();
    throw new Error(`Groq API error (${response.status}): ${errText}`);
  }

  const resultData = await response.json();
  const rawContent = resultData.choices[0]?.message?.content || '{}';
  const parsed = JSON.parse(rawContent);

  const idx = Math.max(0, Math.min(papers.length - 1, (parsed.selectedCandidateIndex || 1) - 1));
  const chosenPaper = papers[idx];

  return {
    id: `${topic.id}-${Date.now().toString(36)}`,
    topicId: topic.id,
    topicName: topic.name,
    topicColor: topic.color,
    topicIcon: topic.icon,
    hook: parsed.hook || `¿Qué revela lo último en ${topic.name}?`,
    headline: parsed.headline || chosenPaper.title,
    finding: parsed.finding || chosenPaper.abstract.substring(0, 180),
    takeaway: parsed.takeaway || 'Aporta evidencia significativa para el campo.',
    paperTitle: chosenPaper.title,
    journal: chosenPaper.journal,
    year: chosenPaper.year,
    doi: chosenPaper.doi,
    url: chosenPaper.url,
    pdfUrl: chosenPaper.pdfUrl,
    tags: parsed.tags || [topic.name, 'Investigación']
  };
}

async function main() {
  console.log('🚀 Iniciando curaduría diaria de Historias para PsyHub...');
  console.log(`📅 Fecha: ${new Date().toISOString()}`);
  console.log(`🤖 Modelo Groq: ${GROQ_MODEL}`);
  console.log(`🔑 Groq API Key: ${GROQ_API_KEY ? 'Presente ✓' : 'No provista (usando fallback de prueba)'}\n`);

  const stories = [];

  for (const topic of TOPICS) {
    console.log(`🔍 [${topic.name}] Buscando papers en OpenAlex...`);
    try {
      const papers = await fetchTopCandidatePapers(topic.query);
      if (papers.length === 0) {
        console.warn(`  ⚠️ No se encontraron papers para "${topic.name}". Saltando.`);
        continue;
      }
      console.log(`  ✓ Encontrados ${papers.length} papers. Analizando con Groq...`);
      const story = await summarizeWithGroq(topic, papers);
      stories.push(story);
      console.log(`  ✨ Historia generada: "${story.hook}"`);
    } catch (err) {
      console.error(`  ❌ Error procesando tópico "${topic.name}":`, err.message);
    }
  }

  if (stories.length === 0) {
    console.error('❌ No se pudo generar ninguna historia. Abortando.');
    process.exit(1);
  }

  const outputData = {
    updatedAt: new Date().toISOString(),
    version: '1.0',
    stories
  };

  const projectRoot = path.resolve(__dirname, '..');
  const targetPath = path.join(projectRoot, 'data', 'stories.json');
  const wwwTargetPath = path.join(projectRoot, 'www', 'data', 'stories.json');

  // Asegurar directorios
  fs.mkdirSync(path.dirname(targetPath), { recursive: true });
  fs.writeFileSync(targetPath, JSON.stringify(outputData, null, 2), 'utf8');
  console.log(`\n💾 Guardado exitosamente en: ${targetPath}`);

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
