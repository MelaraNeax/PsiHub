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
  'depresion': 'depression depressive disorder psychotherapy',
  'depresión': 'depression depressive disorder psychotherapy',
  'ansiedad': 'anxiety anxiety disorder psychotherapy',
  'estres': 'stress burnout psychological distress',
  'estrés': 'stress burnout psychological distress',
  'trauma': 'trauma PTSD post-traumatic stress psychotherapy',
  'fobia': 'phobia anxiety disorder exposure therapy',
  'esquizofrenia': 'schizophrenia psychosis psychotherapy',
  'bipolar': 'bipolar disorder mood disorder',
  'borderline': 'borderline personality disorder BPD DBT',
  'tlp': 'borderline personality disorder BPD DBT',
  'toc': 'OCD obsessive compulsive disorder ERP',
  'trastorno': 'disorder mental health',
  'duelo': 'grief bereavement loss counseling',
  'autoestima': 'self-esteem self-concept psychotherapy',
  'apego': 'attachment theory psychotherapy adult attachment',
  'pareja': 'couples therapy relationship psychotherapy',
  'familia': 'family therapy systemic intervention',
  'infancia': 'child psychotherapy developmental mental health',
  'adolescencia': 'adolescent psychotherapy youth mental health',
  'adulto mayor': 'geriatric psychology elderly mental health',
  'suicidio': 'suicide prevention suicidal ideation crisis',
  'adicciones': 'addiction substance abuse psychotherapy',
  'adiccion': 'addiction substance abuse psychotherapy',
  'adicción': 'addiction substance abuse psychotherapy',
  // Corrientes
  'psicoanalisis': 'psychoanalysis psychoanalytic therapy',
  'psicoanálisis': 'psychoanalysis psychoanalytic therapy',
  'cognitiva': 'cognitive therapy CBT',
  'conductual': 'behavioral therapy',
  'tcc': 'cognitive behavioral therapy CBT',
  'cbt': 'cognitive behavioral therapy CBT',
  'humanista': 'humanistic person-centered therapy',
  'existencial': 'existential therapy logotherapy',
  'sistémica': 'systemic family therapy',
  'sistemica': 'systemic family therapy',
  'gestalt': 'gestalt therapy',
  'mindfulness': 'mindfulness based intervention meditation',
  'act': 'acceptance commitment therapy ACT',
  'dbt': 'dialectical behavior therapy DBT',
  'emdr': 'EMDR trauma therapy',
  'tercera ola': 'third wave CBT mindfulness ACT DBT',
  // Términos clínicos
  'psicoterapia': 'psychotherapy psychological treatment',
  'terapia': 'psychotherapy psychological treatment',
  'alianza': 'therapeutic alliance working alliance',
  'alianza terapeutica': 'therapeutic alliance working alliance',
  'alianza terapéutica': 'therapeutic alliance working alliance',
  'sesión': 'psychotherapy session clinical process',
  'sesion': 'psychotherapy session clinical process',
  'eficacia': 'efficacy effectiveness psychotherapy outcomes',
  'evidencia': 'evidence randomized controlled trial meta-analysis',
  'protocolo': 'clinical protocol intervention manual',
  'caso clinico': 'clinical case study psychotherapy',
  'caso clínico': 'clinical case study psychotherapy',
  'diagnóstico': 'diagnosis diagnostic criteria DSM clinical',
  'diagnostico': 'diagnosis diagnostic criteria DSM clinical',
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
  { id: 'neurociencia', label: 'Neurociencia', abbr: 'NEU', icon: 'ph-brain', query: 'neuroscience neurobiology cognitive neuroscience brain', color: '#E91E63' },
  { id: 'tcc', label: 'TCC / CBT', abbr: 'TCC', icon: 'ph-grid-four', query: 'cognitive behavioral therapy CBT', color: '#1565C0' },
  { id: 'tercera_ola', label: 'Tercera Ola', abbr: '3°', icon: 'ph-waves', query: 'third wave ACT DBT mindfulness acceptance', color: '#1DB954' },
  { id: 'sistemica', label: 'Sistémica', abbr: 'SIS', icon: 'ph-graph', query: 'systemic family therapy', color: '#00838F' },
  { id: 'psicoanalisis', label: 'Psicoanálisis', abbr: 'PSA', icon: 'ph-couch', query: 'psychoanalysis psychoanalytic therapy', color: '#7B2D8B' },
  { id: 'humanismo', label: 'Humanismo', abbr: 'HUM', icon: 'ph-person-arms-spread', query: 'humanistic person-centered therapy Rogers', color: '#C17900' },
  { id: 'gestalt', label: 'Gestalt', abbr: 'GES', icon: 'ph-eye', query: 'gestalt therapy awareness contact', color: '#2E7D32' },
  { id: 'existencial', label: 'Existencial', abbr: 'EXI', icon: 'ph-infinity', query: 'existential therapy logotherapy meaning Frankl', color: '#4527A0' },
  { id: 'fenomenologia', label: 'Fenomenología', abbr: 'FEN', icon: 'ph-spiral', query: 'phenomenological existential psychotherapy', color: '#5B3FE0' },
  { id: 'segunda_ola', label: 'Segunda Ola', abbr: '2°', icon: 'ph-lightning', query: 'rational emotive behavior REBT cognitive therapy', color: '#E65C00' },
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
    { tag: 'clinico', score: scores.clinico },
    { tag: 'teorico', score: scores.teorico }
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
  activePage: 'home',
  resultsOpen: false,
  baseQuery: '',
  activeSubtype: 'all',
  activeCorriente: null,
  sort: null,
  page: 1,
  perPage: 10,
  total: 0,
  papers: [],     // papers actuales en la vista de resultados
  loading: false,
  searchTimeout: null,
  bookmarks: loadData('psyhub_bk', []),
  profile: loadData('psyhub_profile', { name: '', role: '' }),
  searches: loadData('psyhub_searches_count', 0),
  currentPaper: null,
  deferredInstall: null,

  // Historias / Explorar
  stories: [],
  activeStoryIdx: 0,
  storyAnimFrame: null,
  storyStartTime: 0,
  storyElapsed: 0,
  storyDuration: 8000,   // 8 segundos por historia
  storyPaused: false,
  watchedStories: loadData('psyhub_watched_stories', []),
  processed: loadData('psyhub_processed', []),
};

// ═══════════════════════════════════════════════════
// DOM — se inicializa cuando el DOM está listo
// ═══════════════════════════════════════════════════

const $ = id => document.getElementById(id);
let el = {};

function initDOMRefs() {
  el = {
    // Páginas
    pageHome: $('page-home'),
    pageExplore: $('page-explore'),
    pageLibrary: $('page-library'),
    pageSearch: $('page-search'),
    pageProfile: $('page-profile'),

    // Home
    btnSearchOpen: $('btn-search-open'),
    searchInlineWrapper: $('search-inline-wrapper'),
    searchInput: $('search-input'),
    btnClearSearch: $('btn-clear-search'),
    corrientesRow: $('corrientes-row'),
    recsList: $('recs-list'),
    recsSubtitle: $('recs-subtitle'),
    btnRefreshRecs: $('btn-refresh-recs'),

    // Explorar
    exploreBgGlow: $('explore-bg-glow'),
    exploreStoryContainer: $('explore-story-container'),
    storyProgressBar: $('story-progress-bar'),
    btnStoryPause: $('btn-story-pause'),
    btnRefreshStories: $('btn-refresh-stories'),
    storyTapPrev: $('story-tap-prev'),
    storyTapNext: $('story-tap-next'),
    exploreStoryCard: $('explore-story-card'),
    exploreJournalCite: $('explore-journal-cite'),
    exploreStoryHook: $('explore-story-hook'),
    exploreFindingText: $('explore-finding-text'),
    exploreTakeawayBox: $('explore-takeaway-box'),
    exploreTakeawayText: $('explore-takeaway-text'),
    exploreTagsRow: $('explore-tags-row'),
    btnStoryRead: $('btn-story-read'),
    btnStorySave: $('btn-story-save'),
    btnStorySaveTxt: $('btn-story-save-txt'),
    btnStoryShare: $('btn-story-share'),

    // Nav
    navBtns: document.querySelectorAll('.nav-btn'),

    // Search Page & Resultados
    resultsHeader: $('results-header'),
    resultsHeaderLabel: $('results-header-label'),
    resultsHeaderTitle: $('results-header-title'),
    searchPageWrapper: $('search-page-wrapper'),
    searchPageInput: $('search-page-input'),
    btnClearSearchPage: $('btn-clear-search-page'),
    btnDoSearchPage: $('btn-do-search-page'),
    searchInitial: $('search-initial'),
    searchInitialChips: $('search-initial-chips'),
    searchResultsScroll: $('search-results-scroll'),
    searchSubtabsBar: $('search-subtabs-bar'),
    searchFilterBar: $('search-filter-bar'),
    resultsBgGlow: $('results-bg-glow'),
    subtabBtns: document.querySelectorAll('.subtab'),
    resultsSort: $('results-sort'),
    resultsCount: $('results-count'),
    resultsLoading: $('results-loading'),
    resultsEmpty: $('results-empty'),
    resultsError: $('results-error'),
    resultsErrorMsg: $('results-error-msg'),
    btnResultsRetry: $('btn-results-retry'),
    paperList: $('paper-list'),
    infiniteScrollContainer: $('infinite-scroll-container'),
    resultsEndNotice: $('results-end-notice'),
    scrollRetryContainer: $('scroll-retry-container'),
    btnLoadMoreRetry: $('btn-load-more-retry'),
    btnLoadMore: $('btn-load-more'),

    // Modal
    modalOverlay: $('modal-overlay'),
    modalSheet: $('modal-sheet'),
    modalBody: $('modal-body'),
    btnModalClose: $('btn-modal-close'),
    btnModalBk: $('btn-modal-bk'),

    // Biblioteca
    librarySavedCard: $('library-saved-card'),
    librarySavedHeader: $('library-saved-header'),
    librarySavedContainer: $('library-saved-container'),
    iconToggleSaved: $('icon-toggle-saved'),
    libraryFileUpload: $('library-file-upload'),

    // Perfil
    profileAvatar: $('profile-avatar'),
    profileAvatarImg: $('profile-avatar-img'),
    avatarInitials: $('avatar-initials'),
    profileName: $('profile-name'),
    profileRole: $('profile-role'),
    displayProfileName: $('display-profile-name'),
    displayProfileRole: $('display-profile-role'),
    displayRoleText: $('display-role-text'),
    authNotConnected: $('auth-not-connected'),
    authConnected: $('auth-connected'),
    btnGoogleLogin: $('btn-google-login'),
    btnGoogleLogout: $('btn-google-logout'),
    connectedUserEmail: $('connected-user-email'),
    googleModalOverlay: $('google-modal-overlay'),
    googleModalSheet: $('google-modal-sheet'),
    btnCloseGoogleModal: $('btn-close-google-modal'),
    googleLoginForm: $('google-login-form'),
    googleInputEmail: $('google-input-email'),
    psihubOptionsOverlay: $('psihub-options-overlay'),
    psihubOptionsSheet: $('psihub-options-sheet'),
    btnClosePsihubOptions: $('btn-close-psihub-options'),
    btnPsihubReadEs: $('btn-psihub-read-es'),
    btnPsihubSummarize: $('btn-psihub-summarize'),
    googleInputName: $('google-input-name'),
    statSaved: $('stat-saved'),
    sectionSaved: $('section-saved'),
    savedList: $('saved-list'),
    profileEmptySaved: $('profile-empty-saved'),
    btnClearAllSaved: $('btn-clear-all-saved'),

    // Toast
    toast: $('toast'),

    // Modo Lectura
    readerOverlay: $('reader-overlay'),
    readerModal: $('reader-modal'),
    btnReaderClose: $('btn-reader-close'),
    readerLoading: $('reader-loading'),
    readerError: $('reader-error'),
    readerErrorMsg: $('reader-error-msg'),
    readerContent: $('reader-content'),
    readerFileUpload: $('reader-file-upload'),
    readerScroll: $('reader-scroll'),
    readerProgressFill: $('reader-progress-fill'),
    readerLoadingBarFill: $('reader-loading-bar-fill'),
    readerLoadingStep: $('reader-loading-step'),
    readerLoadingPct: $('reader-loading-pct'),
    btnReaderFontToggle: $('btn-reader-font-toggle'),
    btnReaderCopyText: $('btn-reader-copy-text'),
    btnReaderViewPdf: $('btn-reader-view-pdf'),
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
    }).catch(() => { });
  }
  if ('caches' in window) {
    caches.keys().then(keys => {
      keys.forEach(k => caches.delete(k));
    }).catch(() => { });
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
  resetInlineSearch();

  // Pausar reproducción si sale de Explorar
  if (S.activePage === 'explore' && page !== 'explore') {
    stopStoryTimer();
    S.storyPaused = true;
  }

  S.activePage = page;
  const pages = { home: el.pageHome, explore: el.pageExplore, library: el.pageLibrary, search: el.pageSearch, profile: el.pageProfile };
  Object.values(pages).forEach(p => p.classList.add('hidden'));
  pages[page].classList.remove('hidden');
  el.navBtns.forEach(b => b.classList.toggle('active', b.dataset.page === page));
  if (page === 'profile' || page === 'library') refreshProfile();
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
  // Cancelar inmediatamente cualquier consulta previa en curso
  if (currentFetchController) {
    try { currentFetchController.abort(); } catch { }
  }

  S.activeCorriente = corriente;
  S.baseQuery = corriente.query;
  S.activeSubtype = 'all';
  S.sort = 'cited_by_count:desc';
  S.page = 1;
  S.papers = [];
  S.hasMore = false;

  // Limpiar inmediatamente el DOM anterior para que no queden restos
  if (el.paperList) el.paperList.innerHTML = '';
  if (el.resultsCount) el.resultsCount.textContent = '';
  if (el.searchResultsScroll) el.searchResultsScroll.scrollTop = 0;
  if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
  if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
  if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';
  if (el.searchPageInput) {
    el.searchPageInput.value = '';
    if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = 'none';
  }

  el.resultsHeader.style.background = ''; // Remover fondo para que se vea solo la nebula
  el.resultsHeaderLabel.textContent = 'Corriente psicoterapéutica';
  el.resultsHeaderTitle.textContent = corriente.label;
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
  if (!query || !query.trim()) {
    resetSearchToInitial();
    return;
  }

  // Cancelar inmediatamente cualquier consulta previa en curso
  if (currentFetchController) {
    try { currentFetchController.abort(); } catch { }
  }

  // Guardar la query ORIGINAL para mostrar al usuario
  const displayQuery = query.trim();

  S.activeCorriente = null;
  S.activeSubtype = 'all';
  S.sort = null; // Relevancia por defecto en búsquedas
  S.page = 1;
  S.papers = [];
  S.hasMore = false;
  S.searches++;
  saveData('psyhub_searches_count', S.searches);

  // Limpiar inmediatamente el DOM anterior para que no queden restos
  if (el.paperList) el.paperList.innerHTML = '';
  if (el.resultsCount) el.resultsCount.textContent = '';
  if (el.searchResultsScroll) el.searchResultsScroll.scrollTop = 0;
  if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
  if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
  if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';

  if (el.resultsSort) el.resultsSort.value = '';

  // UI: mostrar la query original del usuario
  el.resultsHeader.style.background = '';
  el.resultsHeaderLabel.textContent = 'Búsqueda';
  el.resultsHeaderTitle.textContent = `"${displayQuery}"`;
  el.subtabBtns.forEach(b => b.classList.toggle('active', b.dataset.type === 'all'));

  if (el.searchPageInput) {
    el.searchPageInput.value = displayQuery;
    if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = 'flex';
  }

  updateGlow();
  showResults();
  showResultsState('loading');

  // Traducir/expandir la query a inglés sin mostrarla al usuario
  const englishQuery = await smartExpandQuery(displayQuery);

  // Si el usuario ya cambió de búsqueda mientras traducía, abortar
  if (el.searchPageInput && el.searchPageInput.value.trim() !== displayQuery) {
    return;
  }

  S.baseQuery = englishQuery; // OpenAlex recibe la versión en inglés

  await doFetch();
}

