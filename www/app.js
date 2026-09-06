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
  'psicodrama':    'psychodrama',
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
  { id: 'neurociencia',  label: 'Neurociencia',    abbr: 'NEU', query: 'neuroscience neurobiology cognitive neuroscience brain', color: '#E91E63' },
  { id: 'tcc',           label: 'TCC / CBT',       abbr: 'TCC', query: 'cognitive behavioral therapy CBT',                      color: '#1565C0' },
  { id: 'tercera_ola',   label: 'Tercera Ola',     abbr: '3°',  query: 'third wave ACT DBT mindfulness acceptance',             color: '#1DB954' },
  { id: 'sistemica',     label: 'Sistémica',       abbr: 'SIS', query: 'systemic family therapy',                               color: '#00838F' },
  { id: 'psicoanalisis', label: 'Psicoanálisis',   abbr: 'PSA', query: 'psychoanalysis psychoanalytic therapy',                 color: '#7B2D8B' },
  { id: 'humanismo',     label: 'Humanismo',       abbr: 'HUM', query: 'humanistic person-centered therapy Rogers',             color: '#C17900' },
  { id: 'gestalt',       label: 'Gestalt',         abbr: 'GES', query: 'gestalt therapy awareness contact',                     color: '#2E7D32' },
  { id: 'existencial',   label: 'Existencial',     abbr: 'EXI', query: 'existential therapy logotherapy meaning Frankl',        color: '#4527A0' },
  { id: 'fenomenologia', label: 'Fenomenología',   abbr: 'FEN', query: 'phenomenological existential psychotherapy',             color: '#5B3FE0' },
  { id: 'segunda_ola',   label: 'Segunda Ola',     abbr: '2°',  query: 'rational emotive behavior REBT cognitive therapy',      color: '#E65C00' },
  { id: 'psicodrama',    label: 'Psicodrama',      abbr: 'PDR', query: 'psychodrama Moreno role playing',                       color: '#C62828' },
];

// ═══════════════════════════════════════════════════
// ═══════════════════════════════════════════════════
// CLASIFICACIÓN INTELIGENTE DE PAPERS (Teórico / Evidencia / Clínico)
// ═══════════════════════════════════════════════════

const KW_EVIDENCIA_STRONG = [
  'randomized controlled', 'randomised controlled', 'meta-analysis', 'systematic review',
  'rct', 'clinical trial', 'empirical study', 'placebo', 'effect size', 'double-blind',
  'cohort study', 'control group', 'statistical analysis'
];
const KW_EVIDENCIA_MID = [
  'efficacy', 'effectiveness', 'evidence-based', 'controlled trial', 'empirical',
  'quantitative', 'outcomes measurement'
];

const KW_CLINICO_STRONG = [
  'case report', 'case study', 'treatment protocol', 'clinical intervention',
  'psychotherapy session', 'therapeutic relationship', 'clinical practice',
  'patient care', 'psychotherapeutic technique', 'therapist', 'therapeutic alliance',
  'clinical psychology', 'clinical study', 'clinical application', 'clinical case'
];
const KW_CLINICO_MID = [
  'clinical', 'clinician', 'clinicians', 'patient', 'patients', 'intervention',
  'disorder', 'depression', 'anxiety', 'psychiatric', 'diagnosis', 'symptom reduction',
  'counseling', 'manualized', 'inpatient', 'outpatient', 'session', 'therapy'
];

const KW_TEORICO_STRONG = [
  'theoretical framework', 'conceptual model', 'epistemology', 'psychoanalytic theory',
  'historical review', 'philosophical', 'constructivism', 'hermeneutic',
  'theoretical foundation', 'phenomenological', 'ontological', 'critical review'
];
const KW_TEORICO_MID = [
  'conceptual', 'theory', 'framework', 'paradigm', 'theoretical', 'perspectives',
  'epistemological', 'psychoanalysis'
];

