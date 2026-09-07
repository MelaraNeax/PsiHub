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
 * Busca hasta 5 papers destacados con abstract en OpenAlex
 */
async function fetchTopCandidatePapers(topicQuery) {
  const currentYear = new Date().getFullYear();
  const fromYear = currentYear - 2; // Últimos 2-3 años para garantizar actualidad y rotación continua
  
  // Aleatorizar ordenamiento y página para explorar papers diversos en cada ejecución
  const sortOptions = ['cited_by_count:desc', 'relevance_score:desc', 'publication_date:desc'];
  const randomSort = sortOptions[Math.floor(Math.random() * sortOptions.length)];
  const randomPage = Math.floor(Math.random() * 3) + 1;

  const url = new URL('https://api.openalex.org/works');
  url.searchParams.set('search', topicQuery);
  url.searchParams.set('filter', `is_oa:true,from_publication_date:${fromYear}-01-01`);
  url.searchParams.set('sort', randomSort);
  url.searchParams.set('page', String(randomPage));
  url.searchParams.set('per-page', '25'); // Muestra representativa amplia para filtrar candidatos completos
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
      if (randomPage > 1) {
        url.searchParams.set('page', '1');
        url.searchParams.set('sort', 'cited_by_count:desc');
        res = await fetch(url.toString(), {
          headers: { 'User-Agent': 'PsiHubDailyStoriesBot/1.0 (psyhub.app.research@gmail.com)' }
        });
        if (res.ok) break;
      }
      throw new Error(`OpenAlex error ${res.status}: ${res.statusText}`);
    } else {
      break;
    }
  }

  if (!res || !res.ok) {
    throw new Error('OpenAlex error: Excedido el límite de reintentos 429.');
  }

  const data = await res.json();
  const validPool = [];

  for (const item of (data.results || [])) {
    const abstract = reconstructAbstract(item.abstract_inverted_index);
    if (abstract && abstract.length >= 180 && item.title) {
      const authors = (item.authorships || []).map(a => a.author?.display_name).filter(Boolean);
      validPool.push({
        id: item.id,
        title: item.title,
        abstract: abstract.substring(0, 1200), // Abstract completo para que la IA no trabaje con fragmentos cortados
        fullAbstract: abstract,
        authors,
        journal: item.primary_location?.source?.display_name || 'Journal científico',
        year: item.publication_year || currentYear,
        citations: item.cited_by_count || 0,
        doi: item.doi ? item.doi.replace('https://doi.org/', '') : '',
        url: item.doi || item.primary_location?.landing_page_url || item.id,
        pdfUrl: item.open_access?.oa_url || item.primary_location?.pdf_url || item.doi || item.id
      });
    }
  }

  // Barajar al azar para no elegir siempre los mismos candidatos
  for (let i = validPool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [validPool[i], validPool[j]] = [validPool[j], validPool[i]];
  }

  // Tomar 5 candidatos completos para que la IA elija el mejor
  const selected5 = validPool.slice(0, 5);
  return selected5.length > 0 ? selected5 : validPool.slice(0, 3);
}

/**
 * Extrae y parsea un objeto JSON de una cadena de texto, tolerando bloques markdown o texto extra
 */
function extractJson(text) {
  if (!text || typeof text !== 'string') return null;
  const clean = text.replace(/```(?:json)?\s*/gi, '').replace(/```\s*$/g, '').trim();
  try {
    return JSON.parse(clean);
  } catch {}
  const match = clean.match(/\{[\s\S]*\}/);
  if (match) {
    try {
      return JSON.parse(match[0]);
    } catch {}
  }
  return null;
}

/**
 * Realiza la petición a Groq con cascada de modelos, max_tokens ampliado y reintentos automáticos
 */