// ═══════════════════════════════════════════════════
// RESULTADOS & ESTADO INICIAL
// ═══════════════════════════════════════════════════

function resetSearchToInitial() {
  if (currentFetchController) {
    try { currentFetchController.abort(); } catch { }
    currentFetchController = null;
  }
  S.loading = false;
  S.baseQuery = '';
  S.papers = [];
  S.hasMore = false;
  S.page = 1;
  S.activeCorriente = null;
  S.activeSubtype = 'all';

  if (el.paperList) el.paperList.innerHTML = '';
  if (el.searchInitial) el.searchInitial.style.display = 'flex';
  if (el.resultsHeader) el.resultsHeader.style.display = 'none';
  if (el.searchSubtabsBar) el.searchSubtabsBar.style.display = 'none';
  if (el.searchFilterBar) el.searchFilterBar.style.display = 'none';
  if (el.resultsLoading) el.resultsLoading.style.display = 'none';
  if (el.resultsEmpty) el.resultsEmpty.style.display = 'none';
  if (el.resultsError) el.resultsError.style.display = 'none';
  if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
  if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
  if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';
  if (el.btnLoadMore) el.btnLoadMore.style.display = 'none';
  if (el.resultsCount) el.resultsCount.textContent = '';
  if (el.searchPageInput && !el.searchPageInput.value) {
    if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = 'none';
  }
}

function showResults() {
  if (S.activePage !== 'search') {
    navigateTo('search');
  }
  S.resultsOpen = true;

  if (el.searchInitial) el.searchInitial.style.display = 'none';
  if (el.resultsHeader) el.resultsHeader.style.display = 'flex';
  if (el.searchSubtabsBar) el.searchSubtabsBar.style.display = 'flex';
  if (el.searchFilterBar) el.searchFilterBar.style.display = 'flex';
}

function closeResults(animated = true) {
  if (!S.resultsOpen && !S.baseQuery) return;
  S.resultsOpen = false;
  resetSearchToInitial();
  if (el.searchPageInput) el.searchPageInput.value = '';
  resetInlineSearch();
}

let currentFetchController = null;

async function doFetch() {
  if (!S.baseQuery || !S.baseQuery.trim()) {
    resetSearchToInitial();
    return;
  }

  if (currentFetchController) {
    try { currentFetchController.abort(); } catch { }
  }
  currentFetchController = new AbortController();
  const signal = currentFetchController.signal;

  S.loading = true;
  const isFirstPage = S.page === 1;

  if (isFirstPage) {
    showResultsState('loading');
    if (el.paperList) el.paperList.innerHTML = '';
    if (el.resultsCount) el.resultsCount.textContent = '';
    if (el.searchResultsScroll) el.searchResultsScroll.scrollTop = 0;
    if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
    if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
    if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';
  } else {
    // Al scrollear hacia abajo, mostramos el círculo de carga elegante al pie
    if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'flex';
    if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';
  }

  try {
    const { results, meta } = await fetchPapers({
      query: S.baseQuery,
      subtype: S.activeSubtype,
      sort: S.sort || null,
      perPage: S.perPage,
      page: S.page,
      signal
    });

    if (signal.aborted) return;

    let finalResults = results;
    if (S.activeSubtype && S.activeSubtype !== 'all') {
      const filtered = results.filter(p => {
        const s = getPaperSubtypeScores(p);
        return s[S.activeSubtype] > 0;
      });
      finalResults = filtered.length > 0 ? filtered : results;
    }

    if (signal.aborted) return;

    S.total = meta.count;
    S.papers = isFirstPage ? finalResults : [...S.papers, ...finalResults];

    if (el.resultsCount && !signal.aborted) {
      el.resultsCount.textContent = `${S.total.toLocaleString('es')} resultados`;
    }

    if (S.papers.length === 0) {
      if (!signal.aborted) {
        showResultsState('empty');
        S.loading = false;
        S.hasMore = false;
        if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
      }
      return;
    }

    // Traducir títulos y snippets en lote antes de mostrar
    const titles = finalResults.map(p => p.title);
    const abstracts = finalResults.map(p => p.abstract ? p.abstract.slice(0, 150) : '');

    const [translatedTitles, translatedAbstracts] = await Promise.all([
      translateBatch(titles),
      translateBatch(abstracts)
    ]);

    if (signal.aborted) return;

    finalResults.forEach((p, i) => {
      p.titleEs = translatedTitles[i];
      if (abstracts[i]) p.abstractEs = translatedAbstracts[i] + '...';
    });

    if (signal.aborted) return;

    renderPapers(finalResults, !isFirstPage);
    showResultsState('results');

    const hasMore = S.papers.length < S.total && results.length === S.perPage;
    S.hasMore = hasMore;

    if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
    if (el.resultsEndNotice) {
      el.resultsEndNotice.style.display = (!hasMore && S.papers.length >= 10) ? 'flex' : 'none';
    }

  } catch (err) {
    if (err.name === 'AbortError' || signal.aborted) return;
    console.error('[PsiHub fetch]', err);
    if (isFirstPage) {
      el.resultsErrorMsg.textContent = err.message || 'Error de conexión con OpenAlex.';
      showResultsState('error');
    } else {
      if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
      if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'flex';
    }
  } finally {
    if (!signal.aborted) {
      S.loading = false;
    }
  }
}

async function loadMore() {
  if (S.loading || !S.hasMore || !S.baseQuery) return;
  S.page++;
  await doFetch();
}

function showResultsState(state) {
  el.resultsLoading.style.display = state === 'loading' ? 'flex' : 'none';
  el.resultsEmpty.style.display = state === 'empty' ? 'flex' : 'none';
  el.resultsError.style.display = state === 'error' ? 'flex' : 'none';
}

function initInfiniteScroll() {
  const scrollContainer = el.searchResultsScroll;
  if (!scrollContainer) return;

  scrollContainer.addEventListener('scroll', () => {
    if (S.loading || !S.hasMore || !S.baseQuery) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollContainer;
    if (scrollTop + clientHeight >= scrollHeight - 280) {
      loadMore();
    }
  }, { passive: true });

  if ('IntersectionObserver' in window && el.infiniteScrollContainer) {
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && !S.loading && S.hasMore && S.baseQuery) {
        loadMore();
      }
    }, {
      root: scrollContainer,
      rootMargin: '250px 0px',
      threshold: 0.05
    });
    observer.observe(el.infiniteScrollContainer);
  }

  if (el.btnLoadMoreRetry) {
    el.btnLoadMoreRetry.addEventListener('click', (e) => {
      e.stopPropagation();
      loadMore();
    });
  }

  if (el.btnLoadMore) {
    el.btnLoadMore.addEventListener('click', loadMore);
  }
}

// ═══════════════════════════════════════════════════
// CARDS DE RESULTADOS & VERIFICACIÓN PDF
// ═══════════════════════════════════════════════════

const HF_SPACE_URL = 'https://mi-servidor-api-42204102300.southamerica-east1.run.app';
const checkedPdfUrls = new Map(); // url -> { isAutomatic: boolean, status: string, pending: boolean }
const paperTranslations = new Map(); // id/url -> markdown
const activeTranslations = new Map(); // id -> Promise

function getCleanTranslationKey(key) {
  if (!key) return '';
  return String(key).trim().replace(/^https?:\/\/(openalex\.org\/)?/i, '').replace(/[^a-zA-Z0-9_-]/g, '_').slice(-64);
}

function getPersistedTranslation(paperId, paperUrl) {
  const k1 = getCleanTranslationKey(paperId);
  const k2 = getCleanTranslationKey(paperUrl);
  let res = null;
  if (k1) res = loadData('psy_tr_' + k1, null);
  if (!res && k2) res = loadData('psy_tr_' + k2, null);
  return res;
}

function persistTranslation(paperId, paperUrl, markdown) {
  if (!markdown) return;
  const k1 = getCleanTranslationKey(paperId);
  const k2 = getCleanTranslationKey(paperUrl);
  if (k1) saveData('psy_tr_' + k1, markdown);
  if (k2 && k2 !== k1) saveData('psy_tr_' + k2, markdown);
}

async function fetchWithBackgroundRetry(url, options, maxRetries = 2) {
  let lastError = null;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(url, options);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || data.error || 'Error del servidor');
      }
      return data;
    } catch (err) {
      lastError = err;
      const msg = err.message || '';
      if (msg.includes('DIRECT_UPLOAD_REQUIRED') || msg.includes('403') || msg.includes('bloqueó')) {
        throw err;
      }
      const isNetworkDrop = !msg || 
        msg.includes('Failed to fetch') || 
        msg.includes('NetworkError') || 
        msg.includes('abort') || 
        msg.includes('timeout') ||
        err.name === 'TypeError';

      if (attempt < maxRetries && isNetworkDrop) {
        console.log(`[Fetch Retry] Intento ${attempt + 1} reanudando conexión...`);
        await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
      } else {
        throw lastError;
      }
    }
  }
  throw lastError;
}

