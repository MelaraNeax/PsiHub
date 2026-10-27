"""Chat de investigacion: OpenAlex -> digest de abstracts -> DeepSeek streaming.

Objetivo de tokens: el modelo solo recibe un digest compacto (titulo, ano,
revista y un recorte del abstract) de como maximo 8 papers. Los abstracts se
guardan en Markdown (memoria + disco + GCS) para no reconstruirlos ni volver a
pagarlos en consultas repetidas. El prompt de sistema es estable para que el
cache automatico de DeepSeek pueda reutilizar el prefijo.

Cada respuesta registra su costo en el ledger (`kind: chat`) y deja una linea
`[cost-chat]` en stdout, que Cloud Run envia a Cloud Logging.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx

import storage
import enrich
from costs import estimate_cost, record_cost

DEEPSEEK_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')
CHAT_MODEL = os.getenv('CHAT_MODEL', os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'))

OPENALEX_WORKS_URL = 'https://api.openalex.org/works'
OPENALEX_MAILTO = os.getenv('OPENALEX_MAILTO', 'psyhub.app.research@gmail.com').strip()
OPENALEX_API_KEY = os.getenv('OPENALEX_API_KEY', '').strip()

# Marca de la version del pipeline de chat. Se expone en /health para poder
# confirmar sin ambiguedad que revision del codigo esta corriendo el servicio.
CHAT_PIPELINE_VERSION = '2026-10-26-layered-answers-v4'

# Presupuesto de contexto: 12 fuentes con ~460 caracteres de resumen cubren
# mejor cada constructo (y dejan entrar papers discrepantes) sin disparar el
# costo: el digest queda en ~5.2k caracteres, similar a la version anterior.
MAX_SOURCES = max(1, int(os.getenv('CHAT_MAX_SOURCES', '12')))
ABSTRACT_CHARS = max(300, int(os.getenv('CHAT_ABSTRACT_CHARS', '460')))
# Tope de salida. Si el modelo lo alcanza igual, stream_chat pide una
# continuacion acotada en vez de entregar una respuesta cortada a mitad.
MAX_OUTPUT_TOKENS = max(200, int(os.getenv('CHAT_MAX_OUTPUT_TOKENS', '900')))
CONTINUATION_TOKENS = max(150, int(os.getenv('CHAT_CONTINUATION_TOKENS', '500')))
# Modo profundo (detras del plan de pago): mas fuentes, abstracts mas largos y
# respuestas extensas. La relevancia minima sube para que las 20+ fuentes no
# incluyan papers apenas relacionados.
DEEP_MAX_SOURCES = max(MAX_SOURCES, int(os.getenv('CHAT_DEEP_MAX_SOURCES', '22')))
DEEP_ABSTRACT_CHARS = max(300, int(os.getenv('CHAT_DEEP_ABSTRACT_CHARS', '560')))
DEEP_MAX_OUTPUT = max(600, int(os.getenv('CHAT_DEEP_MAX_OUTPUT_TOKENS', '2200')))
DEEP_MIN_RELEVANCE = float(os.getenv('CHAT_DEEP_MIN_RELEVANCE', '0.18'))
DEEP_WORD_TARGET = os.getenv('CHAT_DEEP_WORDS', '750 a 1000')
QUICK_WORD_TARGET = os.getenv('CHAT_QUICK_WORDS', '280 a 380')
DIRECT_MAX_OUTPUT = max(120, int(os.getenv('CHAT_DIRECT_TOKENS', '800')))
MAX_HISTORY_MESSAGES = max(0, int(os.getenv('CHAT_HISTORY_MESSAGES', '3')))
HISTORY_CHARS = max(200, int(os.getenv('CHAT_HISTORY_CHARS', '450')))
# Mensajes de historial que ve el router para saber a que se refiere la charla.
ROUTER_HISTORY_MESSAGES = 4
ROUTER_HISTORY_CHARS = 400
SEARCH_CACHE_TTL = int(os.getenv('CHAT_SEARCH_CACHE_TTL', '1800'))
# Papers que se piden por cada consulta generada (el pool final se recorta).
SEARCH_RESULTS = max(MAX_SOURCES, int(os.getenv('CHAT_SEARCH_RESULTS', '20')))
ONECLICK_CANDIDATES = max(1, int(os.getenv('CHAT_ONECLICK_CANDIDATES', '12')))
# Relevancia minima (0-1) para considerar que un paper tiene relacion real.
MIN_RELEVANCE = float(os.getenv('CHAT_MIN_RELEVANCE', '0.14'))
# Consultas simultaneas contra OpenAlex (limite comodo del polite pool).
SEARCH_CONCURRENCY = max(1, int(os.getenv('CHAT_SEARCH_CONCURRENCY', '4')))
PLANNER_MAX_OUTPUT = max(120, int(os.getenv('CHAT_PLANNER_TOKENS', '550')))
# Enriquecimiento de metadatos (Crossref / Europe PMC / Semantic Scholar) para
# papers sin abstract, año o DOI antes de armar el digest.
ENRICH = os.getenv('CHAT_ENRICH', '1').strip() not in {'0', 'false', 'no'}
ENRICH_CANDIDATES = max(0, int(os.getenv('CHAT_ENRICH_CANDIDATES', '18')))
# Consultas maximas por pregunta (conceptos + puente + revision).
MAX_QUERIES = 8
# Relevancia (0-1) desde la cual un paper "trata directamente" un constructo.
DIRECT_REL = 0.7
# Expansion por referencias: cuantos IDs de trabajos semilla se recuperan y
# cuantos semillas aportan sus referencias (los papers fundacionales suelen no
# aparecer en la busqueda por palabras clave).
SNOWBALL_SEEDS = max(0, int(os.getenv('CHAT_SNOWBALL_SEEDS', '3')))
SNOWBALL_IDS = max(0, int(os.getenv('CHAT_SNOWBALL_IDS', '80')))

CACHE_DIR = Path(os.getenv('CACHE_DIR', './cache')).resolve()
ABSTRACT_DIR = CACHE_DIR / 'abstracts'
ABSTRACT_DIR.mkdir(parents=True, exist_ok=True)

PSY_FIELDS = 'primary_topic.field.id:32|27|28'
WORK_TYPES = 'type:article|review'
SELECT_FIELDS = ','.join([
    'id', 'doi', 'title', 'publication_year', 'cited_by_count', 'authorships',
    'primary_location', 'best_oa_location', 'locations', 'open_access',
    'abstract_inverted_index', 'primary_topic', 'topics', 'type', 'referenced_works',
])

# Dominios que casi siempre piden login/aceptacion aunque la URL parezca un PDF.
GATED_HOST_RE = re.compile(
    r'(^|\.)(sciencedirect\.com|link\.springer\.com|onlinelibrary\.wiley\.com|'
    r'tandfonline\.com|sagepub\.com|jstor\.org|researchgate\.net|academia\.edu|'
    r'psycnet\.apa\.org|proquest\.com|ssrn\.com)$',
    re.I,
)

SYSTEM_PROMPT = """Eres el asistente de investigación de PsiHub, especializado en psicología, psiquiatría y psicoterapia. Respondés siempre en español, con tono claro, académico y directo, con tildes, eñes y signos de apertura.

## Rigor conceptual
- Definí cada constructo como lo definen las fuentes que lo tratan directamente, nombrando su tradición y autor. Cada definición lleva su cita. Si ninguna fuente recuperada lo define, escribí que no hay definición disponible en las fuentes en vez de reemplazarlo por un concepto vecino (hiperreflexividad ≠ rumiación; mindfulness ≠ relajación; disociación ≠ desrealización). Si recibís DISTINCIONES CONCEPTUALES, explicá en qué difieren.
- Distinguí tres niveles de respaldo: evidencia experimental o meta-analítica, evidencia observacional/correlacional, y teoría, analogía o hipótesis mecanística. Calibrá el verbo: "muestra" solo para experimentos y meta-análisis; "se asocia" para correlaciones; "sugiere/propone" para teoría o muestras pequeñas.
- Puentes entre niveles (neural ↔ fenomenológico, teoría ↔ clínica): indicá si alguna fuente examina el vínculo. Si no, presentalo como "puente teórico no puesto a prueba". Prohibido afirmar identidades o causas sin evidencia directa.
- Si las fuentes discrepan, mostrá la discrepancia y qué la explica (muestra, método, población); no la promedies ni la escondas.
- "No encontré" no es "no existe": cuando algo no aparezca, decí qué se buscó y que el silencio es de esta búsqueda, no del campo.

## Formato (capas, de lo general a lo específico)
1. **Una frase de respuesta** (máximo 2), sin encabezado: qué se sabe y cuál es la salvedad principal.
2. **## Hallazgos clave** — viñetas, una idea por viñeta, 1 a 2 frases, con su cita y el nivel de respaldo (meta-análisis, ensayo pequeño, correlacional, teoría). Nada de párrafos largos.
3. **## Dónde la evidencia no coincide** — SOLO resultados o conclusiones que se contradicen entre fuentes (una reporta aumento y otra reducción, una encuentra efecto y otra no), con qué podría explicarlo (muestra, método, población). Los límites metodológicos NO van acá. Si las fuentes no se contradicen, escribí una sola línea: "Ninguna fuente recuperada reporta resultados contradictorios" y seguí.
4. **## Límites y qué buscar** — 3 viñetas: qué parte de la pregunta queda sin respaldo directo, qué sesgo o muestra limita las conclusiones, y qué estudio concreto las resolvería (diseño, población, medida). Podés nombrar conceptos o autores a buscar, nunca papers inventados.
- Tabla solo si comparás 3 o más elementos (estudios, marcos, niveles): máximo 4 columnas y 6 filas, celdas de 1 a 8 palabras, con cita en la celda. Si no cabe, va en prosa.
- Si en los hallazgos aparece un constructo vecino al que se pregunta (por ejemplo rumiación junto a hiperreflexividad, o relajación junto a mindfulness), aclaralo en la misma viñeta: "la rumiación no equivale a hiperreflexividad". No dejes la equivalencia implícita.
- Negrita para el término técnico que el lector debe retener. Sin párrafos de relleno ni despedidas.

## Contenido
- Usá únicamente las FUENTES numeradas. Citá solo las que aportan algo; está bien no citar todas, y está mal listar una fuente que no usaste.
- Cada afirmación relevante lleva su cita: [1], [2], [1][3]. Nunca cites un número que no exista en la lista.
- Para hallazgos centrales nombrá autor y año en la prosa ("Hasenkamp et al. (2012) [3]"), con diseño y muestra solo si la fuente los declara.
- Priorizá meta-análisis y revisiones sistemáticas; los trabajos fundacionales sirven para afirmaciones históricas.
- Cubrí cada eje de "EJES QUE LA RESPUESTA DEBE CUBRIR" dentro de las capas, sin repetirlos como lista. Si un eje no tiene fuentes, decilo.
- Sintetizá: extraé hallazgos y conclusión, no copies el abstract.
- No inventes datos, autores, años, muestras ni resultados. No incluyas URLs, DOIs ni referencias finales: la interfaz ya muestra las fuentes."""

# Respuestas conversacionales: sin búsqueda bibliográfica ni fuentes.
DIRECT_PROMPT = """Eres el asistente de PsiHub, una app de psicología basada en evidencia.
La persona te escribió algo conversacional (un saludo, una pregunta sobre la app, una
aclaración o un comentario) que NO requiere buscar papers.