async function callGroqWithRetry(messages) {
  const models = [
    process.env.GROQ_MODEL,
    PRIMARY_MODEL,
    FALLBACK_MODEL,
    'llama-3.3-70b-versatile',
    'llama-3.1-8b-instant',
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b'
  ].filter((m, idx, self) => m && self.indexOf(m) === idx);

  let lastError = null;

  for (const model of models) {
    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        console.log(`    🤖 Intentando análisis con ${model} (intento ${attempt})...`);
        const response = await fetch('https://api.groq.com/openai/v1/chat/completions', {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${GROQ_API_KEY}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            model,
            messages,
            temperature: 0.2, // Baja temperatura para máxima fidelidad fáctica y cero alucinaciones
            max_tokens: 2048, // Margen holgado para evitar que el JSON se corte a mitad de camino
            response_format: { type: 'json_object' }
          })
        });

        if (response.status === 429) {
          console.warn(`    ⚠️ Rate limit (429) con ${model}. Esperando 5s...`);
          await new Promise(r => setTimeout(r, 5000));
          continue;
        }

        if (!response.ok) {
          const errText = await response.text();
          // Si falló específicamente por json_validate_failed, intentar sin response_format estricto
          if (errText.includes('json_validate_failed')) {
            console.warn(`    ⚠️ ${model} devolvió json_validate_failed. Reintentando sin response_format forzado...`);
            const retryNoFormat = await fetch('https://api.groq.com/openai/v1/chat/completions', {
              method: 'POST',
              headers: {
                'Authorization': `Bearer ${GROQ_API_KEY}`,
                'Content-Type': 'application/json'
              },
              body: JSON.stringify({
                model,
                messages,
                temperature: 0.2,
                max_tokens: 2048
              })
            });
            if (retryNoFormat.ok) {
              const resJson = await retryNoFormat.json();
              const content = resJson.choices[0]?.message?.content;
              if (content && extractJson(content)) {
                return content;
              }
            }
          }
          throw new Error(`Groq API error (${response.status}) [${model}]: ${errText}`);
        }

        const resultData = await response.json();
        const content = resultData.choices[0]?.message?.content;
        if (content) {
          const parsed = extractJson(content);
          if (parsed) return content;
        }
      } catch (err) {
        lastError = err;
        console.warn(`    ⚠️ Falló ${model}: ${err.message}`);
        await new Promise(r => setTimeout(r, 1500));
      }
    }
  }

  throw lastError || new Error('Se agotaron todos los modelos candidatos de Groq sin respuesta válida.');
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
Abstract Completo: ${p.abstract}
`).join('\n---\n');

  const systemPrompt = `Eres un divulgador de élite y psicólogo científico especializado en neurociencias y psicoterapia basada en evidencia.
Tu misión es seleccionar de una lista de 5 papers candidatos el hallazgo MÁS riguroso, sorprendente y de mayor impacto clínico o conceptual.

REGLAS CRÍTICAS DE FIDELIDAD DE DATOS (CERO ALUCINACIONES):
- NUNCA inventes, aproximes ni extrapoles números, porcentajes o tamaños de muestra.
- Solo menciona cifras (porcentajes, N de muestra, años) si aparecen EXPLÍCITAMENTE en el texto del abstract proporcionado.
- Si el abstract no menciona una cifra o porcentaje exacto, describe la tendencia cualitativa (ej. "aumentó significativamente", "se redujo tras la intervención") sin inventar estadísticas ni porcentajes ficticios.
- Fidelidad estricta al contenido: describe con exactitud lo que los investigadores encontraron, sin exagerar efectos ni atribuir causalidad a simples correlaciones.

REGLAS DE TERMINOLOGÍA Y TRADUCCIÓN ACADÉMICA EN PSICOLOGÍA:
- Utiliza la terminología técnica aceptada internacionalmente en la literatura psicológica hispanohablante.
- NUNCA traduzcas "burnout" como "quemado" ni "estar quemado"; usa "burnout" o "desgaste ocupacional".
- Conserva términos técnicos estándar: "mindfulness" (o "atención plena"), "insight", "coping" (o "afrontamiento"), "priming", "arousal", "red neuronal por defecto" (default mode network), "ensayo controlado aleatorizado" (RCT), "alianza terapéutica", etc.
- Escribe SIEMPRE en español neutro, riguroso, elegante y con rigor científico. NUNCA dejes oraciones en inglés sin traducir.

IMPORTANTE: Debes responder ÚNICAMENTE en formato JSON con la siguiente estructura estricta, comenzando con { y terminando con }. No incluyas bloques de código markdown como \`\`\`json ni texto fuera del JSON:
{
  "selectedCandidateIndex": 1,
  "hook": "Pregunta intrigante en español (máximo 12 palabras, ej: '¿El café antes o después de estudiar?')",
  "headline": "Titular del hallazgo en una sola frase potente y veraz en español (máx 15 palabras)",
  "finding": "Explicación del hallazgo en 2 oraciones claras, fieles al texto y atractivas (máx 50 palabras)",
  "takeaway": "Por qué importa este dato para la práctica clínica o comprensión psicológica (máx 45 palabras)",
  "tags": ["3 etiquetas conceptuales en español"]
}`;

  const userPrompt = `Tópico: "${topic.name}".
Aquí tienes los 5 candidatos completos:\n${promptPapers}\n\nSelecciona el mejor candidato y responde EXCLUSIVAMENTE con el objeto JSON solicitado en español.`;

  const rawContent = await callGroqWithRetry([
    { role: 'system', content: systemPrompt },
    { role: 'user', content: userPrompt }
  ]);

  const parsed = extractJson(rawContent);
  if (!parsed) {
    throw new Error(`No se pudo parsear el JSON generado por Groq: ${rawContent.slice(0, 150)}`);
  }
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
    originalAbstract: chosenPaper.fullAbstract || chosenPaper.abstract || '',
    abstract: chosenPaper.fullAbstract || chosenPaper.abstract || '',
    authors: chosenPaper.authors || [],
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