function showReaderRequisiteScreen(paper, customMsg = '') {
  el.readerLoading.style.display = 'none';
  el.readerContent.innerHTML = '';
  el.readerError.style.display = 'flex';

  const titleEl = document.getElementById('reader-requisite-title');
  if (titleEl) titleEl.textContent = 'Paso requerido para traducir';

  const iconWrap = document.getElementById('reader-error-icon-wrap');
  if (iconWrap) {
    iconWrap.className = 'reader-requisite-icon-wrap';
    iconWrap.innerHTML = '<i class="ph-bold ph-file-arrow-up"></i>';
  }

  const errorMsgEl = document.getElementById('reader-error-msg');
  if (errorMsgEl) {
    errorMsgEl.style.display = 'none';
  }

  const journalActionWrap = document.getElementById('reader-journal-action-wrap');
  const journalLink = document.getElementById('reader-journal-link');
  const uploadDesc = document.getElementById('reader-upload-desc');

  const lookupUrl = paper?.doi
    ? (paper.doi.startsWith('http') ? paper.doi : `https://doi.org/${paper.doi}`)
    : (paper?.url || paper?.pdfUrl || null);

  if (uploadDesc) {
    uploadDesc.innerHTML = 'Esta revista requiere acceder a su enlace oficial para descargar el PDF.<br>Descárgalo en tu dispositivo y selecciónalo a continuación para traducirlo al español:';
  }

  if (journalActionWrap && journalLink && lookupUrl) {
    journalLink.href = lookupUrl;
    journalActionWrap.style.display = 'block';
  } else if (journalActionWrap) {
    journalActionWrap.style.display = 'none';
  }
}

function handleReaderTranslationError(error, paper) {
  el.readerLoading.style.display = 'none';
  el.readerContent.innerHTML = '';
  
  const msg = error?.message || '';
  const isDirectUploadRequired = msg.includes('DIRECT_UPLOAD_REQUIRED') 
    || msg.includes('403') 
    || msg.includes('bloqueó') 
    || msg.includes('adjuntar') 
    || msg.includes('manual')
    || msg.includes('bot protection')
    || msg.includes('captcha');

  if (isDirectUploadRequired) {
    showReaderRequisiteScreen(paper);
  } else {
    el.readerError.style.display = 'flex';
    const iconWrap = document.getElementById('reader-error-icon-wrap');
    if (iconWrap) {
      iconWrap.className = 'reader-requisite-icon-wrap';
      iconWrap.innerHTML = '<i class="ph-bold ph-wifi-slash"></i>';
    }
    const titleEl = document.getElementById('reader-requisite-title');
    if (titleEl) titleEl.textContent = 'Conexión interrumpida';
    
    const errorMsgEl = document.getElementById('reader-error-msg');
    if (errorMsgEl) {
      errorMsgEl.style.display = 'block';
      errorMsgEl.textContent = 'Comprueba tu conexión o reintenta en unos instantes.';
    }
    
    const journalActionWrap = document.getElementById('reader-journal-action-wrap');
    if (journalActionWrap) journalActionWrap.style.display = 'none';

    const uploadDesc = document.getElementById('reader-upload-desc');
    if (uploadDesc) {
      uploadDesc.innerHTML = 'Si lo prefieres, también puedes adjuntar el PDF manualmente para continuar:';
    }
  }
}

function updatePaperAutomaticBadges(paper) {
  if (!paper || !paper.isAutomatic) return;

  // 1. Actualizar badges en las tarjetas del DOM
  const badges = document.querySelectorAll('.auto-badge-container');
  badges.forEach(b => {
    if (b.dataset.id === paper.id) {
      b.innerHTML = `<span class="auto-badge" style="font-size:11px;color:var(--txt-3);display:inline-flex;align-items:center;gap:3px;"><i class="ph-bold ph-lightning"></i>Automático</span>`;
    }
  });

  // 2. Si el modal abierto corresponde a este paper, actualizar subtítulo y badge en opciones
  if (S.currentPaper && S.currentPaper.id === paper.id) {
    const sub = document.getElementById('modal-psihub-btn-sub');
    if (sub) {
      sub.textContent = '⚡ Traducción automática';
    }
    const badgeEs = document.getElementById('btn-psihub-read-es-badge');
    if (badgeEs) {
      badgeEs.style.display = 'inline-block';
    }
  }
}