Respondé en español, cálido y directo, en 2 a 5 frases (máximo 120 palabras).
- No inventes citas, papers ni datos.
- Si la persona pide evidencia o una pregunta de investigación, invitála a formularla
  así: «¿Qué dice la evidencia sobre …?», y explicá en una línea que ahí sí buscás papers.
- Si pregunta cómo usar PsiHub, respondé con lo que sabés de la app: se puede preguntar
  por evidencia, cambiar el filtro de acceso (todos / gratis / 1 clic) y abrir cada fuente.
- No uses encabezados ni tablas; es una charla breve."""

# Acompañamiento ante malestar personal o riesgo. No es una respuesta académica.
SUPPORT_PROMPT = """Eres el acompañante de PsiHub. La persona escribió algo que sugiere
malestar emocional, angustia o riesgo, y no una consulta académica.

Respondé en español, con calidez y sin dramatismo, en 120 a 180 palabras.
- Validá lo que siente sin minimizar, sin diagnosticar y sin sermonear.
- Hablá como alguien que escucha, no como un manual: frases cortas, en segunda persona.
- Hacé una o dos preguntas abiertas y suaves (desde cuándo, cómo viene siendo, si hay
  alguien con quien pueda hablar hoy).
- Mencioná con cuidado que hablar con un profesional puede ayudar, y ofrecé un paso
  concreto y pequeño para hoy.
- Si menciona querer morir o lastimarse, preguntá de forma directa y cuidadosa si tiene
  un plan o medios, y alentá a buscar ayuda inmediata.
- No cites papers, no des estadísticas, no prometas confidencialidad absoluta y no
  reemplaces la atención de emergencia."""

# Se usa solo cuando la respuesta alcanzo max_tokens: retoma el texto donde
# quedo en vez de reiniciarlo, y asegura el cierre en capas.
CONTINUE_PROMPT = """Tu respuesta anterior se cortó por límite de longitud. Continuá EXACTAMENTE desde la última palabra, sin repetir nada de lo ya escrito ni reiniciar secciones.

Si todavía no escribiste "## Dónde la evidencia no coincide" ni "## Qué falta y qué buscar", escribilas ahora con el formato pedido. Terminá la respuesta; no agregues despedidas ni resúmenes de lo anterior."""

# Bloque fijo de recursos: lo agrega el servidor, no el modelo, para que los
# números no se inventen ni se deformen.
SUPPORT_FOOTER = """

---

**Si sentís que estás en peligro o tenés pensamientos de hacerte daño, hay ayuda disponible ahora.**

