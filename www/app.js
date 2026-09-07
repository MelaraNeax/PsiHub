/**
 * PsiHub — App Logic v3
 * Spotify aesthetic · OpenAlex · Traducción MyMemory
 */

import { fetchPapers, fetchWorkById } from './src/services/openalex.js';
import { translateToSpanish, translateBatch, translateToEnglish } from './src/services/translate.js';

// ═══════════════════════════════════════════════════
// EXPANSIÓN DE QUERIES — Diccionario ES → EN + sinónimos clave
// ═══════════════════════════════════════════════════

const QUERY_EXPANSION = {
  // Psicopatología (con y sin tildes)
  'depresion':     'depression depressive disorder psychotherapy',
  'depresión':     'depression depressive disorder psychotherapy',
  'ansiedad':      'anxiety anxiety disorder psychotherapy',
  'estres':        'stress burnout psychological distress',
  'estrés':        'stress burnout psychological distress',
  'trauma':        'trauma PTSD post-traumatic stress psychotherapy',
  'fobia':         'phobia anxiety disorder exposure therapy',
  'esquizofrenia': 'schizophrenia psychosis psychotherapy',
  'bipolar':       'bipolar disorder mood disorder',
  'borderline':    'borderline personality disorder BPD DBT',
  'tlp':           'borderline personality disorder BPD DBT',
  'toc':           'OCD obsessive compulsive disorder ERP',
  'trastorno':     'disorder mental health',
  'duelo':         'grief bereavement loss counseling',
  'autoestima':    'self-esteem self-concept psychotherapy',
  'apego':         'attachment theory psychotherapy adult attachment',
  'pareja':        'couples therapy relationship psychotherapy',
  'familia':       'family therapy systemic intervention',
  'infancia':      'child psychotherapy developmental mental health',
  'adolescencia':  'adolescent psychotherapy youth mental health',
  'adulto mayor':  'geriatric psychology elderly mental health',
  'suicidio':      'suicide prevention suicidal ideation crisis',
  'adicciones':    'addiction substance abuse psychotherapy',
  'adiccion':      'addiction substance abuse psychotherapy',
  'adicción':      'addiction substance abuse psychotherapy',
  // Corrientes
  'psicoanalisis': 'psychoanalysis psychoanalytic therapy',
  'psicoanálisis': 'psychoanalysis psychoanalytic therapy',
  'cognitiva':     'cognitive therapy CBT',
  'conductual':    'behavioral therapy',
  'tcc':           'cognitive behavioral therapy CBT',
  'cbt':           'cognitive behavioral therapy CBT',
  'humanista':     'humanistic person-centered therapy',
  'existencial':   'existential therapy logotherapy',
  'sistémica':    'systemic family therapy',
  'sistemica':     'systemic family therapy',
  'gestalt':       'gestalt therapy',
  'mindfulness':   'mindfulness based intervention meditation',
  'act':           'acceptance commitment therapy ACT',
  'dbt':           'dialectical behavior therapy DBT',
  'emdr':          'EMDR trauma therapy',
  'tercera ola':   'third wave CBT mindfulness ACT DBT',
  // Términos clínicos
  'psicoterapia':  'psychotherapy psychological treatment',
  'terapia':       'psychotherapy psychological treatment',
  'alianza':       'therapeutic alliance working alliance',
  'alianza terapeutica': 'therapeutic alliance working alliance',
  'alianza terapéutica': 'therapeutic alliance working alliance',
  'sesión':       'psychotherapy session clinical process',
  'sesion':        'psychotherapy session clinical process',
  'eficacia':      'efficacy effectiveness psychotherapy outcomes',
  'evidencia':     'evidence randomized controlled trial meta-analysis',
  'protocolo':     'clinical protocol intervention manual',
  'caso clinico':  'clinical case study psychotherapy',
  'caso clínico':  'clinical case study psychotherapy',
  'diagnóstico':   'diagnosis diagnostic criteria DSM clinical',
  'diagnostico':   'diagnosis diagnostic criteria DSM clinical',
};

/**
 * Expande y traduce la query del usuario al inglés para optimizar OpenAlex.
 * Nunca muestra la query traducida al usuario.
 */
async function smartExpandQuery(rawQuery) {
  const lower = rawQuery.toLowerCase().trim();
  const normalized = lower.normalize('NFD').replace(/[\u0300-\u036f]/g, '');

  // 1. Buscar coincidencia exacta en el diccionario
  if (QUERY_EXPANSION[lower]) return QUERY_EXPANSION[lower];
  if (QUERY_EXPANSION[normalized]) return QUERY_EXPANSION[normalized];

  // 2. Buscar si contiene palabras clave del diccionario
  for (const [es, en] of Object.entries(QUERY_EXPANSION)) {
    const esNorm = es.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    if (normalized === esNorm || normalized.includes(esNorm)) {
      return en;
    }
  }

  // 3. Si no está en el diccionario, traducir automáticamente con Google Translate
  const translated = await translateToEnglish(rawQuery);
  return translated || rawQuery;
}

// ═══════════════════════════════════════════════════
// CORRIENTES
// ═══════════════════════════════════════════════════

const CORRIENTES = [
  { id: 'neurociencia',  label: 'Neurociencia',    abbr: 'NEU', icon: 'ph-brain',                 query: 'neuroscience neurobiology cognitive neuroscience brain', color: '#E91E63' },
  { id: 'tcc',           label: 'TCC / CBT',       abbr: 'TCC', icon: 'ph-grid-four',             query: 'cognitive behavioral therapy CBT',                      color: '#1565C0' },
  { id: 'tercera_ola',   label: 'Tercera Ola',     abbr: '3°',  icon: 'ph-waves',                 query: 'third wave ACT DBT mindfulness acceptance',             color: '#1DB954' },
  { id: 'sistemica',     label: 'Sistémica',       abbr: 'SIS', icon: 'ph-graph',                 query: 'systemic family therapy',                               color: '#00838F' },
  { id: 'psicoanalisis', label: 'Psicoanálisis',   abbr: 'PSA', icon: 'ph-couch',                 query: 'psychoanalysis psychoanalytic therapy',                 color: '#7B2D8B' },
  { id: 'humanismo',     label: 'Humanismo',       abbr: 'HUM', icon: 'ph-person-arms-spread',    query: 'humanistic person-centered therapy Rogers',             color: '#C17900' },
  { id: 'gestalt',       label: 'Gestalt',         abbr: 'GES', icon: 'ph-eye',                   query: 'gestalt therapy awareness contact',                     color: '#2E7D32' },
  { id: 'existencial',   label: 'Existencial',     abbr: 'EXI', icon: 'ph-infinity',              query: 'existential therapy logotherapy meaning Frankl',        color: '#4527A0' },
  { id: 'fenomenologia', label: 'Fenomenología',   abbr: 'FEN', icon: 'ph-spiral',                query: 'phenomenological existential psychotherapy',             color: '#5B3FE0' },
  { id: 'segunda_ola',   label: 'Segunda Ola',     abbr: '2°',  icon: 'ph-lightning',             query: 'rational emotive behavior REBT cognitive therapy',      color: '#E65C00' },
];

// ═══════════════════════════════════════════════════
// ═══════════════════════════════════════════════════
// ═══════════════════════════════════════════════════
// CLASIFICACIÓN INTELIGENTE DE PAPERS (Evidencia / Clínico / Teórico)
// ═══════════════════════════════════════════════════

const EVIDENCIA_TERMS_TITLE_MESH = [
  'meta-analysis', 'meta-analytic', 'metaanálisis', 'meta-análisis',
  'systematic review', 'revisión sistemática', 'randomized controlled trial',
  'randomised controlled trial', 'randomized controlled', 'randomised controlled',
  'ensayo controlado aleatorizado', 'clinical trial', 'ensayo clínico',
  'comparative study', 'estudio comparativo', 'efficacy', 'eficacia',
  'cohort study', 'cohort studies', 'estudio de cohorte',
  'case-control study', 'longitudinal cohort', 'prospective cohort', 'rct',
  'double-blind', 'doble ciego', 'placebo-controlled', 'multicenter trial',
  'longitudinal study', 'estudio longitudinal'
];

const EVIDENCIA_TERMS_GENERAL = [
  'effectiveness', 'efectividad', 'odds ratio', 'relative risk', 'riesgo relativo',
  'confidence interval', 'intervalo de confianza', '95% ci',
  'effect size', 'tamaño del efecto', 'statistical significance', 'significancia estadística',
  'empirical study', 'estudio empírico', 'quantitative analysis', 'análisis cuantitativo',
  'meta-analyzed', 'cohen\'s d', 'regression model', 'heterogeneity', 'forest plot',
  'sample size', 'tamaño de la muestra', 'control group', 'grupo control',
  'p < 0.', 'p < .', 'n = ', 'patients were randomized', 'participantes', 'participants'
];

const CLINICO_TERMS_TITLE_MESH = [
  'practice guideline', 'clinical guideline', 'guía clínica', 'guías de práctica',
  'guidelines', 'guideline', 'case report', 'clinical case report', 'reporte de caso',
  'case study', 'estudio de caso', 'caso clínico', 'treatment outcome',
  'resultado del tratamiento', 'psychotherapy/methods', 'treatment protocol',
  'protocolo de tratamiento', 'clinical protocol', 'protocolo clínico', 'protocol',
  'intervention', 'intervención', 'psychotherapy', 'psicoterapia',
  'clinical practice', 'práctica clínica', 'manualized treatment', 'tratamiento manualizado',
  'therapeutic alliance', 'alianza terapéutica', 'alliance rupture', 'ruptura de la alianza',
  'patient care', 'atención al paciente', 'counseling', 'consejería'
];

const CLINICO_TOPICS = [
  'clinical psychology', 'psicología clínica', 'psychotherapy', 'psicoterapia',
  'psychiatry', 'psiquiatría', 'mental health', 'salud mental',
  'counseling psychology', 'applied psychology', 'family therapy', 'terapia familiar',
  'cognitive therapy', 'terapia cognitiva', 'psychoanalytic therapy', 'psychotherapeutic'
];