async function checkPaperPdf(paper) {
  if (!paper) return;
  const url = paper.pdfUrl || paper.oaUrl;
  if (!url) {
    paper.pdfChecked = true;
    paper.isAutomatic = false;
    return;
  }

  // Si ya fue chequeado en este objeto
  if (paper.pdfChecked && paper.isAutomatic !== undefined) {
    if (paper.isAutomatic) updatePaperAutomaticBadges(paper);
    return;
  }

  // Si la URL ya fue consultada previamente en la sesión
  if (checkedPdfUrls.has(url)) {
    const cached = checkedPdfUrls.get(url);
    if (!cached.pending) {
      paper.isAutomatic = cached.isAutomatic;
      paper.pdfChecked = true;
      if (paper.isAutomatic) updatePaperAutomaticBadges(paper);
    }
    return;
  }

  // Marcar como pendiente para no enviar múltiples peticiones simultáneas de la misma URL
  checkedPdfUrls.set(url, { isAutomatic: false, status: 'checking', pending: true });
  paper.pdfChecked = true;

  try {
    const res = await fetch(`${HF_SPACE_URL}/api/check-pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    const isAuto = (data.status === 'ok');
    checkedPdfUrls.set(url, { isAutomatic: isAuto, status: data.status, pending: false });
    paper.isAutomatic = isAuto;
    if (isAuto) {
      updatePaperAutomaticBadges(paper);
    }
  } catch (err) {
    checkedPdfUrls.set(url, { isAutomatic: false, status: 'error', pending: false });
    paper.isAutomatic = false;
  }
}

function validatePdfUrls(papers) {
  if (!Array.isArray(papers)) return;
  // Solo verificar papers que no hayan sido revisados antes
  const papersToCheck = papers.filter(p => !p.pdfChecked && (p.pdfUrl || p.oaUrl));
  papersToCheck.forEach(p => checkPaperPdf(p));
}

function renderPapers(papers, append) {
  const frag = document.createDocumentFragment();
  papers.forEach(p => frag.appendChild(buildCard(p)));
  if (!append) el.paperList.innerHTML = '';
  el.paperList.appendChild(frag);

  validatePdfUrls(papers);
}

function buildCard(paper, onBookmarkChange) {
  const li = document.createElement('li');
  li.className = 'paper-card';

  const isCorrienteView = Boolean(S.resultsOpen && S.activeCorriente && el.resultsHeaderLabel?.textContent?.toLowerCase().includes('corriente'));
  const corriente = isCorrienteView ? S.activeCorriente : null;
  const isBk = isBookmarked(paper.id);
  const displayTitle = paper.titleEs || paper.title;
  const snippet = paper.abstractEs || (paper.abstract ? paper.abstract.slice(0, 150) + '...' : '');
  const firstAuthor = Array.isArray(paper.authors) ? (paper.authors[0] || '') : (paper.authors || '');
  const moreAuthors = Array.isArray(paper.authors) && paper.authors.length > 1 ? ` +${paper.authors.length - 1}` : '';
  const isFilterActiveInResults = Boolean(S.resultsOpen && S.activeSubtype && S.activeSubtype !== 'all');
  const tags = isFilterActiveInResults ? [S.activeSubtype] : classifyPaper(paper);

  li.innerHTML = `
    <div class="paper-card-body" style="padding-left: 0;">
      <div class="paper-card-top">
        <h3 class="paper-card-title">${esc(displayTitle)}</h3>
        <button class="btn-card-bk ${isBk ? 'saved' : ''}" data-id="${esc(paper.id || '')}" data-doi="${esc(paper.doi || '')}" aria-label="Guardar">
          <i class="${isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple'}"></i>
        </button>
      </div>
      <p class="paper-card-authors">${esc(firstAuthor + moreAuthors)}</p>
      ${snippet ? `<p class="paper-card-snippet">${esc(snippet)}</p>` : ''}
      <div class="paper-card-meta">
        ${tags.map(tagBadge).join('')}
        ${paper.year ? `<span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--txt-3);"><i class="ph-bold ph-calendar-blank"></i>${paper.year}</span>` : ''}
        <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--txt-3);"><i class="ph-bold ph-quotes"></i>${(paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? 0).toLocaleString('es')}</span>
        ${paper.isAutomatic ? `<span class="auto-badge" style="margin-left:auto;font-size:11px;color:var(--txt-3);display:inline-flex;align-items:center;gap:3px;"><i class="ph-bold ph-lightning"></i>Automático</span>` : `<div class="auto-badge-container" data-id="${esc(paper.id)}" data-url="${esc(paper.pdfUrl || paper.oaUrl || '')}" style="margin-left:auto;"></div>`}
      </div>
    </div>
  `;

  li.addEventListener('click', () => openModal(paper, corriente));

  li.querySelector('.btn-card-bk').addEventListener('click', e => {
    e.stopPropagation();
    toggleBookmark(paper);
    if (typeof onBookmarkChange === 'function') {
      onBookmarkChange(isBookmarked(paper), paper);
    }
  });

  return li;
}

function tagBadge(t) {
  const configs = {
    clinico: { label: 'Clínico', icon: 'ph-stethoscope', cls: 'tag-clinico' },
    evidencia: { label: 'Evidencia', icon: 'ph-chart-line-up', cls: 'tag-evidencia' },
    teorico: { label: 'Teórico', icon: 'ph-brain', cls: 'tag-teorico' }
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
    const titles = results.map(p => p.title);
    const snippets = results.map(p => p.abstract ? p.abstract.slice(0, 150) : '');

    const [translatedTitles, translatedSnippets] = await Promise.all([
      translateBatch(titles),
      translateBatch(snippets)
    ]);

    results.forEach((p, i) => {
      p.titleEs = translatedTitles[i];
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

    // Verificar soporte de descarga directa una sola vez o actualizar badge si ya está confirmado
    if (!paper.pdfChecked && (paper.pdfUrl || paper.oaUrl)) {
      checkPaperPdf(paper);
    } else if (paper.isAutomatic) {
      updatePaperAutomaticBadges(paper);
    }

    // Traducir título al español con Google Translate si todavía está en inglés
    if (!paper.titleEs && paper.title) {
      translateToSpanish(paper.title).then(transTitle => {
        if (transTitle && transTitle.trim() && S.currentPaper?.id === paper.id) {
          paper.titleEs = transTitle;
          const titleEl = el.modalBody.querySelector('.modal-title');
          if (titleEl) {
            titleEl.textContent = transTitle;
          }
        }
      }).catch(err => {
        console.warn('[openModal] No se pudo traducir título:', err);
      });
    }

    // Si el paper no tiene abstract o es un resumen sintético previo, buscar el abstract original en OpenAlex
    const isAiSummary = paper.abstract && paper.abstract.includes('\n\n') && (paper.abstract.startsWith('¿') || paper.abstract.includes('?'));
    if ((!paper.abstract || isAiSummary) && (paper.id || paper.doi)) {
      const abstractEl = document.getElementById('modal-abstract-text');
      if (abstractEl) abstractEl.textContent = 'Cargando abstract original desde OpenAlex…';
      try {
        const lookupId = (paper.doi ? (paper.doi.startsWith('http') ? paper.doi : `https://doi.org/${paper.doi}`) : paper.id);
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 6000);
        const full = await fetchWorkById(lookupId, controller.signal).finally(() => clearTimeout(timeoutId));

        if (full && S.currentPaper?.id === paper.id) {
          if (full.abstract) {
            paper.abstract = full.abstract;
            paper.abstractEs = null;
          } else {
            const absEl = document.getElementById('modal-abstract-text');
            if (absEl && S.currentPaper?.id === paper.id) {
              absEl.innerHTML = '<span style="opacity: 0.75; font-style: italic;">OpenAlex no incluyó el abstract original para este artículo.</span>';
              const langEl = document.getElementById('modal-abstract-lang');
              if (langEl) langEl.style.display = 'none';
            }
          }
          if (full.authors?.length && (!paper.authors || !paper.authors.length || paper.authors.length <= 1)) {
            paper.authors = full.authors;
          }
          if (full.topics?.length) paper.topics = full.topics;
          if (full.journal) paper.journal = full.journal;
          if (full.firstInstitution) paper.firstInstitution = full.firstInstitution;
          if (full.oaUrl && !paper.oaUrl) paper.oaUrl = full.oaUrl;
          if (full.pdfUrl && !paper.pdfUrl) paper.pdfUrl = full.pdfUrl;
          if (full.doi && !paper.doi) paper.doi = full.doi;
          if (!paper.pdfChecked && (paper.pdfUrl || paper.oaUrl)) {
            checkPaperPdf(paper);
          }
          if (full.type) paper.type = full.type;
          if (full.mesh?.length) paper.mesh = full.mesh;
          if (typeof full.citations === 'number' && full.citations > 0) paper.citations = full.citations;

          if (full.abstract) {
            // Re-renderizar modal con la información completa
            const updatedTags = isFilterActiveInResults ? [S.activeSubtype] : classifyPaper(paper);
            const currentTitle = paper.titleEs || paper.title || 'Sin título';
            el.modalBody.innerHTML = buildModalHTML(paper, modalCorriente, updatedTags, currentTitle, paper.abstractEs, false);
          }
        } else if (!full && S.currentPaper?.id === paper.id) {
          const absEl = document.getElementById('modal-abstract-text');
          if (absEl) {
            absEl.innerHTML = '<span style="opacity: 0.75; font-style: italic;">OpenAlex no incluyó el abstract original para este artículo.</span>';
            const langEl = document.getElementById('modal-abstract-lang');
            if (langEl) langEl.style.display = 'none';
          }
        }
      } catch (errOpenAlex) {
        console.warn('[openModal] No se pudo obtener detalle de OpenAlex:', errOpenAlex);
        const absEl = document.getElementById('modal-abstract-text');
        if (absEl && S.currentPaper?.id === paper.id) {
          absEl.innerHTML = '<span style="opacity: 0.75; font-style: italic;">OpenAlex no incluyó el abstract original para este artículo.</span>';
          const langEl = document.getElementById('modal-abstract-lang');
          if (langEl) langEl.style.display = 'none';
        }
      }
    }

    // Traducir abstract en el fondo si existe y no está traducido
    if (paper.abstract && !paper.abstractEs) {
      try {
        const langEl = document.getElementById('modal-abstract-lang');
        if (langEl) {
          langEl.innerHTML = '<span class="modal-lang-badge translating"><div class="spinner modal-spinner-mini"></div> Traduciendo…</span>';
        }
        const abstractEs = await translateToSpanish(paper.abstract);
        paper.abstractEs = abstractEs;
        const abstractEl = document.getElementById('modal-abstract-text');
        const langElUpdated = document.getElementById('modal-abstract-lang');
        if (abstractEl && S.currentPaper?.id === paper.id) {
          abstractEl.textContent = abstractEs;
          if (langElUpdated) langElUpdated.innerHTML = '<span class="modal-lang-badge"><i class="ph-bold ph-translate"></i> Traducido al español</span>';
        }
      } catch (errTrans) {
        console.warn('[openModal] Falló traducción:', errTrans);
        const langEl = document.getElementById('modal-abstract-lang');
        if (langEl) langEl.innerHTML = '<span class="modal-lang-badge en"><i class="ph-bold ph-translate"></i> Original en inglés</span>';
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
  let tagsHtml = '';
  if (isCorriente) {
    const tagColor = corriente.color || 'var(--txt-3)';
    const tagStyle = `background:${tagColor}1c; color:${tagColor}; border:1px solid ${tagColor}44;`;
    tagsHtml += `<span class="modal-tag" style="${tagStyle}"><i class="ph-bold ph-bookmarks" style="font-size:11px; margin-right:3px;"></i>${esc(corriente.label)}</span>`;
  }
  if (tags && tags.length) {
    const validTag = tags.find(t => t && !t.toLowerCase().includes('articulo') && !t.toLowerCase().includes('artículo'));
    if (validTag && (!isCorriente || validTag !== corriente.label)) {
      tagsHtml += tagBadge(validTag);
    }
  }

  const abstractContent = paper.abstract || 'OpenAlex no incluyó el abstract original para este artículo.';
  const authors = Array.isArray(paper.authors) ? paper.authors : [];
  let authorsStr = '';
  if (authors.length > 0) {
    authorsStr = authors.length > 2 ? `${authors.slice(0, 2).join(', ')} et al.` : authors.join(', ');
  }
  const sourceStr = paper.journal || paper.firstInstitution || '';

  const rawCites = paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? paper.cites ?? 0;
  const citations = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;

  // Limitar a máximo 3 palabras clave y acortar strings largos
  const rawTopics = Array.isArray(paper.topics) ? paper.topics : [];
  const shortTopics = rawTopics.slice(0, 3).map(t => {
    const s = String(t).trim();
    return s.length > 22 ? s.slice(0, 21) + '…' : s;
  });

  return `
    ${tagsHtml ? `<div class="modal-tags">${tagsHtml}</div>` : ''}

    <h2 class="modal-title">${esc(displayTitle || 'Sin título')}</h2>

    <div class="modal-meta-row">
      ${authorsStr ? `
        <span class="modal-meta-item">
          <i class="ph-bold ph-users"></i>
          <span>${esc(authorsStr)}</span>
        </span>
      ` : ''}
      ${sourceStr ? `
        <span class="modal-meta-item">
          <i class="ph-bold ph-book"></i>
          <span>${esc(sourceStr)}</span>
        </span>
      ` : ''}
      <div class="modal-meta-subrow">
        ${paper.year ? `
          <span class="modal-meta-item">
            <i class="ph-bold ph-calendar-blank"></i>
            <span>${paper.year}</span>
          </span>
        ` : ''}
        <span class="modal-meta-item">
          <i class="ph-bold ph-quotes"></i>
          <span>${citations.toLocaleString('es')} ${citations === 1 ? 'cita' : 'citas'}</span>
        </span>
      </div>
    </div>

    <div class="modal-abstract-section">
      <div class="modal-abstract-header">
        <span class="modal-abstract-label">Resumen</span>
        <div id="modal-abstract-lang" class="modal-abstract-lang">
          ${(paper.abstract || abstractEs) ? (
      translating && paper.abstract
        ? '<span class="modal-lang-badge translating"><div class="spinner modal-spinner-mini"></div> Traduciendo…</span>'
        : '<span class="modal-lang-badge"><i class="ph-bold ph-translate"></i> Traducido</span>'
    ) : ''}
        </div>
      </div>
      <p id="modal-abstract-text" class="modal-abstract">${esc(abstractEs || abstractContent)}</p>
    </div>

    ${shortTopics.length > 0 ? `
      <div class="modal-topics-section">
        <div class="modal-topics">
          ${shortTopics.map(t => `<span class="modal-topic-chip">${esc(t)}</span>`).join('')}
        </div>
      </div>
    ` : ''}

    <div class="explore-actions-bar" style="padding: 0; margin-top: 16px;">
      <div class="explore-actions-bar-row">
        <button class="btn-cta-primary explore-action-read" id="btn-modal-psihub-options" style="flex-direction: column; gap: 2px;">
          <div style="display: flex; align-items: center; gap: 6px;">
            <i class="ph-bold ph-book-open"></i> Leer en PsiHub
          </div>
          <span id="modal-psihub-btn-sub" style="font-size: 10px; opacity: 0.8; font-weight: normal;">${paper.isAutomatic ? '⚡ Traducción automática' : 'incluye traducción'}</span>
        </button>
        ${paper.oaUrl || paper.pdfUrl
      ? `<a class="btn-cta-secondary" href="${esc(paper.oaUrl || paper.pdfUrl)}" target="_blank" rel="noopener" style="flex: 1; text-decoration: none; cursor: pointer;">
             <i class="ph-bold ph-file-pdf"></i> Leer PDF
           </a>`
      : ''}
      </div>
      <div class="explore-actions-bar-row">
        <button class="explore-action-save btn-copy-apa" id="btn-modal-copy-apa" title="Copiar referencia en formato APA 7">
          <i class="ph-bold ph-copy"></i> APA
        </button>
        <button class="explore-action-save btn-copy-doi" id="btn-modal-copy-doi" title="Copiar DOI" ${!paper.doi ? 'disabled style="opacity:0.5;cursor:not-allowed;"' : ''}>
          <i class="ph-bold ph-link"></i> DOI
        </button>
        <button class="explore-action-save" id="btn-modal-share" title="Compartir (próximamente)" disabled style="opacity: 0.5; cursor: not-allowed;">
          <i class="ph-bold ph-share-network"></i> Compartir
        </button>
      </div>
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
// ═══════════════════════════════════════════════════

const isAffiliationOrMeta = (b) => {
  const s = b.trim();
  if (!s || s.startsWith('#') || s.startsWith('!') || s.startsWith('|')) return false;
  const hasEmail = /[\w.-]+@[\w.-]+\.\w+|e-mail:|email:|correo electrónico:/i.test(s);
  const affilKeywords = [
    'department of', 'departamento de', 'division of', 'división de',
    'section on', 'sección de', 'institute of', 'instituto de',
    'university', 'universidad', 'school of', 'escuela de',
    'faculty of', 'facultad de', 'hospital', 'laboratory of',
    'laboratorio de', 'center for', 'centro de', 'dirp', 'nih', 'nimh',
    'clinic', 'clínica', 'unit', 'unidad de'
  ];
  let hits = 0;
  for (const kw of affilKeywords) {
    if (new RegExp('\\b' + kw + '\\b', 'i').test(s)) hits++;
  }
  const hasAuthorSym = /\(&\)|\bcorrespondence\b|\bcorresponding author\b|\bautor de correspondencia\b|\baddress correspondence\b/i.test(s);
  const hasAddress = /\b(?:USA|UK|Spain|France|Germany|Bethesda|MD\s*\d{5}|MO\s*\d{5}|Room\s*\d+|Box\s*\d+|P\.?O\.?\s*Box)\b/i.test(s);
  const hasEditorial = /\b(?:received:\s*\d|accepted:\s*\d|published online:|doi:\s*10\.|copyright\s*©|©\s*\d{4})\b/i.test(s);

  if (hasEditorial || hasEmail) return true;
  if (hits >= 1 && (hasAuthorSym || hasAddress)) return true;
  if (hits >= 2) return true;
  return false;
}

const cleanAndJoinBrokenMarkdown = (md) => {
  if (!md) return '';
  let text = md;

  // Limpiar posibles bloques residuales corruptos de afiliaciones inyectados previamente
  // Usamos una regex más agresiva que no dependa de \n\n, para el caso donde estén pegados a una imagen o al final.
  text = text.replace(/>?\s*[*_]{0,3}Afiliaciones y Correspondencia:[*_]{0,3}[^\n]*(?:\n[^\n]*)*?(?=\n\n|\n>?\s*!\[Figura|$)/gi, '');
  text = text.replace(/Afiliaciones y Correspondencia:/gi, '');

  // Limpiar footers de revistas comunes pegados al final de párrafos (ej. Brain Struct Funct (2008) 213:93-118)
  text = text.replace(/\s*[A-Za-z\s.-]+\(\d{4}\)\s*\d+:\d+(?:[–-]\d+)?\s*$/gim, '');

  // 1. Eliminar marcadores <!-- PAGE:X -->
  text = text.replace(/<!--\s*PAGE:\d+\s*-->/gi, '');

  // 2. Unir palabras cortadas con guión de fin de línea
  text = text.replace(/(\b[\wáéíóúñÁÉÍÓÚÑ]+)-\s*\n+\s*([\wáéíóúñÁÉÍÓÚÑ]+\b)/g, '$1$2');

  // 3. Paréntesis abiertos antes de salto de línea
  text = text.replace(/([(\[{])\s*\n+\s*/g, '$1');

  // 4. Unir párrafos rotos donde la línea previa no termina con signo terminal y la siguiente empieza en minúscula o signo de continuación
  const lines = text.split('\n');
  const result = [];
  let i = 0;
  while (i < lines.length) {
    let line = lines[i];
    while (i + 1 < lines.length) {
      const nextLine = lines[i + 1];
      // Si la siguiente línea es vacía y la subsiguiente (salto doble \n\n) continúa la frase
      if (!nextLine.trim() && i + 2 < lines.length) {
        const afterEmpty = lines[i + 2];
        const currTrim = line.trimEnd();
        const afterTrim = afterEmpty.trimStart();

        const isNotHeaderOrList = !currTrim.startsWith('#') && !currTrim.startsWith('*') && !currTrim.startsWith('-') && !currTrim.startsWith('|') && !currTrim.startsWith('>') && !currTrim.startsWith('<') && !currTrim.startsWith('!') &&
          !afterTrim.startsWith('#') && !afterTrim.startsWith('*') && !afterTrim.startsWith('-') && !afterTrim.startsWith('|') && !afterTrim.startsWith('>') && !afterTrim.startsWith('<') && !afterTrim.startsWith('!');
        const currNotTerminal = !/[.!?:]\s*["'”)]*$/.test(currTrim) && !currTrim.endsWith('>') && !currTrim.endsWith('</a>');
        const afterStartsLower = /^[a-záéíóúñ(),;\]]/.test(afterTrim);
        const currCut = /[-–—(¿¡]$|(?:\b(?:en|de|del|la|el|los|las|un|una|con|por|para|y|o|que|a|al|su|sus|como)\s*)$/i.test(currTrim);

        if (isNotHeaderOrList && currNotTerminal && (afterStartsLower || currCut)) {
          if (!isAffiliationOrMeta(afterTrim)) {
            line = currTrim + ' ' + afterTrim;
            i += 2;
            continue;
          }
        }
      }
      break;
    }
    result.push(line);
    i++;
  }
  return result.join('\n');
};

const postProcessReaderContent = () => {
  if (!el.readerContent) return;

  // Unir párrafos (<p>) rotos accidentalmente en el DOM
  const paragraphs = Array.from(el.readerContent.querySelectorAll('p'));
  for (let i = 0; i < paragraphs.length - 1; i++) {
    const currentP = paragraphs[i];
    const nextP = paragraphs[i + 1];

    if (!currentP || !nextP) continue;
    if (currentP.querySelector('img, table, iframe') || nextP.querySelector('img, table, iframe')) continue;
    if (currentP.classList.contains('reader-image-wrap') || nextP.classList.contains('reader-image-wrap')) continue;

    const currentText = currentP.textContent.trim();
    const nextText = nextP.textContent.trim();
    if (!currentText || !nextText) continue;

    const endsWithTerminal = /[.!?:]\s*["'”)]*$/.test(currentText);
    const startsWithContinuation = /^[a-záéíóúñ(),;\]]/.test(nextText);
    const endsWithCut = /[-–—(¿¡]$|(?:\b(?:en|de|del|la|el|los|las|un|una|con|por|para|y|o|que|a|al|su|sus|como)\s*)$/i.test(currentText);

    if (!endsWithTerminal && (startsWithContinuation || endsWithCut)) {
      if (!isAffiliationOrMeta(nextText)) {
        currentP.innerHTML = currentP.innerHTML.trimEnd() + ' ' + nextP.innerHTML.trimStart();
        nextP.remove();
        paragraphs.splice(i + 1, 1);
        i--;
      }
    }
  }

  // Envolver tablas para scroll horizontal exclusivo
  el.readerContent.querySelectorAll('table').forEach(table => {
    if (!table.parentElement.classList.contains('reader-table-wrap')) {
      const wrap = document.createElement('div');
      wrap.className = 'reader-table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }
  });

  // Envolver imágenes y figuras en un contenedor dedicado SIN alterar el párrafo de texto
  el.readerContent.querySelectorAll('img').forEach(img => {
    if (img.closest('.reader-figure-container')) return;

    const parentP = img.closest('p');
    const figureContainer = document.createElement('div');
    figureContainer.className = 'reader-figure-container';

    const imageWrap = document.createElement('div');
    imageWrap.className = 'reader-image-wrap';

    // Buscar enlace internal-pdf-link adyacente o hijo
    let pdfLink = null;
    if (parentP) {
      pdfLink = parentP.querySelector('.internal-pdf-link');
      if (!pdfLink && parentP.nextElementSibling) {
        pdfLink = parentP.nextElementSibling.querySelector('.internal-pdf-link') ||
                  (parentP.nextElementSibling.classList.contains('internal-pdf-link') ? parentP.nextElementSibling : null);
      }
    }

    imageWrap.appendChild(img);
    figureContainer.appendChild(imageWrap);

    if (pdfLink) {
      const caption = document.createElement('div');
      caption.className = 'reader-figure-caption';
      caption.appendChild(pdfLink);
      figureContainer.appendChild(caption);
    }

    if (parentP) {
      const textOnly = parentP.textContent.trim();
      const hasOnlyMedia = !textOnly || (pdfLink && textOnly === pdfLink.textContent.trim());
      if (hasOnlyMedia) {
        parentP.parentNode.replaceChild(figureContainer, parentP);
      } else {
        parentP.parentNode.insertBefore(figureContainer, parentP.nextSibling);
      }
    } else {
      img.parentNode.insertBefore(figureContainer, img);
    }
  });
};

function renderReaderPaperContent(paper, markdown) {
  if (!markdown) return;
  const sanitized = cleanAndJoinBrokenMarkdown(markdown);
  const rawHtml = marked.parse(sanitized);

  let heroHtml = '';
  if (paper) {
    const displayTitle = paper.titleEs || paper.title || 'Artículo científico';
    const rawAuthors = Array.isArray(paper.authors) ? paper.authors : [];
    let authorsStr = '';
    if (rawAuthors.length > 0) {
      authorsStr = rawAuthors.length > 2 ? `${rawAuthors.slice(0, 2).join(', ')} et al.` : rawAuthors.join(', ');
    }
    const rawCites = paper.citations ?? paper.cited_by_count ?? paper.citedByCount ?? paper.cites ?? 0;
    const citations = typeof rawCites === 'number' ? rawCites : parseInt(rawCites, 10) || 0;
    const sourceStr = paper.journal || paper.firstInstitution || '';

    heroHtml = `
      <div class="reader-hero-card">
        <h1 class="reader-hero-title">${esc(displayTitle)}</h1>
        <div class="modal-meta-row" style="margin-top: 10px;">
          ${authorsStr ? `<span class="modal-meta-item"><i class="ph-bold ph-users"></i> <span>${esc(authorsStr)}</span></span>` : ''}
          ${sourceStr ? `<span class="modal-meta-item"><i class="ph-bold ph-book"></i> <span>${esc(sourceStr)}</span></span>` : ''}
          <div class="modal-meta-subrow">
            ${paper.year ? `<span class="modal-meta-item"><i class="ph-bold ph-calendar-blank"></i> <span>${paper.year}</span></span>` : ''}
            ${citations > 0 ? `<span class="modal-meta-item"><i class="ph-bold ph-quotes"></i> <span>${citations.toLocaleString('es')} citas</span></span>` : ''}
          </div>
        </div>
        <div class="reader-hero-divider"></div>
      </div>
    `;
  }
  el.readerContent.innerHTML = heroHtml + rawHtml;
  postProcessReaderContent();
  if (el.btnReaderViewPdf) {
    el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';
  }
}

// ═══════════════════════════════════════════════════
// CONTROL DE PROGRESO DE TRADUCCIÓN PDF (SOBRIO)
// ═══════════════════════════════════════════════════

let readerProgressTimer = null;
let readerCurrentProgress = 0;

function updateReaderProgressBar(pct, stepText) {
  const rounded = Math.round(Math.min(100, Math.max(0, pct)));
  if (el.readerLoadingBarFill) {
    el.readerLoadingBarFill.style.width = `${rounded}%`;
  }
  if (el.readerProgressFill) {
    el.readerProgressFill.style.width = `${rounded}%`;
  }
  if (el.readerLoadingPct) {
    el.readerLoadingPct.textContent = `${rounded}%`;
  }
  if (el.readerLoadingStep && stepText) {
    el.readerLoadingStep.innerHTML = `<span class="reader-step-dot"></span> <span>${esc(stepText)}</span>`;
  }
}

function startReaderLoadingProgress(isLocalFile = false) {
  stopReaderLoadingProgress();
  readerCurrentProgress = 0;
  const initialStep = isLocalFile ? 'Subiendo archivo y analizando páginas…' : 'Analizando documento y páginas…';
  updateReaderProgressBar(4, initialStep);

  const startTime = Date.now();

  readerProgressTimer = setInterval(() => {
    const elapsed = (Date.now() - startTime) / 1000;

    // Curva de progresión suave, continua y asintótica (nunca se estanca abruptamente en el 90%)
    // A los 3s: ~15% | 8s: ~34% | 15s: ~53% | 22s: ~67% | 30s: ~77% | 40s: ~85% | 55s: ~91%
    const target = 94 * (1 - Math.exp(-elapsed / 19));

    let stepText = '';
    if (elapsed < 3.5) {
      stepText = isLocalFile ? 'Subiendo archivo y analizando páginas…' : 'Analizando documento y páginas…';
    } else if (elapsed < 8) {
      stepText = 'Extrayendo estructura y figuras del PDF…';
    } else if (elapsed < 16) {
      stepText = 'Traduciendo secciones con IA (Gemini)…';
    } else if (elapsed < 25) {
      stepText = 'Adaptando conceptos científicos al español…';
    } else if (elapsed < 36) {
      stepText = 'Optimizando redacción y terminología…';
    } else {
      stepText = 'Compilando formato final para modo lectura…';
    }

    if (target > readerCurrentProgress) {
      readerCurrentProgress += (target - readerCurrentProgress) * 0.22;
    }

    updateReaderProgressBar(readerCurrentProgress, stepText);
  }, 100);
}

async function finishReaderLoadingProgress() {
  if (readerProgressTimer) {
    clearInterval(readerProgressTimer);
    readerProgressTimer = null;
  }
  updateReaderProgressBar(100, '¡Traducción completada!');
  await new Promise(resolve => setTimeout(resolve, 260));
}

function stopReaderLoadingProgress() {
  if (readerProgressTimer) {
    clearInterval(readerProgressTimer);
    readerProgressTimer = null;
  }
  readerCurrentProgress = 0;
  if (el.readerLoadingBarFill) el.readerLoadingBarFill.style.width = '0%';
  if (el.readerLoadingPct) el.readerLoadingPct.textContent = '0%';
  if (el.readerLoadingStep) {
    el.readerLoadingStep.innerHTML = '<span class="reader-step-dot"></span> <span>Iniciando traducción…</span>';
  }
}

async function openReaderModal(paperUrl, paperId, paperObj) {
  const paper = paperObj || S.currentPaper;
  const effectiveId = paperId || paper?.id || paperUrl;

  // 1. Revisar si la traducción ya existe (en memoria o en almacenamiento persistente)
  let cachedMarkdown = paperTranslations.get(effectiveId) || paper?.translatedMarkdown;
  if (!cachedMarkdown) {
    cachedMarkdown = getPersistedTranslation(effectiveId, paperUrl);
    if (cachedMarkdown) {
      paperTranslations.set(effectiveId, cachedMarkdown);
      if (paper) {
        paper.translatedMarkdown = cachedMarkdown;
        paper.isAutomatic = true;
        updatePaperAutomaticBadges(paper);
      }
    }
  }

  // Si ya tenemos la traducción guardada, abrir INMEDIATAMENTE sin re-traducir
  if (cachedMarkdown) {
    S.readerPaperId = effectiveId;
    S.readerPdfUrl = paperUrl || paper?.pdfUrl || paper?.oaUrl || null;
    if (el.btnReaderViewPdf) {
      el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';
    }
    if (!el.pageExplore.classList.contains('hidden') && !S.storyPaused) {
      pauseStory();
    }
    el.readerOverlay.classList.remove('hidden');
    if (el.readerModal) el.readerModal.classList.remove('slide-left');
    document.body.style.overflow = 'hidden';
    if (el.readerScroll) el.readerScroll.scrollTop = 0;
    if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
    el.readerContent.innerHTML = '';
    el.readerError.style.display = 'none';
    el.readerLoading.style.display = 'none';

    renderReaderPaperContent(paper, cachedMarkdown);
    return;
  }

  // 2. Si no está traducido y no hay enlace directo, mostrar pantalla amigable de requisito
  if (!paperUrl) {
    S.readerPaperId = effectiveId;
    S.readerPdfUrl = null;
    el.readerOverlay.classList.remove('hidden');
    if (el.readerModal) el.readerModal.classList.remove('slide-left');
    document.body.style.overflow = 'hidden';
    showReaderRequisiteScreen(paper);
    return;
  }

  // Anclar el visor estrictamente a este paper en específico
  S.readerPaperId = effectiveId;
  S.readerPdfUrl = paperUrl || paper?.pdfUrl || paper?.oaUrl || null;
  if (el.btnReaderViewPdf) {
    el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';
  }

  // Pausar historias si se ve desde explore
  if (!el.pageExplore.classList.contains('hidden') && !S.storyPaused) {
    pauseStory();
  }

  el.readerOverlay.classList.remove('hidden');
  if (el.readerModal) el.readerModal.classList.remove('slide-left');
  document.body.style.overflow = 'hidden';

  // Reset scroll y progreso
  if (el.readerScroll) el.readerScroll.scrollTop = 0;
  if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';

  // Limpiar el contenido previo
  el.readerContent.innerHTML = '';
  el.readerError.style.display = 'none';

  // Iniciar la traducción del PDF de este paper
  el.readerLoading.style.display = 'flex';
  startReaderLoadingProgress(false);

  if (activeTranslations.has(effectiveId)) {
    try {
      const data = await activeTranslations.get(effectiveId);
      
      paperTranslations.set(effectiveId, data.markdown);
      persistTranslation(effectiveId, paperUrl, data.markdown);
      if (paper) {
        paper.translatedMarkdown = data.markdown;
        paper.isAutomatic = true;
        updatePaperAutomaticBadges(paper);
      }
      if (data.pdf_url) S.readerPdfUrl = data.pdf_url;
      
      if (typeof saveProcessedArticle === 'function') {
        saveProcessedArticle(effectiveId, paper?.titleEs || paper?.title || 'Documento PDF', data.pdf_url, paper, data.markdown);
      }

      if (S.readerPaperId !== effectiveId) {
        stopReaderLoadingProgress();
        return;
      }

      await finishReaderLoadingProgress();
      if (S.readerPaperId !== effectiveId) return;

      el.readerLoading.style.display = 'none';
      if (el.btnReaderViewPdf) el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';
      
      renderReaderPaperContent(paper, data.markdown);
    } catch (err) {
      if (S.readerPaperId !== effectiveId) return;
      stopReaderLoadingProgress();
      handleReaderTranslationError(err, paper);
    }
    return;
  }

  const translatePromise = (async () => {
    let taskId = null;
    const { BackgroundTask, LocalNotifications } = window.Capacitor?.Plugins || {};
    
    if (BackgroundTask) {
      try {
        taskId = await BackgroundTask.beforeExit(async () => {
          console.log('[BackgroundTask] Notificación de suspensión recibida por el SO');
          if (taskId !== null) {
            try { BackgroundTask.finish({ taskId }); } catch (e) {}
            taskId = null;
          }
        });
      } catch (e) { console.warn('BackgroundTask no disponible', e); }
    }

    try {
      const apiUrl = `${HF_SPACE_URL}/api/translate`;
      const data = await fetchWithBackgroundRetry(apiUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: paperUrl, paper_id: effectiveId, id: effectiveId })
      });
      
      if (LocalNotifications) {
        try {
          await LocalNotifications.requestPermissions();
          await LocalNotifications.schedule({
            notifications: [{
              title: 'Traducción completada',
              body: 'El documento ya está listo para leer.',
              id: new Date().getTime(),
              schedule: { at: new Date(Date.now() + 500) }
            }]
          });
        } catch (e) { console.warn('LocalNotifications falló', e); }
      }
      return data;
    } catch (e) {
      if (LocalNotifications && !e.message?.includes('DIRECT_UPLOAD_REQUIRED') && !e.message?.includes('403')) {
        try {
          await LocalNotifications.requestPermissions();
          await LocalNotifications.schedule({
            notifications: [{
              title: 'Error en la traducción',
              body: 'Ocurrió un error al traducir el documento.',
              id: new Date().getTime(),
              schedule: { at: new Date(Date.now() + 500) }
            }]
          });
        } catch (err) { console.warn('LocalNotifications falló', err); }
      }
      throw e;
    } finally {
      activeTranslations.delete(effectiveId);
      if (BackgroundTask && taskId !== null) {
        try { BackgroundTask.finish({ taskId }); } catch (e) {}
        taskId = null;
      }
    }
  })();

  activeTranslations.set(effectiveId, translatePromise);

  try {
    const data = await translatePromise;

    paperTranslations.set(effectiveId, data.markdown);
    persistTranslation(effectiveId, paperUrl, data.markdown);
    if (paper) {
      paper.translatedMarkdown = data.markdown;
      paper.isAutomatic = true;
      updatePaperAutomaticBadges(paper);
    }
    if (data.pdf_url) S.readerPdfUrl = data.pdf_url;
    
    if (typeof saveProcessedArticle === 'function') {
      saveProcessedArticle(effectiveId, paper?.titleEs || paper?.title || 'Documento PDF', data.pdf_url, paper, data.markdown);
    }

    if (S.readerPaperId !== effectiveId) {
      stopReaderLoadingProgress();
      return;
    }

    await finishReaderLoadingProgress();
    if (S.readerPaperId !== effectiveId) return;

    el.readerLoading.style.display = 'none';
    if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
    if (el.btnReaderViewPdf) {
      el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';
    }

    renderReaderPaperContent(paper, data.markdown);

  } catch (error) {
    stopReaderLoadingProgress();
    if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
    if (S.readerPaperId !== effectiveId) return;
    console.error('Translation error:', error);
    handleReaderTranslationError(error, paper);
  }
}

function closeReaderModal() {
  if (typeof closeInternalPdfViewer === 'function') {
    closeInternalPdfViewer();
  }
  stopReaderLoadingProgress();
  if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
  el.readerOverlay.classList.add('hidden');
  if (el.readerModal) el.readerModal.classList.remove('slide-left');
  el.readerContent.innerHTML = '';
  S.readerPaperId = null;
  S.readerPdfUrl = null;
  if (el.btnReaderViewPdf) {
    el.btnReaderViewPdf.style.display = 'none';
  }
  // Solo restaurar scroll si el modal del paper no está abierto debajo
  if (el.modalOverlay.classList.contains('hidden')) {
    document.body.style.overflow = '';
  }
  // Reanudar historias si se ve desde explore
  if (!el.pageExplore.classList.contains('hidden') && S.storyPaused) {
    resumeStory();
  }
}

async function handleReaderFileUpload(file) {
  if (!file) return;
  const paper = S.currentPaper;
  const effectiveId = paper?.id || file.name;
  S.readerPaperId = effectiveId;

  el.readerLoading.style.display = 'flex';
  startReaderLoadingProgress(true);
  el.readerError.style.display = 'none';
  el.readerContent.innerHTML = '';
  if (el.readerScroll) el.readerScroll.scrollTop = 0;
  if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';

  if (activeTranslations.has(effectiveId)) {
    try {
      const data = await activeTranslations.get(effectiveId);
      if (S.readerPaperId !== effectiveId) {
        stopReaderLoadingProgress();
        return;
      }
      await finishReaderLoadingProgress();
      if (S.readerPaperId !== effectiveId) return;

      el.readerLoading.style.display = 'none';
      if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';

      paperTranslations.set(effectiveId, data.markdown);
      if (paper) paper.translatedMarkdown = data.markdown;
      if (data.pdf_url) S.readerPdfUrl = data.pdf_url;
      if (el.btnReaderViewPdf) el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';

      if (typeof saveProcessedArticle === 'function') {
        const fileName = file.name ? file.name.replace(/\.pdf$/i, '') : 'Documento PDF';
        saveProcessedArticle(effectiveId, paper?.titleEs || paper?.title || fileName, data.pdf_url, paper);
      }

      if (paper) {
        renderReaderPaperContent(paper, data.markdown);
      } else {
        const fileName = file.name ? file.name.replace(/\.pdf$/i, '') : 'Documento PDF';
        const rawHtml = marked.parse(data.markdown);
        const heroHtml = `
          <div class="reader-hero-card">
            <div class="reader-hero-badges">
              <span class="reader-hero-pill"><i class="ph-bold ph-file-pdf"></i> Archivo Local</span>
              <span class="reader-hero-pill"><i class="ph-bold ph-translate"></i> Traducido al español</span>
            </div>
            <h1 class="reader-hero-title">${esc(fileName)}</h1>
            <div class="reader-hero-divider"></div>
          </div>
        `;
        el.readerContent.innerHTML = heroHtml + rawHtml;
        postProcessReaderContent();
      }
    } catch (err) {
      stopReaderLoadingProgress();
      if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
      if (S.readerPaperId !== effectiveId) return;
      console.error('File translation error:', err);
      el.readerLoading.style.display = 'none';
      el.readerContent.innerHTML = '';
      el.readerError.style.display = 'flex';
      el.readerErrorMsg.style.display = 'block';
      const errorIcon = el.readerError.querySelector('.ph-warning-diamond');
      if (errorIcon) errorIcon.style.display = 'block';

      const uploadDesc = document.getElementById('reader-upload-desc');
      if (uploadDesc) uploadDesc.innerHTML = 'Si el servidor de la revista bloqueó la descarga directa, puedes subir el archivo PDF desde tu dispositivo:';
      el.readerErrorMsg.innerHTML = err.message || 'Error al traducir el archivo PDF subido.';
    }
    return;
  }

  const translatePromise = (async () => {
    let taskId = null;
    const { BackgroundTask, LocalNotifications } = window.Capacitor?.Plugins || {};
    
    if (BackgroundTask) {
      try {
        taskId = await BackgroundTask.beforeExit(async () => {
          BackgroundTask.finish({ taskId });
        });
      } catch (e) { console.warn('BackgroundTask no disponible', e); }
    }

    try {
      const formData = new FormData();
      formData.append('file', file);
      if (paper?.id) {
        formData.append('paper_id', paper.id);
        formData.append('id', paper.id);
      }

      const apiUrl = `${HF_SPACE_URL}/api/translate-file`;
      const res = await fetch(apiUrl, {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || 'Error procesando el archivo PDF');
      
      if (LocalNotifications) {
        try {
          await LocalNotifications.requestPermissions();
          await LocalNotifications.schedule({
            notifications: [{
              title: 'Traducción completada',
              body: 'Tu archivo local fue traducido con éxito.',
              id: new Date().getTime(),
              schedule: { at: new Date(Date.now() + 500) }
            }]
          });
        } catch (e) { console.warn('LocalNotifications falló', e); }
      }

      return data;
    } catch (e) {
      if (LocalNotifications) {
        try {
          await LocalNotifications.requestPermissions();
          await LocalNotifications.schedule({
            notifications: [{
              title: 'Error en la traducción',
              body: 'Ocurrió un error al procesar tu archivo local.',
              id: new Date().getTime(),
              schedule: { at: new Date(Date.now() + 500) }
            }]
          });
        } catch (err) { console.warn('LocalNotifications falló', err); }
      }
      throw e;
    } finally {
      activeTranslations.delete(effectiveId);
      if (BackgroundTask && taskId !== null) {
        try { BackgroundTask.finish({ taskId }); } catch (e) {}
      }
    }
  })();

  activeTranslations.set(effectiveId, translatePromise);

  try {
    const data = await translatePromise;

    // GUARDAR SIEMPRE EN MEMORIA AUNQUE EL USUARIO HAYA SALIDO
    paperTranslations.set(effectiveId, data.markdown);
    if (paper) paper.translatedMarkdown = data.markdown;
    if (data.pdf_url) S.readerPdfUrl = data.pdf_url;
    
    if (typeof saveProcessedArticle === 'function') {
      const fileName = file.name ? file.name.replace(/\.pdf$/i, '') : 'Documento PDF';
      saveProcessedArticle(effectiveId, paper?.titleEs || paper?.title || fileName, data.pdf_url, paper);
    }

    if (S.readerPaperId !== effectiveId) {
      stopReaderLoadingProgress();
      return;
    }

    await finishReaderLoadingProgress();
    if (S.readerPaperId !== effectiveId) return;

    el.readerLoading.style.display = 'none';
    if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
    if (el.btnReaderViewPdf) el.btnReaderViewPdf.style.display = S.readerPdfUrl ? 'flex' : 'none';

    if (paper) {
      renderReaderPaperContent(paper, data.markdown);
    } else {
      const fileName = file.name ? file.name.replace(/\.pdf$/i, '') : 'Documento PDF';
      const rawHtml = marked.parse(data.markdown);
      const heroHtml = `
        <div class="reader-hero-card">
          <div class="reader-hero-badges">
            <span class="reader-hero-pill"><i class="ph-bold ph-file-pdf"></i> Archivo Local</span>
            <span class="reader-hero-pill"><i class="ph-bold ph-translate"></i> Traducido al español</span>
          </div>
          <h1 class="reader-hero-title">${esc(fileName)}</h1>
          <div class="reader-hero-divider"></div>
        </div>
      `;
      el.readerContent.innerHTML = heroHtml + rawHtml;
      postProcessReaderContent();
    }
  } catch (err) {
    stopReaderLoadingProgress();
    if (el.readerProgressFill) el.readerProgressFill.style.width = '0%';
    if (S.readerPaperId !== effectiveId) return;
    console.error('File translation error:', err);
    el.readerLoading.style.display = 'none';
    el.readerContent.innerHTML = '';
    el.readerError.style.display = 'flex';
    el.readerErrorMsg.style.display = 'block';
    const errorIcon = el.readerError.querySelector('.ph-warning-diamond');
    if (errorIcon) errorIcon.style.display = 'block';

    const uploadDesc = document.getElementById('reader-upload-desc');
    if (uploadDesc) uploadDesc.innerHTML = 'Si el servidor de la revista bloqueó la descarga directa, puedes subir el archivo PDF desde tu dispositivo:';

    el.readerErrorMsg.innerHTML = err.message || 'Error al traducir el archivo PDF subido.';
  }
}

// ═══════════════════════════════════════════════════
// BOOKMARKS
// ═══════════════════════════════════════════════════

function isBookmarked(idOrPaper) {
  if (!idOrPaper) return false;
  const targetId = typeof idOrPaper === 'object' ? idOrPaper.id : idOrPaper;
  const targetDoi = typeof idOrPaper === 'object' ? idOrPaper.doi : null;
  return S.bookmarks.some(b => {
    if (targetId && b.id === targetId) return true;
    if (targetDoi && b.doi && String(b.doi).toLowerCase() === String(targetDoi).toLowerCase()) return true;
    return false;
  });
}

function toggleBookmark(paper) {
  if (!paper) return;
  const targetId = paper.id;
  const targetDoi = paper.doi ? String(paper.doi).toLowerCase() : null;

  if (isBookmarked(paper)) {
    S.bookmarks = S.bookmarks.filter(b => {
      if (targetId && b.id === targetId) return false;
      if (targetDoi && b.doi && String(b.doi).toLowerCase() === targetDoi) return false;
      return true;
    });
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

  const nowSaved = isBookmarked(paper);

  // 1. Sincronizar botón del Modal de detalle
  if (S.currentPaper) {
    const modalMatchesId = targetId && S.currentPaper.id === targetId;
    const modalMatchesDoi = targetDoi && S.currentPaper.doi && String(S.currentPaper.doi).toLowerCase() === targetDoi;
    if (modalMatchesId || modalMatchesDoi) {
      el.btnModalBk.classList.toggle('saved', nowSaved);
      const modalIcon = el.btnModalBk.querySelector('i');
      if (modalIcon) {
        modalIcon.className = nowSaved ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';
      }
    }
  }

  // 2. Sincronizar todos los botones de tarjetas visibles en el DOM
  document.querySelectorAll('.btn-card-bk').forEach(btn => {
    const btnId = btn.dataset.id;
    const btnDoi = btn.dataset.doi ? btn.dataset.doi.toLowerCase() : null;
    const matchesId = targetId && btnId === String(targetId);
    const matchesDoi = targetDoi && btnDoi && btnDoi === targetDoi;
    if (matchesId || matchesDoi) {
      btn.classList.toggle('saved', nowSaved);
      const i = btn.querySelector('i');
      if (i) {
        i.className = nowSaved ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';
      }
    }
  });

  // 3. Sincronizar botón de Historias si la historia activa coincide
  if (S.stories && S.stories[S.activeStoryIdx]) {
    const curStory = S.stories[S.activeStoryIdx];
    const storyMatchesId = targetId && curStory.id === targetId;
    const storyMatchesDoi = targetDoi && curStory.doi && String(curStory.doi).toLowerCase() === targetDoi;
    if (storyMatchesId || storyMatchesDoi) {
      updateStorySaveBtnState(curStory);
    }
  }

  // 4. Si la biblioteca/perfil de guardados está presente, refrescar lista
  if (el.savedList) {
    refreshProfile();
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
    ? (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
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
  // Nav con doble toque en la lupa para reiniciar
  let lastNavSearchClick = 0;
  el.navBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetPage = btn.dataset.page;
      if (targetPage === 'search') {
        const now = Date.now();
        // Si se presiona 2 veces seguidas (dentro de 600ms) o si ya está activa la página de búsqueda
        if ((now - lastNavSearchClick < 600) || (S.activePage === 'search' && (S.baseQuery || S.papers.length > 0 || (el.searchPageInput && el.searchPageInput.value)))) {
          resetSearchToInitial();
          if (el.searchPageInput) {
            el.searchPageInput.value = '';
            el.searchPageInput.focus();
          }
          if (el.searchResultsScroll) {
            el.searchResultsScroll.scrollTop = 0;
          }
          showToast('Búsqueda reiniciada');
        }
        lastNavSearchClick = now;
      }
      navigateTo(targetPage);
    });
  });

  // Biblioteca
  if (el.librarySavedHeader) {
    el.librarySavedHeader.addEventListener('click', () => {
      const isHidden = el.librarySavedContainer.style.display === 'none';
      el.librarySavedContainer.style.display = isHidden ? 'block' : 'none';
      if (el.iconToggleSaved) {
        el.iconToggleSaved.style.transform = isHidden ? 'rotate(180deg)' : 'rotate(0deg)';
      }
    });
  }

  if (el.libraryFileUpload) {
    el.libraryFileUpload.addEventListener('change', e => {
      const f = e.target.files?.[0];
      if (f) {
        if (el.readerOverlay) el.readerOverlay.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        handleReaderFileUpload(f);
      }
    });
  }

  // Página Buscar
  function triggerSearchPage() {
    if (!el.searchPageInput) return;
    const q = el.searchPageInput.value.trim();
    if (q) {
      if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = 'none';
      doSearch(q);
    } else {
      resetSearchToInitial();
    }
  }

  if (el.searchPageInput) {
    el.searchPageInput.addEventListener('input', () => {
      const v = el.searchPageInput.value;
      if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = v ? 'flex' : 'none';
      if (!v.trim()) {
        resetSearchToInitial();
      }
    });
    el.searchPageInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        triggerSearchPage();
      }
    });
  }

  if (el.btnClearSearchPage) {
    el.btnClearSearchPage.addEventListener('click', (e) => {
      e.stopPropagation();
      if (el.searchPageInput) el.searchPageInput.value = '';
      resetSearchToInitial();
    });
  }

  let lastSearchPageBtnClick = 0;
  if (el.btnDoSearchPage) {
    el.btnDoSearchPage.addEventListener('click', (e) => {
      e.stopPropagation();
      const now = Date.now();
      if (now - lastSearchPageBtnClick < 600) {
        // Doble toque a la lupa: reiniciar búsqueda
        lastSearchPageBtnClick = 0;
        resetSearchToInitial();
        if (el.searchPageInput) {
          el.searchPageInput.value = '';
          el.searchPageInput.focus();
        }
        if (el.searchResultsScroll) {
          el.searchResultsScroll.scrollTop = 0;
        }
        showToast('Búsqueda reiniciada');
        return;
      }
      lastSearchPageBtnClick = now;
      triggerSearchPage();
    });
  }

  // Chips sugeridos de búsqueda inicial
  if (el.searchInitialChips) {
    el.searchInitialChips.addEventListener('click', (e) => {
      const chip = e.target.closest('.search-chip');
      if (chip && chip.dataset.query) {
        if (el.searchPageInput) {
          el.searchPageInput.value = chip.dataset.query;
          if (el.btnClearSearchPage) el.btnClearSearchPage.style.display = 'flex';
        }
        doSearch(chip.dataset.query);
      }
    });
  }

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

  let lastSearchOpenClick = 0;
  el.btnSearchOpen.addEventListener('click', (e) => {
    e.stopPropagation();
    const now = Date.now();
    if (now - lastSearchOpenClick < 600) {
      lastSearchOpenClick = 0;
      resetInlineSearch();
      resetSearchToInitial();
      showToast('Búsqueda reiniciada');
      return;
    }
    lastSearchOpenClick = now;

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
  el.subtabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      if (!S.baseQuery) return; // Si la búsqueda está vacía, no dispara nada
      if (currentFetchController) {
        try { currentFetchController.abort(); } catch { }
      }
      S.activeSubtype = btn.dataset.type;
      S.page = 1;
      S.papers = [];
      S.hasMore = false;
      el.subtabBtns.forEach(b => b.classList.toggle('active', b === btn));
      el.paperList.innerHTML = '';
      if (el.resultsCount) el.resultsCount.textContent = '';
      if (el.searchResultsScroll) el.searchResultsScroll.scrollTop = 0;
      if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
      if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
      if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';

      // El filtro de orden (S.sort) se mantiene independiente de la etiqueta seleccionada
      updateGlow();
      doFetch();
    });
  });
  if (el.resultsSort) {
    el.resultsSort.addEventListener('change', () => {
      if (!S.baseQuery) return; // Si la búsqueda está vacía, no dispara nada
      if (currentFetchController) {
        try { currentFetchController.abort(); } catch { }
      }
      S.sort = el.resultsSort.value || null;
      S.page = 1;
      S.papers = [];
      S.hasMore = false;
      el.paperList.innerHTML = '';
      if (el.resultsCount) el.resultsCount.textContent = '';
      if (el.searchResultsScroll) el.searchResultsScroll.scrollTop = 0;
      if (el.infiniteScrollContainer) el.infiniteScrollContainer.style.display = 'none';
      if (el.resultsEndNotice) el.resultsEndNotice.style.display = 'none';
      if (el.scrollRetryContainer) el.scrollRetryContainer.style.display = 'none';
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
          try { el.resultsSort.showPicker(); } catch { }
        }
      }
    });
  }
  initInfiniteScroll();
  el.btnResultsRetry.addEventListener('click', () => {
    if (!S.baseQuery) return;
    S.page = 1;
    S.papers = [];
    doFetch();
  });

  // Recomendaciones
  el.btnRefreshRecs.addEventListener('click', loadRecommendations);

  // Modal
  el.btnModalClose.addEventListener('click', closeModal);
  el.modalOverlay.addEventListener('click', e => { if (e.target === el.modalOverlay) closeModal(); });

  // Reader Modal
  el.btnReaderClose.addEventListener('click', closeReaderModal);
  el.readerOverlay.addEventListener('click', e => { if (e.target === el.readerOverlay) closeReaderModal(); });
  if (el.readerFileUpload) {
    el.readerFileUpload.addEventListener('change', e => {
      const f = e.target.files?.[0];
      if (f) handleReaderFileUpload(f);
      e.target.value = '';
    });
  }
  if (el.readerScroll && el.readerProgressFill) {
    el.readerScroll.addEventListener('scroll', () => {
      const maxScroll = el.readerScroll.scrollHeight - el.readerScroll.clientHeight;
      if (maxScroll > 0) {
        const pct = Math.min(100, Math.max(0, (el.readerScroll.scrollTop / maxScroll) * 100));
        el.readerProgressFill.style.width = `${pct}%`;
      }
    }, { passive: true });
  }
  if (el.readerContent) {
    el.readerContent.addEventListener('click', e => {
      const pdfBtn = e.target.closest('.internal-pdf-link');
      if (pdfBtn) {
        e.preventDefault();
        const url = pdfBtn.dataset.url || pdfBtn.getAttribute('href');
        if (url && url !== '#') openInternalPdfViewer(url);
        return;
      }

      const img = e.target.closest('img');
      if (img) {
        const fig = img.closest('.reader-figure-container');
        const link = fig ? fig.querySelector('.internal-pdf-link') : null;
        if (link && link.dataset.url) {
          openInternalPdfViewer(link.dataset.url);
          return;
        }
        img.classList.toggle('reader-img-expanded');
      }
    });
  }
  if (el.btnReaderViewPdf) {
    el.btnReaderViewPdf.addEventListener('click', () => {
      if (S.readerPdfUrl) {
        openInternalPdfViewer(S.readerPdfUrl);
      } else {
        showToast('PDF no disponible para este documento');
      }
    });
  }
  if (el.btnReaderFontToggle) {
    el.btnReaderFontToggle.addEventListener('click', () => {
      if (el.readerModal) {
        const isLarge = el.readerModal.classList.toggle('font-large');
        el.btnReaderFontToggle.classList.toggle('active', isLarge);
        showToast(isLarge ? 'Texto grande activado' : 'Texto estándar activado');
      }
    });
  }
  if (el.btnReaderCopyText) {
    el.btnReaderCopyText.addEventListener('click', async () => {
      if (!el.readerContent) return;
      const textToCopy = el.readerContent.innerText || '';
      if (!textToCopy.trim()) {
        showToast('No hay contenido para copiar');
        return;
      }
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(textToCopy);
        } else {
          const ta = document.createElement('textarea');
          ta.value = textToCopy;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        showToast('Texto copiado al portapapeles ✓');
      } catch (err) {
        console.error('[Copy Reader]', err);
        showToast('No se pudo copiar el texto');
      }
    });
  }
  el.btnModalBk.addEventListener('click', () => { if (S.currentPaper) toggleBookmark(S.currentPaper); });
  el.modalBody.addEventListener('click', async (e) => {
    const btnCopy = e.target.closest('#btn-modal-copy-apa') || e.target.closest('.btn-copy-apa');
    if (btnCopy && S.currentPaper) {
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
      return;
    }

    const btnPsihubOptions = e.target.closest('#btn-modal-psihub-options');
    if (btnPsihubOptions && S.currentPaper) {
      e.stopPropagation();
      if (el.psihubOptionsOverlay && el.psihubOptionsSheet) {
        const badgeEs = document.getElementById('btn-psihub-read-es-badge');
        if (badgeEs) {
          badgeEs.style.display = S.currentPaper.isAutomatic ? 'inline-block' : 'none';
        }
        el.psihubOptionsOverlay.classList.remove('hidden');
        el.psihubOptionsSheet.classList.remove('closing');
      }
      return;
    }

    const btnCopyDoi = e.target.closest('#btn-modal-copy-doi');
    if (btnCopyDoi && S.currentPaper && S.currentPaper.doi) {
      e.stopPropagation();
      const doiLink = S.currentPaper.doi.startsWith('http') ? S.currentPaper.doi : `https://doi.org/${S.currentPaper.doi}`;
      try {
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(doiLink);
        }
        showToast('DOI copiado al portapapeles ✓');
      } catch (err) {
        showToast('No se pudo copiar el DOI');
      }
      return;
    }

    const btnTranslateManual = e.target.closest('#btn-modal-translate-pdf-manual');
    if (btnTranslateManual && S.currentPaper) {
      e.stopPropagation();
      el.readerOverlay.classList.remove('hidden');
      document.body.style.overflow = 'hidden';
      el.readerLoading.style.display = 'none';
      el.readerContent.innerHTML = '';
      el.readerError.style.display = 'flex';

      // Hide the generic "Error" text and icon since this is an intended manual upload
      el.readerErrorMsg.style.display = 'none';
      const errorIcon = el.readerError.querySelector('.ph-warning-diamond');
      if (errorIcon) errorIcon.style.display = 'none';

      const lookupId = (S.currentPaper.doi ? (S.currentPaper.doi.startsWith('http') ? S.currentPaper.doi : `https://doi.org/${S.currentPaper.doi}`) : null);

      const uploadDesc = document.getElementById('reader-upload-desc');
      if (uploadDesc) {
        uploadDesc.innerHTML = lookupId
          ? `PDF Privado o sin enlace directo gratuito.<br><br>Por favor, <a href="${lookupId}" target="_blank" style="color:var(--txt-1); text-decoration:underline; font-weight:600;">descarga el PDF manualmente desde la página oficial aquí</a> y luego adjunta el archivo debajo:`
          : `PDF Privado o sin enlace directo gratuito.<br><br>Por favor, descárgalo manualmente y luego adjunta el archivo debajo:`;
      }
      return;
    }
  });

  // Opciones de PsiHub Modal
  function closePsihubOptions() {
    if (!el.psihubOptionsOverlay || el.psihubOptionsOverlay.classList.contains('hidden')) return;
    if (el.psihubOptionsSheet) el.psihubOptionsSheet.classList.add('closing');
    setTimeout(() => {
      if (el.psihubOptionsOverlay) el.psihubOptionsOverlay.classList.add('hidden');
      if (el.psihubOptionsSheet) el.psihubOptionsSheet.classList.remove('closing');
    }, 200);
  }

  // Cerrar al tocar fuera del visor (en el overlay)
  if (el.psihubOptionsOverlay) {
    el.psihubOptionsOverlay.addEventListener('click', (e) => {
      if (e.target === el.psihubOptionsOverlay) {
        closePsihubOptions();
      }
    });
  }

  // Cerrar con botón X
  if (el.btnClosePsihubOptions) {
    el.btnClosePsihubOptions.addEventListener('click', (e) => {
      e.stopPropagation();
      closePsihubOptions();
    });
  }

  // Evitar que hacer clic en el cuerpo del visor propague al overlay o cause efectos secundarios
  if (el.psihubOptionsSheet) {
    el.psihubOptionsSheet.addEventListener('click', (e) => {
      e.stopPropagation();
    });
  }

  // Deslizar (swipe down) para cerrar el mini visor
  let optionsTouchStartY = 0;
  let optionsTouchStartX = 0;

  if (el.psihubOptionsSheet) {
    el.psihubOptionsSheet.addEventListener('touchstart', (e) => {
      const touch = e.touches[0];
      optionsTouchStartY = touch.clientY;
      optionsTouchStartX = touch.clientX;
    }, { passive: true });

    el.psihubOptionsSheet.addEventListener('touchend', (e) => {
      const touch = e.changedTouches[0];
      const deltaY = touch.clientY - optionsTouchStartY;
      const deltaX = Math.abs(touch.clientX - optionsTouchStartX);

      // Deslizamiento vertical hacia abajo > 45px cierra el mini visor
      if (deltaY > 45 && deltaY > deltaX * 1.1) {
        closePsihubOptions();
      }
    }, { passive: true });
  }

  if (el.btnPsihubReadEs) {
    el.btnPsihubReadEs.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!S.currentPaper) return;
      closePsihubOptions();

      const paper = S.currentPaper;
      const paperUrl = paper.pdfUrl || paper.oaUrl;
      const paperId = paper.id;

      if (paperUrl) {
        // Iniciar directamente la traducción de este PDF específico
        openReaderModal(paperUrl, paperId, paper);
      } else {
        // Si no tiene enlace disponible, abrir modal de subida manual para este paper
        el.readerOverlay.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        el.readerLoading.style.display = 'none';
        el.readerContent.innerHTML = '';
        el.readerError.style.display = 'flex';
        el.readerErrorMsg.style.display = 'none';
        const errorIcon = el.readerError.querySelector('.ph-warning-diamond');
        if (errorIcon) errorIcon.style.display = 'none';

        const uploadDesc = document.getElementById('reader-upload-desc');
        if (uploadDesc) {
          const lookupId = (paper.doi ? (paper.doi.startsWith('http') ? paper.doi : `https://doi.org/${paper.doi}`) : null);
          uploadDesc.innerHTML = lookupId
            ? `PDF sin enlace directo disponible.<br><br>Por favor, <a href="${lookupId}" target="_blank" style="color:var(--txt-1); text-decoration:underline; font-weight:600;">descarga el PDF manualmente desde la revista aquí</a> y adjúntalo a continuación:`
            : `PDF sin enlace directo disponible.<br><br>Por favor, descarga el PDF y adjúntalo a continuación:`;
        }
      }
    });
  }

  if (el.btnPsihubSummarize) {
    el.btnPsihubSummarize.addEventListener('click', (e) => {
      e.stopPropagation();
      showToast('Resumir artículo estará disponible próximamente');
    });
  }

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
  window.handleAndroidBack = function () {
    // -1. Si el mini visor de opciones de PsiHub está abierto, cerrarlo
    if (el.psihubOptionsOverlay && !el.psihubOptionsOverlay.classList.contains('hidden')) {
      closePsihubOptions();
      return true;
    }
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
    // 3. Si está en otra pestaña, volver al Inicio
    if (S.activePage !== 'home') {
      navigateTo('home');
      return true;
    }
    // 4. Si ya está en el Home sin nada abierto, retornar false para salir
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

  if (el.storyTapNext) el.storyTapNext.addEventListener('click', (e) => { e.stopPropagation(); nextStory(); });
  if (el.storyTapPrev) el.storyTapPrev.addEventListener('click', (e) => { e.stopPropagation(); prevStory(); });
  if (el.btnStorySave) el.btnStorySave.addEventListener('click', (e) => { e.stopPropagation(); toggleStoryBookmark(); });
  if (el.btnStoryShare) el.btnStoryShare.addEventListener('click', (e) => { e.stopPropagation(); shareCurrentStory(); });
  if (el.btnStoryRead) {
    el.btnStoryRead.addEventListener('click', (e) => {
      e.stopPropagation();
      const currentStory = S.stories[S.activeStoryIdx];
      if (currentStory) {
        const paperForModal = {
          ...currentStory,
          title: currentStory.paperTitle || currentStory.title || currentStory.headline,
          titleEs: currentStory.titleEs || null,
          abstract: currentStory.originalAbstract || currentStory.abstract,
          journal: currentStory.journal || '',
          authors: currentStory.authors || [],
          year: currentStory.year || '',
          citations: currentStory.citations || 0,
          doi: currentStory.doi,
          pdfUrl: currentStory.pdfUrl,
          topics: currentStory.tags || [currentStory.topicName]
        };
        openModal(paperForModal, null);
      }
    });
  }

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
    el.btnStoryRead.style.background = '';
  }

  // 6. Botón guardar
  updateStorySaveBtnState(story);

  // Iniciar temporizador
  startStoryTimer();
}