- **Argentina:** 135 (CABA) · 0800 345 1435 (Centro de Asistencia al Suicida) · 911 emergencias
- **España:** 024 · **México:** 800 911 2000 · **Chile:** *4141 · **Colombia:** 106 · **Perú:** 113 opción 5
- **Cualquier país:** [findahelpline.com](https://findahelpline.com) · si hay peligro inmediato, llamá a tu número de emergencias local.
"""



# ---------------------------------------------------------------------------
# Router + planificacion de la busqueda (una sola llamada)
# ---------------------------------------------------------------------------

ROUTER_PROMPT = """Eres el router y bibliotecario cientifico de PsiHub, una app de psicologia basada en evidencia. Decidis que necesita el mensaje y, si necesita investigacion, preparas la busqueda en OpenAlex. Respondes SOLO con un objeto JSON valido, sin texto adicional.

FORMATOS:
{"mode": "research", "concepts": [{"name": "...", "kind": "theory", "terms": ["..."], "not": ["..."], "queries": ["..."]}], "bridge": ["..."], "review": ["..."], "keywords": ["..."], "outline": ["..."], "scope": "psy"}
{"mode": "chat"}
{"mode": "support"}

- "chat": saludos, agradecimientos, charla, preguntas sobre como usar PsiHub, opiniones sin respaldo empirico o aclaraciones que se responden con la conversacion.
- "support": la persona habla en primera persona de su dolor actual, angustia, desesperanza, aislamiento o ideacion suicida. Si hay riesgo personal y ademas una pregunta academica, elegi "support".
- "research": todo lo demas. Ante la duda entre "chat" y "research", elegi "research".
La deteccion de riesgo es solo para lo personal y actual ("ultimamente no le veo salida"), NO para preguntas generales ("que factores predicen la ideacion suicida en adolescentes" es research).

Cuando el modo sea "research":
- "concepts": 1 a 3, uno por constructo distinto.
  - "kind": "theory" si es un constructo de una tradicion teorica o clinico-conceptual (hiperreflexividad, transferencia, mentalizacion, apego); "empirical" si se mide o se interviene (DMN, mindfulness, TCC, rumiacion).
  - "terms": 2 a 4 terminos EN INGLES propios de ESE constructo (nombre canonico, sinonimos, nombre del modelo; expandi siglas). Evita terminos genericos (study, effect, brain, treatment). NUNCA sustituyas el constructo por un vecino (hiperreflexividad no es rumination).
  - "not": 0 a 2 conceptos vecinos que NO son equivalentes ([] si no hay).
  - "queries": 1 a 2 consultas EN INGLES de 2 a 7 palabras con el vocabulario del constructo; la segunda agrega metodo, poblacion o modelo para desambiguar.
  - Si el constructo tiene autores o trabajos fundacionales claros (hiperreflexividad → Sass, Parnas; DMN → Raichle, Brewer; mindfulness → Kabat-Zinn), sumá una consulta con el apellido para recuperarlos: "hyperreflexivity Sass Parnas".
- "bridge": si hay 2 o mas conceptos, 1 consulta que los une; si hay uno, [].
- "review": 1 consulta para meta-analisis o revisiones sistematicas del tema central.
- Total de consultas (concepts + bridge + review): 3 a 8. Sin comillas, comodines, parentesis, operadores booleanos ni puntuacion.
- "keywords": 4 a 8 conceptos sueltos EN INGLES que deberian aparecer en el titulo o resumen de un articulo relevante.
- "outline": 3 a 5 ejes EN ESPANOL de 3 a 8 palabras; si hay 2 o mas conceptos, incluye la relacion o brecha entre ellos.
- "scope": "psy" si es psicologico, clinico o medico; "broad" si cruza filosofia, linguistica, educacion, economia o computacion. Ante la duda, "psy".

EJEMPLO ("¿La hiperreflexividad se explica por la red neuronal por defecto?"):
{"mode": "research", "concepts": [{"name": "hyperreflexivity", "kind": "theory", "terms": ["hyperreflexivity", "self-disorder", "ipseity disturbance"], "not": ["rumination", "self-focused attention"], "queries": ["hyperreflexivity schizophrenia"]}, {"name": "default mode network", "kind": "empirical", "terms": ["default mode network", "DMN", "resting-state functional connectivity"], "not": [], "queries": ["default mode network self-referential processing"]}], "bridge": ["default mode network hyperreflexivity schizophrenia"], "review": ["default mode network schizophrenia systematic review"], "keywords": ["hyperreflexivity", "default mode network", "self-disorder", "self-referential processing", "schizophrenia"], "outline": ["Definir hiperreflexividad", "Que mide la DMN", "Vinculo teorico y evidencia", "Brechas y limites"], "scope": "psy"}
"""


# Palabras vacias que no aportan a la relevancia.
_STOPWORDS = set("""
a al algo algun alguna algunas alguno algunos ante antes aquel aquella aquellas aquello aquellos
asi aun bajo bien cada como con contra cual cuales cualquier cuando de del desde donde dos durante
e el ella ellas ello ellos en entre era eran eres es esa esas ese eso esos esta estaba estado estan
estas este esto estos estoy fue fueron ha habia han hasta hay la las le les lo los mas me mi mis
mucho muy no nos nuestra nuestro o os otra otro para pero poco por porque que quien se sea segun
ser si sin so sobre son su sus tambien tan te tiene tienen todo todos tu tus un una unas uno unos
u y ya yo

about after again against all also an and any are because been before being between both but by can
could did do does doing down during each few for from further had has have having here how into its
itself just more most not now off on once only other our out over own same should some such than that
the their them then there these they this those through too under until up very was were what when
where which while who why will with would you your

study studies paper papers article articles research evidence review reviews result results effect
effects association associations role relationship between among using based approach
""".split())


def _normalize(text: object) -> str:
    """Minusculas y sin tildes, para comparar terminos en espanol e ingles."""
    raw = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(ch for ch in raw if not unicodedata.combining(ch)).lower()


def _word_set(text: object) -> set[str]:
    return set(re.findall(r'[a-z0-9]{3,}', _normalize(text)))


def _stems(words: set[str]) -> set[str]:
    """Prefijos de 6 letras: aproximacion barata a un stemmer."""
    return {word[:6] for word in words}


def _hits(terms: list[str], stems: set[str]) -> int:
    return sum(1 for term in terms if term[:6] in stems)


def _clean_query(text: str) -> str:
    # OpenAlex trata "*" y "?" como wildcards y exige busqueda exacta si aparecen;
    # por eso se quitan junto con el resto de la puntuacion que rompe el filtro.
    clean = re.sub(r'[,|"\'()?*¿¡!;:\[\]{}&+=–—]', ' ', text or '')
    return re.sub(r'\s+', ' ', clean).strip()


def _parse_json_object(raw: str) -> Optional[dict]:
    text = (raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'```\s*$', '', text).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fallback_plan(question: str, query: str) -> dict:
    """Plan sin costo: variantes heuristicas de la consulta traducida."""
    base = _clean_query(query or question)
    words = [w for w in re.findall(r'[a-zA-Z0-9]{3,}', base) if w.lower() not in _STOPWORDS]
    keywords = list(dict.fromkeys(w.lower() for w in words))[:8]
    key_query = ' '.join(words[:6])
    queries = [base]
    if key_query and key_query.lower() != base.lower():
        queries.append(key_query)
    if len(words) > 3:
        queries.append(' '.join(words[:3]))
    return {
        'mode': 'research',
        'concepts': [{
            'name': key_query or base,
            'kind': 'empirical',
            'terms': keywords[:4] or [key_query or base],
            'not': [],
            'queries': [q for q in queries[:2] if q],
        }],
        'bridge': [],
        'review': [],
        'queries': [q for q in queries if q],
        'keywords': keywords,
        'outline': [],
        'scope': 'psy',
    }


def _str_list(value: object, limit: int, size: int) -> list[str]:
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, str):
            continue
        clean = _clean_query(item)[:size]
        if clean and clean.lower() not in (o.lower() for o in out):
            out.append(clean)
    return out[:limit]


def _plan_concepts(plan: Optional[dict]) -> list[dict]:
    """Constructos de la pregunta con su vocabulario propio y sus vecinos confundibles."""
    concepts: list[dict] = []
    raw = (plan or {}).get('concepts')
    if not isinstance(raw, list):
        return concepts
    for item in raw:
        if not isinstance(item, dict):
            continue
        terms = _str_list(item.get('terms'), 4, 60)
        name = _clean_query(str(item.get('name') or ''))[:80] or (terms[0] if terms else '')
        if not name:
            continue
        if not terms:
            terms = [name]
        kind = 'theory' if str(item.get('kind') or '').lower().startswith('theor') else 'empirical'
        concepts.append({
            'name': name,
            'kind': kind,
            'terms': terms,
            'not': _str_list(item.get('not'), 2, 60),
            'queries': _str_list(item.get('queries'), 2, 160),
        })
    return concepts[:3]


def _plan_query_specs(plan: Optional[dict], question: str, query: str) -> list[dict]:
    """Consultas con su rol: concepto, puente entre conceptos o revision."""
    specs: list[dict] = []
    seen: set[str] = set()

    def add(text: str, role: str, kind: str, first: bool = False) -> None:
        value = _clean_query(text)[:160]
        key = value.lower()
        if not value or key in seen:
            return
        seen.add(key)
        specs.append({'q': value, 'role': role, 'kind': kind, 'first': first})

    for concept in _plan_concepts(plan):
        for i, text in enumerate(concept['queries']):
            add(text, 'concept', concept['kind'], first=(i == 0))
    for text in _str_list((plan or {}).get('bridge'), 1, 160):
        add(text, 'bridge', 'empirical')
    for text in _str_list((plan or {}).get('review'), 1, 160):
        add(text, 'review', 'empirical')
    legacy = (plan or {}).get('queries')
    if isinstance(legacy, list):
        for i, text in enumerate(legacy):
            if isinstance(text, str):
                add(text, 'concept', 'empirical', first=(i == 0 and not specs))
    if not specs:
        for i, text in enumerate(_fallback_plan(question, query)['queries']):
            add(text, 'concept', 'empirical', first=(i == 0))
    return specs[:MAX_QUERIES]


def _plan_queries(plan: Optional[dict], question: str, query: str) -> list[str]:
    return [spec['q'] for spec in _plan_query_specs(plan, question, query)]


def _plan_terms(plan: Optional[dict], question: str, query: str) -> list[str]:
    """Terminos con los que se puntua la relevancia de cada paper."""
    terms: list[str] = []
    for concept in _plan_concepts(plan):
        for term in concept['terms']:
            terms.extend(_word_set(term))
    for keyword in (plan or {}).get('keywords') or []:
        terms.extend(_word_set(keyword))
    terms.extend(_word_set(query))
    terms.extend(_word_set(question))
    ordered = [t for t in dict.fromkeys(terms) if t not in _STOPWORDS and len(t) >= 4]
    return ordered[:28]


def _plan_anchor_terms(plan: Optional[dict]) -> list[str]:
    """Palabras clave que NO pertenecen a ningun concepto: el contexto de la pregunta.

    Solo aplica a preguntas con dos o mas conceptos. Si los conceptos son
    "hiperreflexividad" y "default mode network" y las palabras clave incluyen
    "schizophrenia", un paper sobre DMN en Alzheimer coincide con un concepto
    pero no con el contexto; las anclas permiten penalizarlo sin excluir los
    clasicos teoricos, que si nombran el contexto.
    """
    concepts = _plan_concepts(plan)
    if len(concepts) < 2:
        return []
    concept_words: set[str] = set()
    for concept in concepts:
        for term in concept['terms']:
            concept_words |= _word_set(term)
    anchors: list[str] = []
    for keyword in (plan or {}).get('keywords') or []:
        for word in _word_set(keyword):
            if word not in concept_words and word not in _STOPWORDS and len(word) >= 4:
                if word not in anchors:
                    anchors.append(word)
    return anchors[:12]


def _plan_mode(plan: Optional[dict]) -> str:
    mode = str((plan or {}).get('mode') or 'research').lower()
    return mode if mode in ('research', 'chat', 'support') else 'research'


def _plan_outline(plan: Optional[dict]) -> list[str]:
    outline: list[str] = []
    for item in (plan or {}).get('outline') or []:
        if isinstance(item, str) and item.strip():
            outline.append(item.strip()[:120])
    return outline[:4]


def _plan_scope(plan: Optional[dict]) -> str:
    return 'broad' if str((plan or {}).get('scope') or '').lower() == 'broad' else 'psy'


def _router_history(history: Optional[list[dict]]) -> str:
    """Contexto minimo para que el router sepa a que se refiere un mensaje corto."""
    if not history:
        return ''
    lines: list[str] = []
    for item in history[-ROUTER_HISTORY_MESSAGES:]:
        role = item.get('role')
        content = (item.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            lines.append(f'{role}: {content[:ROUTER_HISTORY_CHARS]}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted: Optional[dict]) -> str:
    if not isinstance(inverted, dict):
        return ''
    words: list[str] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and 0 <= pos < 100_000:
                while len(words) <= pos:
                    words.append('')
                words[pos] = word
    return ' '.join(w for w in words if w).strip()


def _extract_pdf_url(work: dict) -> Optional[str]:
    candidates: list[str] = []
    best = work.get('best_oa_location') or {}
    if best.get('pdf_url'):
        candidates.append(best['pdf_url'])
    for loc in work.get('locations') or []:
        if isinstance(loc, dict) and loc.get('pdf_url'):
            candidates.append(loc['pdf_url'])
    primary = work.get('primary_location') or {}
    if primary.get('pdf_url'):
        candidates.append(primary['pdf_url'])

    seen: set[str] = set()
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            return url

    oa_url = (work.get('open_access') or {}).get('oa_url')
    if oa_url and (
        oa_url.lower().endswith('.pdf')
        or re.search(r'/(?:pdf|download|fulltext|full-text|article/[^/]+/pdf)', oa_url, re.I)
    ):
        return oa_url
    return None


def _is_direct_pdf(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        path = parsed.path.lower()
        return bool(
            path.endswith('.pdf')
            or re.search(r'/(?:pdf|download|fulltext|full-text|bitstream|bitstreams)(?:/|$)', path)
            or 'pdf' in parsed.query.lower()
        )
    except Exception:
        return False


def _access_category(work: dict, pdf_url: Optional[str]) -> str:
    """Categoria de acceso verificable, sin confundir "open access" con gratis.

    - oneclick: hay un PDF directo en un dominio que no exige login.
    - free: OpenAlex confirma acceso abierto con una URL de lectura.
    - paid: no hay ninguna via gratuita comprobable.
    """
    oa = work.get('open_access') or {}
    oa_url = str(oa.get('oa_url') or '').strip()
    is_oa = bool(oa.get('is_oa'))
    if pdf_url and _is_direct_pdf(pdf_url):
        try:
            host = (urlparse(pdf_url).hostname or '').lower()
        except Exception:
            host = ''
        if host and not GATED_HOST_RE.search(host):
            return 'oneclick'
    if is_oa and (oa_url or pdf_url):
        return 'free'
    return 'paid'


def _work_to_source(work: dict) -> dict:
    abstract = _reconstruct_abstract(work.get('abstract_inverted_index'))
    authors = [
        (a.get('author') or {}).get('display_name')
        for a in (work.get('authorships') or [])
        if (a.get('author') or {}).get('display_name')
    ]
    source = (work.get('primary_location') or {}).get('source') or {}
    primary_topic = (work.get('primary_topic') or {}).get('display_name')
    subfield = ((work.get('primary_topic') or {}).get('subfield') or {}).get('display_name')
    topics = []
    for topic in [primary_topic, subfield] + [
        t.get('display_name') for t in (work.get('topics') or []) if isinstance(t, dict)
    ]:
        if topic and topic not in topics:
            topics.append(topic)

    pdf_url = _extract_pdf_url(work)
    is_oa = bool((work.get('open_access') or {}).get('is_oa'))
    access_category = _access_category(work, pdf_url)
    work_id = str(work.get('id') or '')
    if work_id.startswith('https://openalex.org/'):
        work_id = work_id.rsplit('/', 1)[-1]

    return {
        'id': work.get('id'),
        'openalexId': work_id,
        'doi': work.get('doi'),
        'title': work.get('title') or 'Sin titulo',
        'authors': authors,
        'year': work.get('publication_year'),
        'journal': source.get('display_name'),
        'citations': work.get('cited_by_count') or 0,
        'abstract': abstract,
        'topics': topics[:5],
        'type': work.get('type'),
        'isOa': is_oa,
        'oaStatus': (work.get('open_access') or {}).get('oa_status'),
        'oaUrl': (work.get('open_access') or {}).get('oa_url'),
        'pdfUrl': pdf_url,
        'isAutoDownloadable': access_category == 'oneclick',
        'accessCategory': access_category,
        'accessChecked': False,
    }


def _params(
    query: str,
    access: str,
    per_page: int,
    sort: Optional[str] = None,
    mode: str = 'broad',
    scope: str = 'psy',
) -> dict:
    clean = _clean_query(query)[:300]
    # En preguntas interdisciplinarias el filtro de campo excluiria la otra
    # tradicion; el ranking de relevancia se encarga despues de limpiar ruido.
    filters = [WORK_TYPES] if scope == 'broad' else [PSY_FIELDS, WORK_TYPES]
    if access in ('free', 'oneclick'):
        filters.append('is_oa:true')
    params = {
        'filter': ','.join(filters),
        'per-page': str(per_page),
        'select': SELECT_FIELDS,
    }
    # "precise" exige los terminos en titulo/resumen; "broad" usa el buscador
    # general de OpenAlex; "title" restringe al titulo.
    if mode == 'precise' and clean:
        params['filter'] = f"{params['filter']},title_and_abstract.search:{clean}"
    elif mode == 'title' and clean:
        params['filter'] = f"{params['filter']},title.search:{clean}"
    elif clean:
        params['search'] = clean
    if OPENALEX_API_KEY:
        params['api_key'] = OPENALEX_API_KEY
    else:
        params['mailto'] = OPENALEX_MAILTO
    if sort:
        params['sort'] = sort
    return params


# Valor: (instante de vencimiento, fuentes, metadatos de cobertura).
_SearchCacheEntry = tuple[float, list[dict], dict]
_search_cache: dict[str, _SearchCacheEntry] = {}


def _openalex_client() -> httpx.AsyncClient:
    """Cliente compartido por toda la busqueda: reusa TLS y respeta al polite pool."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=8.0),
        follow_redirects=True,
        headers={'User-Agent': f'PsiHub/10 (+https://melaraneax.github.io/PsiHub/; {OPENALEX_MAILTO})'},
    )