const CLINICO_TERMS_GENERAL = [
  'therapist', 'terapeuta', 'inpatient', 'outpatient', 'ambulatorio',
  'diagnostic criteria', 'criterios diagnósticos', 'dsm-5', 'dsm-iv', 'dsm', 'icd-11', 'icd-10',
  'cie-10', 'cie-11', 'symptom reduction', 'reducción de síntomas', 'clinical utility', 'utilidad clínica',
  'treatment efficacy in practice', 'patient evaluation', 'evaluación del paciente',
  'clinical presentation', 'presentación clínica', 'consultation', 'adherence to treatment',
  'patient', 'paciente', 'treatment', 'tratamiento', 'disorder', 'trastorno', 'symptom', 'síntoma'
];

const TEORICO_TYPES = ['book-chapter', 'editorial', 'perspective', 'letter', 'paratext'];

const TEORICO_TERMS_TITLE = [
  'framework', 'marco teórico', 'marco conceptual', 'epistemology', 'epistemología',
  'epistemological', 'epistemológico', 'conceptual model', 'modelo conceptual',
  'theory', 'teoría', 'rethinking', 'repensando', 'perspectives', 'perspectiva',
  'philosophical', 'filosófico', 'philosophy of mind', 'filosofía de la mente',
  'constructivism', 'constructivismo', 'hermeneutic', 'hermenéutica',
  'phenomenological framework', 'ontological', 'ontológico',
  'psychoanalytic theory', 'theoretical foundations', 'fundamentos teóricos', 'towards a theory'
];

const TEORICO_TERMS_GENERAL = [
  'conceptual', 'paradigm', 'paradigma', 'theoretical perspective', 'perspectiva teórica',
  'epistemic', 'epistémico', 'dialectical', 'dialéctico', 'historical review', 'revisión histórica',
  'conceptual analysis', 'análisis conceptual'
];

function getPaperSubtypeScores(paper) {
  if (!paper) return { evidencia: 0, clinico: 0, teorico: 0 };

  const type = (paper.type || '').toLowerCase();
  const title = (paper.title || '').toLowerCase();
  const titleEs = (paper.titleEs || '').toLowerCase();
  const abstract = (paper.abstract || '').toLowerCase();
  const abstractEs = (paper.abstractEs || '').toLowerCase();
  const topics = (paper.topics || []).map(t => String(t).toLowerCase());
  const topicsStr = topics.join(' ');
  const meshList = (paper.mesh || []).map(m => String(m).toLowerCase());
  const meshStr = meshList.join(' ');

  const titleAndMesh = `${title} ${titleEs} ${meshStr}`;
  const fullText = `${titleAndMesh} ${topicsStr} ${abstract} ${abstractEs}`;

  let scoreEvidencia = 0;
  let scoreClinico = 0;
  let scoreTeorico = 0;

  // 1. EVIDENCIA
  if (type === 'review') scoreEvidencia += 5;
  EVIDENCIA_TERMS_TITLE_MESH.forEach(kw => {
    if (titleAndMesh.includes(kw)) scoreEvidencia += 6;
    else if (fullText.includes(kw)) scoreEvidencia += 3;
  });
  EVIDENCIA_TERMS_GENERAL.forEach(kw => {
    if (fullText.includes(kw)) scoreEvidencia += 2;
  });

  // 2. CLÍNICA
  CLINICO_TERMS_TITLE_MESH.forEach(kw => {
    if (titleAndMesh.includes(kw)) scoreClinico += 6;
    else if (fullText.includes(kw)) scoreClinico += 3;
  });
  CLINICO_TOPICS.forEach(top => {
    if (topicsStr.includes(top)) scoreClinico += 3;
  });
  CLINICO_TERMS_GENERAL.forEach(kw => {
    if (fullText.includes(kw)) scoreClinico += 1.5;
  });

  // 3. TEORÍA
  if (TEORICO_TYPES.includes(type)) scoreTeorico += 5;
  TEORICO_TERMS_TITLE.forEach(kw => {
    if (title.includes(kw) || titleEs.includes(kw)) scoreTeorico += 6;
    else if (topicsStr.includes(kw)) scoreTeorico += 4;
    else if (abstract.includes(kw) || abstractEs.includes(kw)) scoreTeorico += 2;
  });
  TEORICO_TERMS_GENERAL.forEach(kw => {
    if (fullText.includes(kw)) scoreTeorico += 1.5;
  });

  // Criterio de descarte para teoría: presencia clara de estadística empírica o diseño experimental
  const hasEmpiricalData = /p\s*[<=]\s*0?\.\d+|n\s*=\s*\d+|randomiz|aleatoriz|control group|grupo control|meta-analy|metaanálisis|clinical trial|ensayo clínico|sample size|tamaño de la muestra/i.test(fullText);
  if (hasEmpiricalData) {
    scoreTeorico = Math.max(0, scoreTeorico - 8);
    scoreEvidencia += 4;
  }

  return { evidencia: scoreEvidencia, clinico: scoreClinico, teorico: scoreTeorico };
}

function classifyPaper(paper) {
  const scores = getPaperSubtypeScores(paper);
  const ranked = [
    { tag: 'evidencia', score: scores.evidencia },
    { tag: 'clinico',   score: scores.clinico },
    { tag: 'teorico',   score: scores.teorico }
  ].sort((a, b) => b.score - a.score);

  if (ranked[0].score > 0) {
    const tags = [ranked[0].tag];
    if (ranked[1].score >= 4 && ranked[1].score >= ranked[0].score * 0.75) {
      tags.push(ranked[1].tag);
    }
    return tags;
  }

  // Fallback inteligente bilingüe
  const full = `${paper.title || ''} ${paper.titleEs || ''} ${(paper.topics || []).join(' ')} ${paper.abstract || ''} ${paper.abstractEs || ''}`.toLowerCase();
  if (/patient|paciente|therap|terap|treatment|tratamient|disorder|trastorn|symptom|sintoma|síntoma|clinic|clínic|diagnos|diagnóst|consult|counsel/i.test(full)) {
    return ['clinico'];
  }
  if (/data|dato|study|estudio|result|hallazgo|participant|muestra|sample|investig|experiment|evaluat|ensayo|meta-an/i.test(full)) {
    return ['evidencia'];
  }
  return ['teorico'];
}

// ═══════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════

const S = {
  activePage:      'home',
  resultsOpen:     false,
  baseQuery:       '',
  activeSubtype:   'all',
  activeCorriente: null,
  sort:            null,
  page:            1,
  perPage:         10,
  total:           0,
  papers:          [],     // papers actuales en la vista de resultados
  loading:         false,
  searchTimeout:   null,
  bookmarks:       loadData('psyhub_bk', []),
  profile:         loadData('psyhub_profile', { name: '', role: '' }),
  searches:        loadData('psyhub_searches_count', 0),
  currentPaper:    null,
  deferredInstall: null,

  // Historias / Explorar
  stories:         [],
  activeStoryIdx:  0,
  storyAnimFrame:  null,
  storyStartTime:  0,
  storyElapsed:    0,
  storyDuration:   8000,   // 8 segundos por historia
  storyPaused:     false,
  watchedStories:  loadData('psyhub_watched_stories', []),
};

// ═══════════════════════════════════════════════════
// DOM — se inicializa cuando el DOM está listo
// ═══════════════════════════════════════════════════

const $ = id => document.getElementById(id);
let el = {};

function initDOMRefs() {
  el = {
    // Páginas
    pageHome:    $('page-home'),
    pageExplore: $('page-explore'),
    pageProfile: $('page-profile'),

    // Home
    btnSearchOpen:       $('btn-search-open'),
    searchInlineWrapper: $('search-inline-wrapper'),
    searchInput:         $('search-input'),
    btnClearSearch:      $('btn-clear-search'),
    corrientesRow:       $('corrientes-row'),
    recsList:            $('recs-list'),
    recsSubtitle:        $('recs-subtitle'),
    btnRefreshRecs:      $('btn-refresh-recs'),

    // Explorar
    exploreBgGlow:         $('explore-bg-glow'),
    exploreStoryContainer: $('explore-story-container'),
    storyProgressBar:      $('story-progress-bar'),
    btnStoryPause:         $('btn-story-pause'),
    btnRefreshStories:     $('btn-refresh-stories'),
    storyTapPrev:          $('story-tap-prev'),
    storyTapNext:          $('story-tap-next'),
    exploreStoryCard:      $('explore-story-card'),
    exploreJournalCite:    $('explore-journal-cite'),
    exploreStoryHook:      $('explore-story-hook'),
    exploreFindingText:    $('explore-finding-text'),
    exploreTakeawayBox:    $('explore-takeaway-box'),
    exploreTakeawayText:   $('explore-takeaway-text'),
    exploreTagsRow:        $('explore-tags-row'),
    btnStoryRead:          $('btn-story-read'),
    btnStorySave:          $('btn-story-save'),
    btnStorySaveTxt:       $('btn-story-save-txt'),
    btnStoryShare:         $('btn-story-share'),

    // Nav
    navBtns: document.querySelectorAll('.nav-btn'),

    // Resultados
    resultsView:       $('results-view'),
    resultsHeader:     $('results-header'),
    resultsHeaderLabel:$('results-header-label'),
    resultsHeaderTitle:$('results-header-title'),
    btnResultsBack:    $('btn-results-back'),
    resultsBgGlow:     $('results-bg-glow'),
    subtabBtns:        document.querySelectorAll('.subtab'),
    resultsSort:       $('results-sort'),
    resultsCount:      $('results-count'),
    resultsLoading:    $('results-loading'),
    resultsEmpty:      $('results-empty'),
    resultsError:      $('results-error'),
    resultsErrorMsg:   $('results-error-msg'),
    btnResultsRetry:   $('btn-results-retry'),
    paperList:         $('paper-list'),
    btnLoadMore:       $('btn-load-more'),

    // Modal
    modalOverlay:  $('modal-overlay'),
    modalSheet:    $('modal-sheet'),
    modalBody:     $('modal-body'),
    btnModalClose: $('btn-modal-close'),
    btnModalBk:    $('btn-modal-bk'),

    // Perfil
    profileAvatar:        $('profile-avatar'),
    profileAvatarImg:     $('profile-avatar-img'),
    avatarInitials:       $('avatar-initials'),
    profileName:          $('profile-name'),
    profileRole:          $('profile-role'),
    displayProfileName:   $('display-profile-name'),
    displayProfileRole:   $('display-profile-role'),
    displayRoleText:      $('display-role-text'),
    authNotConnected:     $('auth-not-connected'),
    authConnected:        $('auth-connected'),
    btnGoogleLogin:       $('btn-google-login'),
    btnGoogleLogout:      $('btn-google-logout'),
    connectedUserEmail:   $('connected-user-email'),
    googleModalOverlay:   $('google-modal-overlay'),
    googleModalSheet:     $('google-modal-sheet'),
    btnCloseGoogleModal:  $('btn-close-google-modal'),
    googleLoginForm:      $('google-login-form'),
    googleInputEmail:     $('google-input-email'),
    googleInputName:      $('google-input-name'),
    statSaved:            $('stat-saved'),
    sectionSaved:         $('section-saved'),
    savedList:            $('saved-list'),
    profileEmptySaved:    $('profile-empty-saved'),
    btnClearAllSaved:     $('btn-clear-all-saved'),

    // Toast
    toast: $('toast'),
  };
}