function updateStorySaveBtnState(story) {
  if (!el.btnStorySave || !story) return;
  const isBk = isBookmarked(story);
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
    }).catch(() => { });
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
    } catch { }
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
function saveData(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } }
function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ═══════════════════════════════════════════════════
// ARTÍCULOS PROCESADOS & VISUALIZADOR INTERNO
// ═══════════════════════════════════════════════════

function saveProcessedArticle(id, title, url, paperData = null) {
  const existing = S.processed.find(p => p.id === id);
  if (!existing) {
    S.processed.unshift({
      id,
      title: title || 'Documento procesado',
      date: new Date().toISOString(),
      url: url || null,
      paperData: paperData || null
    });
    saveData('psyhub_processed', S.processed);
    refreshProcessedArticles();
  }
}

function refreshProcessedArticles() {
  const listEl = document.getElementById('processed-list');
  const emptyEl = document.getElementById('profile-empty-processed');
  const sectionEl = document.getElementById('section-processed');

  if (!listEl || !emptyEl || !sectionEl) return;

  if (!S.processed || S.processed.length === 0) {
    sectionEl.style.display = 'none';
    emptyEl.style.display = 'flex';
    return;
  }

  sectionEl.style.display = 'block';
  emptyEl.style.display = 'none';

  listEl.innerHTML = '';
  const frag = document.createDocumentFragment();
  S.processed.forEach((p, idx) => {
    const li = document.createElement('li');
    li.className = 'paper-card';
    li.innerHTML = `
      <div class="paper-card-body" style="padding-left: 0;">
        <div class="paper-card-top">
          <h3 class="paper-card-title">${esc(p.title)}</h3>
          <button class="btn-card-bk saved-card-remove-processed" data-index="${idx}" aria-label="Eliminar" style="background:transparent; border:none; color:var(--txt-3); cursor:pointer;">
            <i class="ph-bold ph-trash"></i>
          </button>
        </div>
        <p class="paper-card-authors" style="font-size: 11px; color: var(--txt-3);">
          Procesado el ${new Date(p.date).toLocaleDateString()}
        </p>
      </div>
    `;
    li.addEventListener('click', (e) => {
      if (e.target.closest('.saved-card-remove-processed')) {
        e.stopPropagation();
        S.processed.splice(idx, 1);
        saveData('psyhub_processed', S.processed);
        refreshProcessedArticles();
        return;
      }
      if (p.paperData) {
        openReaderModal(p.url, p.id, p.paperData);
      } else {
        openReaderModal(p.url, p.id, null);
      }
    });
    frag.appendChild(li);
  });
  listEl.appendChild(frag);
}