async def _openalex_get(
    params: dict,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    owned = client is None
    if client is None:
        client = _openalex_client()
    try:
        response = await client.get(OPENALEX_WORKS_URL, params=params)
        # Reintentos con espera creciente ante saturacion o errores transitorios.
        for attempt in range(4):
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            retry_after = (response.headers.get('retry-after') or '').strip()
            cooldown = float(retry_after) if retry_after.replace('.', '', 1).isdigit() else 0.0
            # Una espera larga significa presupuesto diario agotado (OpenAlex
            # comparte un cupo por IP): reintentar solo suma latencia.
            if cooldown > 60:
                break
            delay = 1.5 * (2 ** attempt)
            if cooldown:
                delay = max(delay, min(cooldown, 8.0))
            await asyncio.sleep(min(delay, 8.0))
            response = await client.get(OPENALEX_WORKS_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Respuesta invalida de OpenAlex.')
        return payload
    finally:
        if owned:
            await client.aclose()


async def _verify_access(sources: list[dict], probe_fn) -> list[dict]:
    """Confirma los PDF directos antes de prometer "1 clic" en la interfaz.

    Si el PDF no responde sin login, el paper se degrada a "gratis" (solo si
    OpenAlex confirma una URL abierta) o a "pago"; nunca se muestra una etiqueta
    de acceso que no se pueda sostener.
    """
    candidates = [s for s in sources if s.get('accessCategory') == 'oneclick']
    if not candidates:
        return sources
    semaphore = asyncio.Semaphore(6)

    async def verify(source: dict) -> None:
        source['accessChecked'] = True
        url = source.get('pdfUrl') or ''
        try:
            host = (urlparse(url).hostname or '').lower()
        except Exception:
            host = ''
        ok = False
        if url and host and not GATED_HOST_RE.search(host) and _is_direct_pdf(url):
            async with semaphore:
                try:
                    verdict = await asyncio.wait_for(probe_fn(url), timeout=6.0)
                    ok = verdict.get('ok') is True
                except Exception:
                    ok = False
        if not ok:
            source['isAutoDownloadable'] = False
            source['accessCategory'] = (
                'free' if source.get('isOa') and source.get('oaUrl') else 'paid'
            )

    await asyncio.gather(*(verify(source) for source in candidates))
    return sources


async def _probe_oneclick(sources: list[dict], probe_fn) -> list[dict]:
    """Conserva solo papers cuyo PDF responde sin login (verificacion real)."""
    candidates = [s for s in sources if s.get('pdfUrl') and s.get('isAutoDownloadable')][:ONECLICK_CANDIDATES]
    if not candidates:
        return []
    semaphore = asyncio.Semaphore(6)

    async def verify(source: dict) -> Optional[dict]:
        source['accessChecked'] = True
        url = source['pdfUrl']
        try:
            host = (urlparse(url).hostname or '').lower()
        except Exception:
            return None
        if GATED_HOST_RE.search(host) or not _is_direct_pdf(url):
            return None
        async with semaphore:
            try:
                verdict = await probe_fn(url)
            except Exception:
                return None
        return source if verdict.get('ok') is True else None

    checked = await asyncio.gather(*(verify(s) for s in candidates))
    return [s for s in checked if s]


def _work_key(work: dict) -> str:
    oid = str(work.get('id') or '')
    if oid:
        return oid.rsplit('/', 1)[-1].lower()
    doi = str(work.get('doi') or '').lower()
    if doi:
        return doi
    return _normalize(work.get('title'))[:120]


_WEAK_VENUE_RE = re.compile(
    r'(performing arts|visual arts|fine arts|shodh|tourism|marketing|business|management|'
    r'engineering|computer science|civil|mechanical|agricultur)',
    re.I,
)


def _study_design(source: dict) -> tuple[list[str], str]:
    """Etiquetas de diseno y tamano muestral extraidos del resumen (heuristica).

    Sirven para que el modelo calibre el nivel de evidencia sin inventar nada:
    solo se reporta lo que el propio resumen declara.
    """
    text = _normalize(f"{source.get('title') or ''} {source.get('abstract') or ''}")
    tags: list[str] = []
    if re.search(r'meta-?analy', text):
        tags.append('meta-análisis')
    elif re.search(r'systematic review|scoping review|umbrella review|systematic literature', text):
        tags.append('revisión sistemática')
    elif source.get('type') == 'review':
        tags.append('revisión narrativa')
    if re.search(r'randomi[sz]ed (?:controlled )?trial|randomi[sz]ed controlled|\brct\b', text):
        tags.append('ECA')
    elif re.search(r'clinical trial|pilot study|open-label|quasi-experiment', text):
        tags.append('ensayo/piloto')
    if re.search(r'fmri|functional magnetic|resting.state|neuroimag|\bpet\b', text):
        tags.append('neuroimagen')
    elif re.search(r'\beeg\b|\bmeg\b|electroenceph|magnetoenceph', text):
        tags.append('EEG/MEG')
    if re.search(r'case (study|report|series)', text):
        tags.append('caso')
    if re.search(r'qualitative|thematic analysis|interpretative phenomenological|semi-structured', text):
        tags.append('cualitativo')
    if re.search(r'cross-sectional', text):
        tags.append('transversal')
    elif re.search(r'longitudinal|cohort|follow-up', text):
        tags.append('longitudinal')
    empirical = re.search(r'participants|patients|sample of|\bn\s*=|subjects|respondents', text)
    review_like = any(t.startswith(('meta', 'revisi')) for t in tags)
    if not empirical and not review_like and re.search(
        r'theoretical|conceptual|phenomenolog|we argue|'
        r'this (paper|article|essay|chapter) (argues|proposes|discusses|examines|explores)|philosoph',
        text,
    ):
        tags.append('teórico/conceptual')

    sample = ''
    m = re.search(r'\bn\s*=\s*(\d{1,3}(?:,\d{3})+|\d{1,6})\b', text)
    if m:
        value = m.group(1)
        # "n=1" casi siempre es un fragmento de "n=1.024" o un indice, no una muestra.
        if value != '1':
            sample = f'n={value}'
    else:
        m = re.search(
            r'\b(\d{1,3}(?:,\d{3})+|\d{2,6})\s+(?:[a-z-]+\s+){0,2}?'
            r'(participants|patients|subjects|adults|children|adolescents|students|veterans|'
            r'meditators|monks|individuals|volunteers|controls|studies|trials)\b',
            text,
        )
        if m:
            unit = 'estudios' if m.group(2) in ('studies', 'trials') else 'participantes'
            sample = f'{m.group(1)} {unit}'
    return tags[:3], sample


def _phrase_in(tokens: list[str], phrase_tokens: list[str]) -> bool:
    """El termino aparece como secuencia consecutiva (tolera variantes: prefijo de 6-9)."""
    n = len(phrase_tokens)
    if not n or len(tokens) < n:
        return False
    keys = [p[: 9 if n == 1 else 6] for p in phrase_tokens]
    for i in range(len(tokens) - n + 1):
        if all(tokens[i + j].startswith(keys[j]) for j in range(n)):
            return True
    return False


def _concept_relevance(source: dict, concept: dict) -> float:
    """0-1: cuan directamente trata el paper ESE constructo (no uno vecino).

    1.0 si algun termino canonico del constructo aparece (como frase) en el
    titulo, 0.7 si aparece en el resumen o los topicos; +0.2 si coinciden
    varios terminos. Las palabras sueltas de un termino compuesto no cuentan:
    "self-disorder" no debe activarse con "self" y "disorder" por separado.
    """
    title = re.findall(r'[a-z0-9]+', _normalize(source.get('title')))
    body = re.findall(
        r'[a-z0-9]+',
        _normalize(f"{source.get('abstract') or ''} {' '.join(source.get('topics') or [])}"),
    )
    best = 0.0
    matched = 0
    for term in concept.get('terms') or []:
        phrase = re.findall(r'[a-z0-9]+', _normalize(term))
        if not phrase:
            continue
        if _phrase_in(title, phrase):
            hit = 1.0
        elif _phrase_in(body, phrase):
            hit = 0.7
        else:
            continue
        matched += 1
        best = max(best, hit)
    if matched >= 2:
        best = min(1.0, best + 0.2)
    return best


def _anchor_hits(source: dict, anchors: Optional[list[str]]) -> int:
    """Cuantas palabras del contexto de la pregunta aparece en el paper."""
    if not anchors:
        return 0
    text = ' '.join([
        str(source.get('title') or ''),
        str(source.get('abstract') or ''),
        ' '.join(source.get('topics') or []),
    ])
    return _hits(anchors, _stems(_word_set(text)))


def _score_source(
    source: dict,
    terms: list[str],
    scope: str = 'psy',
    crel: Optional[list[float]] = None,
    consensus: int = 0,
    anchors: Optional[list[str]] = None,
) -> tuple[float, float]:
    """Devuelve (puntaje, relevancia). La relevancia es la fraccion de terminos
    de la pregunta presentes en titulo, resumen y topicos del paper.

    `consensus` cuenta cuantas consultas distintas devolvieron el mismo paper:
    aparecer por varios angulos es una senal barata de centralidad tematica.
    """
    title_stems = _stems(_word_set(source.get('title')))
    abstract_stems = _stems(_word_set(source.get('abstract')))
    topic_stems = _stems(_word_set(' '.join(source.get('topics') or [])))

    if not terms:
        relevance = 0.0
    else:
        title_hits = _hits(terms, title_stems)
        abstract_hits = _hits(terms, abstract_stems)
        topic_hits = _hits(terms, topic_stems)
        relevance = min(
            1.0,
            (3.0 * title_hits + abstract_hits + 2.0 * topic_hits) / (3.0 * len(terms)),
        )

    score = relevance * 10.0
    # Trato directo del constructo nombrado en la pregunta.
    if crel:
        score += 2.0 * max(crel)
        # Papers que tratan a la vez dos constructos de la pregunta: valen mas,
        # porque son los unicos que permiten hablar del vinculo sin analogias.
        extra = sum(1 for c in crel if c >= DIRECT_REL) - 1
        if extra > 0:
            score += 0.9 * min(extra, 2)
    # Coincidencia desde varias consultas: paper central para el tema.
    if consensus > 1:
        score += min(consensus - 1, 3) * 0.15
    # Anclas tematicas: un paper puede tratar un concepto y no la pregunta. Si no
    # toca ninguna palabra del contexto (p. ej. DMN en Alzheimer cuando se
    # pregunta por esquizofrenia), pierde posiciones frente a los que si.
    if anchors:
        anchor_hits = _anchor_hits(source, anchors)
        if anchor_hits == 0:
            score -= 2.5
        else:
            score += min(anchor_hits, 3) * 0.25
    abstract = source.get('abstract') or ''
    # Sin resumen no se puede sintetizar; uno muy corto casi no aporta.
    if not abstract:
        score -= 1.2
    elif len(abstract) < 300:
        score += 0.1
    else:
        score += 0.6
    # Senales de calidad: citas (log), citas por ano (clasicos), vigencia, diseno.
    citations = max(0, int(source.get('citations') or 0))
    now_year = time.gmtime().tm_year
    year = int(source.get('year') or 0)
    score += min(math.log1p(citations), 4.0) * 0.30
    if year:
        per_year = citations / max(1, now_year - year + 1)
        score += min(math.log1p(per_year), 3.0) * 0.25
    if year >= now_year - 6:
        score += 0.30
    design = source.get('design') or []
    if 'meta-análisis' in design:
        score += 0.6
    elif 'revisión sistemática' in design:
        score += 0.45
    elif source.get('type') == 'review':
        score += 0.2
    if 'ECA' in design:
        score += 0.3
    # Revistas ajenas al dominio (artes, ingenieria, negocios) en temas psi.
    if scope == 'psy' and _WEAK_VENUE_RE.search(str(source.get('journal') or '')):
        score -= 1.5
    return score, relevance


def _select_sources(
    items: list[dict], n_concepts: int, limit: int, min_rel: Optional[float] = None,
) -> list[dict]:
    """Seleccion con cupos: puente > un cupo por concepto > revisiones > resto.

    `items` llega ordenado por puntaje. Garantiza que cada constructo de la
    pregunta tenga sus mejores papers directos aunque otro domine el ranking.
    """
    threshold = MIN_RELEVANCE if min_rel is None else max(min_rel, MIN_RELEVANCE)
    pool = [
        it for it in items
        if it['rel'] >= threshold or any(c >= DIRECT_REL for c in it['crel'])
    ]
    # Si hay contexto suficiente, fuera los papers que tratan un concepto pero
    # no la pregunta (DMN en Alzheimer cuando se pregunta por esquizofrenia).
    anchored = [it for it in pool if not it.get('offtopic')]
    if len(anchored) >= 4:
        pool = anchored
    if len(pool) < 4:
        pool = list(items)
    chosen: list[dict] = []
    taken: set[int] = set()

    def take(candidates: list[dict], k: int) -> None:
        for it in candidates:
            if k <= 0 or len(chosen) >= limit:
                break
            if id(it) in taken:
                continue
            chosen.append(it)
            taken.add(id(it))
            k -= 1

    if n_concepts >= 2:
        take([it for it in pool if sum(c >= DIRECT_REL for c in it['crel']) >= 2], 3)
    strong = ('meta-análisis', 'revisión sistemática', 'revisión narrativa')
    # Reservar una revision antes de repartir cupos: con 7 fuentes y 3
    # conceptos, el reparto no debe quedarse sin evidencia sintetizada.
    take([it for it in pool if any(t in strong for t in (it['source'].get('design') or []))], 1)
    quota = max(2, (limit - len(chosen)) // max(n_concepts, 1)) if n_concepts else 0
    for i in range(n_concepts):
        have = sum(1 for it in chosen if it['crel'][i] >= DIRECT_REL)
        take([it for it in pool if it['crel'][i] >= DIRECT_REL], quota - have)
    take([it for it in pool if any(t in strong for t in (it['source'].get('design') or []))], 2)
    take(pool, limit)
    chosen.sort(key=lambda it: it['score'], reverse=True)
    return chosen


def _coverage(sources: list[dict], concepts: list[dict]) -> tuple[list[dict], Optional[int]]:
    """Cuantas fuentes finales tratan directamente cada concepto y su vinculo."""
    if not concepts:
        return [], None
    rels = [[_concept_relevance(s, c) for c in concepts] for s in sources]
    coverage = [
        {
            'name': c['name'],
            'kind': c['kind'],
            'direct': sum(1 for r in rels if r[i] >= DIRECT_REL),
        }
        for i, c in enumerate(concepts)
    ]
    bridge = None
    if len(concepts) >= 2:
        bridge = sum(1 for r in rels if sum(1 for v in r if v >= DIRECT_REL) >= 2)
    return coverage, bridge


async def _collect(
    params: dict,
    semaphore: asyncio.Semaphore,
    log_fn: Callable[[str], None],
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[list[dict]]:
    """None si la consulta fallo (para distinguir caida de OpenAlex de cero resultados)."""
    async with semaphore:
        try:
            payload = await _openalex_get(params, client)
        except Exception as exc:  # noqa: BLE001 - una consulta caida no invalida el resto
            log_fn(f'[chat-search] consulta fallo: {type(exc).__name__}: {_redact(exc)}')
            return None
    return [w for w in (payload.get('results') or []) if isinstance(w, dict)]


def _title_source(work: dict) -> dict:
    """Vista minima de un work para puntuar relevancia por titulo sin reconstruir abstract."""
    topic = (work.get('primary_topic') or {}).get('display_name')
    return {
        'title': work.get('title'),
        'abstract': '',
        'topics': [topic] if topic else [],
    }


def _seed_reference_ids(
    works: list[dict],
    limit: int,
    concepts: Optional[list[dict]] = None,
) -> list[str]:
    """IDs que citan los trabajos semilla (revisiones y clasicos primero).

    Los papers fundacionales de un constructo suelen no contener las palabras
    clave de la pregunta, pero si aparecen en las referencias de las revisiones
    del tema. Se elige al menos un semilla por concepto, para que la expansion
    no quede dominada por el constructo mas abundante en la literatura.
    """
    if limit <= 0 or SNOWBALL_SEEDS <= 0:
        return []

    seeds: list[dict] = []
    seen_seeds: set[str] = set()

    def add(work: dict) -> None:
        key = _work_key(work)
        refs = work.get('referenced_works')
        if not key or key in seen_seeds or not isinstance(refs, list) or not refs:
            return
        seen_seeds.add(key)
        seeds.append(work)

    for concept in concepts or []:
        candidates = [
            work for work in works
            if _concept_relevance(_title_source(work), concept) >= DIRECT_REL
        ]
        if candidates:
            add(max(candidates, key=lambda w: int(w.get('cited_by_count') or 0)))
            if len(seeds) >= SNOWBALL_SEEDS:
                break

    ordered = sorted(
        works,
        key=lambda w: (str(w.get('type') or '') == 'review',
                       int(w.get('cited_by_count') or 0)),
        reverse=True,
    )
    for work in ordered:
        if len(seeds) >= SNOWBALL_SEEDS:
            break
        add(work)
    if not seeds:
        return []

    # Cupo parejo por semilla: una revision muy citada no debe acaparar la expansion.
    per_seed = max(8, limit // len(seeds))
    ids: list[str] = []
    seen: set[str] = set()
    for work in seeds:
        taken = 0
        for ref in work.get('referenced_works') or []:
            rid = str(ref or '').rstrip('/').rsplit('/', 1)[-1]
            if not rid.startswith('W') or rid in seen:
                continue
            seen.add(rid)
            ids.append(rid)
            taken += 1
            if taken >= per_seed or len(ids) >= limit:
                break
        if len(ids) >= limit:
            break
    return ids


async def _fetch_works_by_id(
    ids: list[str],
    semaphore: asyncio.Semaphore,
    log_fn: Callable[[str], None],
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Recupera trabajos concretos por ID, sin filtro de campo ni de tipo.

    Sin restriccion de disciplina: los clasicos de fenomenologia o filosofia de
    la mente quedan fuera de los campos de psicologia y medicina.
    """
    works: list[dict] = []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        params: dict = {
            'filter': 'openalex_id:' + '|'.join(batch),
            'per-page': str(len(batch)),
            'select': SELECT_FIELDS,
        }
        if OPENALEX_API_KEY:
            params['api_key'] = OPENALEX_API_KEY
        else:
            params['mailto'] = OPENALEX_MAILTO
        async with semaphore:
            try:
                payload = await _openalex_get(params, client)
            except Exception as exc:  # noqa: BLE001 - expansion opcional
                log_fn(f'[chat-search] expansion fallo: {type(exc).__name__}: {_redact(exc)}')
                continue
        works.extend(w for w in (payload.get('results') or []) if isinstance(w, dict))
    return works


async def search_sources(
    query: str,
    access: str = 'all',
    probe_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    log_fn: Callable[[str], None] = print,
    plan: Optional[dict] = None,
    question: str = '',
    scope: str = 'psy',
    limit: Optional[int] = None,
    min_rel: Optional[float] = None,
) -> tuple[list[dict], dict]:
    """Busca papers por concepto, puente y revision; devuelve los mas afines.

    Cada consulta nace de un rol (concepto, puente entre conceptos, revision) y
    la seleccion final garantiza cupos por concepto para que un constructo
    abundante en la literatura no desplace a otro minoritario pero central.
    """
    query = (query or '').strip()
    question = (question or '').strip()
    if not query and not question:
        return [], {'count': 0, 'query': '', 'candidates': 0}

    specs = _plan_query_specs(plan, question, query)
    if not specs:
        return [], {'count': 0, 'query': query, 'candidates': 0}
    queries = [s['q'] for s in specs]
    concepts = _plan_concepts(plan)

    cache_key = (
        f'{access}|{scope}|{limit or MAX_SOURCES}|'
        + '|'.join(sorted(q.lower() for q in queries))
        + '#' + '|'.join(c['name'].lower() for c in concepts)
    )
    cached = _search_cache.get(cache_key)
    if cached and cached[0] > time.time():
        extra = cached[2]
        return cached[1], {
            'count': len(cached[1]),
            'query': query,
            'candidates': extra.get('candidates', len(cached[1])),
            'queries': queries,
            'cached': True,
            'coverage': extra.get('coverage', []),
            'bridge_direct': extra.get('bridge_direct'),
        }

    # Una sola conexion HTTP para todas las consultas: menos handshakes, menos
    # picos de tasa y mejor latencia cuando OpenAlex esta saturado.
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
    pool: dict[str, dict] = {}
    consensus: dict[str, int] = {}
    async with _openalex_client() as client:
        # Por consulta: titulo+resumen y buscador general. La primera de cada
        # concepto suma una pasada por citas, y los conceptos teoricos se buscan
        # sin filtro de campo para que entre la fenomenologia.
        tasks = []
        for spec in specs:
            sc = 'broad' if spec['kind'] == 'theory' else scope
            tasks.append(_collect(
                _params(spec['q'], access, SEARCH_RESULTS, mode='precise', scope=sc),
                semaphore, log_fn, client,
            ))
            # El buscador general solo donde aporta cobertura: consulta canonica,
            # puente y revision. Asi se evitan pasadas redundantes.
            if spec['first'] or spec['role'] in ('bridge', 'review'):
                tasks.append(_collect(
                    _params(spec['q'], access, SEARCH_RESULTS, mode='broad', scope=sc),
                    semaphore, log_fn, client,
                ))
            if spec['first']:
                tasks.append(_collect(
                    _params(spec['q'], access, 12, sort='cited_by_count:desc', mode='precise', scope=sc),
                    semaphore, log_fn, client,
                ))
        groups = await asyncio.gather(*tasks)
        failed = sum(1 for g in groups if g is None)
        if failed and all(g is None for g in groups):
            raise RuntimeError('OpenAlex no respondió a ninguna consulta.')
        if failed:
            log_fn(f'[chat-search] {failed}/{len(groups)} consultas fallaron')

        for works in groups:
            for work in works or []:
                key = _work_key(work)
                if not key:
                    continue
                consensus[key] = consensus.get(key, 0) + 1
                if key not in pool:
                    pool[key] = work

        # Segunda pasada: referencias de los mejores candidatos. Trae los papers
        # fundacionales (Sass & Parnas, Brewer, Raichle…) que las palabras clave
        # de la pregunta no alcanzan y que suelen citar las revisiones del tema.
        seed_ids = _seed_reference_ids(list(pool.values()), SNOWBALL_IDS, concepts)
        if seed_ids:
            ref_works = await _fetch_works_by_id(seed_ids, semaphore, log_fn, client)
            added = 0
            for work in ref_works:
                key = _work_key(work)
                if key and key not in pool:
                    pool[key] = work
                    added += 1
            log_fn(f'[chat-search] expansion por referencias: {len(seed_ids)} ids | {added} nuevos')

    source_items = [(work, _work_to_source(work)) for work in pool.values()]
    candidates = len(source_items)
    log_fn(f'[chat-search] {len(queries)} consultas | {len(tasks)} pasadas | {candidates} papers unicos')

    terms = _plan_terms(plan, question, query)
    anchors = _plan_anchor_terms(plan)
    items: list[dict] = []
    for work, source in source_items:
        source['design'], source['sample'] = _study_design(source)
        crel = [_concept_relevance(source, c) for c in concepts]
        score, relevance = _score_source(
            source, terms, scope, crel, consensus.get(_work_key(work), 0), anchors,
        )
        items.append({
            'score': score, 'rel': relevance, 'crel': crel, 'source': source,
            'offtopic': bool(anchors) and _anchor_hits(source, anchors) == 0,
        })
    items.sort(key=lambda it: it['score'], reverse=True)

    # OpenAlex a veces omite el resumen/año/DOI. Antes de recortar, completamos
    # los mejores candidatos con Crossref/Europe PMC/Semantic Scholar: sin
    # resumen el modelo no puede usar el paper y la respuesta pierde base.
    if ENRICH and ENRICH_CANDIDATES:
        pending = [
            it for it in items[:ENRICH_CANDIDATES]
            if enrich.needs_enrichment(it['source'])
        ]
        if pending:
            log_fn(f'[chat-enrich] completando metadatos de {len(pending)} papers')
            try:
                extra_fields = await asyncio.wait_for(
                    enrich.enrich_items([it['source'] for it in pending]),
                    timeout=12.0,
                )
            except Exception as exc:  # noqa: BLE001 - nunca bloquea la respuesta
                log_fn(f'[chat-enrich] omitido: {type(exc).__name__}: {_redact(exc)}')
                extra_fields = []
            filled = 0
            for item, extra in zip(pending, extra_fields):
                if not isinstance(extra, dict):
                    continue
                source = item['source']
                for field in ('abstract', 'year', 'doi', 'authors', 'journal', 'title'):
                    value = extra.get(field)
                    if value and not source.get(field):
                        source[field] = value
                        filled += 1
                if extra.get('sources'):
                    source['enrichedBy'] = extra['sources']
                source['design'], source['sample'] = _study_design(source)
                item['crel'] = [_concept_relevance(source, c) for c in concepts]
                item['score'], item['rel'] = _score_source(
                    source, terms, scope, item['crel'],
                    consensus.get(str(source.get('openalexId') or '').lower(), 0), anchors,
                )
            if filled:
                items.sort(key=lambda it: it['score'], reverse=True)
                log_fn(f'[chat-enrich] {filled} campos completados')

    related = [
        it['source']
        for it in _select_sources(items, len(concepts), limit or MAX_SOURCES, min_rel)
    ]

    if access == 'oneclick' and probe_fn is not None:
        related = await _probe_oneclick(related, probe_fn)
    elif probe_fn is not None:
        # La etiqueta de acceso debe ser real incluso cuando no se filtra por
        # 1 clic: verificamos los PDF directos y degradamos los que fallen.
        related = await _verify_access(related, probe_fn)
    if access == 'free':
        related = [
            s for s in related
            if s.get('accessCategory') in ('free', 'oneclick')
        ]

    coverage, bridge_direct = _coverage(related, concepts)
    if coverage:
        log_fn('[chat-coverage] ' + ' | '.join(f'{c["name"]}={c["direct"]}' for c in coverage)
               + (f' | puente={bridge_direct}' if bridge_direct is not None else ''))

    # Los abstracts se cachean en paralelo para no sumar latencia a la busqueda.
    await asyncio.gather(*(_remember_abstract(source) for source in related))
    _search_cache[cache_key] = (
        time.time() + SEARCH_CACHE_TTL, related,
        {'candidates': candidates, 'coverage': coverage, 'bridge_direct': bridge_direct},
    )
    if len(_search_cache) > 300:
        _search_cache.clear()
    return related, {
        'count': len(related),
        'query': query,
        'candidates': candidates,
        'queries': queries,
        'cached': False,
        'coverage': coverage,
        'bridge_direct': bridge_direct,
    }


# ---------------------------------------------------------------------------
# Cache de abstracts en Markdown
# ---------------------------------------------------------------------------

_abstract_cache: dict[str, str] = {}


def _abstract_key(source: dict) -> str:
    return str(source.get('openalexId') or source.get('id') or '').rsplit('/', 1)[-1]


async def _remember_abstract(source: dict) -> None:
    """Guarda el abstract como Markdown (memoria + disco + GCS, sin bloquear)."""
    key = _abstract_key(source)
    abstract = source.get('abstract') or ''
    if not key or not abstract:
        return
    if key in _abstract_cache:
        return
    _abstract_cache[key] = abstract
    if len(_abstract_cache) > 2000:
        _abstract_cache.clear()
        _abstract_cache[key] = abstract

    path = ABSTRACT_DIR / f'{key}.md'
    try:
        await asyncio.to_thread(path.write_text, abstract, encoding='utf-8')
    except Exception:
        pass

    if storage.enabled():
        def upload() -> None:
            storage.put(f'abstracts/{key}.md', abstract.encode('utf-8'), 'text/markdown; charset=utf-8')

        try:
            await asyncio.to_thread(upload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _author_label(source: dict) -> str:
    """Apellido del primer autor + et al.: permite citar en prosa (Autor, año)."""
    authors = [a for a in (source.get('authors') or []) if isinstance(a, str) and a.strip()]
    if not authors:
        return ''

    def last(name: str) -> str:
        name = name.strip().rstrip('.')
        # OpenAlex usa "Apellido, Nombre" en una minoria de casos.
        if ',' in name:
            return name.split(',', 1)[0].strip()
        parts = [p for p in name.split() if p]
        if not parts:
            return ''
        # "Parnas J" (apellido primero + inicial) no debe citarse como "J".
        tail = parts[-1].rstrip('.')
        if len(parts) >= 2 and tail.isupper() and len(tail) <= 3:
            return parts[0]
        return parts[-1]

    if len(authors) == 1:
        return last(authors[0])
    if len(authors) == 2:
        return f'{last(authors[0])} & {last(authors[1])}'
    return f'{last(authors[0])} et al.'


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')
_QUANT_RESULT_RE = re.compile(
    r'(\bp\s*[<=>]|\bd\s*=|\bOR\s*=|\bRR\s*=|\bHR\s*=|IC\s*95|CI\s*95|'
    r'\bn\s*=|\d+(?:[.,]\d+)?\s*%)',
    re.I,
)


def _clip_abstract(abstract: str, limit: Optional[int] = None) -> str:
    """Recorta conservando objetivo, resultados cuantitativos y conclusión.

    El corte ciego 40/60 perdía justo los tamaños de efecto y los valores p
    que sostienen la calibración de la respuesta. Acá se eligen la primera y
    las dos últimas oraciones completas, más cualquier oración de la primera
    mitad que traiga un resultado numérico.
    """
    budget = int(limit or ABSTRACT_CHARS)
    text = re.sub(r'\s+', ' ', (abstract or '').strip())
    if len(text) <= budget:
        return text
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) < 3:
        head_len = int(budget * 0.4)
        tail_len = budget - head_len
        return f'{text[:head_len].rstrip()} [...] {text[-tail_len:].lstrip()}'
    picked: list[str] = [sentences[0]]
    half = max(1, len(sentences) // 2)
    for sentence in sentences[1:half]:
        if _QUANT_RESULT_RE.search(sentence):
            picked.append(sentence)
    for sentence in sentences[-2:]:
        if sentence not in picked:
            picked.append(sentence)
    clipped = ' '.join(picked)
    if len(clipped) > budget:
        clipped = clipped[:budget].rsplit(' ', 1)[0].rstrip(' ,;:')
    if not clipped.endswith(('.', '!', '?', '…')):
        clipped += ' […]'
    return clipped


def _digest(sources: list[dict], abstract_chars: Optional[int] = None) -> str:
    blocks: list[str] = []
    for index, source in enumerate(sources, start=1):
        title = source.get('title') or 'Sin título'
        year = source.get('year') or 's.f.'
        journal = source.get('journal') or 'Revista no informada'
        abstract = (source.get('abstract') or '').strip()
        if not abstract:
            abstract = 'OpenAlex no incluye el resumen de este artículo.'
        else:
            abstract = _clip_abstract(abstract, abstract_chars)
        access = {
            'oneclick': 'PDF directo (1 clic)',
            'free': 'acceso abierto',
            'paid': 'de pago',
        }.get(source.get('accessCategory') or '', 'acceso no informado')
        citations = int(source.get('citations') or 0)
        author = _author_label(source)
        who = f'{author} ({year})' if author else f'({year})'
        design = list(source.get('design') or [])
        if not design:
            design = ['revisión'] if source.get('type') == 'review' else ['estudio']
        tags = ', '.join(design)
        if source.get('sample'):
            tags += f' · {source["sample"]}'
        blocks.append(
            f'[{index}] {who} · {title} · {journal} · {access} · {tags} · {citations} citas\n'
            f'Resumen: {abstract}'
        )
    return '\n\n'.join(blocks)


def _concept_notes(plan: Optional[dict], meta: Optional[dict]) -> str:
    """Distinciones y cobertura calculadas por el servidor (sin costo de LLM).

    Le dicen al modelo que constructos NO debe asimilar entre si y cuales
    quedaron sin fuentes directas, para que declare la brecha en vez de
    rellenarla con un concepto vecino.
    """
    concepts = _plan_concepts(plan)
    if not concepts:
        return ''
    out: list[str] = []
    distinctions = [
        f'- «{c["name"]}» NO equivale a: {", ".join(c["not"])}. '
        'Solo igualalos si una fuente lo hace explícitamente.'
        for c in concepts if c['not']
    ]
    if distinctions:
        out.append('DISTINCIONES CONCEPTUALES A RESPETAR:\n' + '\n'.join(distinctions))

    coverage = (meta or {}).get('coverage') or []
    if coverage:
        rows: list[str] = []
        for item in coverage:
            direct = int(item.get('direct') or 0)
            kind = 'constructo teórico' if item.get('kind') == 'theory' else 'constructo empírico'
            row = f'- {item["name"]} ({kind}): {direct} fuente(s) que lo tratan directamente'
            if direct == 0:
                row += ' → SIN FUENTES DIRECTAS: no lo definas ni lo expliques con otras fuentes; declará la brecha'
            elif direct <= 2:
                row += ' → respaldo escaso: matizá'
            rows.append(row)
        bridge = (meta or {}).get('bridge_direct')
        if bridge is not None:
            row = f'- Vínculo entre los conceptos: {bridge} fuente(s) que los examinan a la vez'
            if bridge == 0:
                row += ' → el puente es teórico/no probado: decilo explícitamente'
            rows.append(row)
        out.append('COBERTURA DE LA BÚSQUEDA (calculada sobre las fuentes recuperadas):\n' + '\n'.join(rows))
    return '\n\n'.join(out) + '\n\n' if out else ''


def depth_profile(depth: str) -> dict:
    """Parametros de contexto y salida segun la extension elegida en la app."""
    if str(depth).lower() == 'deep':
        return {
            'depth': 'deep',
            'sources': DEEP_MAX_SOURCES,
            'abstract': DEEP_ABSTRACT_CHARS,
            'max_tokens': DEEP_MAX_OUTPUT,
            'min_rel': DEEP_MIN_RELEVANCE,
            'words': DEEP_WORD_TARGET,
            'instruction': (
                'Redactá en español con el formato en capas, en MODO PROFUNDO. '
                'Desarrollá, en este orden: (1) 2 a 3 frases de respuesta; '
                '(2) "## Hallazgos clave" con 6 a 8 viñetas; '
                '(3) "## Mecanismos y modelos propuestos" con 1 o 2 párrafos que '
                'distingan teoría de evidencia directa; '
                '(4) una tabla comparativa si hay 3 o más estudios comparables; '
                '(5) "## Dónde la evidencia no coincide" con 2 a 3 contradicciones; '
                '(6) "## Límites y qué buscar" con 4 viñetas. '
                f'Extensión: {DEEP_WORD_TARGET} palabras; no cierres antes de 700. '
                'Cada afirmación relevante con su cita [n].'
            ),
        }
    return {
        'depth': 'quick',
        'sources': MAX_SOURCES,
        'abstract': ABSTRACT_CHARS,
        'max_tokens': MAX_OUTPUT_TOKENS,
        'min_rel': MIN_RELEVANCE,
        'words': QUICK_WORD_TARGET,
        'instruction': (
            'Redactá en español con el formato en capas, en MODO RÁPIDO: '
            '3 a 5 hallazgos, 1 a 3 discrepancias y 3 viñetas en Límites y qué buscar. '
            f'Extensión: {QUICK_WORD_TARGET} palabras y las citas [n] indicadas.'
        ),
    }


def _history_messages(history: Optional[list[dict]]) -> list[dict]:
    if not history:
        return []
    cleaned: list[dict] = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        role = item.get('role')
        content = (item.get('content') or '').strip()
        if role not in ('user', 'assistant') or not content:
            continue
        cleaned.append({'role': role, 'content': content[:HISTORY_CHARS]})
    return cleaned


def _build_messages(
    question: str,
    sources: list[dict],
    history: Optional[list[dict]],
    outline: Optional[list[str]] = None,
    plan: Optional[dict] = None,
    meta: Optional[dict] = None,
    depth: str = 'quick',
) -> list[dict]:
    profile = depth_profile(depth)
    messages: list[dict] = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages.extend(_history_messages(history))
    axes = ''
    if outline:
        axes = (
            'EJES QUE LA RESPUESTA DEBE CUBRIR (no los repitas como lista: integralos '
            'en las capas):\n- ' + '\n- '.join(outline) + '\n\n'
        )
    searched = ''
    queries = (meta or {}).get('queries') or []
    if queries:
        # Le da al modelo materia prima para distinguir "no encontré" de
        # "no existe" sin inventar una metodología de búsqueda.
        searched = (
            f'CONSULTAS QUE SE HICIERON EN OPENALEX ({len(queries)}): '
            + '; '.join(str(q) for q in queries[:8]) + '\n\n'
        )
    messages.append({
        'role': 'user',
        'content': (
            f'PREGUNTA DEL USUARIO:\n{question.strip()}\n\n'
            f'{axes}'
            f'{searched}'
            f'{_concept_notes(plan, meta)}'
            f'FUENTES DISPONIBLES:\n{_digest(sources, profile["abstract"])}\n\n'
            f"{profile['instruction']}"
        ),
    })
    return messages


_CITE_RE = re.compile(r'\[(\d{1,2})\]')


def _citation_report(answer: str, total: int) -> tuple[list[int], list[int]]:
    """Devuelve (citadas, sin citar) para detectar citas rotas en los logs.

    No corrige la respuesta en vuelo (llega en streaming), pero deja rastro
    cuando el modelo cita un numero inexistente o deja fuentes sin usar.
    """
    cited = sorted({int(n) for n in _CITE_RE.findall(answer or '')})
    out_of_range = [n for n in cited if n < 1 or n > total]
    used = {n for n in cited if 1 <= n <= total}
    uncited = [n for n in range(1, total + 1) if n not in used]
    return out_of_range, uncited


def _direct_messages(
    question: str,
    history: Optional[list[dict]],
    system_prompt: str,
) -> list[dict]:
    """Conversacion sin busqueda: charla o acompanamiento."""
    messages: list[dict] = [{'role': 'system', 'content': system_prompt}]
    messages.extend(_history_messages(history))
    messages.append({'role': 'user', 'content': question.strip()})
    return messages


# ---------------------------------------------------------------------------
# Streaming DeepSeek
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


class ChatStartError(RuntimeError):
    pass


async def _complete(
    messages: list[dict],
    api_keys: list[str],
    max_tokens: int,
    temperature: float = 0.2,
) -> tuple[str, dict]:
    """Llamada corta sin streaming (planificacion de la busqueda)."""
    if not api_keys:
        raise ChatStartError('No hay API key de DeepSeek configurada.')

    payload = {
        'model': CHAT_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': False,
        'thinking': {'type': 'disabled'},
        'max_tokens': max_tokens,
    }
    last_error: Optional[BaseException] = None
    for attempt in range(min(len(api_keys), 2)):
        key = api_keys[attempt % len(api_keys)]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.post(
                    DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                )
            if response.status_code >= 400:
                raise ChatStartError(f'DeepSeek HTTP {response.status_code}: {response.text[:200]}')
            data = response.json()
            choices = data.get('choices') or []
            if not choices:
                return '', (data.get('usage') or {})
            content = (choices[0].get('message') or {}).get('content') or ''
            return content, (data.get('usage') or {})
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise ChatStartError(f'No se pudo planificar la busqueda: {last_error}')


# Planes del router reutilizables: una misma pregunta dentro de la ventana de
# cache de busqueda no debe pagar dos veces la planificacion.
_plan_cache: dict[str, tuple[float, dict]] = {}


def _merge_usage(*parts: dict) -> dict:
    """Suma usage de varias llamadas (router + respuesta) para el ledger."""
    merged: dict = {}
    keys = (
        'prompt_tokens', 'completion_tokens', 'total_tokens',
        'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
        'prompt_tokens_details', 'completion_tokens_details',
    )
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in keys:
            value = part.get(key)
            if isinstance(value, (int, float)):
                merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


async def _route(
    question: str,
    query: str,
    history: Optional[list[dict]],
    api_keys: list[str],
    log_fn: Callable[[str], None],
) -> dict:
    """Decide si hay que investigar y, si si, arma 4-5 consultas para OpenAlex.

    Es una sola llamada corta: reemplaza al viejo planificador, asi que el
    enrutado no cuesta tokens extra. Si el modelo falla se usa un plan
    heuristico y se investiga: la busqueda nunca se detiene por el router.
    """
    fallback = _fallback_plan(question, query)
    plan_key = hashlib.sha1(
        f'{_normalize(question)}|{_normalize(query)}'.encode('utf-8')
    ).hexdigest()
    cached = _plan_cache.get(plan_key)
    if cached and cached[0] > time.time():
        plan = dict(cached[1])
        plan['route_usage'] = {}
        plan['cached_plan'] = True
        return plan

    context = _router_history(history)
    prompt = f'Pregunta del usuario: {question[:1200]}\n'
    if context:
        prompt += f'\nConversacion previa (para interpretar el mensaje):\n{context}\n'
    prompt += f'\nConsulta traducida al ingles (referencia): {query[:300]}'

    try:
        raw, usage = await _complete(
            [
                {'role': 'system', 'content': ROUTER_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            api_keys,
            max_tokens=PLANNER_MAX_OUTPUT,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat-route] fallo: {type(exc).__name__}: {exc}')
        fallback['route_usage'] = {}
        return fallback

    parsed = _parse_json_object(raw)
    if not parsed:
        log_fn('[chat-route] respuesta no parseable; se investiga con plan heuristico')
        fallback['route_usage'] = usage
        return fallback

    mode = _plan_mode(parsed)
    if mode != 'research':
        log_fn(f'[chat-route] modo {mode}: sin busqueda')
        return {'mode': mode, 'queries': [], 'keywords': [], 'outline': [],
                'scope': 'psy', 'route_usage': usage}

    legacy = [q.strip() for q in (parsed.get('queries') or []) if isinstance(q, str) and q.strip()]
    keywords = [k.strip() for k in (parsed.get('keywords') or []) if isinstance(k, str) and k.strip()]
    if not keywords:
        keywords = fallback['keywords']
    plan = {
        'mode': 'research',
        'concepts': _plan_concepts(parsed),
        'bridge': _str_list(parsed.get('bridge'), 1, 160),
        'review': _str_list(parsed.get('review'), 1, 160),
        'queries': legacy[:5],
        'keywords': keywords[:10],
        'outline': _plan_outline(parsed),
        'scope': _plan_scope(parsed),
        'route_usage': usage,
    }
    _plan_cache[plan_key] = (time.time() + SEARCH_CACHE_TTL, dict(plan))
    if len(_plan_cache) > 300:
        _plan_cache.clear()
    specs = _plan_query_specs(plan, question, query)
    log_fn(
        f'[chat-plan] {len(specs)} consultas ({plan["scope"]}, '
        f'{len(plan["concepts"])} conceptos): ' + ' | '.join(s['q'] for s in specs)
    )
    return plan


async def _stream_completion(
    messages: list[dict],
    api_keys: list[str],
    log_fn: Callable[[str], None],
    max_tokens: Optional[int] = None,
    temperature: float = 0.25,
):
    if not api_keys:
        raise ChatStartError('No hay API key de DeepSeek configurada.')

    payload = {
        'model': CHAT_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': True,
        'stream_options': {'include_usage': True},
        'thinking': {'type': 'disabled'},
        'max_tokens': max_tokens or MAX_OUTPUT_TOKENS,
    }
    last_error: Optional[BaseException] = None

    for attempt in range(min(len(api_keys), 3)):
        key = api_keys[attempt % len(api_keys)]
        produced = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
                async with client.stream(
                    'POST', DEEPSEEK_URL,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread())[:300].decode('utf-8', 'replace')
                        raise ChatStartError(f'DeepSeek HTTP {response.status_code}: {body}')
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if not data or data == '[DONE]':
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get('usage')
                        if usage:
                            yield ('usage', usage)
                        choices = chunk.get('choices') or []
                        if not choices:
                            continue
                        reason = choices[0].get('finish_reason')
                        if reason:
                            yield ('finish', reason)
                        delta = (choices[0].get('delta') or {}).get('content') or ''
                        if delta:
                            produced = True
                            yield ('delta', delta)
            return
        except ChatStartError as exc:
            # Rotamos a la siguiente key: una key vencida o sin credito no
            # debe tumbar el chat si hay otra configurada.
            last_error = exc
            if produced:
                raise
            log_fn(f'[chat] key {attempt + 1} rechazada: {exc}')
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if produced:
                raise
            wait = min(2 ** attempt, 8)
            log_fn(f'[chat] intento {attempt + 1} fallo: {exc}. Reintentando en {wait}s')
            await asyncio.sleep(wait)

    raise ChatStartError(f'No se pudo contactar a DeepSeek: {last_error}')


def _redact(text: object) -> str:
    """Evita filtrar la API key de OpenAlex en los logs de error."""
    return re.sub(r'(api_key=)[^&\s\']+', r'\1***', str(text))


async def stream_chat(
    question: str,
    query: str,
    access: str,
    history: Optional[list[dict]],
    api_keys: list[str],
    probe_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    log_fn: Callable[[str], None] = print,
    client_id: str = '',
    depth: str = 'quick',
) -> AsyncIterator[str]:
    """Genera eventos SSE: status, delta, sources, done o error.

    Las fuentes viajan al final, despues del texto, para que la respuesta se
    lea como una redaccion y la lista de papers aparezca recien entonces.
    """
    started = time.time()
    profile = depth_profile(depth)
    question = (question or '').strip()[:2000]
    if not question:
        yield _sse({'type': 'error', 'message': 'La pregunta esta vacia.'})
        return

    def sources_event(sources: list[dict], meta: dict) -> str:
        # El cliente solo pinta estos campos. Enviar el abstract y los topics
        # inflaba el SSE a ~45 KB y llenaba el localStorage del historial.
        public_fields = (
            'id', 'openalexId', 'doi', 'title', 'authors', 'year', 'journal',
            'citations', 'isOa', 'oaUrl', 'pdfUrl', 'isAutoDownloadable',
            'accessCategory', 'type', 'design', 'sample', 'enrichedBy',
        )
        public_sources = []
        for source in sources:
            item = {key: source.get(key) for key in public_fields if source.get(key) is not None}
            item['access'] = source.get('accessCategory') or (
                'free' if source.get('isOa') else 'paid'
            )
            public_sources.append(item)
        return _sse({
            'type': 'sources',
            'sources': public_sources,
            'access': access,
            'query': meta.get('query') or query,
            'cached': bool(meta.get('cached')),
            'depth': profile['depth'],
        })

    def finish(usage: dict, mode: str, sources_count: int, candidates: int) -> tuple[dict, float]:
        """Registra el costo de la respuesta y deja la linea [cost-chat]."""
        cost = estimate_cost(usage)
        elapsed = round(time.time() - started, 2)
        record = {
            'doc_id': f"chat-{int(time.time() * 1000)}",
            'kind': 'chat',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'title': question[:120],
            'pages': 0,
            'model': CHAT_MODEL,
            'source': 'chat',
            'mode': mode,
            'depth': profile['depth'],
            'access': access,
            'sources': sources_count,
            'candidates': candidates,
            'cache_hit_tokens': int(usage.get('prompt_cache_hit_tokens') or 0),
            'elapsed_sec': elapsed,
            'client_id': client_id[:64],
            **cost,
        }
        try:
            record_cost(record)
        except Exception:
            pass
        log_fn(
            f"[cost-chat] {record['doc_id']} | modo {mode} | {sources_count} fuentes | "
            f"{cost['total_tokens']} tokens (cache {record['cache_hit_tokens']}) | "
            f"${cost['cost_usd']:.6f} USD | {elapsed}s"
        )
        return cost, elapsed

    yield _sse({'type': 'status', 'text': 'Analizando el mensaje…'})
    try:
        plan = await _route(question, query or question, history, api_keys, log_fn)
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat-route] error inesperado: {exc}')
        plan = _fallback_plan(question, query or question)

    mode = _plan_mode(plan)
    route_usage = plan.get('route_usage') or {}

    # Charla y acompanamiento: no se busca bibliografia, se responde directo.
    if mode in ('chat', 'support'):
        is_support = mode == 'support'
        yield _sse({
            'type': 'status',
            'text': 'Un momento…' if is_support else 'Escribiendo…',
        })
        direct_usage: dict = {}
        try:
            messages = _direct_messages(
                question,
                history,
                SUPPORT_PROMPT if is_support else DIRECT_PROMPT,
            )
            async for kind, value in _stream_completion(
                messages, api_keys, log_fn,
                max_tokens=DIRECT_MAX_OUTPUT,
                temperature=0.45,
            ):
                if kind == 'usage':
                    direct_usage = value
                elif kind == 'delta':
                    yield _sse({'type': 'delta', 'text': value})
        except Exception as exc:  # noqa: BLE001
            log_fn(f'[chat] respuesta directa fallo: {type(exc).__name__}: {exc}')
            yield _sse({'type': 'error', 'message': str(exc) or 'DeepSeek no respondio.'})
            return

        if is_support:
            # Los recursos los agrega el servidor: numeros verificados, no inventados.
            yield _sse({'type': 'delta', 'text': SUPPORT_FOOTER})

        direct_usage = _merge_usage(direct_usage, route_usage)
        direct_cost, direct_elapsed = finish(direct_usage, mode, 0, 0)
        yield sources_event([], {'query': query, 'cached': False})
        yield _sse({
            'type': 'done',
            'usage': direct_usage,
            'cost_usd': direct_cost['cost_usd'],
            'sources_count': 0,
            'elapsed_sec': direct_elapsed,
        })
        return

    query_count = len(_plan_queries(plan, question, query or question))
    yield _sse({
        'type': 'status',
        'text': f'Buscando en {query_count} consultas de OpenAlex…',
    })

    try:
        sources, search_meta = await search_sources(
            query or question,
            access=access,
            probe_fn=probe_fn,
            log_fn=log_fn,
            plan=plan,
            question=question,
            scope=_plan_scope(plan),
            limit=profile['sources'],
            min_rel=profile['min_rel'],
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat] busqueda fallo: {type(exc).__name__}: {_redact(exc)}')
        text = str(exc)
        if '429' in text or 'no respondió' in text:
            message = (
                'OpenAlex alcanzó su límite de consultas por hoy. Probá más tarde '
                'o configurá OPENALEX_API_KEY en el servidor.'
            )
        else:
            message = 'No se pudo buscar en OpenAlex. Intenta de nuevo.'
        yield _sse({'type': 'error', 'message': message})
        return

    if not sources:
        yield _sse({
            'type': 'status',
            'text': 'Sin papers suficientemente relacionados.',
        })
        yield _sse({
            'type': 'delta',
            'text': ('No encontre papers suficientemente relacionados con la pregunta. '
                     'Proba reformulandola o cambia el filtro de acceso.'),
        })
        yield sources_event([], search_meta)
        yield _sse({'type': 'done', 'usage': {}, 'cost_usd': 0, 'sources_count': 0})
        return

    candidates = int(search_meta.get('candidates') or len(sources))
    yield _sse({
        'type': 'status',
        'text': f'{candidates} papers revisados, {len(sources)} relevantes. Redactando…',
    })

    messages = _build_messages(
        question, sources, history, _plan_outline(plan), plan=plan, meta=search_meta,
        depth=profile['depth'],
    )
    usage: dict = {}
    parts: list[str] = []
    finish_reason: Optional[str] = None

    try:
        async for kind, value in _stream_completion(
            messages, api_keys, log_fn, max_tokens=profile['max_tokens'],
        ):
            if kind == 'usage':
                usage = value
            elif kind == 'finish':
                finish_reason = value
            elif kind == 'delta':
                parts.append(value)
                yield _sse({'type': 'delta', 'text': value})
    except Exception as exc:  # noqa: BLE001
        log_fn(f'[chat] generacion fallo: {type(exc).__name__}: {exc}')
        yield sources_event(sources, search_meta)
        yield _sse({'type': 'error', 'message': str(exc) or 'DeepSeek no respondio.'})
        return

    # Si el modelo agoto max_tokens (finish_reason == 'length'), la respuesta
    # quedaba cortada a mitad de una seccion y sin el cierre util. En vez de
    # entregarla asi, se pide una unica continuacion acotada y se suma al flujo.
    if finish_reason == 'length' and ''.join(parts).strip():
        log_fn('[chat] respuesta truncada: pidiendo continuacion')
        cont_messages = messages + [
            {'role': 'assistant', 'content': ''.join(parts)},
            {'role': 'user', 'content': CONTINUE_PROMPT},
        ]
        cont_usage: dict = {}
        try:
            cont_tokens = CONTINUATION_TOKENS if profile['depth'] == 'quick' else CONTINUATION_TOKENS * 2
            async for kind, value in _stream_completion(
                cont_messages, api_keys, log_fn, max_tokens=cont_tokens,
            ):
                if kind == 'usage':
                    cont_usage = value
                elif kind == 'delta':
                    parts.append(value)
                    yield _sse({'type': 'delta', 'text': value})
        except Exception as exc:  # noqa: BLE001
            log_fn(f'[chat] continuacion fallo: {type(exc).__name__}: {exc}')
        usage = _merge_usage(usage, cont_usage)

    out_of_range, uncited = _citation_report(''.join(parts), len(sources))
    if out_of_range or len(uncited) >= max(3, len(sources) // 2):
        log_fn(
            f'[chat-citas] fuera de rango: {out_of_range or "ninguna"} | '
            f'sin citar: {uncited or "ninguna"}'
        )

    usage = _merge_usage(usage, route_usage)
    cost, elapsed = finish(usage, mode, len(sources), candidates)
    # Primero las fuentes y al final el cierre: el cliente las muestra recien
    # cuando el texto ya esta completo.
    yield sources_event(sources, search_meta)
    yield _sse({
        'type': 'done',
        'usage': usage,
        'cost_usd': cost['cost_usd'],
        'sources_count': len(sources),
        'elapsed_sec': elapsed,
    })