function getPaperSubtypeScores(paper) {
  if (!paper) return { evidencia: 0, clinico: 0, teorico: 0 };
  const title = (paper.title || '').toLowerCase();
  const abstract = (paper.abstract || '').toLowerCase();
  const topics = (paper.topics || []).join(' ').toLowerCase();
  const fullText = `${title} ${title} ${topics} ${abstract}`;

  let scoreEvidencia = 0;
  let scoreClinico = 0;
  let scoreTeorico = 0;

  KW_EVIDENCIA_STRONG.forEach(kw => { if (fullText.includes(kw)) scoreEvidencia += 3; });
  KW_EVIDENCIA_MID.forEach(kw => { if (fullText.includes(kw)) scoreEvidencia += 1; });

  KW_CLINICO_STRONG.forEach(kw => { if (fullText.includes(kw)) scoreClinico += 3; });
  KW_CLINICO_MID.forEach(kw => { if (fullText.includes(kw)) scoreClinico += 1; });

  KW_TEORICO_STRONG.forEach(kw => { if (fullText.includes(kw)) scoreTeorico += 3; });
  KW_TEORICO_MID.forEach(kw => { if (fullText.includes(kw)) scoreTeorico += 1; });

  return { evidencia: scoreEvidencia, clinico: scoreClinico, teorico: scoreTeorico };
}