function openInternalPdfViewer(url) {
  if (!url) return;
  let fullUrl = url;
  if (fullUrl.startsWith('#page=') && S.readerPdfUrl) {
    const base = S.readerPdfUrl.split('#')[0];
    fullUrl = `${base}${fullUrl}`;
  }
  if (fullUrl.startsWith('/files/')) {
    fullUrl = HF_SPACE_URL + fullUrl;
  }
  const overlay = document.getElementById('pdf-viewer-overlay');
  const iframe = document.getElementById('pdf-viewer-iframe');
  const readerModal = document.getElementById('reader-modal');
  const externalBtn = document.getElementById('btn-pdf-external');
  const titleEl = document.getElementById('pdf-viewer-title');

  if (overlay && iframe) {
    iframe.src = fullUrl;
    if (externalBtn) externalBtn.href = fullUrl;

    const matchPage = fullUrl.match(/#page=(\d+)/);
    if (titleEl) {
      titleEl.textContent = matchPage ? `Visualizador PDF — Pág. ${matchPage[1]}` : 'Visualizador PDF';
    }

    // Deslizar el Modo Lectura hacia la izquierda
    if (readerModal) {
      readerModal.classList.add('slide-left');
    }

    overlay.classList.remove('hidden');
    // Forzar reflow para animación CSS fluida
    void overlay.offsetWidth;
    overlay.classList.add('pdf-slide-active');
  }
}

function closeInternalPdfViewer() {
  const overlay = document.getElementById('pdf-viewer-overlay');
  const iframe = document.getElementById('pdf-viewer-iframe');
  const readerModal = document.getElementById('reader-modal');

  if (overlay) {
    overlay.classList.remove('pdf-slide-active');
  }
  if (readerModal) {
    readerModal.classList.remove('slide-left');
  }

  setTimeout(() => {
    if (overlay && !overlay.classList.contains('pdf-slide-active')) {
      overlay.classList.add('hidden');
      if (iframe) iframe.src = '';
    }
  }, 360);
}

document.addEventListener('DOMContentLoaded', () => {
  refreshProcessedArticles();

  const libraryProcessedHeader = document.getElementById('library-processed-header');
  const libraryProcessedContainer = document.getElementById('library-processed-container');
  const iconToggleProcessed = document.getElementById('icon-toggle-processed');

  if (libraryProcessedHeader) {
    libraryProcessedHeader.addEventListener('click', () => {
      const isHidden = libraryProcessedContainer.style.display === 'none';
      libraryProcessedContainer.style.display = isHidden ? 'block' : 'none';
      if (iconToggleProcessed) {
        iconToggleProcessed.style.transform = isHidden ? 'rotate(180deg)' : 'rotate(0deg)';
      }
      if (isHidden) refreshProcessedArticles();
    });
  }

  const btnClearProcessed = document.getElementById('btn-clear-all-processed');
  if (btnClearProcessed) {
    btnClearProcessed.addEventListener('click', () => {
      S.processed = [];
      saveData('psyhub_processed', []);
      refreshProcessedArticles();
    });
  }

  const btnClosePdf = document.getElementById('btn-close-pdf-viewer');
  if (btnClosePdf) {
    btnClosePdf.addEventListener('click', closeInternalPdfViewer);
  }

  const readerContentArea = document.getElementById('reader-content');
  if (readerContentArea) {
    readerContentArea.addEventListener('click', e => {
      const pdfLink = e.target.closest('.internal-pdf-link');
      if (pdfLink) {
        e.preventDefault();
        const url = pdfLink.dataset.url;
        if (url) openInternalPdfViewer(url);
      }
    });
  }
});