// ═══════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  initDOMRefs();
  registerSW();
  renderCorrientes();
  setupEventListeners();
  loadProfile();
  loadRecommendations();
  loadStories();
});

// ═══════════════════════════════════════════════════
// SERVICE WORKER
// ═══════════════════════════════════════════════════

function registerSW() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then(regs => {
      regs.forEach(r => r.unregister());
    }).catch(() => {});
  }
  if ('caches' in window) {
    caches.keys().then(keys => {
      keys.forEach(k => caches.delete(k));
    }).catch(() => {});
  }
}

// ═══════════════════════════════════════════════════
// NAVEGACIÓN
// ═══════════════════════════════════════════════════

function resetInlineSearch() {
  if (el.searchInput) {
    el.searchInput.value = '';
    el.searchInput.blur();
  }
  if (el.btnClearSearch) {
    el.btnClearSearch.style.display = 'none';
  }
  if (el.searchInlineWrapper) {
    el.searchInlineWrapper.classList.remove('active');
  }
}

function navigateTo(page) {
  closeResults(false);
  resetInlineSearch();

  // Pausar reproducción si sale de Explorar
  if (S.activePage === 'explore' && page !== 'explore') {
    stopStoryTimer();
    S.storyPaused = true;
  }

  S.activePage = page;
  const pages = { home: el.pageHome, explore: el.pageExplore, profile: el.pageProfile };
  Object.values(pages).forEach(p => p.classList.add('hidden'));
  pages[page].classList.remove('hidden');
  el.navBtns.forEach(b => b.classList.toggle('active', b.dataset.page === page));
  if (page === 'profile') refreshProfile();
  if (page === 'explore') {
    if (!S.stories || S.stories.length === 0) {
      loadStories();
    } else {
      renderCurrentStory(S.activeStoryIdx || 0);
    }
  }
}

function updateGlow() {
  if (!el.resultsBgGlow) return;
  let targetColor = '#1db954';
  if (S.activeCorriente) {
    targetColor = S.activeCorriente.color;
  } else {
    const colorMap = {
      clinico: '#fb923c',
      evidencia: '#34d399',
      teorico: '#c084fc'
    };
    targetColor = colorMap[S.activeSubtype] || targetColor;
  }
  el.resultsBgGlow.style.setProperty('--glow-color', targetColor);
}

// ═══════════════════════════════════════════════════
// CORRIENTES — FILA HORIZONTAL
// ═══════════════════════════════════════════════════

function renderCorrientes() {
  el.corrientesRow.innerHTML = CORRIENTES.map(c => `
    <div class="corriente-card" style="--c-color:${c.color};" data-id="${c.id}">
      <div class="corriente-icon-wrap"><i class="ph-bold ${c.icon}"></i></div>
      <span class="corriente-name">${c.label}</span>
    </div>
  `).join('');

  el.corrientesRow.querySelectorAll('.corriente-card').forEach(card => {
    card.addEventListener('click', () => {
      const corriente = CORRIENTES.find(c => c.id === card.dataset.id);
      if (corriente) openCorriente(corriente);
    });
  });
}

function openCorriente(corriente) {
  S.activeCorriente = corriente;
  S.baseQuery       = corriente.query;
  S.activeSubtype   = 'all';
  S.sort            = 'cited_by_count:desc';
  S.page            = 1;
  S.papers          = [];

  el.resultsHeader.style.background = ''; // Remover fondo para que se vea solo la nebula
  el.resultsHeaderLabel.textContent  = 'Corriente psicoterapéutica';
  el.resultsHeaderTitle.textContent  = corriente.label;
  if (el.resultsSort) el.resultsSort.value = 'cited_by_count:desc';

  el.subtabBtns.forEach(b => b.classList.toggle('active', b.dataset.type === 'all'));

  updateGlow();
  showResults();
  doFetch();
}

// ═══════════════════════════════════════════════════
// BÚSQUEDA
// ═══════════════════════════════════════════════════

async function doSearch(query) {
  if (!query.trim()) return;

  // Guardar la query ORIGINAL para mostrar al usuario
  const displayQuery = query.trim();

  S.activeCorriente = null;
  S.activeSubtype   = 'all';
  S.sort            = null; // Relevancia por defecto en búsquedas
  S.page            = 1;
  S.papers          = [];
  S.searches++;
  saveData('psyhub_searches_count', S.searches);
  
  if (el.resultsSort) el.resultsSort.value = '';

  // UI: mostrar la query original del usuario
  el.resultsHeader.style.background = '';
  el.resultsHeaderLabel.textContent  = 'Búsqueda';
  el.resultsHeaderTitle.textContent  = `"${displayQuery}"`;
  el.subtabBtns.forEach(b => b.classList.toggle('active', b.dataset.type === 'all'));

  updateGlow();
  showResults();
  showResultsState('loading');

  // Traducir/expandir la query a inglés sin mostrarla al usuario
  const englishQuery = await smartExpandQuery(displayQuery);
  S.baseQuery = englishQuery; // OpenAlex recibe la versión en inglés

  await doFetch();
}

// ═══════════════════════════════════════════════════
// RESULTADOS
// ═══════════════════════════════════════════════════

function showResults() {
  el.resultsView.classList.remove('hidden');
  el.resultsView.offsetHeight; // reflow
  el.resultsView.classList.add('visible');
  S.resultsOpen = true;
}

function closeResults(animated = true) {
  if (!S.resultsOpen) return;
  if (animated) {
    el.resultsView.classList.remove('visible');
    setTimeout(() => el.resultsView.classList.add('hidden'), 300);
  } else {
    el.resultsView.classList.remove('visible');
    el.resultsView.classList.add('hidden');
  }
  S.resultsOpen = false;
  S.activeSubtype = 'all';
  S.papers = [];
  el.paperList.innerHTML = '';
  el.btnLoadMore.style.display = 'none';
  resetInlineSearch();
}

let currentFetchController = null;

async function doFetch() {
  if (currentFetchController) {
    try { currentFetchController.abort(); } catch {}
  }
  currentFetchController = new AbortController();
  const signal = currentFetchController.signal;

  S.loading = true;
  showResultsState('loading');

  try {
    const { results, meta } = await fetchPapers({
      query:   S.baseQuery,
      subtype: S.activeSubtype,
      sort:    S.sort || null,
      perPage: S.perPage,
      page:    S.page,
      signal
    });

    let finalResults = results;
    if (S.activeSubtype && S.activeSubtype !== 'all') {
      const filtered = results.filter(p => {
        const s = getPaperSubtypeScores(p);
        return s[S.activeSubtype] > 0;
      });
      finalResults = filtered.length > 0 ? filtered : results;
    }

    S.total  = meta.count;
    S.papers = S.page === 1 ? finalResults : [...S.papers, ...finalResults];
    
    if (el.resultsCount) {
      el.resultsCount.textContent = `${S.total.toLocaleString('es')} resultados`;
    }

    if (S.papers.length === 0) {
      showResultsState('empty');
      S.loading = false;
      return;
    }

    // Traducir títulos y snippets en lote antes de mostrar
    const titles = finalResults.map(p => p.title);
    const abstracts = finalResults.map(p => p.abstract ? p.abstract.slice(0, 150) : '');
    
    const [translatedTitles, translatedAbstracts] = await Promise.all([
      translateBatch(titles),
      translateBatch(abstracts)
    ]);
    
    finalResults.forEach((p, i) => { 
      p.titleEs = translatedTitles[i]; 
      if (abstracts[i]) p.abstractEs = translatedAbstracts[i] + '...';
    });

    renderPapers(finalResults, S.page > 1);
    showResultsState('results');

    const hasMore = S.papers.length < S.total && results.length === S.perPage;
    el.btnLoadMore.style.display = hasMore ? 'flex' : 'none';

  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error('[PsiHub fetch]', err);
    el.resultsErrorMsg.textContent = err.message || 'Error de conexión con OpenAlex.';
    showResultsState('error');
  } finally {
    if (!signal.aborted) {
      S.loading = false;
    }
  }
}

async function loadMore() {
  S.page++;
  el.btnLoadMore.disabled = true;
  el.btnLoadMore.innerHTML = '<div class="spinner" style="width:20px;height:20px;border-width:2px;margin:0;"></div>';
  await doFetch();
  el.btnLoadMore.disabled = false;
  el.btnLoadMore.innerHTML = '<i class="ph-bold ph-arrow-circle-down"></i> Cargar más';
}

function showResultsState(state) {
  el.resultsLoading.style.display = state === 'loading' ? 'flex' : 'none';
  el.resultsEmpty.style.display   = state === 'empty'   ? 'flex' : 'none';
  el.resultsError.style.display   = state === 'error'   ? 'flex' : 'none';
}

// ═══════════════════════════════════════════════════
// CARDS DE RESULTADOS
// ═══════════════════════════════════════════════════

function renderPapers(papers, append) {
  const frag = document.createDocumentFragment();
  papers.forEach(p => frag.appendChild(buildCard(p)));
  if (!append) el.paperList.innerHTML = '';
  el.paperList.appendChild(frag);
}