function classifyPaper(paper) {
  const scoresObj = getPaperSubtypeScores(paper);
  const scores = [
    { tag: 'evidencia', score: scoresObj.evidencia },
    { tag: 'clinico',   score: scoresObj.clinico },
    { tag: 'teorico',   score: scoresObj.teorico }
  ].sort((a, b) => b.score - a.score);

  const tags = [];
  if (scores[0].score > 0) {
    tags.push(scores[0].tag);
    if (scores[1].score >= 3 && scores[1].score >= scores[0].score * 0.65) {
      tags.push(scores[1].tag);
    }
  } else {
    tags.push('teorico');
  }

  return tags;
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
// DOM
// ═══════════════════════════════════════════════════

const $ = id => document.getElementById(id);
const el = {
  // Páginas
  pageHome:    $('page-home'),
  pageExplore: $('page-explore'),
  pageProfile: $('page-profile'),

  // Home
  btnSearchOpen:   $('btn-search-open'),
  searchInlineWrapper: $('search-inline-wrapper'),
  searchInput:     $('search-input'),
  btnClearSearch:  $('btn-clear-search'),
  corrientesRow:   $('corrientes-row'),
  recsList:        $('recs-list'),
  recsSubtitle:    $('recs-subtitle'),
  btnRefreshRecs:  $('btn-refresh-recs'),

  // Explorar (Historias Directas & Ambient Nebula)
  exploreBgGlow:         $('explore-bg-glow'),
  exploreStoryContainer: $('explore-story-container'),
  storyProgressBar:      $('story-progress-bar'),
  exploreTopicAvatar:    $('explore-topic-avatar'),
  exploreTopicIcon:      $('explore-topic-icon'),
  exploreStoryTopicLabel:$('explore-story-topic-label'),
  btnStoryPause:         $('btn-story-pause'),
  btnRefreshStories:     $('btn-refresh-stories'),
  storyTapPrev:          $('story-tap-prev'),
  storyTapNext:          $('story-tap-next'),
  exploreStoryCard:      $('explore-story-card'),
  exploreBadgeTopic:     $('explore-badge-topic'),
  exploreJournalCite:    $('explore-journal-cite'),
  exploreStoryHook:      $('explore-story-hook'),
  exploreHeadline:       $('explore-headline'),
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
  profileAvatar:     $('profile-avatar'),
  avatarInitials:    $('avatar-initials'),
  profileName:       $('profile-name'),
  profileRole:       $('profile-role'),
  statSaved:         $('stat-saved'),
  statSearches:      $('stat-searches'),
  sectionSaved:      $('section-saved'),
  savedList:         $('saved-list'),
  profileEmptySaved: $('profile-empty-saved'),
  btnClearAllSaved:  $('btn-clear-all-saved'),
  trEmail:           $('tr-email'),
  btnSaveTrEmail:    $('btn-save-tr-email'),
  trEmailStatus:     $('tr-email-status'),

  // Toast
  toast: $('toast'),
};

// ═══════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
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

function navigateTo(page) {
  closeResults(false);

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
    <div class="corriente-card" style="background:${c.color};" data-id="${c.id}">
      <span class="corriente-abbr">${c.abbr}</span>
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
  S.papers = [];
  el.paperList.innerHTML = '';
  el.btnLoadMore.style.display = 'none';
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

function buildCard(paper) {
  const li = document.createElement('li');
  li.className = 'paper-card';

  const corriente   = S.activeCorriente;
  const cardColor   = corriente ? corriente.color : '#555';
  const isBk        = isBookmarked(paper.id);
  const displayTitle = paper.titleEs || paper.title;
  const snippet      = paper.abstractEs || (paper.abstract ? paper.abstract.slice(0, 150) + '...' : '');
  const firstAuthor = paper.authors[0] || '';
  const moreAuthors = paper.authors.length > 1 ? ` +${paper.authors.length - 1}` : '';
  const tags        = (S.activeSubtype && S.activeSubtype !== 'all') ? [S.activeSubtype] : classifyPaper(paper);

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
        <span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--txt-3);"><i class="ph-bold ph-quotes"></i>${paper.citations.toLocaleString('es')}</span>
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
  if (S.bookmarks.length > 0) {
    // Basar recomendaciones en el paper guardado más reciente
    const lastBk = S.bookmarks[S.bookmarks.length - 1];
    // Tomar el título para construir query
    const baseTitle = lastBk.title.split(' ').slice(0, 5).join(' ');
    query = baseTitle;
    el.recsSubtitle.textContent = 'Basado en tus guardados';
  } else {
    // Aleatorio de una corriente al azar
    const randCorrente = CORRIENTES[Math.floor(Math.random() * CORRIENTES.length)];
    query = randCorrente.query;
    el.recsSubtitle.textContent = `Explorando: ${randCorrente.label}`;
  }

  try {
    const { results } = await fetchPapers({ query, perPage: 8, sort: 'cited_by_count:desc' });

    if (!results.length) {
      el.recsList.innerHTML = '<p style="font-size:13px; color:var(--txt-3); padding: 8px 0;">No se encontraron resultados.</p>';
      return;
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

    // Detectar corriente para color
    const detectCorriente = paper => {
      const text = (paper.title + ' ' + paper.topics.join(' ')).toLowerCase();
      return CORRIENTES.find(c =>
        c.query.split(' ').some(term => text.includes(term.toLowerCase()))
      ) || CORRIENTES[Math.floor(Math.random() * CORRIENTES.length)];
    };

    el.recsList.innerHTML = results.map((paper, i) => {
      const tags = classifyPaper(paper);
      const corriente = detectCorriente(paper);
      return `
        <div class="rec-card" data-index="${i}">
          <div class="rec-body" style="padding-left: 0;">
            <p class="rec-title">${esc(paper.titleEs || paper.title)}</p>
            <p class="rec-snippet">${esc(paper.abstractEs || paper.abstract?.slice(0,150) || '')}</p>
            <div class="rec-meta">
              ${paper.year ? `<span><i class="ph-bold ph-calendar-blank"></i>${paper.year}</span>` : ''}
              <span><i class="ph-bold ph-quotes"></i>${paper.citations.toLocaleString('es')} citas</span>
            </div>
          </div>
        </div>
      `;
    }).join('');

    // Bind click en cada rec-card
    el.recsList.querySelectorAll('.rec-card').forEach(card => {
      const index = parseInt(card.dataset.index);
      card.addEventListener('click', () => {
        const paper = results[index];
        const corriente = detectCorriente(paper);
        S.activeCorriente = corriente;
        openModal(paper, corriente);
      });
    });

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
    paper.citations = typeof paper.citations === 'number' ? paper.citations : 0;

    S.currentPaper = paper;
    corriente = corriente || (paper.corrienteId ? CORRIENTES.find(c => c.id === paper.corrienteId) : null) || S.activeCorriente;

    const isBk = isBookmarked(paper.id);
    el.btnModalBk.classList.toggle('saved', isBk);
    el.btnModalBk.querySelector('i').className = isBk ? 'ph-fill ph-bookmark-simple' : 'ph-bold ph-bookmark-simple';

    const tags = (S.activeSubtype && S.activeSubtype !== 'all') ? [S.activeSubtype] : classifyPaper(paper);
    const displayTitle = paper.titleEs || paper.title || 'Sin título';

    // Mostrar modal con abstract existente o aviso
    el.modalBody.innerHTML = buildModalHTML(paper, corriente, tags, displayTitle, paper.abstractEs, false);
    el.modalBody.scrollTop = 0;
    el.modalOverlay.classList.remove('hidden');
    el.modalSheet.classList.remove('closing');
    document.body.style.overflow = 'hidden';

    // Si el paper no tiene abstract (ej. guardado previo a la actualización), buscarlo en OpenAlex
    if (!paper.abstract && paper.id) {
      const abstractEl = document.getElementById('modal-abstract-text');
      if (abstractEl) abstractEl.textContent = 'Cargando información completa desde OpenAlex…';
      try {
        const full = await fetchWorkById(paper.id);
        if (full && S.currentPaper?.id === paper.id) {
          paper.abstract = full.abstract;
          if (full.topics?.length) paper.topics = full.topics;
          if (full.journal) paper.journal = full.journal;
          if (full.firstInstitution) paper.firstInstitution = full.firstInstitution;
          if (full.oaUrl && !paper.oaUrl) paper.oaUrl = full.oaUrl;
          if (full.doi && !paper.doi) paper.doi = full.doi;

          // Re-renderizar modal con la información completa
          const updatedTags = (S.activeSubtype && S.activeSubtype !== 'all') ? [S.activeSubtype] : classifyPaper(paper);
          el.modalBody.innerHTML = buildModalHTML(paper, corriente, updatedTags, displayTitle, paper.abstractEs, false);
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

function buildModalHTML(paper, corriente, tags, displayTitle, abstractEs, translating) {
  const tagColor = corriente ? corriente.color : '#555';
  const tagLabel_ = corriente ? corriente.label : 'Artículo';
  const abstractContent = paper.abstract || 'Abstract no disponible en los metadatos de OpenAlex.';
  const authors = Array.isArray(paper.authors) ? paper.authors : [];
  const citations = typeof paper.citations === 'number' ? paper.citations : 0;
  const topics = Array.isArray(paper.topics) ? paper.topics : [];

  return `
    <div class="modal-tags">
      <span class="modal-tag" style="background:${tagColor}22; color:${tagColor}; border:1px solid ${tagColor}55;">${esc(tagLabel_)}</span>
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
        ? `<a class="btn-cta-primary" href="${esc(paper.oaUrl)}" target="_blank" rel="noopener"><i class="ph-bold ph-file-pdf"></i> Leer texto completo (Open Access)</a>`
        : ''}
      <button class="btn-cta-secondary" disabled style="opacity:0.4; cursor:not-allowed;">
        <i class="ph-bold ph-translate"></i> Traducir PDF completo <span style="font-size:11px; opacity:0.7;">(próximamente)</span>
      </button>
      ${paper.doi
        ? `<a class="btn-cta-secondary" href="${esc(paper.doi)}" target="_blank" rel="noopener"><i class="ph-bold ph-link"></i> Ver DOI original</a>`
        : ''}
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
    S.bookmarks.push({
      id: paper.id,
      title: paper.title || 'Sin título',
      titleEs: paper.titleEs || null,
      authors: Array.isArray(paper.authors) ? paper.authors : [],
      year: paper.year || null,
      citations: typeof paper.citations === 'number' ? paper.citations : 0,
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
  el.profileName.value = S.profile.name || '';
  el.profileRole.value = S.profile.role || '';
  el.trEmail.value = localStorage.getItem('psyhub_tr_email') || '';
  updateAvatarInitials();
  updateStatBadges();
}

function updateAvatarInitials() {
  const parts = (S.profile.name || '').trim().split(' ').filter(Boolean);
  const initials = parts.length >= 2
    ? (parts[0][0] + parts[parts.length-1][0]).toUpperCase()
    : (S.profile.name || '?').slice(0,2).toUpperCase();
  el.avatarInitials.textContent = initials;
}

function refreshProfile() {
  updateStatBadges();
  if (S.bookmarks.length === 0) {
    el.sectionSaved.style.display      = 'none';
    el.profileEmptySaved.style.display = 'flex';
    return;
  }
  el.sectionSaved.style.display      = 'block';
  el.profileEmptySaved.style.display = 'none';

  el.savedList.innerHTML = S.bookmarks.map((b, index) => {
    const displayTitle = b.titleEs || b.title || 'Sin título';
    const firstAuthor = (Array.isArray(b.authors) && b.authors[0]) ? b.authors[0] : 'Autor no disponible';
    const yearStr = b.year ? `${b.year} · ` : '';
    const citesStr = `${(b.citations || 0).toLocaleString('es')} citas`;

    return `
      <li class="saved-card" data-index="${index}">
        <div class="saved-card-header">
          <span class="saved-card-badge"><i class="ph-bold ph-bookmark-simple"></i> Guardado</span>
          <button type="button" class="saved-card-remove" data-index="${index}" aria-label="Eliminar guardado" title="Eliminar guardado">
            <i class="ph-bold ph-trash"></i>
          </button>
        </div>
        <div class="saved-card-main" data-index="${index}">
          <h4 class="saved-card-title">${esc(displayTitle)}</h4>
          <p class="saved-card-authors">${esc(firstAuthor)}</p>
          <div class="saved-card-footer">
            <span class="saved-card-meta">${yearStr}${citesStr}</span>
            <span class="saved-card-action">Ver artículo <i class="ph-bold ph-arrow-right"></i></span>
          </div>
        </div>
      </li>
    `;
  }).join('');

  // Event listeners directos a cada tarjeta
  el.savedList.querySelectorAll('.saved-card-main').forEach(elem => {
    elem.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = parseInt(elem.dataset.index, 10);
      openSavedPaperByIndex(idx);
    });
  });

  el.savedList.querySelectorAll('.saved-card-remove').forEach(elem => {
    elem.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = parseInt(elem.dataset.index, 10);
      removeSavedPaperByIndex(idx);
    });
  });
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


  el.btnSearchOpen.addEventListener('click', () => {
    el.searchInlineWrapper.classList.toggle('active');
    if (el.searchInlineWrapper.classList.contains('active')) {
      setTimeout(() => el.searchInput.focus(), 300);
    } else {
      triggerSearch();
    }
  });

  el.searchInput.addEventListener('input', () => {
    const v = el.searchInput.value;
    el.btnClearSearch.style.display = v ? 'flex' : 'none';
  });

  const triggerSearch = () => {
    const q = el.searchInput.value.trim();
    if (q) {
      doSearch(q);
      el.searchInput.blur();
    }
  };

  el.searchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      triggerSearch();
    }
  });

  // Se eliminó searchIcon.addEventListener duplicado

  el.btnClearSearch.addEventListener('click', () => {
    el.searchInput.value = '';
    el.btnClearSearch.style.display = 'none';
    closeResults();
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
    // 2. Si el buscador expandible está abierto, cerrarlo
    if (el.searchContainer && el.searchContainer.classList.contains('open')) {
      el.searchContainer.classList.remove('open');
      if (el.searchInput) {
        el.searchInput.value = '';
        el.searchInput.blur();
      }
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

  if (el.storyTapNext)  el.storyTapNext.addEventListener('click', nextStory);
  if (el.storyTapPrev)  el.storyTapPrev.addEventListener('click', prevStory);
  if (el.btnStorySave)  el.btnStorySave.addEventListener('click', toggleStoryBookmark);
  if (el.btnStoryShare) el.btnStoryShare.addEventListener('click', shareCurrentStory);

  // Mantener presionado para pausar (touch y mouse)
  if (el.exploreStoryContainer) {
    let holdTimeout = null;
    const onHoldStart = (e) => {
      if (e.target.closest('.explore-header-controls') || e.target.closest('.explore-actions-bar')) return;
      holdTimeout = setTimeout(() => {
        pauseStory();
      }, 160);
    };
    const onHoldEnd = () => {
      if (holdTimeout) clearTimeout(holdTimeout);
      if (S.storyPaused) resumeStory();
    };

    el.exploreStoryContainer.addEventListener('touchstart', onHoldStart, { passive: true });
    el.exploreStoryContainer.addEventListener('touchend', onHoldEnd, { passive: true });
    el.exploreStoryContainer.addEventListener('mousedown', onHoldStart);
    el.exploreStoryContainer.addEventListener('mouseup', onHoldEnd);
  }

  // Perfil
  el.profileName.addEventListener('input', () => {
    S.profile.name = el.profileName.value;
    saveData('psyhub_profile', S.profile);
    updateAvatarInitials();
  });
  el.profileRole.addEventListener('change', () => {
    S.profile.role = el.profileRole.value;
    saveData('psyhub_profile', S.profile);
  });

  // Email de traducción
  el.btnSaveTrEmail.addEventListener('click', () => {
    const email = el.trEmail.value.trim();
    if (email && !email.includes('@')) {
      el.trEmailStatus.textContent = 'Ingresá un email válido.';
      el.trEmailStatus.style.color = 'var(--red)';
      return;
    }
    localStorage.setItem('psyhub_tr_email', email);
    el.trEmailStatus.textContent = email ? '✓ Email guardado. Límite aumentado a 10.000 palabras/día.' : '✓ Email eliminado. Usando modo anónimo.';
    el.trEmailStatus.style.color = 'var(--green)';
  });

  // Limpiar guardados
  el.btnClearAllSaved.addEventListener('click', () => {
    if (!confirm('¿Eliminar todos los artículos guardados?')) return;
    S.bookmarks = [];
    saveData('psyhub_bk', S.bookmarks);
    refreshProfile();
    updateStatBadges();
    showToast('Guardados eliminados');
  });
}

// ═══════════════════════════════════════════════════
// SECCIÓN EXPLORAR — HISTORIAS DIRECTAS CON NEBULA
// ═══════════════════════════════════════════════════

const GITHUB_STORIES_REMOTE_URL = window.PSYHUB_STORIES_REMOTE_URL || '';

async function loadStories(forceRefresh = false) {
  try {
    let data = null;

    if (GITHUB_STORIES_REMOTE_URL && (forceRefresh || !S.stories.length)) {
      try {
        const res = await fetch(GITHUB_STORIES_REMOTE_URL, { cache: forceRefresh ? 'no-cache' : 'default' });
        if (res.ok) {
          data = await res.json();
          console.log('[Stories] Cargadas desde GitHub remoto ✓');
        }
      } catch (errRemote) {
        console.warn('[Stories] Error con GitHub remoto, usando copia local:', errRemote);
      }
    }

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
  if (metaCites) metaCites.innerHTML = `<i class="ph-bold ph-quotes"></i> ${esc(story.citations || 0)} citas`;

  // 5. Botón de lectura (PDF / DOI)
  if (el.btnStoryRead) {
    const paperUrl = story.pdfUrl || story.url || (story.doi ? `https://doi.org/${story.doi}` : '#');
    el.btnStoryRead.href = paperUrl;
    el.btnStoryRead.style.background = color;
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
  if (S.storyElapsed > 1500) {
    renderCurrentStory(S.activeStoryIdx);
  } else if (S.activeStoryIdx > 0) {
    renderCurrentStory(S.activeStoryIdx - 1);
  } else {
    renderCurrentStory(0);
  }
}

function toggleStoryBookmark() {
  if (!S.stories || !S.stories[S.activeStoryIdx]) return;
  const story = S.stories[S.activeStoryIdx];

  const paperObj = {
    id: story.id,
    title: story.headline || story.hook,
    titleEs: story.headline || story.hook,
    authors: [story.topicName],
    year: story.year || new Date().getFullYear(),
    citations: 0,
    oaUrl: story.pdfUrl || story.url,
    doi: story.doi || null,
    abstract: `${story.hook}\n\n${story.finding}\n\n${story.takeaway || ''}`,
    abstractEs: `${story.hook}\n\n${story.finding}\n\n${story.takeaway || ''}`,
    topics: story.tags || [story.topicName],
    journal: story.journal || 'Curaduría Diaria',
    firstInstitution: 'PsiHub Science Stories',
    corrienteId: story.topicId
  };

  toggleBookmark(paperObj);
  updateStorySaveBtnState(story);
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