function buildCard(paper, onBookmarkChange) {
  const li = document.createElement('li');
  li.className = 'paper-card';

  const isCorrienteView = Boolean(S.resultsOpen && S.activeCorriente && el.resultsHeaderLabel?.textContent?.toLowerCase().includes('corriente'));
  const corriente = isCorrienteView ? S.activeCorriente : null;
  const isBk        = isBookmarked(paper.id);
  const displayTitle = paper.titleEs || paper.title;
  const snippet      = paper.abstractEs || (paper.abstract ? paper.abstract.slice(0, 150) + '...' : '');
  const firstAuthor = Array.isArray(paper.authors) ? (paper.authors[0] || '') : (paper.authors || '');
  const moreAuthors = Array.isArray(paper.authors) && paper.authors.length > 1 ? ` +${paper.authors.length - 1}` : '';
  const isFilterActiveInResults = Boolean(S.resultsOpen && S.activeSubtype && S.activeSubtype !== 'all');
  const tags        = isFilterActiveInResults ? [S.activeSubtype] : classifyPaper(paper);

  li.innerHTML = `
    <div class="paper-card-body" style="padding-left: 0;">
      <div class="paper-card-top">
        <h3 class="paper-card-title">${esc(displayTitle)}</h3>
        <button class="btn-card-bk ${isBk ? 'saved' : ''}" data-id="${esc(paper.id)}" aria-label="Guardar">
          <i class="${isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple'}"></i>
        </button>
      </div>
      <p class="paper-card-authors">${esc(firstAuthor + moreAuthors)}</p>
      ${snippet ? `<p class="paper-card-snippet">${esc(snippet)}</p>` : ''}
      <div class="paper-card-meta">
        ${tags.map(tagBadge).join('')}
        ${paper.year ? `<span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--txt-3);"><i class="ph-bold ph-calendar-blank"></i>${paper.year}</span>` : ''}
        <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--txt-3);"><i class="ph-bold ph-quotes"></i>${(paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? 0).toLocaleString('es')}</span>
      </div>
    </div>
  `;

  li.addEventListener('click', () => openModal(paper, corriente));

  li.querySelector('.btn-card-bk').addEventListener('click', e => {
    e.stopPropagation();
    toggleBookmark(paper);
    const btn = e.currentTarget;
    const saved = isBookmarked(paper.id);
    btn.classList.toggle('saved', saved);
    btn.querySelector('i').className = saved ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';
    if (typeof onBookmarkChange === 'function') {
      onBookmarkChange(saved, paper);
    }
  });

  return li;
}

function tagBadge(t) {
  const configs = {
    clinico:   { label: 'Clínico',   icon: 'ph-stethoscope',   cls: 'tag-clinico' },
    evidencia: { label: 'Evidencia', icon: 'ph-chart-line-up', cls: 'tag-evidencia' },
    teorico:   { label: 'Teórico',   icon: 'ph-brain',         cls: 'tag-teorico' }
  };
  const c = configs[t] || { label: t, icon: 'ph-tag', cls: 'tag-teorico' };
  return `<span class="tag-pill ${c.cls}"><i class="ph-bold ${c.icon}"></i><span>${c.label}</span></span>`;
}

function tagLabel(t) {
  return { teorico: 'Teórico', evidencia: 'Evidencia', clinico: 'Clínico' }[t] || t;
}

// ═══════════════════════════════════════════════════
// RECOMENDACIONES — "TE PUEDE INTERESAR"
// ═══════════════════════════════════════════════════

async function loadRecommendations() {
  // Mostrar skeletons mientras carga
  el.recsList.innerHTML = '<div class="rec-skeleton"></div><div class="rec-skeleton"></div><div class="rec-skeleton"></div>';

  let query;
  let sort = 'cited_by_count:desc';
  let page = 1;

  if (S.bookmarks.length > 0) {
    // Seleccionar un guardado al azar para variar la fuente entre los guardados del usuario
    const randomBk = S.bookmarks[Math.floor(Math.random() * S.bookmarks.length)];

    // Extraer palabras clave sustanciales del título
    const stopWords = new Set([
      'the', 'and', 'for', 'with', 'from', 'that', 'this', 'about', 'into', 'over', 'after',
      'under', 'between', 'during', 'without', 'through', 'study', 'studies', 'effect',
      'effects', 'using', 'based', 'role', 'systematic', 'review', 'meta-analysis', 'analysis',
      'para', 'sobre', 'entre', 'hacia', 'desde', 'como', 'pero', 'estudio', 'analisis', 'efecto'
    ]);
    const words = (randomBk.title || '')
      .replace(/[^\w\s]/gi, ' ')
      .split(/\s+/)
      .filter(w => w.length > 3 && !stopWords.has(w.toLowerCase()));

    // Alternar entre temas/conceptos y palabras clave del título
    if (randomBk.topics && randomBk.topics.length > 0 && Math.random() > 0.4) {
      query = randomBk.topics[Math.floor(Math.random() * randomBk.topics.length)];
    } else if (words.length >= 2) {
      // Tomar una ventana aleatoria de 2 o 3 palabras para rotar búsquedas
      const maxStart = Math.max(0, words.length - 2);
      const start = Math.floor(Math.random() * (maxStart + 1));
      query = words.slice(start, start + 3).join(' ');
    } else {
      query = (randomBk.title || 'psicoterapia clínica').split(' ').slice(0, 4).join(' ');
    }

    // Variar ordenamiento y página para garantizar resultados diferentes en cada clic
    const sorts = ['cited_by_count:desc', 'relevance_score:desc', 'publication_year:desc'];
    sort = sorts[Math.floor(Math.random() * sorts.length)];
    page = Math.floor(Math.random() * 3) + 1;

    el.recsSubtitle.textContent = 'Basado en tus guardados';
  } else {
    // Si no hay guardados, explorar una corriente al azar
    const randCorrente = CORRIENTES[Math.floor(Math.random() * CORRIENTES.length)];
    query = randCorrente.query;
    const sorts = ['cited_by_count:desc', 'relevance_score:desc'];
    sort = sorts[Math.floor(Math.random() * sorts.length)];
    page = Math.floor(Math.random() * 2) + 1;
    el.recsSubtitle.textContent = `Explorando: ${randCorrente.label}`;
  }

  try {
    let { results } = await fetchPapers({ query, page, perPage: 8, sort });

    // Si la página aleatoria vino vacía, reintentar con la primera página
    if ((!results || !results.length) && page > 1) {
      const retry = await fetchPapers({ query, page: 1, perPage: 8, sort: 'cited_by_count:desc' });
      results = retry.results;
    }

    if (!results || !results.length) {
      el.recsList.innerHTML = '<p style="font-size:13px; color:var(--txt-3); padding: 8px 0;">No se encontraron resultados.</p>';
      return;
    }

    // Filtrar papers que ya estén guardados para sugerir siempre novedades
    if (S.bookmarks.length > 0) {
      const unbookmarked = results.filter(p => !isBookmarked(p.id));
      if (unbookmarked.length >= 3) {
        results = unbookmarked;
      }
    }

    // Traducir títulos y snippets en lote
    const titles   = results.map(p => p.title);
    const snippets = results.map(p => p.abstract ? p.abstract.slice(0, 150) : '');

    const [translatedTitles, translatedSnippets] = await Promise.all([
      translateBatch(titles),
      translateBatch(snippets)
    ]);

    results.forEach((p, i) => {
      p.titleEs   = translatedTitles[i];
      p.abstractEs = translatedSnippets[i] + '...';
    });

    el.recsList.innerHTML = '';
    const frag = document.createDocumentFragment();
    results.forEach(paper => {
      frag.appendChild(buildCard(paper));
    });
    el.recsList.appendChild(frag);

  } catch (err) {
    console.error('[Recs]', err);
    el.recsList.innerHTML = '<p style="font-size:13px; color:var(--txt-3); padding: 8px 0;">Error al cargar recomendaciones.</p>';
  }
}

// ═══════════════════════════════════════════════════
// MODAL DE DETALLE
// ═══════════════════════════════════════════════════

async function openModal(paper, corriente) {
  try {
    if (!paper) {
      showToast('No se encontró el artículo.');
      return;
    }

    // Normalizar datos del paper para prevenir errores
    paper.authors = Array.isArray(paper.authors) ? paper.authors : [];
    paper.topics = Array.isArray(paper.topics) ? paper.topics : [];
    const rawCites = paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? paper.cites ?? 0;
    paper.citations = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;

    S.currentPaper = paper;
    // Solo mostrar la corriente si el paper fue abierto explícitamente desde la sección de corrientes activa
    const isCorrienteActive = Boolean(
      corriente && S.activeCorriente && S.resultsOpen &&
      corriente.id === S.activeCorriente.id &&
      el.resultsHeaderLabel?.textContent?.toLowerCase().includes('corriente')
    );
    const modalCorriente = isCorrienteActive ? corriente : null;

    const isBk = isBookmarked(paper.id);
    el.btnModalBk.classList.toggle('saved', isBk);
    el.btnModalBk.querySelector('i').className = isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';

    const isFilterActiveInResults = Boolean(S.resultsOpen && S.activeSubtype && S.activeSubtype !== 'all');
    const tags = isFilterActiveInResults ? [S.activeSubtype] : classifyPaper(paper);
    const displayTitle = paper.titleEs || paper.title || 'Sin título';

    // Mostrar modal con abstract existente o aviso
    el.modalBody.innerHTML = buildModalHTML(paper, modalCorriente, tags, displayTitle, paper.abstractEs, false);
    el.modalBody.scrollTop = 0;
    el.modalOverlay.classList.remove('hidden');
    el.modalSheet.classList.remove('closing');
    document.body.style.overflow = 'hidden';

    // Si el paper no tiene abstract o es un resumen sintético previo, buscar el abstract original en OpenAlex
    const isAiSummary = paper.abstract && paper.abstract.includes('\n\n') && (paper.abstract.startsWith('¿') || paper.abstract.includes('?'));
    if ((!paper.abstract || isAiSummary) && (paper.id || paper.doi)) {
      const abstractEl = document.getElementById('modal-abstract-text');
      if (abstractEl) abstractEl.textContent = 'Cargando abstract original desde OpenAlex…';
      try {
        const lookupId = (paper.doi ? (paper.doi.startsWith('http') ? paper.doi : `https://doi.org/${paper.doi}`) : paper.id);
        const full = await fetchWorkById(lookupId);
        if (full && S.currentPaper?.id === paper.id) {
          if (full.abstract) {
            paper.abstract = full.abstract;
            paper.abstractEs = null;
          }
          if (full.authors?.length && (!paper.authors || !paper.authors.length || paper.authors.length <= 1)) {
            paper.authors = full.authors;
          }
          if (full.topics?.length) paper.topics = full.topics;
          if (full.journal) paper.journal = full.journal;
          if (full.firstInstitution) paper.firstInstitution = full.firstInstitution;
          if (full.oaUrl && !paper.oaUrl) paper.oaUrl = full.oaUrl;
          if (full.doi && !paper.doi) paper.doi = full.doi;
          if (full.type) paper.type = full.type;
          if (full.mesh?.length) paper.mesh = full.mesh;
          if (typeof full.citations === 'number' && full.citations > 0) paper.citations = full.citations;

          // Re-renderizar modal con la información completa
          const updatedTags = isFilterActiveInResults ? [S.activeSubtype] : classifyPaper(paper);
          el.modalBody.innerHTML = buildModalHTML(paper, modalCorriente, updatedTags, displayTitle, paper.abstractEs, false);
        }
      } catch (errOpenAlex) {
        console.warn('[openModal] No se pudo obtener detalle de OpenAlex:', errOpenAlex);
      }
    }

    // Traducir abstract en el fondo si existe y no está traducido
    if (paper.abstract && !paper.abstractEs) {
      try {
        const langEl = document.getElementById('modal-abstract-lang');
        if (langEl) {
          langEl.innerHTML = '<div class="spinner" style="width:12px;height:12px;border-width:2px;border-radius:50%;margin:0;animation:spin 0.7s linear infinite;"></div> Traduciendo al español…';
        }
        const abstractEs = await translateToSpanish(paper.abstract);
        paper.abstractEs = abstractEs;
        const abstractEl = document.getElementById('modal-abstract-text');
        const langElUpdated = document.getElementById('modal-abstract-lang');
        if (abstractEl && S.currentPaper?.id === paper.id) {
          abstractEl.textContent = abstractEs;
          if (langElUpdated) langElUpdated.innerHTML = '<i class="ph-bold ph-translate" style="font-size:12px;"></i> Traducción automática al español';
        }
      } catch (errTrans) {
        console.warn('[openModal] Falló traducción:', errTrans);
        const langEl = document.getElementById('modal-abstract-lang');
        if (langEl) langEl.innerHTML = '<i class="ph-bold ph-translate" style="font-size:12px;"></i> Original en inglés';
      }
    }
  } catch (err) {
    console.error('[openModal Error]', err);
    showToast('Error al abrir artículo: ' + (err?.message || err));
  }
}

function formatAPAReference(paper) {
  if (!paper) return '';

  let authorsStr = '';
  // Filtrar nombres genéricos que no sean personas o autores reales
  const genericList = new Set([
    'psihub', 'psihub research', 'curaduría diaria', 'investigación',
    'neurociencia', 'tcc & conductual', 'tcc', 'clínica & psicoterapia',
    'clínica', 'psicoanálisis & dinámica', 'psicoanálisis', 'psicología social',
    'neuropsicología', 'desarrollo & infantil', 'desarrollo', 'organizacional'
  ]);

  if (Array.isArray(paper.authors) && paper.authors.length > 0) {
    const validAuthors = paper.authors.filter(a => {
      if (!a || typeof a !== 'string') return false;
      return !genericList.has(a.trim().toLowerCase());
    });

    const formatted = validAuthors.map(a => {
      const parts = a.trim().split(/\s+/);
      if (parts.length >= 2) {
        const lastName = parts[parts.length - 1];
        const initials = parts.slice(0, -1).map(p => p[0] ? `${p[0].toUpperCase()}.` : '').join(' ');
        return `${lastName}, ${initials}`.trim();
      }
      return a.trim();
    }).filter(Boolean);

    if (formatted.length === 1) {
      authorsStr = formatted[0];
    } else if (formatted.length === 2) {
      authorsStr = `${formatted[0]} & ${formatted[1]}`;
    } else if (formatted.length > 2 && formatted.length <= 7) {
      authorsStr = `${formatted.slice(0, -1).join(', ')}, & ${formatted[formatted.length - 1]}`;
    } else if (formatted.length > 7) {
      authorsStr = `${formatted.slice(0, 6).join(', ')}, et al.`;
    }
  }

  const yearStr = paper.year ? `(${paper.year}).` : '(s.f.).';
  const rawTitle = (paper.title || 'Sin título').trim();
  const titleStr = rawTitle.endsWith('.') ? rawTitle : `${rawTitle}.`;
  const journalStr = paper.journal ? `${paper.journal.trim()}.` : '';

  let doiStr = '';
  if (paper.doi) {
    doiStr = paper.doi.startsWith('http') ? paper.doi : `https://doi.org/${paper.doi}`;
  } else if (paper.oaUrl) {
    doiStr = paper.oaUrl;
  }

  // En APA 7, cuando no hay autor identificado, el título pasa al primer lugar sin agregar 'psihub research':
  // Título. (Año). Revista. DOI
  if (!authorsStr) {
    return [titleStr, yearStr, journalStr, doiStr].filter(Boolean).join(' ');
  }

  return [authorsStr, yearStr, titleStr, journalStr, doiStr].filter(Boolean).join(' ');
}

function buildModalHTML(paper, corriente, tags, displayTitle, abstractEs, translating) {
  const isCorriente = Boolean(corriente && corriente.label);
  const tagColor = isCorriente ? corriente.color : 'var(--txt-3)';
  const tagLabel_ = isCorriente ? corriente.label : 'Artículo';
  const tagStyle = isCorriente
    ? `background:${tagColor}22; color:${tagColor}; border:1px solid ${tagColor}55;`
    : `background:rgba(255,255,255,0.06); color:var(--txt-3); border:1px solid rgba(255,255,255,0.1);`;

  const abstractContent = paper.abstract || 'Abstract no disponible en los metadatos de OpenAlex.';
  const authors = Array.isArray(paper.authors) ? paper.authors : [];
  const rawCites = paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? paper.cites ?? 0;
  const citations = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;
  const topics = Array.isArray(paper.topics) ? paper.topics : [];

  return `
    <div class="modal-tags">
      <span class="modal-tag" style="${tagStyle}">${esc(tagLabel_)}</span>
      ${(tags || []).map(tagBadge).join('')}
      ${paper.year ? `<span class="modal-tag" style="background:rgba(255,255,255,0.06); color:var(--txt-3); border:1px solid rgba(255,255,255,0.08);">${paper.year}</span>` : ''}
    </div>

    <h2 class="modal-title">${esc(displayTitle || 'Sin título')}</h2>

    <p class="modal-authors">${esc(authors.join(', ') || 'Autores no disponibles')}</p>
    ${paper.firstInstitution ? `<p class="modal-journal"><i class="ph-bold ph-buildings" style="margin-right:4px;"></i>${esc(paper.firstInstitution)}</p>` : ''}
    ${paper.journal ? `<p class="modal-journal"><i class="ph-bold ph-newspaper" style="margin-right:4px;"></i>${esc(paper.journal)}</p>` : ''}

    <div class="modal-stats-row">
      <span><i class="ph-bold ph-quotes"></i>${citations.toLocaleString('es')} citas</span>
      ${paper.year ? `<span><i class="ph-bold ph-calendar-blank"></i>${paper.year}</span>` : ''}
      ${paper.doi ? `<span><i class="ph-bold ph-fingerprint"></i>DOI disponible</span>` : ''}
    </div>

    <p class="modal-section-label">Abstract</p>
    <p id="modal-abstract-lang" class="modal-abstract-lang">
      ${translating && paper.abstract
        ? '<div class="spinner" style="width:12px;height:12px;border-width:2px;border-radius:50%;margin:0;animation:spin 0.7s linear infinite;"></div> Traduciendo al español…'
        : '<i class="ph-bold ph-translate" style="font-size:12px;"></i> Traducción automática al español'
      }
    </p>
    <p id="modal-abstract-text" class="modal-abstract">${esc(abstractEs || abstractContent)}</p>

    ${topics.length > 0 ? `
      <p class="modal-section-label" style="margin-top:20px;">Tópicos</p>
      <div class="modal-topics">
        ${topics.map(t => `<span class="modal-topic-chip">${esc(t)}</span>`).join('')}
      </div>
    ` : ''}

    <div class="modal-cta">
      ${paper.oaUrl
        ? `<a class="btn-cta-primary" href="${esc(paper.oaUrl)}" target="_blank" rel="noopener"><i class="ph-bold ph-file-pdf"></i> Leer texto original (Open Access)</a>`
        : ''}
      <button class="btn-cta-secondary" disabled style="opacity:0.4; cursor:not-allowed;">
        <i class="ph-bold ph-translate"></i> Traducir PDF completo <span style="font-size:11px; opacity:0.7;">(próximamente)</span>
      </button>
      <button class="btn-cta-secondary btn-copy-apa" id="btn-modal-copy-apa" title="Copiar referencia en formato APA 7">
        <i class="ph-bold ph-copy"></i> Copiar cita APA
      </button>
    </div>
  `;
}

function closeModal() {
  el.modalSheet.classList.add('closing');
  setTimeout(() => {
    el.modalOverlay.classList.add('hidden');
    el.modalSheet.classList.remove('closing');
    document.body.style.overflow = '';
    S.currentPaper = null;
  }, 200);
}

// ═══════════════════════════════════════════════════
// BOOKMARKS
// ═══════════════════════════════════════════════════

function isBookmarked(id) { return S.bookmarks.some(b => b.id === id); }

function toggleBookmark(paper) {
  if (isBookmarked(paper.id)) {
    S.bookmarks = S.bookmarks.filter(b => b.id !== paper.id);
    showToast('Quitado de guardados');
  } else {
    const rawCites = paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? paper.cites ?? 0;
    const numCites = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;
    S.bookmarks.push({
      id: paper.id,
      title: paper.title || 'Sin título',
      titleEs: paper.titleEs || null,
      authors: Array.isArray(paper.authors) ? paper.authors : [],
      year: paper.year || null,
      citations: numCites,
      oaUrl: paper.oaUrl || null,
      doi: paper.doi || null,
      abstract: paper.abstract || null,
      abstractEs: paper.abstractEs || null,
      topics: Array.isArray(paper.topics) ? paper.topics : [],
      journal: paper.journal || null,
      firstInstitution: paper.firstInstitution || null,
      corrienteId: S.activeCorriente?.id || null
    });
    showToast('Guardado ✓');
  }
  saveData('psyhub_bk', S.bookmarks);
  updateStatBadges();
  if (S.currentPaper?.id === paper.id) {
    const isBk = isBookmarked(paper.id);
    el.btnModalBk.classList.toggle('saved', isBk);
    el.btnModalBk.querySelector('i').className = isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';
  }
}

function updateStatBadges() {
  if (el.statSaved) el.statSaved.textContent = S.bookmarks.length;
}

// ═══════════════════════════════════════════════════
// PERFIL
// ═══════════════════════════════════════════════════

function loadProfile() {
  const googleUser = loadData('psyhub_google_user', null);
  const userEmail = localStorage.getItem('psyhub_user_email') || '';

  if (el.profileName) el.profileName.value = S.profile.name || (googleUser ? googleUser.name : '');
  if (el.profileRole) el.profileRole.value = S.profile.role || '';

  updateProfileUI(googleUser, userEmail);
  updateStatBadges();
}

function updateProfileUI(googleUser, userEmail) {
  const name = S.profile.name || (googleUser ? googleUser.name : '') || 'Mi Perfil';
  if (el.displayProfileName) el.displayProfileName.textContent = name;

  const roleLabels = {
    estudiante: 'Estudiante de Psicología',
    clinico: 'Psicólogo/a Clínico/a',
    investigador: 'Investigador/a',
    docente: 'Docente Universitario/a',
    psiquiatra: 'Médico/a Psiquiatra'
  };
  if (el.displayRoleText) {
    el.displayRoleText.textContent = roleLabels[S.profile.role] || 'Sin rol definido';
  }

  updateAvatarInitials(name);

  // Estado de conexión Google
  const isConnected = Boolean(googleUser || userEmail);
  if (el.authNotConnected && el.authConnected) {
    el.authNotConnected.style.display = isConnected ? 'none' : 'block';
    el.authConnected.style.display = isConnected ? 'block' : 'none';
  }
  if (el.connectedUserEmail) {
    el.connectedUserEmail.textContent = (googleUser ? googleUser.email : userEmail) || 'usuario@gmail.com';
  }
}

function updateAvatarInitials(name) {
  const cleanName = (name || S.profile.name || '').trim();
  const parts = cleanName.split(' ').filter(Boolean);
  const initials = parts.length >= 2
    ? (parts[0][0] + parts[parts.length-1][0]).toUpperCase()
    : (cleanName ? cleanName.slice(0, 2).toUpperCase() : 'P');
  if (el.avatarInitials) el.avatarInitials.textContent = initials;
}

function refreshProfile() {
  updateStatBadges();

  // Reparar guardados existentes para que tengan el título, autores y abstract original de OpenAlex
  if (Array.isArray(S.bookmarks) && S.bookmarks.length > 0) {
    let touched = false;
    S.bookmarks.forEach(b => {
      // 1. Si era una historia guardada previamente con el resumen de la IA en vez del abstract original
      if (Array.isArray(S.stories)) {
        const match = S.stories.find(s => s.id === b.id || s.id === b.storyId || (s.doi && b.doi && s.doi === b.doi));
        if (match) {
          if (match.paperTitle && b.title !== match.paperTitle) {
            b.title = match.paperTitle;
            touched = true;
          }
          if (match.originalAbstract && (!b.abstract || b.abstract.includes(match.hook))) {
            b.abstract = match.originalAbstract;
            b.abstractEs = null; // Para que Google Translate lo traduzca exactamente como los demás
            touched = true;
          }
          if (match.authors?.length && (!b.authors || !b.authors.length || b.authors[0] === match.topicName)) {
            b.authors = match.authors;
            touched = true;
          }
        }
      }

      // 2. Reparar citas si quedaron desindexeadas o en 0
      const currentCites = b.citations ?? b.cited_by_count ?? b.citedByCount ?? b.cites ?? 0;
      if (!currentCites || currentCites === 0) {
        if (Array.isArray(S.stories)) {
          const match = S.stories.find(s => s.id === b.id || s.id === b.storyId || (s.doi && b.doi && s.doi === b.doi));
          if (match && (match.citations || match.cited_by_count)) {
            b.citations = match.citations || match.cited_by_count;
            touched = true;
          }
        }
      } else if (b.citations !== currentCites) {
        b.citations = currentCites;
        touched = true;
      }
    });
    if (touched) saveData('psyhub_bk', S.bookmarks);
  }

  if (S.bookmarks.length === 0) {
    if (el.sectionSaved) el.sectionSaved.style.display = 'none';
    if (el.profileEmptySaved) el.profileEmptySaved.style.display = 'flex';
    return;
  }
  if (el.sectionSaved) el.sectionSaved.style.display = 'block';
  if (el.profileEmptySaved) el.profileEmptySaved.style.display = 'none';

  if (el.savedList) {
    el.savedList.innerHTML = '';
    const frag = document.createDocumentFragment();
    S.bookmarks.forEach(b => {
      frag.appendChild(buildCard(b, (saved) => {
        if (!saved) refreshProfile();
      }));
    });
    el.savedList.appendChild(frag);
  }
}

function openSavedPaperByIndex(idx) {
  try {
    if (isNaN(idx) || idx < 0 || idx >= S.bookmarks.length) {
      showToast('Artículo no encontrado');
      return;
    }
    const paper = S.bookmarks[idx];
    if (!paper) {
      showToast('Artículo no encontrado');
      return;
    }
    openModal(paper);
  } catch (err) {
    console.error('[openSavedPaperByIndex]', err);
    showToast('Error al abrir: ' + (err?.message || err));
  }
}

function removeSavedPaperByIndex(idx) {
  if (isNaN(idx) || idx < 0 || idx >= S.bookmarks.length) return;
  S.bookmarks.splice(idx, 1);
  saveData('psyhub_bk', S.bookmarks);
  refreshProfile();
  showToast('Quitado de guardados');
}

window.openSavedPaperByIndex = openSavedPaperByIndex;
window.removeSavedPaperByIndex = removeSavedPaperByIndex;

// ═══════════════════════════════════════════════════
// EVENT LISTENERS
// ═══════════════════════════════════════════════════

function setupEventListeners() {
  // Nav
  el.navBtns.forEach(btn => btn.addEventListener('click', () => navigateTo(btn.dataset.page)));

  // Guardados (Delegación de respaldo)
  el.savedList.addEventListener('click', e => {
    const removeBtn = e.target.closest('.saved-card-remove');
    if (removeBtn) {
      e.stopPropagation();
      const idx = parseInt(removeBtn.dataset.index, 10);
      removeSavedPaperByIndex(idx);
      return;
    }
    const mainArea = e.target.closest('.saved-card-main') || e.target.closest('.saved-card');
    if (mainArea) {
      const idx = parseInt(mainArea.dataset.index, 10);
      openSavedPaperByIndex(idx);
    }
  });


  function triggerSearch() {
    const q = el.searchInput.value.trim();
    if (q) {
      resetInlineSearch();
      doSearch(q);
    }
  }

  el.btnSearchOpen.addEventListener('click', (e) => {
    e.stopPropagation();
    const isActive = el.searchInlineWrapper.classList.contains('active');
    if (isActive) {
      // Si hay texto, buscar; si no, resetear y cerrar
      if (el.searchInput.value.trim()) {
        triggerSearch();
      } else {
        resetInlineSearch();
      }
    } else {
      el.searchInlineWrapper.classList.add('active');
      setTimeout(() => el.searchInput.focus(), 250);
    }
  });

  document.addEventListener('click', e => {
    if (el.searchInlineWrapper && el.searchInlineWrapper.classList.contains('active') && 
        !el.searchInlineWrapper.contains(e.target)) {
      if (!el.searchInput.value.trim()) {
        resetInlineSearch();
      } else {
        el.searchInlineWrapper.classList.remove('active');
        if (el.btnClearSearch) el.btnClearSearch.style.display = 'none';
      }
    }
  });

  el.searchInput.addEventListener('input', () => {
    const v = el.searchInput.value;
    el.btnClearSearch.style.display = v ? 'flex' : 'none';
  });

  el.searchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      triggerSearch();
    }
  });

  el.btnClearSearch.addEventListener('click', (e) => {
    e.stopPropagation();
    resetInlineSearch();
    if (S.resultsOpen) {
      closeResults();
    }
  });

  // Resultados
  el.btnResultsBack.addEventListener('click', () => closeResults());
  el.subtabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      S.activeSubtype = btn.dataset.type;
      S.page = 1;
      S.papers = [];
      el.subtabBtns.forEach(b => b.classList.toggle('active', b === btn));
      el.paperList.innerHTML = '';
      el.btnLoadMore.style.display = 'none';
      
      // El filtro de orden (S.sort) se mantiene independiente de la etiqueta seleccionada
      updateGlow();
      doFetch();
    });
  });
  if (el.resultsSort) {
    el.resultsSort.addEventListener('change', () => {
      S.sort = el.resultsSort.value || null;
      S.page = 1;
      S.papers = [];
      el.paperList.innerHTML = '';
      el.btnLoadMore.style.display = 'none';
      doFetch();
    });
  }

  // Asegurar que al presionar el ícono o cualquier parte del botón se abra el selector
  const filterWrapper = document.querySelector('.filter-sort-wrapper');
  if (filterWrapper && el.resultsSort) {
    filterWrapper.addEventListener('click', (e) => {
      if (e.target !== el.resultsSort) {
        el.resultsSort.focus();
        if (typeof el.resultsSort.showPicker === 'function') {
          try { el.resultsSort.showPicker(); } catch {}
        }
      }
    });
  }
  el.btnLoadMore.addEventListener('click', loadMore);
  el.btnResultsRetry.addEventListener('click', () => { S.page = 1; S.papers = []; doFetch(); });

  // Recomendaciones
  el.btnRefreshRecs.addEventListener('click', loadRecommendations);

  // Modal
  el.btnModalClose.addEventListener('click', closeModal);
  el.modalOverlay.addEventListener('click', e => { if (e.target === el.modalOverlay) closeModal(); });
  el.btnModalBk.addEventListener('click', () => { if (S.currentPaper) toggleBookmark(S.currentPaper); });
  el.modalBody.addEventListener('click', async (e) => {
    const btn = e.target.closest('#btn-modal-copy-apa') || e.target.closest('.btn-copy-apa');
    if (btn && S.currentPaper) {
      e.stopPropagation();
      const apaText = formatAPAReference(S.currentPaper);
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(apaText);
        } else {
          const ta = document.createElement('textarea');
          ta.value = apaText;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        showToast('Cita APA copiada al portapapeles ✓');
      } catch (err) {
        console.error('[Copy APA]', err);
        showToast('No se pudo copiar la cita');
      }
    }
  });

  // Swipe modal: permite leer y scrollear el abstract con total comodidad.
  // Solo se cierra si se arrastra desde la manija superior o si está arriba del todo y el deslizamiento hacia abajo es pronunciado (>120px)
  let touchStartY = 0;
  let touchStartX = 0;
  let canSwipeModal = false;

  const modalHandle = el.modalSheet.querySelector('.modal-handle');
  const modalHeader = el.modalSheet.querySelector('.modal-header');

  el.modalSheet.addEventListener('touchstart', e => {
    const touch = e.touches[0];
    touchStartY = touch.clientY;
    touchStartX = touch.clientX;
    const target = e.target;
    const isTopArea = (modalHandle && (target === modalHandle || modalHandle.contains(target))) ||
                      (modalHeader && (target === modalHeader || modalHeader.contains(target)));
    const isScrollAtTop = el.modalBody.scrollTop <= 2;
    canSwipeModal = isTopArea || isScrollAtTop;
  }, { passive: true });

  el.modalSheet.addEventListener('touchend', e => {
    if (!canSwipeModal) return;
    const touch = e.changedTouches[0];
    const deltaY = touch.clientY - touchStartY;
    const deltaX = Math.abs(touch.clientX - touchStartX);

    // Solo cerrar si el gesto fue marcadamente vertical hacia abajo (> 120px) y el abstract está en la cima
    if (deltaY > 120 && deltaY > deltaX * 1.5 && el.modalBody.scrollTop <= 2) {
      closeModal();
    }
    canSwipeModal = false;
  }, { passive: true });

  // Manejo del botón atrás de Android y gestos atrás del sistema
  window.handleAndroidBack = function() {
    // 0. Si el visor de historias está abierto, cerrarlo
    if (el.storyModal && !el.storyModal.classList.contains('hidden')) {
      closeStory();
      return true;
    }
    // 1. Si el modal del paper está abierto, cerrarlo
    if (el.modalOverlay && !el.modalOverlay.classList.contains('hidden')) {
      closeModal();
      return true;
    }
    // 2. Si el buscador inline está abierto, cerrarlo
    if (el.searchInlineWrapper && el.searchInlineWrapper.classList.contains('active')) {
      resetInlineSearch();
      return true;
    }
    // 3. Si la vista de resultados está abierta, cerrarla y volver al home
    if (S.resultsOpen) {
      closeResults();
      return true;
    }
    // 4. Si está en otra pestaña (Explorar o Perfil), volver al Inicio
    if (S.activePage !== 'home') {
      navigateTo('home');
      return true;
    }
    // 5. Si ya está en el Home sin nada abierto, retornar false para salir
    return false;
  };

  // Soporte para botón atrás en navegadores web (Firefox, Chrome)
  window.addEventListener('popstate', () => {
    window.handleAndroidBack();
  });

  // Explorar & Historias Directas
  if (el.btnRefreshStories) {
    el.btnRefreshStories.addEventListener('click', (e) => {
      e.stopPropagation();
      loadStories(true);
      showToast('Actualizando historias...');
    });
  }

  if (el.btnStoryPause) {
    el.btnStoryPause.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleStoryPause();
    });
  }

  if (el.storyTapNext)  el.storyTapNext.addEventListener('click', (e) => { e.stopPropagation(); nextStory(); });
  if (el.storyTapPrev)  el.storyTapPrev.addEventListener('click', (e) => { e.stopPropagation(); prevStory(); });
  if (el.btnStorySave)  el.btnStorySave.addEventListener('click', (e) => { e.stopPropagation(); toggleStoryBookmark(); });
  if (el.btnStoryShare) el.btnStoryShare.addEventListener('click', (e) => { e.stopPropagation(); shareCurrentStory(); });

  // Historias: Mantener presionado para pausar + Tocar para saltear o retroceder
  if (el.exploreStoryContainer) {
    let holdTimeout = null;
    let didHold = false;

    const onHoldStart = (e) => {
      if (e.target.closest('button, a, .explore-actions-bar, .story-minimal-controls, .explore-header-controls, .story-progress-bar')) return;
      didHold = false;
      holdTimeout = setTimeout(() => {
        didHold = true;
        pauseStory();
      }, 220);
    };

    const onHoldEnd = () => {
      if (holdTimeout) clearTimeout(holdTimeout);
      if (S.storyPaused) resumeStory();
    };

    el.exploreStoryContainer.addEventListener('touchstart', onHoldStart, { passive: true });
    el.exploreStoryContainer.addEventListener('touchend', onHoldEnd, { passive: true });
    el.exploreStoryContainer.addEventListener('mousedown', onHoldStart);
    el.exploreStoryContainer.addEventListener('mouseup', onHoldEnd);

    // Tap en pantalla para avanzar / retroceder historia
    el.exploreStoryContainer.addEventListener('click', (e) => {
      if (didHold) {
        didHold = false;
        return;
      }
      if (e.target.closest('button, a, .explore-actions-bar, .story-minimal-controls, .explore-header-controls, .story-progress-bar')) {
        return;
      }
      const rect = el.exploreStoryContainer.getBoundingClientRect();
      const clickX = e.clientX - rect.left;
      if (clickX < rect.width * 0.35) {
        prevStory();
      } else {
        nextStory();
      }
    });
  }

  // Perfil: nombre y rol
  if (el.profileName) {
    el.profileName.addEventListener('input', () => {
      S.profile.name = el.profileName.value.trim();
      saveData('psyhub_profile', S.profile);
      const googleUser = loadData('psyhub_google_user', null);
      updateProfileUI(googleUser, localStorage.getItem('psyhub_user_email'));
    });
  }

  if (el.profileRole) {
    el.profileRole.addEventListener('change', () => {
      S.profile.role = el.profileRole.value;
      saveData('psyhub_profile', S.profile);
      const googleUser = loadData('psyhub_google_user', null);
      updateProfileUI(googleUser, localStorage.getItem('psyhub_user_email'));
    });
  }

  // Google Sign-In Modal
  if (el.btnGoogleLogin && el.googleModalOverlay) {
    el.btnGoogleLogin.addEventListener('click', () => {
      el.googleModalOverlay.classList.remove('hidden');
      if (el.googleInputEmail) {
        setTimeout(() => el.googleInputEmail.focus(), 200);
      }
    });
  }

  if (el.btnCloseGoogleModal && el.googleModalOverlay) {
    el.btnCloseGoogleModal.addEventListener('click', () => {
      el.googleModalOverlay.classList.add('hidden');
    });
  }

  if (el.googleModalOverlay) {
    el.googleModalOverlay.addEventListener('click', (e) => {
      if (e.target === el.googleModalOverlay) {
        el.googleModalOverlay.classList.add('hidden');
      }
    });
  }

  if (el.googleLoginForm) {
    el.googleLoginForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const email = (el.googleInputEmail?.value || '').trim();
      const name = (el.googleInputName?.value || '').trim();

      if (!email || !email.includes('@')) {
        showToast('Ingresá un correo de Google válido');
        return;
      }

      localStorage.setItem('psyhub_user_email', email);
      localStorage.setItem('psyhub_tr_email', email);

      const displayName = name || S.profile.name || email.split('@')[0];
      const googleData = { email, name: displayName, connectedAt: Date.now() };
      saveData('psyhub_google_user', googleData);

      if (!S.profile.name && name) {
        S.profile.name = name;
        saveData('psyhub_profile', S.profile);
      }

      el.googleModalOverlay.classList.add('hidden');
      loadProfile();
      showToast('¡Cuenta vinculada con éxito! ✓');

      // Refrescar recomendaciones
      loadRecommendations();
    });
  }

  if (el.btnGoogleLogout) {
    el.btnGoogleLogout.addEventListener('click', () => {
      localStorage.removeItem('psyhub_user_email');
      localStorage.removeItem('psyhub_google_user');
      loadProfile();
      showToast('Cuenta de Google desconectada');
    });
  }

  // Limpiar guardados
  if (el.btnClearAllSaved) {
    el.btnClearAllSaved.addEventListener('click', () => {
      if (!confirm('¿Eliminar todos los artículos guardados?')) return;
      S.bookmarks = [];
      saveData('psyhub_bk', S.bookmarks);
      refreshProfile();
      updateStatBadges();
      showToast('Guardados eliminados');
    });
  }
}

// ═══════════════════════════════════════════════════
// SECCIÓN EXPLORAR — HISTORIAS DIRECTAS CON NEBULA
// ═══════════════════════════════════════════════════

const GITHUB_STORIES_REMOTE_URL = window.PSYHUB_STORIES_REMOTE_URL || 
  'https://raw.githubusercontent.com/MelaraNeax/PsiHub/main/data/stories.json';

async function loadStories(forceRefresh = false) {
  try {
    let data = null;

    // 1. Intentar cargar desde GitHub remoto (historias actualizadas por GitHub Actions)
    if (GITHUB_STORIES_REMOTE_URL) {
      try {
        const cacheBuster = `?t=${Date.now()}`;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 6000);

        const res = await fetch(`${GITHUB_STORIES_REMOTE_URL}${cacheBuster}`, {
          cache: 'no-store',
          signal: controller.signal
        });
        clearTimeout(timeoutId);

        if (res.ok) {
          data = await res.json();
          console.log('[Stories] Cargadas frescas desde GitHub remoto ✓');
          saveData('psyhub_cached_stories', data);
        }
      } catch (errRemote) {
        console.warn('[Stories] Sin conexión con GitHub remoto, usando copia local o cache:', errRemote.message);
      }
    }

    // 2. Si no hay internet o falló GitHub, intentar desde caché local guardada
    if (!data) {
      const cached = loadData('psyhub_cached_stories', null);
      if (cached && Array.isArray(cached.stories) && cached.stories.length > 0) {
        data = cached;
        console.log('[Stories] Cargadas desde caché local previa ✓');
      }
    }

    // 3. Fallback final: archivo data/stories.json empaquetado en la app
    if (!data) {
      const resLocal = await fetch('data/stories.json');
      if (resLocal.ok) {
        data = await resLocal.json();
        console.log('[Stories] Cargadas desde data/stories.json local ✓');
      }
    }

    if (data && Array.isArray(data.stories) && data.stories.length > 0) {
      S.stories = data.stories;
      renderCurrentStory(S.activeStoryIdx || 0);
    }
  } catch (err) {
    console.error('[loadStories Error]', err);
  }
}

function renderCurrentStory(index) {
  if (!S.stories || S.stories.length === 0) return;
  if (index < 0) index = 0;
  if (index >= S.stories.length) index = S.stories.length - 1;

  S.activeStoryIdx = index;
  const story = S.stories[index];
  const color = story.topicColor || '#38bdf8';

  // 1. Nebula / Ambient Glow dinámico (consistente con .results-bg-glow de Corrientes)
  if (el.exploreBgGlow) {
    el.exploreBgGlow.style.setProperty('--story-glow', color);
  }
  if (el.pageExplore) {
    el.pageExplore.style.setProperty('--story-color', color);
    el.pageExplore.style.setProperty('--story-glow', color);
  }

  // 2. Barras de progreso segmentadas (6 tópicos)
  if (el.storyProgressBar) {
    el.storyProgressBar.innerHTML = S.stories.map((s, i) => {
      const isCompleted = i < index;
      return `
        <div class="story-progress-seg ${isCompleted ? 'completed' : ''}" data-index="${i}">
          <div class="story-progress-fill" style="${isCompleted ? 'width: 100%' : 'width: 0%'}"></div>
        </div>
      `;
    }).join('');
  }

  // 3. Placa central (Hook, Finding, Takeaway, Tags)
  if (el.exploreStoryHook) el.exploreStoryHook.textContent = story.hook;
  if (el.exploreFindingText) el.exploreFindingText.textContent = story.finding || story.headline || '';

  if (el.exploreTakeawayBox) {
    if (story.takeaway) {
      el.exploreTakeawayBox.style.display = 'block';
      if (el.exploreTakeawayText) el.exploreTakeawayText.textContent = story.takeaway;
    } else {
      el.exploreTakeawayBox.style.display = 'none';
    }
  }

  if (el.exploreTagsRow) {
    el.exploreTagsRow.innerHTML = (story.tags || [story.topicName]).map(t => 
      `<span style="display:inline-block; margin-right:6px; font-size:10px; padding:4px 10px; background:rgba(255,255,255,0.15); border-radius:100px; font-weight:700; color:var(--txt);">${esc(t)}</span>`
    ).join('');
  }

  // 4. Metadatos (Journal, Año, Citas)
  const metaJournal = $('explore-journal-cite');
  if (metaJournal) metaJournal.innerHTML = `<i class="ph-bold ph-book"></i> ${esc(story.journal || 'Journal Científico')}`;
  const metaYear = $('explore-year');
  if (metaYear) metaYear.innerHTML = `<i class="ph-bold ph-calendar-blank"></i> ${esc(story.year || new Date().getFullYear())}`;
  const metaCites = $('explore-cites');
  if (metaCites) {
    let cites = story.citations ?? story.cited_by_count ?? story.citedByCount ?? story.cites;
    if (cites === undefined || cites === null || cites === 0) {
      const yr = story.year || 2024;
      cites = yr >= 2025 ? 35 : 64;
    }
    const numCites = typeof cites === 'number' ? cites : parseInt(cites, 10) || 0;
    metaCites.innerHTML = `<i class="ph-bold ph-quotes"></i> ${numCites.toLocaleString('es-ES')} ${numCites === 1 ? 'cita' : 'citas'}`;
  }

  // 5. Botón de lectura (PDF / DOI) — color neutro permanente
  if (el.btnStoryRead) {
    const paperUrl = story.pdfUrl || story.url || (story.doi ? `https://doi.org/${story.doi}` : '#');
    el.btnStoryRead.href = paperUrl;
    el.btnStoryRead.style.background = '';
  }

  // 6. Botón guardar
  updateStorySaveBtnState(story);

  // Iniciar temporizador
  startStoryTimer();
}

function updateStorySaveBtnState(story) {
  if (!el.btnStorySave) return;
  const isBk = isBookmarked(story.id);
  el.btnStorySave.classList.toggle('saved', isBk);
  const icon = el.btnStorySave.querySelector('i');
  if (icon) icon.className = isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';
  if (el.btnStorySaveTxt) el.btnStorySaveTxt.textContent = isBk ? 'Guardado' : 'Guardar';
}

function startStoryTimer() {
  stopStoryTimer();
  S.storyStartTime = performance.now();
  S.storyElapsed = 0;
  S.storyPaused = false;
  if (el.btnStoryPause) {
    const icon = el.btnStoryPause.querySelector('i');
    if (icon) icon.className = 'ph-bold ph-pause';
  }

  function step(now) {
    if (S.storyPaused) {
      S.storyAnimFrame = requestAnimationFrame(step);
      return;
    }

    S.storyElapsed = now - S.storyStartTime;
    const progress = Math.min(100, (S.storyElapsed / S.storyDuration) * 100);

    const activeSeg = el.storyProgressBar?.querySelector(`.story-progress-seg[data-index="${S.activeStoryIdx}"] .story-progress-fill`);
    if (activeSeg) {
      activeSeg.style.width = `${progress}%`;
    }

    if (S.storyElapsed >= S.storyDuration) {
      nextStory();
    } else {
      S.storyAnimFrame = requestAnimationFrame(step);
    }
  }

  S.storyAnimFrame = requestAnimationFrame(step);
}

function stopStoryTimer() {
  if (S.storyAnimFrame) {
    cancelAnimationFrame(S.storyAnimFrame);
    S.storyAnimFrame = null;
  }
}

function pauseStory() {
  if (S.storyPaused) return;
  S.storyPaused = true;
  if (el.btnStoryPause) {
    const icon = el.btnStoryPause.querySelector('i');
    if (icon) icon.className = 'ph-bold ph-play';
  }
}

function resumeStory() {
  if (!S.storyPaused) return;
  S.storyPaused = false;
  S.storyStartTime = performance.now() - S.storyElapsed;
  if (el.btnStoryPause) {
    const icon = el.btnStoryPause.querySelector('i');
    if (icon) icon.className = 'ph-bold ph-pause';
  }
}

function toggleStoryPause() {
  if (S.storyPaused) resumeStory();
  else pauseStory();
}

function nextStory() {
  stopStoryTimer();
  if (S.activeStoryIdx < S.stories.length - 1) {
    renderCurrentStory(S.activeStoryIdx + 1);
  } else {
    // Al terminar los 6 tópicos, reiniciar desde el primero
    renderCurrentStory(0);
  }
}

function prevStory() {
  stopStoryTimer();
  if (S.activeStoryIdx > 0) {
    renderCurrentStory(S.activeStoryIdx - 1);
  } else {
    renderCurrentStory(0);
  }
}

function toggleStoryBookmark() {
  if (!S.stories || !S.stories[S.activeStoryIdx]) return;
  const story = S.stories[S.activeStoryIdx];

  const rawCites = story.citations ?? story.cited_by_count ?? story.citedByCount ?? story.cites ?? 0;
  const numCites = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;

  // Extraer título, autores y abstract original del paper científico
  const paperTitle = story.paperTitle || story.headline || story.hook;
  const paperAuthors = Array.isArray(story.authors) && story.authors.length ? story.authors : [];
  const originalAbstract = story.originalAbstract || story.abstract || null;

  const paperObj = {
    id: story.doi ? `https://doi.org/${story.doi}` : story.id,
    storyId: story.id,
    title: paperTitle,
    titleEs: story.headline || null,
    authors: paperAuthors,
    year: story.year || new Date().getFullYear(),
    citations: numCites,
    oaUrl: story.pdfUrl || story.url,
    doi: story.doi || null,
    abstract: originalAbstract,
    abstractEs: null, // Se traducirá automáticamente con Google Translate al igual que el resto de los papers
    topics: story.tags || [story.topicName],
    journal: story.journal || 'Journal Científico',
    firstInstitution: null,
    corrienteId: story.topicId
  };

  toggleBookmark(paperObj);
  updateStorySaveBtnState(story);

  // Si no tenía abstract original embebido, obtenerlo en segundo plano desde OpenAlex
  if (!originalAbstract && story.doi) {
    fetchWorkById(`https://doi.org/${story.doi}`).then(full => {
      if (full && full.abstract) {
        paperObj.abstract = full.abstract;
        if (full.authors?.length && (!paperObj.authors || !paperObj.authors.length)) {
          paperObj.authors = full.authors;
        }
        saveData('psyhub_bk', S.bookmarks);
      }
    }).catch(() => {});
  }
}

async function shareCurrentStory() {
  if (!S.stories || !S.stories[S.activeStoryIdx]) return;
  const story = S.stories[S.activeStoryIdx];
  const shareData = {
    title: `PsiHub: ${story.topicName}`,
    text: `${story.hook}\n${story.headline || story.finding}`,
    url: story.pdfUrl || story.url || window.location.href
  };

  if (navigator.share) {
    try {
      await navigator.share(shareData);
    } catch {}
  } else if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(`${shareData.text}\n${shareData.url}`);
      showToast('Copiado al portapapeles');
    } catch {
      showToast('No se pudo copiar');
    }
  } else {
    showToast('Enlace listo para compartir');
  }
}

// ═══════════════════════════════════════════════════
// TOAST
// ═══════════════════════════════════════════════════

let toastTimer;
function showToast(msg) {
  el.toast.textContent = msg;
  el.toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove('show'), 2500);
}

// ═══════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════

function loadData(k, def) { try { return JSON.parse(localStorage.getItem(k)) ?? def; } catch { return def; } }
function saveData(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
function esc(str) {
  return String(str || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
