/* =====================================================================
 * ui.js — TransferPath SEP Planner (UI layer)
 *
 * DATA:  everything comes from the FastAPI backend through api.js
 *        (fetchInstitutions, fetchCollegeCatalog, searchCourses, generateSep, dev injector).
 *        Plans, term names ("Fall 2026"), graphs and critical paths are all
 *        computed server-side by sep_engine; this file only renders them.
 *        Plans depend on College + University + Major + GE pathway, plus the
 *        student's completed courses, starting semester and summer choice.
 *        AP scores are evaluated by ap_engine/apWaiver.js (loaded from the
 *        backend) against the college's AP chart and the target campus rules.
 *  GE:   the GE pathway's requirements come back per plan (plan.ge: satisfied / planned / open,
 *        with the college's eligible courses). The student picks GE courses in the term
 *        cards (Summer first, 9-unit limit); picks go back as ge_choices (section 4c).
 * ===================================================================== */

/* ---------------------------------------------------------------------
 * 1. CONFIG + STATE
 * ------------------------------------------------------------------- */
// API campus systems -> dropdown section headers (same labels as the <optgroup>s in index.html)
const SYSTEM_LABELS = { UC: 'UC Campuses', CSU: 'CSU Campuses' };
const SEASONS = ['Spring', 'Summer', 'Fall'];   // calendar order within a year
const SEASON_ICON = { Fall: 'fa-leaf', Spring: 'fa-seedling', Summer: 'fa-sun' };
const PLAN_ICON = {
  A: { icon: 'fa-bolt', cls: 'bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300' },
  B: { icon: 'fa-scale-balanced', cls: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-300' }
};
// Target-university names spelled differently in the AP campus dataset (ap_engine/data)
const AP_CAMPUS_ALIASES = { 'Cal State East Bay': 'csueb' };

const state = {
  institutions: { colleges: [], universities: [], majors: [] },   // each: [{ name, group }]
  catalog: null,          // { code: { title, units } } for the selected college
  catalogFor: null,
  // Required answers (section 3b). An empty list is NOT an answer; "none" is an explicit flag:
  //   completed [] + noCompleted false = unanswered    completed [] + noCompleted true = "No completed courses"
  completed: [],
  noCompleted: false,     // only ever true while `completed` is empty
  courseTitles: {},       // code -> title for completed-course tag tooltips
  apScores: [],           // [{ subject: 'AP Calculus BC', score: 5 }] -> evaluated by ap_engine (section 4b)
  noApScores: false,      // "No AP scores"; only ever true while `apScores` is empty
  pathway: null,          // GE pathway id ('cal_getc'): none until the student picks one
  gePathways: [],         // GET /ge-pathways rows: names, and whether each pathway's rules are loaded
  geChoices: [],          // GE courses picked in a term card: [{ requirement: '3A', course: 'ARTH 1', term: 'Summer 2027' }]
  startTerm: null,        // e.g. 'Fall 2026': none until the student picks one
  includeSummer: false,   // a switch, so always a definite Yes / No
  touched: new Set(),     // required sections the student has left: their errors show
  showAllErrors: false,   // a Generate click on an incomplete form shows every section's error
  loading: false,
  response: null,         // SEPResponse from POST /generate-sep
  plans: {},              // id -> { data: SemesterPlan, cy }
  panel: null,            // { planId, term, itemCodes, allOpen } while the GE panel is open
  golden: null,           // dev injector status when mock data is loaded
  requestId: 0
};

const $ = id => document.getElementById(id);
const sum = arr => arr.reduce((s, x) => s + x, 0);
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const esc = s => String(s).replace(/[&<>"']/g,
  ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
/** 'Summer 2027' -> "Su '27" (fits on a graph node) */
const shortTerm = term => {
  const [season, year] = String(term).split(' ');
  return `${season.slice(0, 2)} '${String(year).slice(2)}`;
};

/* ---------------------------------------------------------------------
 * 2. THEME
 * ------------------------------------------------------------------- */
const isDark = () => document.documentElement.classList.contains('dark');

const GRAPH_THEME = {
  light: {
    planned: '#2563eb', plannedBorder: '#1d4ed8',
    completed: '#cbd5e1', completedBorder: '#94a3b8', completedText: '#334155',
    critical: '#dc2626', criticalBorder: '#991b1b',
    ge: '#0f766e', geBorder: '#134e4a', geHover: '#14b8a6', summer: '#d97706',
    lane: '#0f766e', laneOpacity: 0.06, laneText: '#0f766e',
    nodeText: '#ffffff', edge: '#94a3b8',
    gridDot: 'rgba(15, 23, 42, 0.10)', gridBg: '#f8fafc'
  },
  dark: {
    planned: '#3b82f6', plannedBorder: '#93c5fd',
    completed: '#475569', completedBorder: '#64748b', completedText: '#e2e8f0',
    critical: '#ef4444', criticalBorder: '#fca5a5',
    ge: '#0f766e', geBorder: '#5eead4', geHover: '#99f6e4', summer: '#fbbf24',
    lane: '#14b8a6', laneOpacity: 0.08, laneText: '#5eead4',
    nodeText: '#ffffff', edge: '#64748b',
    gridDot: 'rgba(148, 163, 184, 0.14)', gridBg: '#0b1220'
  }
};

function buildGraphStyle() {
  const t = isDark() ? GRAPH_THEME.dark : GRAPH_THEME.light;
  return [
    { selector: 'node', style: {
      'shape': 'round-rectangle', 'width': 96, 'height': 48,
      'background-color': t.planned, 'border-width': 2, 'border-color': t.plannedBorder,
      'label': 'data(label)', 'color': t.nodeText, 'font-family': 'Inter, sans-serif',
      'font-size': 11, 'font-weight': 600, 'text-wrap': 'wrap',
      'text-valign': 'center', 'text-halign': 'center',
      'transition-property': 'background-color, border-color, color', 'transition-duration': '0.25s'
    }},
    { selector: 'node.completed', style: {
      'background-color': t.completed, 'border-color': t.completedBorder, 'color': t.completedText,
      'border-style': 'dashed'
    }},
    { selector: 'node.critical', style: {
      'background-color': t.critical, 'border-color': t.criticalBorder, 'border-width': 3
    }},
    { selector: 'node.ge', style: {
      'width': 108, 'background-color': t.ge, 'border-color': t.geBorder, 'border-width': 2, 'z-index': 10
    }},
    { selector: 'node.ge.summer', style: { 'border-color': t.summer, 'border-width': 3 } },
    { selector: 'node.major.hover', style: { 'border-width': 4 } },
    { selector: 'node.ge.hover', style: { 'border-width': 4, 'border-color': t.geHover } },
    { selector: 'node.lane', style: {
      'width': 'data(w)', 'height': 'data(h)', 'background-color': t.lane, 'background-opacity': t.laneOpacity,
      'border-width': 1, 'border-style': 'dashed', 'border-color': t.lane, 'border-opacity': 0.45,
      'color': t.laneText, 'font-size': 10, 'font-weight': 700, 'text-valign': 'top', 'text-margin-y': -6,
      'events': 'no', 'z-index': 0
    }},
    { selector: 'node:active', style: { 'overlay-opacity': 0.08 } },
    { selector: 'edge', style: {
      'width': 2, 'line-color': t.edge, 'target-arrow-color': t.edge,
      'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'arrow-scale': 1.1
    }},
    { selector: 'edge.coreq', style: { 'line-style': 'dashed' } },
    { selector: 'edge.critical', style: {
      'width': 4, 'line-color': t.critical, 'target-arrow-color': t.critical, 'z-index': 10
    }}
  ];
}

/** Re-theme every live graph: stylesheet + grid background. */
function applyGraphTheme() {
  const t = isDark() ? GRAPH_THEME.dark : GRAPH_THEME.light;
  Object.values(state.plans).forEach(p => {
    if (!p.cy) return;
    p.cy.style(buildGraphStyle());
    const el = p.cy.container();
    el.style.backgroundColor = t.gridBg;
    el.style.backgroundImage = `radial-gradient(${t.gridDot} 1.2px, transparent 1.2px)`;
    el.style.backgroundSize = '20px 20px';
  });
}

function toggleTheme() {
  const dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
  document.dispatchEvent(new CustomEvent('themechange', { detail: { dark } }));
}

$('themeToggle').addEventListener('click', toggleTheme);
document.addEventListener('themechange', applyGraphTheme);

/* ---------------------------------------------------------------------
 * 3. INPUTS: college / target / major search, completed-course tags,
 *    GE pathway, starting semester, summer toggle, dev injector
 * ------------------------------------------------------------------- */
const OPTION_CLS = 'px-3 py-2 cursor-pointer text-sm hover:bg-blue-50 dark:hover:bg-slate-700';
const GROUP_CLS = 'sticky top-0 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider ' +
  'bg-slate-50 dark:bg-slate-900 text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700 cursor-default';

/** Searchable dropdown. `groupOf(option)` adds a header row whenever the group changes. */
function wireDropdown(input, list, getOptions, onPick, { describe = () => '', groupOf = null, limit = 8 } = {}) {
  const render = () => {
    const q = input.value.trim().toLowerCase();
    const opts = getOptions().filter(o => o.toLowerCase().includes(q)).slice(0, limit);
    let lastGroup = null;
    list.innerHTML = opts.map(o => {
      const g = groupOf ? groupOf(o) : null;
      const head = g && g !== lastGroup ? `<li role="presentation" class="${GROUP_CLS}">${esc(g)}</li>` : '';
      lastGroup = g;
      const d = describe(o);
      return head + `<li class="${OPTION_CLS}" data-v="${esc(o)}"><span class="font-medium">${esc(o)}</span>` +
        (d ? `<span class="ml-2 text-slate-400 dark:text-slate-500">${esc(d)}</span>` : '') + '</li>';
    }).join('');
    list.classList.toggle('hidden', !opts.length || document.activeElement !== input);
  };
  input.addEventListener('input', render);
  input.addEventListener('focus', render);
  input.addEventListener('blur', () => setTimeout(() => list.classList.add('hidden'), 150));
  list.addEventListener('mousedown', e => {
    const li = e.target.closest('li');
    if (!li) return;
    e.preventDefault();                       // keep focus; group headers are not pickable
    if (li.dataset.v !== undefined) { onPick(li.dataset.v); render(); }
  });
  return render;
}

function pickInstitution(inputId, value) {
  $(inputId).value = value;
  $(inputId).blur();
  onInstitutionChange();
}

/* Dropdown options: built-in lists from the hidden <select>s in index.html (so the demo works with the
 * API offline), merged with GET /institutions when the API is reachable. Only these options are valid
 * answers (section 3b); nothing is pre-selected. */
const optionKey = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').trim().replace(/\s+/g, ' ').toLowerCase();   // "San José" == "San Jose"

function readOptions(selectId) {
  return [...$(selectId).options].map(o => ({
    name: o.value.trim(),
    group: o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : null
  }));
}

const BUILT_IN_OPTIONS = {
  colleges: readOptions('collegeOptions'),
  universities: readOptions('targetOptions'),
  majors: readOptions('majorOptions')
};

/** Union by name (built-in spelling wins), with each group's entries kept together in first-seen group order. */
function mergeOptions(...lists) {
  const seen = new Set();
  const items = lists.flat().filter(o => {
    const key = optionKey(o.name);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const groups = [...new Set(items.map(o => o.group))];
  return groups.flatMap(g => items.filter(o => o.group === g));
}

/** api = GET /institutions payload, or null when the API is unreachable. */
function setInstitutions(api) {
  const plain = names => (names || []).map(name => ({ name, group: null }));
  const fromApi = {
    colleges: plain(api?.colleges),
    universities: Object.entries(api?.universities || {})
      .flatMap(([system, names]) => names.map(name => ({ name, group: SYSTEM_LABELS[system] || system }))),
    majors: plain(api?.majors)
  };
  for (const key of ['colleges', 'universities', 'majors']) {
    state.institutions[key] = mergeOptions(BUILT_IN_OPTIONS[key], fromApi[key]);
  }
}

const optionNames = key => () => state.institutions[key].map(o => o.name);
const groupOfUniversity = name => state.institutions.universities.find(u => u.name === name)?.group ?? null;

/** The three institution inputs: [input id, option list, what the error message asks for]. */
const INSTITUTION_INPUTS = {
  college: ['collegeInput', 'colleges', 'a community college'],
  target: ['targetInput', 'universities', 'a target university'],
  major: ['majorInput', 'majors', 'an intended major']
};
/** The listed option the text names (any case, accents or spacing), else null: free text is never an answer. */
const findOption = (key, text) => {
  const k = optionKey(text);
  return (k && state.institutions[key].find(o => optionKey(o.name) === k)) || null;
};
const selectedOption = field => {
  const [id, key] = INSTITUTION_INPUTS[field];
  return findOption(key, $(id).value)?.name ?? null;
};

setInstitutions(null);

wireDropdown($('collegeInput'), $('collegeList'), optionNames('colleges'),
  v => pickInstitution('collegeInput', v));
wireDropdown($('targetInput'), $('targetList'), optionNames('universities'),
  v => pickInstitution('targetInput', v), { groupOf: groupOfUniversity, limit: Infinity });
wireDropdown($('majorInput'), $('majorList'), optionNames('majors'),
  v => pickInstitution('majorInput', v), { limit: Infinity });

['collegeInput', 'targetInput', 'majorInput'].forEach(id => {
  $(id).addEventListener('input', () => updateValidation());
  $(id).addEventListener('change', onInstitutionChange);
});

function onInstitutionChange() {
  // Typed text that names an option ("chabot college") becomes that option's exact spelling
  for (const field of Object.keys(INSTITUTION_INPUTS)) {
    const [id] = INSTITUTION_INPUTS[field];
    const name = selectedOption(field);
    if (name && $(id).value !== name) $(id).value = name;
  }
  updateValidation();
  loadCatalog();
}

/** Course catalog for the chosen college powers tag suggestions + validation (404 -> none uploaded). */
async function loadCatalog() {
  const cc = selectedOption('college') ?? '';
  if (cc === state.catalogFor) return;
  state.catalogFor = cc;
  setCourseFilter(null);                 // subject codes belong to one college
  let catalog = null;
  try { catalog = cc ? await fetchCollegeCatalog(cc) : null; } catch (e) { console.error(e); }
  if (state.catalogFor !== cc) return;   // a newer request superseded this one
  state.catalog = catalog;
  renderTags();
}

/* Completed courses: a searchable course picker with subject CATEGORIES.
 * Suggestions come from GET /colleges/{college}/courses (api.js searchCourses): the college's
 * catalog in ap_engine/data/catalog plus any uploaded pathway courses.
 *   - The backend resolves the SUBJECT first ("math" -> Mathematics / MTH) and returns courses
 *     grouped by subject. Each group starts with a CATEGORY option, followed by its COURSES.
 *   - Picking a category (click, or arrow keys + Enter) makes it a filter: a "[Mathematics x]"
 *     chip in the box, and only that subject is searched ("1" -> MTH 1, "calculus" -> ...).
 *   - Picking a course adds it as a completed-course tag. Categories are never added, and typed
 *     text is never added. */
const courseInput = $('courseInput');
const courseList = $('courseList');
const SUGGESTIONS = 20;            // courses shown when searching all subjects
const SUBJECT_LIST_LIMIT = 100;    // courses shown inside a chosen subject (largest Chabot subject: 95)
const COURSE_PLACEHOLDER = courseInput.placeholder;
const courseSearch = {
  filter: null,           // { code, name, count } while a subject category is chosen
  options: [],            // [{ kind: 'subject' | 'course', item }] in display order (arrow keys move through these)
  active: -1, message: '', more: 0, seq: 0, timer: null
};
const subjectName = s => s.name && s.name !== s.code ? s.name : s.code;

function queueCourseSearch() {
  clearTimeout(courseSearch.timer);
  const q = courseInput.value.trim();
  if (!q && !courseSearch.filter) { courseSearch.seq++; closeCourseResults(); return; }
  courseSearch.timer = setTimeout(() => runCourseSearch(q), 120);   // wait for a pause in typing
}

async function runCourseSearch(q) {
  const seq = ++courseSearch.seq;
  const cc = selectedOption('college');
  const filter = courseSearch.filter;
  const limit = filter ? SUBJECT_LIST_LIMIT : SUGGESTIONS;
  let data = null, message = '';
  if (!cc) {
    message = 'Choose a community college from the list first.';
  } else {
    try {
      // Ask for extra rows so courses already added can be dropped and still leave a full list
      data = await searchCourses(cc, q, Math.min(100, limit + state.completed.length), filter?.code);
      if (!data) {
        message = `No course catalog has been uploaded for ${cc} yet.`;
      } else if (!Array.isArray(data.groups)) {   // an older server process answered
        data = null;
        message = 'The server is running an older version of this code. Restart it: Ctrl+C, then python -m uvicorn main:app --reload';
      }
    } catch (e) {
      console.error(e);
      message = e.offline ? 'Can’t reach the server to search courses.' : e.message;
    }
  }
  if (seq !== courseSearch.seq) return;                       // a newer search superseded this one

  // Drop courses already added; keep `limit` courses, still grouped by subject.
  // A category stays listed even when its courses are cut off, so it can still be chosen.
  let room = limit, returned = 0, added = 0;
  const groups = (data?.groups ?? []).map(g => {
    const notAdded = g.courses.filter(c => !state.completed.includes(c.code));
    returned += g.courses.length;
    added += g.courses.length - notAdded.length;
    const courses = notAdded.slice(0, room);
    room -= courses.length;
    return { ...g, courses };
  });
  const shown = limit - room;
  if (data && !shown) {
    const what = filter ? `${subjectName(filter)} course` : 'course';
    // "Already added" only when the server returned real courses and every one of them is in the list
    const allAdded = returned > 0 && added === returned && data.total === returned;
    message = allAdded ? (q ? `Every ${what} matching “${q}” is already added.` : `Every ${what} is already added.`)
      : q ? `No ${what}s match “${q}”.` : `No ${what}s found.`;
  }
  const more = data ? data.total - added - shown : 0;
  showCourseResults(groups, message, more);
}

function showCourseResults(groups = [], message = '', more = 0) {
  const options = [];
  for (const g of groups) {
    if (!courseSearch.filter) options.push({ kind: 'subject', item: g });   // category = filter option
    for (const c of g.courses) options.push({ kind: 'course', item: c });
  }
  Object.assign(courseSearch, { options, message, more,
    // an exactly named course ("mth 1", "math 1" -> MTH 1) is pre-highlighted, so Enter adds it
    active: options.findIndex(o => o.kind === 'course' && o.item.match === 'code') });
  renderCourseResults();
}

function closeCourseResults() {
  showCourseResults();
}

const SUBJECT_ICON = `<span class="w-9 h-9 shrink-0 rounded-md grid place-items-center bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300">
  <i class="fa-solid fa-layer-group text-xs"></i></span>`;

/** Category option: choosing it filters the list to this subject. */
function categoryRowHTML(g, i, selected) {
  return `
    <li id="course-opt-${i}" role="option" aria-selected="${selected}" data-i="${i}" data-kind="subject"
      class="sticky top-0 z-10 flex items-center gap-3 px-3 py-2 cursor-pointer border-t border-b border-slate-200 dark:border-slate-700 ${selected ? 'bg-blue-50 dark:bg-slate-700' : 'bg-slate-50 dark:bg-slate-900 hover:bg-blue-50 dark:hover:bg-slate-700'}">
      ${SUBJECT_ICON}
      <span class="flex-1 min-w-0">
        <span class="block text-sm font-semibold truncate">${esc(subjectName(g))}</span>
        <span class="block text-xs text-slate-500 dark:text-slate-400">${esc(g.code)} · ${plural(g.count, 'course')}</span>
      </span>
      <span class="shrink-0 text-xs font-medium text-blue-600 dark:text-blue-400">Only this subject <i class="fa-solid fa-chevron-right text-[10px]"></i></span>
    </li>`;
}

/** Heading while a subject filter is on: a label, not an option. */
function filterHeaderHTML(f) {
  return `
    <li role="presentation" class="sticky top-0 z-10 flex items-center gap-3 px-3 py-2 bg-slate-50 dark:bg-slate-900 border-b border-slate-200 dark:border-slate-700">
      ${SUBJECT_ICON}
      <span class="flex-1 min-w-0">
        <span class="block text-sm font-semibold truncate">${esc(subjectName(f))}</span>
        <span class="block text-xs text-slate-500 dark:text-slate-400">${esc(f.code)} · ${plural(f.count, 'course')} · type a number or title to search inside</span>
      </span>
    </li>`;
}

function courseRowHTML(c, i, selected) {
  return `
    <li id="course-opt-${i}" role="option" aria-selected="${selected}" data-i="${i}" data-kind="course"
      class="px-3 py-2 cursor-pointer ${selected ? 'bg-blue-50 dark:bg-slate-700' : 'hover:bg-blue-50 dark:hover:bg-slate-700'}">
      <div class="text-sm truncate"><span class="font-semibold">${esc(c.code)}</span>
        <span class="text-slate-400 dark:text-slate-500">—</span> ${esc(c.title)}</div>
      <div class="text-xs text-slate-500 dark:text-slate-400 truncate">${esc([c.college, c.units, ...c.sources].filter(Boolean).join(' · '))}</div>
    </li>`;
}

function renderCourseResults() {
  const { filter, options, active, message, more } = courseSearch;
  const rows = [];
  if (filter) rows.push(filterHeaderHTML(filter));
  options.forEach((o, i) => rows.push(o.kind === 'subject'
    ? categoryRowHTML(o.item, i, i === active) : courseRowHTML(o.item, i, i === active)));
  if (options.some(o => o.kind === 'course') && more > 0) {
    const hint = filter ? 'type a number or title to narrow' : 'choose a subject, or add a course number (e.g. “MTH 1”)';
    rows.push(`<li role="presentation" class="px-3 py-1.5 text-xs text-slate-400 dark:text-slate-500">+${more} more · ${hint}</li>`);
  }
  if (message) rows.push(`<li role="presentation" class="px-3 py-2 text-sm text-slate-500 dark:text-slate-400">${esc(message)}</li>`);
  courseList.innerHTML = rows.join('');
  const open = rows.length > 0 && document.activeElement === courseInput;
  courseList.classList.toggle('hidden', !open);
  courseInput.setAttribute('aria-expanded', String(open));
  if (open && active >= 0) {
    courseInput.setAttribute('aria-activedescendant', `course-opt-${active}`);
    $(`course-opt-${active}`).scrollIntoView({ block: 'nearest' });
  } else {
    courseInput.removeAttribute('aria-activedescendant');
  }
}

/* Subject filter: the "[Mathematics x]" chip in the box */
function setCourseFilter(subject) {
  courseSearch.filter = subject ? { code: subject.code, name: subject.name, count: subject.count } : null;
  $('courseFilter').classList.toggle('hidden', !subject);
  $('courseFilterName').textContent = subject ? subjectName(subject) : '';
  $('courseFilter').title = subject ? `Only searching ${subjectName(subject)} (${subject.code}) courses` : '';
  courseInput.placeholder = subject ? `Search ${subjectName(subject)}: a number or title, e.g. 1` : COURSE_PLACEHOLDER;
}

/** A category was picked: show only that subject's courses. */
function chooseSubject(subject) {
  setCourseFilter(subject);
  courseInput.value = '';
  clearTimeout(courseSearch.timer);
  runCourseSearch('');
}

/** Chip x or Backspace: back to searching every subject. */
function clearCourseFilter() {
  if (!courseSearch.filter) return;
  setCourseFilter(null);
  queueCourseSearch();
}

/** `course` is always a search result from the backend, never typed text. */
function selectCourse(course) {
  if (!state.completed.includes(course.code)) {
    state.completed.push(course.code);
    state.courseTitles[course.code] = course.title;
  }
  state.noCompleted = false;                                  // a course replaces "No completed courses"
  state.touched.add('completed');
  renderTags();
  courseInput.value = '';
  courseSearch.seq++;                                         // ignore any search still in flight
  closeCourseResults();
}

function pickOption(option) {
  if (option.kind === 'subject') chooseSubject(option.item);  // a category filters, it is never added
  else selectCourse(option.item);
}

function removeTag(code) {
  state.completed = state.completed.filter(c => c !== code);
  state.touched.add('completed');           // removing the last course leaves the section unanswered
  renderTags();
}

/** "No completed courses" / "No AP scores": only available while nothing is listed (remove entries first). */
function syncNoneToggle(toggleId, hintId, checked, hasEntries) {
  const toggle = $(toggleId);
  toggle.checked = checked;
  toggle.disabled = hasEntries;
  $(hintId).classList.toggle('hidden', !hasEntries);
  if (hasEntries) toggle.setAttribute('aria-describedby', hintId);
  else toggle.removeAttribute('aria-describedby');
}

function renderTags() {
  $('tagBox').querySelectorAll('.tag').forEach(t => t.remove());
  state.completed.forEach(code => {
    const known = state.catalog && code in state.catalog;
    const style = !state.catalog
      ? 'bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-200 ring-slate-200 dark:ring-slate-600'
      : known
        ? 'bg-blue-100 text-blue-800 dark:bg-blue-500/15 dark:text-blue-300 ring-blue-200 dark:ring-blue-500/30'
        : 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300 ring-amber-200 dark:ring-amber-500/30';
    const tag = document.createElement('span');
    tag.className = `tag inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 rounded-md text-sm font-medium ring-1 ${style}`;
    tag.title = [state.courseTitles[code] ?? state.catalog?.[code]?.title,
      state.catalog && !known ? 'Not in the uploaded transfer-pathway data, so the plan generator ignores it' : '']
      .filter(Boolean).join(' — ');
    tag.innerHTML = `${esc(code)}<button type="button" aria-label="Remove ${esc(code)}"
      class="w-4 h-4 grid place-items-center rounded hover:bg-black/10 dark:hover:bg-white/10">
      <i class="fa-solid fa-xmark text-[10px]"></i></button>`;
    tag.querySelector('button').addEventListener('click', e => { e.stopPropagation(); removeTag(code); });
    $('tagBox').insertBefore(tag, $('courseFilter'));        // tags, then the subject chip, then the input
  });
  syncNoneToggle('noCompletedToggle', 'noCompletedHint', state.noCompleted, state.completed.length > 0);
  updateValidation();
}

$('noCompletedToggle').addEventListener('change', e => {
  state.noCompleted = e.target.checked && !state.completed.length;
  state.touched.add('completed');
  renderTags();
});

courseInput.addEventListener('input', queueCourseSearch);
courseInput.addEventListener('focus', queueCourseSearch);
courseInput.addEventListener('blur', () => setTimeout(() => courseList.classList.add('hidden'), 150));

courseInput.addEventListener('keydown', e => {
  const { options, active } = courseSearch;
  const open = !courseList.classList.contains('hidden');
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && options.length) {
    e.preventDefault();
    const down = e.key === 'ArrowDown';
    courseSearch.active = active < 0 ? (down ? 0 : options.length - 1)
      : (active + (down ? 1 : -1) + options.length) % options.length;
    renderCourseResults();
  } else if (e.key === 'Enter') {
    e.preventDefault();                                       // Enter never adds free text
    if (open && options[active]) pickOption(options[active]);
    else if (courseInput.value.trim() && options.length) {
      courseSearch.message = 'Pick a subject or a course from the list: click it, or use ↑ ↓ and press Enter.';
      renderCourseResults();
    }
  } else if (e.key === 'Escape' && open) {
    e.stopPropagation();
    courseList.classList.add('hidden');
  } else if (e.key === 'Backspace' && !courseInput.value) {
    if (courseSearch.filter) clearCourseFilter();             // first the subject chip, then course tags
    else if (state.completed.length) removeTag(state.completed[state.completed.length - 1]);
  }
});

courseList.addEventListener('mousedown', e => {
  const li = e.target.closest('li[data-i]');
  e.preventDefault();                                         // keep focus in the input
  if (li) pickOption(courseSearch.options[Number(li.dataset.i)]);
});
$('courseFilterClear').addEventListener('click', e => {
  e.stopPropagation();
  clearCourseFilter();
  courseInput.focus();
});
$('tagBox').addEventListener('click', () => courseInput.focus());

/* AP scores: subject + score dropdowns, removable tags. One entry per subject (re-adding updates the score). */
const apSubject = $('apSubjectSelect');
const apScore = $('apScoreSelect');

function updateApAddEnabled() {
  $('apAddBtn').disabled = !apSubject.value || !apScore.value;
}

function addApScore() {
  const subject = apSubject.value;
  const score = Number(apScore.value);
  if (!subject || !score) return;
  const existing = state.apScores.find(a => a.subject === subject);
  if (existing) existing.score = score;
  else state.apScores.push({ subject, score });
  state.noApScores = false;                                   // a score replaces "No AP scores"
  state.touched.add('ap');
  apSubject.value = '';
  apScore.value = '';
  updateApAddEnabled();
  renderApTags();
  apSubject.focus();
}

function removeApScore(subject) {
  state.apScores = state.apScores.filter(a => a.subject !== subject);
  state.touched.add('ap');                  // removing the last score leaves the section unanswered
  renderApTags();
}

function renderApTags() {
  $('apTags').innerHTML = state.apScores.map(a => `
    <span class="inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 rounded-md text-sm font-medium ring-1 bg-indigo-50 text-indigo-800 ring-indigo-200 dark:bg-indigo-500/15 dark:text-indigo-300 dark:ring-indigo-500/30">
      ${esc(a.subject)} <span class="text-indigo-500 dark:text-indigo-400">(${a.score})</span>
      <button type="button" data-remove-ap="${esc(a.subject)}" aria-label="Remove ${esc(a.subject)}"
        class="w-4 h-4 grid place-items-center rounded hover:bg-black/10 dark:hover:bg-white/10">
        <i class="fa-solid fa-xmark text-[10px]"></i></button>
    </span>`).join('');
  syncNoneToggle('noApToggle', 'noApHint', state.noApScores, state.apScores.length > 0);
  updateValidation();
}

$('noApToggle').addEventListener('change', e => {
  state.noApScores = e.target.checked && !state.apScores.length;
  state.touched.add('ap');
  renderApTags();
});
// A chosen-but-not-added score changes the AP message ("Click Add…"), so validate on every change
apSubject.addEventListener('change', () => { updateApAddEnabled(); updateValidation(); });
apScore.addEventListener('change', () => { updateApAddEnabled(); updateValidation(); });
$('apAddBtn').addEventListener('click', addApScore);
$('apTags').addEventListener('click', e => {
  const b = e.target.closest('[data-remove-ap]');
  if (b) removeApScore(b.dataset.removeAp);
});

const resultsShowing = () => !$('results').classList.contains('hidden');

// GE pathway: switching re-runs generation if results are already showing. GE choices are sent as they
// are: the backend drops (and names) any that don't count toward the new pathway.
$('geFrame').addEventListener('change', e => {
  const r = e.target.closest('input[name="gePathway"]');
  if (!r) return;
  state.pathway = r.value;
  state.touched.add('ge');
  updateValidation();
  if (resultsShowing()) generatePlans({ refresh: true });
});

const GE_OPTION_CLS = 'block rounded-md px-3 py-1.5 text-center text-slate-600 dark:text-slate-300 transition ' +
  'peer-checked:bg-white dark:peer-checked:bg-slate-700 peer-checked:text-slate-900 dark:peer-checked:text-white ' +
  'peer-checked:shadow-sm peer-focus-visible:ring-2 peer-focus-visible:ring-blue-500';

/** 'UC + CSU · 11 courses', 'UC 11 · CSU 12 courses · 2019 rules', 'UC only · 7 courses', 'Data not loaded' */
function geSummary(p) {
  if (!p.loaded) return 'Data not loaded';
  const counts = Object.entries(p.courses_by_system).sort(([a], [b]) => (b === 'UC') - (a === 'UC'));
  const text = counts.length === 1 ? `${counts[0][0]} only · ${plural(counts[0][1], 'course')}`
    : new Set(counts.map(([, n]) => n)).size === 1 ? `${counts.map(([sys]) => sys).join(' + ')} · ${plural(counts[0][1], 'course')}`
      : `${counts.map(([sys, n]) => `${sys} ${n}`).join(' · ')} courses`;
  return p.historical ? `${text} · 2019 rules` : text;
}

/** GET /ge-pathways -> the radios in index.html get their summary; a pathway only the data knows is added. */
function setGePathways(api) {
  state.gePathways = api?.pathways || [];
  const frame = $('geFrame');
  for (const p of state.gePathways) {
    const find = () => [...frame.querySelectorAll('input[name="gePathway"]')].find(i => i.value === p.id);
    if (!find()) {
      frame.insertAdjacentHTML('beforeend', `<label class="cursor-pointer">
        <input type="radio" name="gePathway" value="${esc(p.id)}" class="peer sr-only" />
        <span class="${GE_OPTION_CLS}"><span class="block text-sm font-semibold">${esc(p.name)}</span>
          <span data-ge-summary class="block text-[11px] text-slate-500 dark:text-slate-400"></span></span></label>`);
    }
    find().closest('label').querySelector('[data-ge-summary]').textContent = geSummary(p);
  }
}

const gePathwayName = id => state.gePathways.find(p => p.id === id)?.name
  ?? [...document.querySelectorAll('input[name="gePathway"]')].find(i => i.value === id)?.closest('label').querySelector('.font-semibold')?.textContent
  ?? id;

/* Starting semester: the next 7 terms from today (Summer terms only when summer is on) */
function termOptions(includeSummer) {
  const now = new Date();
  const m = now.getMonth();                                   // Jan–May Spring, Jun–Jul Summer, Aug–Dec Fall
  let ord = now.getFullYear() * 3 + (m <= 4 ? 0 : m <= 6 ? 1 : 2);
  const out = [];
  for (; out.length < 7; ord++) {
    const season = SEASONS[ord % 3];
    if (includeSummer || season !== 'Summer') out.push(`${season} ${Math.floor(ord / 3)}`);
  }
  return out;
}

function renderStartTerms() {
  const opts = termOptions(state.includeSummer);
  // Nothing is pre-selected. A chosen term that is no longer offered (a Summer term after turning
  // summer off) is cleared rather than swapped for another term, so the student chooses again.
  if (state.startTerm && !opts.includes(state.startTerm)) state.startTerm = null;
  $('startTermSelect').innerHTML =
    `<option value="" disabled${state.startTerm ? '' : ' selected'}>Select a starting semester…</option>` +
    opts.map(o => `<option value="${esc(o)}"${o === state.startTerm ? ' selected' : ''}>${esc(o)}</option>`).join('');
  updateValidation();
}

$('startTermSelect').addEventListener('change', e => {
  state.startTerm = e.target.value || null;
  state.touched.add('term');
  updateValidation();
  if (resultsShowing()) generatePlans({ refresh: true });
});

/* Summer classes: a switch, so the answer is always a definite Yes or No (shown beside it) */
function renderSummerState() {
  $('summerToggle').checked = state.includeSummer;
  $('summerState').textContent = state.includeSummer ? 'Yes' : 'No';
}

$('summerToggle').addEventListener('change', e => {
  state.includeSummer = e.target.checked;
  renderSummerState();
  renderStartTerms();
  if (resultsShowing()) generatePlans({ refresh: true });
});

/* ---------------------------------------------------------------------
 * 3b. REQUIRED FIELDS — every section needs an explicit answer before a plan is generated.
 *     Nothing is pre-filled. College / university / major must be one of the listed options.
 *     Completed courses and AP scores accept "none" only as an explicit choice (state.noCompleted,
 *     state.noApScores): an empty list alone is unanswered. Summer is a switch, so it is always answered.
 *     A section's error shows once focus has left it, or for every section after a Generate click on
 *     an incomplete form, and clears as soon as the answer is fixed. POST /generate-sep checks the
 *     same rules (GenerateSEPRequest in main.py) and rejects incomplete requests.
 * ------------------------------------------------------------------- */
function institutionError(field) {
  const [id, , what] = INSTITUTION_INPUTS[field];
  if (!$(id).value.trim()) return `Please select ${what}.`;
  return selectedOption(field) ? null : `Please select ${what} from the list.`;
}

/** key -> error slot `${key}Error`; controls get aria-invalid, `frame` gets a red border, target() is where to go. */
const REQUIRED_FIELDS = [
  { key: 'college', label: 'Community college', controls: ['collegeInput'], target: () => $('collegeInput'),
    error: () => institutionError('college') },
  { key: 'target', label: 'Target university', controls: ['targetInput'], target: () => $('targetInput'),
    error: () => institutionError('target') },
  { key: 'major', label: 'Intended major', controls: ['majorInput'], target: () => $('majorInput'),
    error: () => institutionError('major') },
  { key: 'completed', label: 'Completed courses', controls: ['courseInput'], frame: 'tagBox', target: () => courseInput,
    error: () => state.completed.length || state.noCompleted ? null
      : 'Add at least one completed course or choose “No completed courses.”' },
  { key: 'ap', label: 'AP scores', controls: ['apSubjectSelect', 'apScoreSelect'], target: () => apSubject,
    error: () => state.apScores.length || state.noApScores ? null
      : apSubject.value && apScore.value ? 'Click “Add” to save this AP score, or choose “No AP scores.”'
      : 'Add at least one AP score or choose “No AP scores.”' },
  { key: 'ge', label: 'GE pathway', controls: ['geGroup'], frame: 'geFrame',
    target: () => document.querySelector('input[name="gePathway"]'),
    error: () => state.pathway ? null : 'Please select a GE pathway.' },
  { key: 'term', label: 'Starting semester', controls: ['startTermSelect'], target: () => $('startTermSelect'),
    error: () => state.startTerm ? null : 'Please select a starting semester.' }
];

/** Show or clear every section's error and set the Generate button. Returns true when every answer is in. */
function updateValidation() {
  const missing = [];
  for (const f of REQUIRED_FIELDS) {
    const message = f.error();
    if (message) missing.push(f);
    const show = Boolean(message) && (state.showAllErrors || state.touched.has(f.key));
    const slot = $(`${f.key}Error`);
    slot.innerHTML = show ? `<i class="fa-solid fa-triangle-exclamation mr-1" aria-hidden="true"></i>${esc(message)}` : '';
    slot.classList.toggle('hidden', !show);
    f.controls.forEach(id => show ? $(id).setAttribute('aria-invalid', 'true') : $(id).removeAttribute('aria-invalid'));
    if (f.frame) $(f.frame).dataset.invalid = String(show);
  }
  const ready = missing.length === 0;
  const btn = $('generateBtn');
  btn.setAttribute('aria-disabled', String(!ready));   // still focusable: a click explains what's missing
  btn.disabled = state.loading;                          // truly disabled only while a request runs
  $('generateHint').textContent = ready ? ''
    : `Answer every required section (*) to generate your plans. Still needed: ${missing.map(f => f.label).join(', ')}.`;
  $('generateHint').classList.toggle('hidden', ready);
  return ready;
}

/** Generate on an incomplete form: show every missing answer beside its field and go to the first one. */
function explainMissing() {
  state.showAllErrors = true;
  updateValidation();
  REQUIRED_FIELDS.find(f => f.error())?.target()?.focus();
}

// A section counts as visited once focus leaves it, not while moving between its own controls
[['college', 'collegeInput'], ['target', 'targetInput'], ['major', 'majorInput'], ['completed', 'completedSection'],
 ['ap', 'apSection'], ['ge', 'geGroup'], ['term', 'startTermSelect']].forEach(([key, id]) => {
  $(id).addEventListener('focusout', e => {
    if ($(id).contains(e.relatedTarget)) return;
    state.touched.add(key);
    updateValidation();
  });
});

/* Backend connection banner */
function setApiStatus(error) {
  const box = $('apiStatus');
  box.classList.toggle('hidden', !error);
  if (!error) return;
  box.innerHTML = `<div class="flex flex-wrap items-center gap-x-3 gap-y-2">
    <i class="fa-solid fa-plug-circle-xmark"></i>
    <span class="flex-1 min-w-[200px]">${esc(error.message)} Start it with
      <code class="px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 font-mono text-xs">uvicorn main:app --reload</code></span>
    <button type="button" id="apiRetry" class="px-2.5 py-1 rounded-md ring-1 ring-amber-300 dark:ring-amber-500/40 hover:bg-amber-100 dark:hover:bg-amber-500/20 font-medium">Retry</button>
  </div>`;
  $('apiRetry').addEventListener('click', init);
}

/* Dev-only: hackathon data injector (loads the Golden Pathway into the backend) */
const DEV_BTN = 'inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-dashed border-slate-300 dark:border-slate-700 ' +
  'text-slate-400 dark:text-slate-500 hover:border-slate-400 hover:text-slate-600 dark:hover:border-slate-500 dark:hover:text-slate-300';

function renderDevTools() {
  const g = state.golden;
  $('devTools').innerHTML = !g
    ? `<button type="button" data-dev="inject" class="${DEV_BTN}" title="Loads one demo pathway into the backend">
         <i class="fa-solid fa-flask"></i> Developer: Inject Mock Data</button>`
    : `<span class="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400" title="Only this pathway has data">
         <i class="fa-solid fa-circle-check"></i> Mock data: ${esc(g.pathway.college)} → ${esc(g.pathway.university)} → ${esc(g.pathway.major)}</span>
       <button type="button" data-dev="fill" class="${DEV_BTN}"><i class="fa-solid fa-pen-to-square"></i> Fill form</button>
       <button type="button" data-dev="clear" class="${DEV_BTN}"><i class="fa-solid fa-trash-can"></i> Clear</button>`;
}

$('devTools').addEventListener('click', async e => {
  const b = e.target.closest('[data-dev]');
  if (!b) return;
  if (b.dataset.dev === 'fill') {
    const { pathway, suggested_completed } = state.golden;
    $('collegeInput').value = pathway.college;
    $('targetInput').value = pathway.university;
    $('majorInput').value = pathway.major;
    state.completed = [...suggested_completed];
    state.noCompleted = false;
    renderTags();
    onInstitutionChange();                  // AP scores, GE pathway and start term still need answers
    return;
  }
  try {
    const status = b.dataset.dev === 'inject' ? await injectMockData() : await clearMockData();
    state.golden = status.loaded ? status : null;
    setApiStatus(null);
  } catch (err) {
    if (err.offline) setApiStatus(err); else console.error(err);
    return;
  }
  resetResults();               // shown results were computed against the old data set
  state.catalogFor = undefined; // force catalog reload for tag validation
  loadCatalog();
  renderDevTools();
});

async function init() {
  try {
    setInstitutions(await fetchInstitutions());
    setGePathways(await fetchGePathways());
    const status = await fetchMockDataStatus();
    state.golden = status.loaded ? status : null;
    setApiStatus(null);
  } catch (e) {
    console.error(e);
    setApiStatus(e.offline ? e : new Error('The TransferPath API returned an error.'));
  }
  renderDevTools();
  onInstitutionChange();                    // the API's options may now match what was typed
}

// Start unanswered: undo any choice the browser restored on reload (the page state is the source of truth)
document.querySelectorAll('input[name="gePathway"]').forEach(r => { r.checked = false; });
apSubject.value = apScore.value = '';
updateApAddEnabled();
renderTags();
renderApTags();
renderSummerState();
renderStartTerms();
renderDevTools();
updateValidation();
init();

/* ---------------------------------------------------------------------
 * 4. RESULTS: POST /generate-sep -> N/A state or plan cards
 * ------------------------------------------------------------------- */
$('generateBtn').addEventListener('click', () => {
  if (state.loading) return;
  if (updateValidation()) generatePlans();
  else explainMissing();                    // incomplete: nothing is sent; say what's missing, beside each field
});

const GENERATE_LABEL = $('generateBtn').innerHTML;
function setLoading(on) {
  state.loading = on;
  $('generateBtn').innerHTML = on
    ? '<i class="fa-solid fa-circle-notch fa-spin"></i> Building your plans…'
    : GENERATE_LABEL;
  $('results').classList.toggle('opacity-60', on);
  $('results').classList.toggle('pointer-events-none', on);
  updateValidation();
}

/** Every required answer, explicitly (the shape of GenerateSEPRequest in main.py). */
function buildRequest() {
  return {
    college: selectedOption('college'),
    university: selectedOption('target'),
    major: selectedOption('major'),
    completed_courses: [...state.completed],
    no_completed_courses: state.noCompleted,
    ap_scores: state.apScores.map(({ subject, score }) => ({ subject, score })),
    no_ap_scores: state.noApScores,
    ge_pathway: state.pathway,
    start_term: state.startTerm,
    include_summer: state.includeSummer,
    ge_choices: state.geChoices.map(({ requirement, course, term }) => ({ requirement, course, term }))
  };
}

/** Keep only the GE choices the backend accepted (dropped ones come back as warnings, with the reason).
 *  A choice this plan had to move (its term left the plan) follows Plan A to the term it landed in. */
function syncGeChoices(resp) {
  if (!resp.ge_choices) return;
  const placed = new Map((resp.plans[0]?.ge?.choices || []).filter(c => c.status === 'planned' && c.placed_term)
    .map(c => [c.course, c.placed_term]));
  state.geChoices = resp.ge_choices.accepted.map(c => ({ ...c, term: placed.get(c.course) || c.term }));
}

/**
 * refresh: re-run after an in-place change (node click, GE checkbox, option toggle) —
 * keeps open cards and the GE panel, and doesn't scroll.
 */
async function generatePlans({ refresh = false } = {}) {
  if (!updateValidation()) {                // never send an incomplete form
    if (refresh) resetResults();            // the plans shown no longer match the form
    explainMissing();
    return;
  }
  const request = buildRequest();

  const openIds = refresh ? Object.keys(state.plans).filter(id => $(`card-${id}`)?.classList.contains('open')) : [];
  const req = ++state.requestId;
  setLoading(true);
  let data = null, error = null, ap = null;
  try {
    ap = await evaluateAp(request);                           // <- ap_engine (never throws)
    // AP credit counts as done but, unlike a completed course, does not imply its prerequisites
    if (ap) request.waived_courses = ap.applied.filter(c => !request.completed_courses.includes(c));
    data = await generateSep(request);                        // <- BACKEND (api.js -> POST /generate-sep)
  } catch (e) {
    error = e;
    console.error(e);
  }
  if (req !== state.requestId) return;                        // superseded by a newer request
  setLoading(false);
  setApiStatus(error && error.offline ? error : null);

  const panel = state.panel;
  resetResults({ keepPanel: refresh && !!data });
  $('results').classList.remove('hidden');
  if (data) {
    syncGeChoices(data);
    renderPlans(data, openIds);
    if (panel && state.plans[panel.planId]) { state.panel = panel; renderGePanel(); }
    else closeGePanel();
  } else {
    renderUnavailable(request, error);                        // no graph area is rendered at all
  }
  if (ap && !error?.offline) $('resultsBody').insertAdjacentHTML('beforeend', apCardHTML(ap));
  if (!refresh) $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Tear down any rendered plans/graphs and hide the results section. */
function resetResults({ keepPanel = false } = {}) {
  if (!keepPanel) closeGePanel();
  Object.values(state.plans).forEach(p => p.cy && p.cy.destroy());
  state.plans = {};
  state.response = null;
  $('resultsBody').innerHTML = '';
  $('results').classList.add('hidden');
}

function toggleCompleted(code) {
  state.geChoices = state.geChoices.filter(c => c.course !== code);   // a completed course is no longer a choice
  state.completed = state.completed.includes(code)
    ? state.completed.filter(c => c !== code)
    : [...state.completed, code];
  // Clicking a course in the plan is an explicit answer: un-marking the last one means "No completed courses"
  state.noCompleted = state.completed.length === 0;
  state.touched.add('completed');
  renderTags();
  generatePlans({ refresh: true });
}

function renderUnavailable(request, error) {
  const step = (icon, label, value) => `
    <div class="flex items-center gap-2.5 rounded-lg bg-slate-50 dark:bg-slate-800/60 ring-1 ring-slate-200 dark:ring-slate-700 px-3 py-2 text-left min-w-0">
      <i class="fa-solid ${icon} text-slate-400 dark:text-slate-500"></i>
      <div class="min-w-0">
        <div class="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500">${label}</div>
        <div class="text-sm font-medium text-slate-700 dark:text-slate-200 truncate">${esc(value)}</div>
      </div>
    </div>`;
  const arrow = '<i class="fa-solid fa-arrow-right text-slate-300 dark:text-slate-600 rotate-90 sm:rotate-0"></i>';

  if (error) {
    const title = error.offline ? 'Couldn’t reach the server' : 'Couldn’t build a plan';
    $('resultsBody').innerHTML = `
    <div class="card fade-in rounded-xl border-2 border-dashed border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-6 py-12 text-center">
      <div class="mx-auto w-14 h-14 rounded-full grid place-items-center bg-slate-100 dark:bg-slate-800 text-slate-400 dark:text-slate-500">
        <i class="fa-solid ${error.offline ? 'fa-plug-circle-xmark' : 'fa-triangle-exclamation'} text-2xl"></i>
      </div>
      <h2 class="mt-4 text-xl font-semibold">${title}</h2>
      <p class="mt-2 text-sm text-slate-500 dark:text-slate-400 max-w-md mx-auto">${esc(error.message)}</p>
    </div>`;
    return;
  }

  $('resultsBody').innerHTML = `
  <div class="card fade-in rounded-xl border-2 border-dashed border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-6 py-12 sm:py-14 text-center">
    <div class="relative mx-auto w-16 h-16">
      <div class="absolute inset-0 rounded-2xl bg-blue-500/10 dark:bg-blue-400/10 rotate-6"></div>
      <div class="relative w-16 h-16 rounded-2xl grid place-items-center bg-white dark:bg-slate-800 ring-1 ring-slate-200 dark:ring-slate-700 text-slate-400 dark:text-slate-500 shadow-sm">
        <i class="fa-solid fa-folder-open text-2xl"></i>
      </div>
    </div>
    <h2 class="mt-5 text-xl font-semibold tracking-tight">N/A - Transfer data for this specific pathway has not been uploaded yet.</h2>
    <p class="mt-2 text-sm text-slate-500 dark:text-slate-400 max-w-lg mx-auto">
      SEP plans are generated only from uploaded articulation agreements for this exact
      college, university and major combination.</p>
    <div class="mt-6 flex flex-col sm:flex-row items-stretch sm:items-center justify-center gap-2 sm:gap-3 max-w-3xl mx-auto">
      ${step('fa-school', 'Community college', request.college)}
      ${arrow}
      ${step('fa-building-columns', 'Target university', request.university)}
      ${arrow}
      ${step('fa-graduation-cap', 'Intended major', request.major)}
    </div>
    <p class="mt-4 text-xs text-slate-400 dark:text-slate-500">GE pathway: ${esc(gePathwayName(request.ge_pathway))} ·
      Starting ${esc(request.start_term)} · Summer ${request.include_summer ? 'on' : 'off'}</p>
  </div>`;
}

function renderPlans(resp, openIds = []) {
  state.response = resp;
  resp.plans.forEach(p => (state.plans[p.id] = { data: p, cy: null }));

  const pw = resp.pathway;
  const art = resp.articulation;                                   // ASSIST agreement (null for mock data)
  const reqs = resp.plans[0].requirements;
  const geReqs = reqs.filter(r => r.category === 'ge');
  // Every major requirement (required prep + choose-one articulation groups), each course counted once
  const majorCourses = [...new Map(reqs.filter(r => r.category === 'major')
    .flatMap(r => r.courses).map(c => [c.code, c])).values()];
  const majorDone = majorCourses.filter(c => c.status === 'completed').length;
  const geDone = geReqs.filter(r => r.courses.every(c => c.status === 'completed')).length;
  const ge = resp.plans[0].ge;
  const geLine = ge ? geBannerHTML(ge, pw)
    : geReqs.length ? `GE: <b>${geDone}/${geReqs.length}</b> areas complete.` : 'GE: <b>not included</b> in this plan.';

  $('resultsBody').innerHTML = `
    <div class="fade-in rounded-xl border border-blue-200 dark:border-blue-900/60 bg-blue-50 dark:bg-blue-950/40 text-blue-900 dark:text-blue-100 p-4 flex gap-3">
      <i class="fa-solid fa-circle-info mt-0.5"></i>
      <p class="text-sm leading-relaxed">
        Planning <b>${esc(pw.degree)}</b> at <b>${esc(pw.university)}</b> from <b>${esc(pw.college)}</b>
        using <b>${esc(pw.ge_pathway)}</b>, starting <b>${esc(pw.start_term)}</b>${pw.include_summer ? ' with summer classes' : ''}.
        Major prep: <b>${majorDone}/${majorCourses.length}</b> done · ${geLine}
        Every plan ends in a Spring term, ready for Fall transfer admission.
        Click a course to toggle completed.${geReqs.length || ge?.requirements.length ? ' Click a teal <b>GE</b> node to see the specific requirements.' : ''}
        ${art ? `<br><i class="fa-solid fa-file-contract mr-1"></i>Pathway available from the ${esc(art.source)}
        ${esc(art.academic_year || '')} agreement: <b>${esc(art.scope_label.toLowerCase())} only</b>${art.complete_degree_requirements ? ''
          : `, not the full${art.program_total_units ? ` ${art.program_total_units}-unit` : ''} degree`}. See <b>Transfer agreement</b> below.` : ''}
        ${pw.inferred_courses?.length ? `<br><i class="fa-solid fa-circle-check mr-1"></i>Not planned because a course you
        completed required them: <b>${pw.inferred_courses.map(esc).join(', ')}</b>.` : ''}
      </p>
    </div>
    ${resp.warnings.map(w => `
    <div class="rounded-lg border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-amber-900 dark:text-amber-200 px-4 py-2 text-sm">
      <i class="fa-solid fa-triangle-exclamation mr-1.5"></i>${esc(w)}</div>`).join('')}
    ${resp.plans.map(p => cardHTML(p, openIds.includes(p.id))).join('')}
    ${art ? articulationCardHTML(resp) : ''}`;

  openIds.forEach(id => {
    if (!state.plans[id]) return;
    renderGraph(id);
    state.plans[id].cy.fit(undefined, 24);
  });
}

function cardHTML(p, open) {
  const s = p.summary;
  const style = PLAN_ICON[p.id] || PLAN_ICON.A;
  const stat = (label, value) => `
    <div class="text-right">
      <div class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">${label}</div>
      <div class="text-xl font-bold whitespace-nowrap">${esc(value)}</div>
    </div>`;
  const legendItem = (cls, label) =>
    `<span class="inline-flex items-center gap-2"><span class="w-3.5 h-3.5 rounded ${cls}"></span>${label}</span>`;
  const last = p.semesters[p.semesters.length - 1];
  const hasGe = p.requirements.some(r => r.category === 'ge') || Boolean(p.ge?.requirements.length)
    || p.semesters.some(sem => sem.courses.some(c => c.is_ge));
  const geOpen = Boolean(p.ge?.requirements.some(r => r.open > 0));
  const minNote = `Prereq chain ${s.prereq_chain_length} · Unit load ${s.unit_load_terms}` +
    (s.terms_needed > s.min_terms_required ? ` · This plan ${s.terms_needed}` : '');

  return `
  <article id="card-${p.id}" class="plan-card card fade-in rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm dark:shadow-black/30 overflow-hidden${open ? ' open' : ''}">
    <button data-toggle-plan="${p.id}" aria-expanded="${open}"
      class="w-full flex items-center gap-4 p-5 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50">
      <div class="w-11 h-11 shrink-0 rounded-lg grid place-items-center ${style.cls}">
        <i class="fa-solid ${style.icon}"></i>
      </div>
      <div class="flex-1 min-w-0">
        <div class="font-semibold">${esc(p.name)} <span class="text-slate-400 dark:text-slate-500 font-normal">·</span> ${esc(p.label)}</div>
        <div class="text-sm text-slate-500 dark:text-slate-400 truncate">${esc(p.description)}</div>
      </div>
      <div class="hidden sm:flex gap-8">
        ${stat('Terms', s.terms_needed)}
        ${stat('Units', s.total_units)}
        ${stat('Transfer', s.transfer_admission_term || '—')}
      </div>
      <i class="chevron fa-solid fa-chevron-down text-slate-400 transition-transform duration-300"></i>
    </button>

    <div class="accordion-body">
      <div>
        <div class="border-t border-slate-200 dark:border-slate-800 p-5 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-4">
            <div class="flex flex-wrap items-center gap-3">
              <div class="flex items-center gap-3 rounded-lg bg-red-50 dark:bg-red-500/10 ring-1 ring-red-200 dark:ring-red-500/30 px-4 py-2.5">
                <i class="fa-solid fa-flag-checkered text-red-600 dark:text-red-400"></i>
                <div>
                  <div class="text-xs text-red-700/80 dark:text-red-300/80">Minimum Terms Required</div>
                  <div class="text-lg font-bold text-red-700 dark:text-red-300 leading-tight">${plural(s.min_terms_required, 'term')}</div>
                  <div class="text-[11px] text-red-700/70 dark:text-red-300/70">${esc(minNote)}</div>
                </div>
              </div>
              ${last ? `
              <div class="flex items-center gap-3 rounded-lg bg-blue-50 dark:bg-blue-500/10 ring-1 ring-blue-200 dark:ring-blue-500/30 px-4 py-2.5">
                <i class="fa-solid fa-graduation-cap text-blue-600 dark:text-blue-400"></i>
                <div>
                  <div class="text-xs text-blue-700/80 dark:text-blue-300/80">Transfer Target</div>
                  <div class="text-lg font-bold text-blue-700 dark:text-blue-300 leading-tight">${esc(s.transfer_admission_term || '—')} admission</div>
                  <div class="text-[11px] text-blue-700/70 dark:text-blue-300/70">Final term: ${esc(last.term)}${last.is_padding ? ' (no courses needed)' : ''}</div>
                </div>
              </div>` : ''}
              ${hasGe ? `<button data-open-ge="${p.id}"
                class="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-teal-800 dark:text-teal-200 bg-teal-50 dark:bg-teal-500/10 ring-1 ring-teal-200 dark:ring-teal-500/30 hover:bg-teal-100 dark:hover:bg-teal-500/20">
                <i class="fa-solid fa-layer-group"></i> All GE requirements
              </button>` : ''}
            </div>
            <div class="flex flex-wrap gap-x-4 gap-y-2 text-sm text-slate-600 dark:text-slate-300">
              ${legendItem('bg-red-600 dark:bg-red-500', 'Critical path')}
              ${legendItem('bg-blue-600 dark:bg-blue-500', 'Planned')}
              ${legendItem('bg-slate-300 dark:bg-slate-600 border border-dashed border-slate-400 dark:border-slate-500', 'Completed')}
              ${hasGe ? legendItem('bg-teal-700', 'GE bundle') : ''}
              ${hasGe ? legendItem('bg-teal-700 ring-2 ring-amber-500 dark:ring-amber-400', 'Summer GE') : ''}
            </div>
          </div>

          ${s.major_prep_in_summer.length ? `
          <div class="rounded-lg bg-amber-50 dark:bg-amber-500/10 ring-1 ring-amber-200 dark:ring-amber-500/30 px-3 py-2 text-sm text-amber-900 dark:text-amber-200">
            <i class="fa-solid fa-sun mr-1.5"></i>Major prep in summer (needed to finish on time): <b>${s.major_prep_in_summer.map(esc).join(', ')}</b>
          </div>` : ''}

          <div id="cy-${p.id}" class="w-full h-[460px] rounded-lg border border-slate-200 dark:border-slate-800"></div>
          <p class="text-xs text-slate-500 dark:text-slate-400"><i class="fa-regular fa-hand-pointer mr-1"></i>
            Click a course to toggle completed / planned.${hasGe ? ` Click a <span class="font-semibold text-teal-700 dark:text-teal-300">GE</span> node to see which requirements it covers.` : ''}</p>

          <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            ${p.semesters.length ? p.semesters.map((sem, i) => semesterCardHTML(p, sem, i === p.semesters.length - 1)).join('')
              : geOpen ? semesterCardHTML(p, firstTermOf(p), false)
              : `<div class="text-sm text-emerald-600 dark:text-emerald-400"><i class="fa-solid fa-circle-check mr-1"></i>All requirements complete — you're ready to transfer!</div>`}
          </div>
          ${p.ge ? geSectionHTML(p) : ''}
        </div>
      </div>
    </div>
  </article>`;
}

/** A plan with nothing left to schedule still needs a term to add GE courses to: its start term. */
function firstTermOf(p) {
  const [season, year] = state.response.pathway.start_term.split(' ');
  return { index: 1, term: state.response.pathway.start_term, season, year: Number(year), units: 0,
           max_units: season === 'Summer' ? p.max_units_summer : p.max_units_regular, is_padding: false, courses: [] };
}

function semesterCardHTML(p, s, isLast) {
  const summer = s.season === 'Summer';
  const major = s.courses.filter(c => !c.is_ge);
  const ge = s.courses.filter(c => c.is_ge);
  if (p.ge) return termCardHTML(p, s, isLast, major, ge);
  const frame = s.is_padding
    ? 'border-dashed border-emerald-300 dark:border-emerald-500/40 bg-emerald-50/60 dark:bg-emerald-500/5'
    : summer
      ? 'border-amber-200 dark:border-amber-500/30 bg-amber-50/70 dark:bg-amber-500/5'
      : 'border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/50';
  const iconColor = { Fall: 'text-orange-500', Spring: 'text-emerald-500', Summer: 'text-amber-500' }[s.season];

  let body;
  if (s.is_padding) {
    body = `<p class="text-sm text-emerald-700 dark:text-emerald-300 leading-snug">
      <i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready — no courses required. Ending on this Spring
      lines you up for <b>Fall ${s.year}</b> admission.</p>`;
  } else if (!s.courses.length) {
    body = '<p class="text-sm text-slate-400 dark:text-slate-500">No courses this term.</p>';
  } else {
    body = `
      <ul class="space-y-1 text-sm">${major.map(c => `
        <li class="flex justify-between gap-2"><span class="font-medium whitespace-nowrap">${esc(c.code)}</span>
          <span class="text-slate-500 dark:text-slate-400 truncate">${esc(c.title)}</span></li>`).join('')}
      </ul>
      ${ge.length ? `
      <button data-open-ge="${p.id}" data-sem="${s.index}"
        class="mt-2 w-full flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm text-teal-800 dark:text-teal-200 bg-teal-50 dark:bg-teal-500/10 ring-1 ${summer ? 'ring-amber-300 dark:ring-amber-500/40' : 'ring-teal-200 dark:ring-teal-500/30'} hover:bg-teal-100 dark:hover:bg-teal-500/20">
        <span class="font-medium"><i class="fa-solid fa-layer-group mr-1.5"></i>GE × ${ge.length}</span>
        <span class="text-xs">${sum(ge.map(c => c.units))} units <i class="fa-solid fa-chevron-right ml-1"></i></span>
      </button>` : ''}
      ${isLast ? `<p class="mt-2 text-xs text-emerald-600 dark:text-emerald-400"><i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready for Fall ${s.year}</p>` : ''}`;
  }

  return `
    <div class="rounded-lg border ${frame} p-3 flex flex-col">
      <div class="flex justify-between gap-2 text-xs font-semibold text-slate-500 dark:text-slate-400 mb-2">
        <span class="inline-flex items-center gap-1.5 tracking-wide"><i class="fa-solid ${SEASON_ICON[s.season]} ${iconColor}"></i>${esc(s.term.toUpperCase())}</span>
        <span class="whitespace-nowrap">${s.units}/${s.max_units} units</span>
      </div>
      ${body}
    </div>`;
}

// One delegated handler for accordion headers, "open GE panel" buttons and removing a GE choice
$('resultsBody').addEventListener('click', e => {
  const t = e.target.closest('[data-toggle-plan], [data-open-ge], [data-ge-remove]');
  if (!t) return;
  if (t.dataset.geRemove) removeGeChoice(t.dataset.geRemove);
  else if (t.dataset.togglePlan) togglePlan(t.dataset.togglePlan);
  else openGePanel(t.dataset.openGe, t.dataset.sem ? Number(t.dataset.sem) : null);
});
$('resultsBody').addEventListener('change', e => {
  const select = e.target.closest('select[data-ge-pick]');
  if (select && select.value) pickGeCourse(select);
});

function togglePlan(id) {
  const card = $(`card-${id}`);
  const open = !card.classList.contains('open');
  card.classList.toggle('open', open);
  card.querySelector('[data-toggle-plan]').setAttribute('aria-expanded', open);
  if (!open) return;
  const plan = state.plans[id];
  if (!plan.cy) renderGraph(id);
  // Container size changes during the expand animation — refit when it ends
  setTimeout(() => { if (plan.cy) { plan.cy.resize(); plan.cy.fit(undefined, 24); } }, 380);
}

/* ---------------------------------------------------------------------
 * 4a. TRANSFER AGREEMENT — the articulation behind the plan (resp.articulation):
 *     each university requirement, the community-college course(s) that meet it
 *     ("or" = take any one), and where Plan A puts them.
 * ------------------------------------------------------------------- */
const CHIP_STYLE = {
  done: 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30',
  planned: 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30',
  other: 'bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-600',
  partial: 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30'
};

function articulationCardHTML(resp) {
  const a = resp.articulation;
  const pw = resp.pathway;
  const nodes = new Map(resp.plans[0].graph.nodes.map(n => [n.data.label, n.data]));
  const done = new Set([...pw.completed_courses, ...pw.waived_courses]);
  const courseChip = c => {
    const node = nodes.get(c.code);
    const [style, where] = node?.status === 'completed' || (!node && done.has(c.code))
      ? ['done', `<i class="fa-solid fa-check"></i> ${node?.satisfied_by ? 'prereq met' : 'done'}`]
      : node ? ['planned', esc(node.term)] : ['other', 'alternative'];
    return `<span class="${CHIP} ${CHIP_STYLE[style]}" title="${esc(c.title)}${c.units ? ` · ${c.units} units` : ''}">` +
      `<b>${esc(c.code)}</b> · ${where}</span>`;
  };
  const rows = a.requirements.map(r => `
    <li class="py-2.5 flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5">
      <div class="min-w-0 text-sm">${r.targets.map(t => `<span class="font-semibold">${esc(t.code)}</span>
        <span class="text-slate-500 dark:text-slate-400">${esc(t.title)}</span>`).join(' + ')}</div>
      <div class="flex flex-wrap items-center gap-1.5">${r.options.map(opt => opt.map(courseChip).join(' + '))
        .join('<span class="text-xs text-slate-400 dark:text-slate-500">or</span>')}</div>
    </li>`).join('');
  const degree = [a.major, a.degree].filter(Boolean).join(', ');
  const scope = a.complete_degree_requirements ? '' : `This agreement data covers the ${esc(a.scope_label.toLowerCase())},
    not the full${a.program_total_units ? ` ${a.program_total_units}-unit` : ''} ${esc(degree)}; other degree requirements
    are not in this dataset.`;
  const year = a.requested_academic_year && a.requested_academic_year !== a.academic_year
    ? ` Your start term falls in ${esc(a.requested_academic_year)}; no agreement for that year is loaded.` : '';
  return `
  <article class="card fade-in rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm dark:shadow-black/30 p-5 space-y-4">
    <div class="flex items-center gap-4">
      <div class="w-11 h-11 shrink-0 rounded-lg grid place-items-center bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300">
        <i class="fa-solid fa-file-contract"></i>
      </div>
      <div class="flex-1 min-w-0">
        <div class="font-semibold">Transfer agreement <span class="text-slate-400 dark:text-slate-500 font-normal">·</span>
          ${esc(a.source)} ${esc(a.academic_year || '')}</div>
        <div class="text-sm text-slate-500 dark:text-slate-400">${esc(a.sending.name)} → ${esc(a.receiving.name)} · ${esc(degree)}</div>
      </div>
      <span class="${CHIP} ${a.complete_degree_requirements ? CHIP_STYLE.done : CHIP_STYLE.partial}">${esc(a.scope_label)}${a.complete_degree_requirements ? '' : ' only'}</span>
    </div>
    <ul class="divide-y divide-slate-200 dark:divide-slate-800">${rows}</ul>
    <p class="text-sm text-slate-600 dark:text-slate-300">${scope}${year}
      Course status shows Plan A; <b>alternative</b> = another course that also satisfies the requirement.</p>
    ${a.notes.length ? `<ul class="space-y-1 text-xs text-slate-500 dark:text-slate-400">${a.notes.map(n =>
      `<li><i class="fa-solid fa-circle-info mr-1"></i>${esc(n)}</li>`).join('')}</ul>` : ''}
  </article>`;
}

/* ---------------------------------------------------------------------
 * 4b. AP CREDIT — ap_engine/apWaiver.js checks each score twice: does the
 *     college waive the course, and does the target campus accept it?
 *     Only transfer-safe waivers ('waive') of courses in the college's
 *     catalog are sent as waived_courses (done, but their prerequisites are not
 *     implied, unlike completed_courses). "One of" rules stay manual.
 * ------------------------------------------------------------------- */
const apDataCache = new Map();   // college (lowercase) -> Promise<GET /colleges/{cc}/ap-data | null>

function getApData(college) {
  const key = college.toLowerCase();
  if (!apDataCache.has(key)) {
    apDataCache.set(key, fetchApData(college).catch(e => { apDataCache.delete(key); throw e; }));
  }
  return apDataCache.get(key);
}

/** null when no AP scores were entered; otherwise { result, applied, note, names }. */
async function evaluateAp(request) {
  if (!state.apScores.length) return null;
  const out = { result: null, applied: [], note: null, names: {} };
  try {
    const [data, engine] = await Promise.all([getApData(request.college), loadApEngine()]);
    if (!data) return { ...out, note: `No AP credit chart has been uploaded for ${request.college} yet.` };
    const { campuses, exams } = data.campus_data;
    const campusId = AP_CAMPUS_ALIASES[request.university] ?? campuses.find(c => c.name === request.university)?.id;
    if (!campusId) return { ...out, note: `No AP score rules are recorded for ${request.university} yet.` };

    const scores = state.apScores.map(a => {
      const examId = exams.find(e => e.name === a.subject)?.id ?? a.subject;
      out.names[examId] = a.subject;
      return { examId, score: a.score };
    });
    out.result = engine.evaluateApWaivers({ scores, ccWaivers: data.cc_waivers, campusData: data.campus_data,
      articulation: data.articulation, target: { campusId } });
    out.applied = [...new Set(out.result.courses
      .filter(c => c.recommendation === 'waive' && !c.choiceGroup && state.catalog && c.courseId in state.catalog)
      .map(c => c.courseId))];
  } catch (e) {
    console.error(e);
    out.note = e.message;
  }
  return out;
}

const AP_BADGE = {
  waive: ['Waive', 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30'],
  take_course_for_transfer: ['Take the course', 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30'],
  verify_before_waiving: ['Ask a counselor', 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30'],
  not_eligible: ['Score too low', 'bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-600']
};

function apCourseHTML(c) {
  const [label, cls] = AP_BADGE[c.recommendation];
  const need = c.recommendation === 'take_course_for_transfer' && c.requiredTransferScore
    ? ` <span class="text-xs text-slate-500 dark:text-slate-400">campus needs a ${c.requiredTransferScore}</span>` : '';
  return `
    <div class="mt-2">
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <span class="font-medium">${esc(c.courseId)}</span><span class="${CHIP} ${cls}">${label}</span>${need}
      </div>
      ${c.reasons.length ? `<details class="mt-1">
        <summary class="cursor-pointer text-xs text-slate-500 dark:text-slate-400">Why</summary>
        <ul class="mt-1 space-y-1 text-xs text-slate-500 dark:text-slate-400">${c.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>
      </details>` : ''}
    </div>`;
}

function apCardHTML(ap) {
  let body;
  if (ap.note) {
    body = `<p class="text-sm text-slate-500 dark:text-slate-400">${esc(ap.note)}</p>`;
  } else {
    const exams = ap.result.exams.map(exam => {
      const rows = ap.result.courses.filter(c => c.examId === exam.examId);
      const cc = exam.communityCollege;
      const empty = cc.eligible ? 'No course waived here (GE / unit credit only).' : 'Score below the college minimum.';
      return `
        <div class="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/50 p-3">
          <div class="flex justify-between gap-2 text-sm font-semibold">
            <span>${esc(ap.names[exam.examId] || exam.examId)} <span class="font-normal text-slate-500 dark:text-slate-400">(${exam.score})</span></span>
            ${cc.calGetc.length ? `<span class="text-xs font-normal text-slate-500 dark:text-slate-400">Cal-GETC ${esc(cc.calGetc.join('; '))}</span>` : ''}
          </div>
          ${rows.length ? rows.map(apCourseHTML).join('') : `<p class="mt-2 text-sm text-slate-500 dark:text-slate-400">${empty}</p>`}
        </div>`;
    }).join('');
    const applied = ap.applied.length
      ? `<i class="fa-solid fa-circle-check text-emerald-500 mr-1"></i>Counted as completed in your plan: <b>${ap.applied.map(esc).join(', ')}</b>`
      : 'Your plan is unchanged: no transfer-safe AP waiver matches a course in this pathway’s course list.';
    body = `<div class="grid gap-3 sm:grid-cols-2">${exams}</div>
      <p class="text-sm text-slate-600 dark:text-slate-300">${applied}</p>`;
  }
  return `
  <article class="card fade-in rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm dark:shadow-black/30 p-5 space-y-4">
    <div class="flex items-center gap-4">
      <div class="w-11 h-11 shrink-0 rounded-lg grid place-items-center bg-indigo-50 text-indigo-800 dark:bg-indigo-500/15 dark:text-indigo-300">
        <i class="fa-solid fa-award"></i>
      </div>
      <div class="flex-1 min-w-0">
        <div class="font-semibold">AP credit</div>
        <div class="text-sm text-slate-500 dark:text-slate-400">What your community college waives, and whether your target university accepts it.</div>
      </div>
    </div>
    ${body}
  </article>`;
}

/* ---------------------------------------------------------------------
 * 5. GRAPH (Cytoscape + dagre) — major prep via dagre, GE bundles per term in a lane below
 * ------------------------------------------------------------------- */
function renderGraph(id) {
  const plan = state.plans[id];
  const p = plan.data;
  const majorNodes = p.graph.nodes.map(n => n.data).filter(d => !d.is_ge);
  const majorIds = new Set(majorNodes.map(d => d.id));
  const cpNodes = new Set(p.critical_path);
  const cpEdges = new Set(p.critical_path_edges);

  const elements = [
    ...majorNodes.map(d => ({
      data: {
        id: d.id, code: d.label,
        label: d.status !== 'completed' ? `${d.label}\n${shortTerm(d.term)} · ${d.units}u`
          : d.satisfied_by ? `${d.label}\n✓ prereq met` : `${d.label}\n✓ done`
      },
      classes: ['major', d.status === 'completed' ? 'completed' : cpNodes.has(d.id) ? 'critical' : '',
                d.satisfied_by ? 'inferred' : ''].join(' ').trim()
    })),
    ...p.graph.edges.map(e => e.data)
      .filter(d => majorIds.has(d.source) && majorIds.has(d.target))
      .map(d => ({
        data: { id: d.id, source: d.source, target: d.target },
        classes: [cpEdges.has(d.id) && 'critical', d.relation === 'coreq' && 'coreq'].filter(Boolean).join(' ')
      }))
  ];

  const cy = plan.cy = cytoscape({
    container: $(`cy-${id}`),
    elements,
    style: buildGraphStyle(),
    layout: { name: 'preset' },
    minZoom: 0.3, maxZoom: 2.5, wheelSensitivity: 0.2
  });
  cy.layout({ name: 'dagre', rankDir: 'LR', nodeSep: 24, rankSep: 70, edgeSep: 10, fit: false }).run();
  addGeLane(id);

  // A course implied by a completed one can't be toggled on its own: remove the course that requires it
  cy.on('tap', 'node.major', evt => { if (!evt.target.hasClass('inferred')) toggleCompleted(evt.target.data('code')); });
  cy.on('tap', 'node.ge', evt => openGePanel(id, evt.target.data('sem')));
  cy.on('mouseover', 'node.major, node.ge', evt => {
    evt.target.addClass('hover');
    cy.container().style.cursor = 'pointer';
  });
  cy.on('mouseout', 'node.major, node.ge', evt => {
    evt.target.removeClass('hover');
    cy.container().style.cursor = '';
  });

  applyGraphTheme();
  cy.fit(undefined, 24);
}

/**
 * One aggregated "GE" node per term that has GE work, laid out in a lane under
 * the major-prep graph and spaced in term order. Summer bundles get an amber border.
 */
function addGeLane(id) {
  const plan = state.plans[id];
  const cy = plan.cy;
  const sems = plan.data.semesters;
  const bundles = sems
    .map(s => ({ s, items: s.courses.filter(c => c.is_ge) }))
    .filter(b => b.items.length);
  if (!bundles.length) return;

  const majors = cy.nodes('.major');
  const bb = majors.length ? majors.boundingBox() : { x1: 0, x2: 0, y2: 0, w: 0 };
  const n = sems.length;
  const step = n > 1 ? Math.max(124, (bb.w - 108) / (n - 1)) : 0;
  const x0 = n > 1 ? bb.x1 + 54 : (bb.x1 + bb.x2) / 2;
  const y = bb.y2 + 90;
  const xs = bundles.map(b => x0 + (b.s.index - 1) * step);
  const laneX1 = Math.min(...xs) - 72, laneX2 = Math.max(...xs) + 72;

  cy.add({
    group: 'nodes', classes: 'lane', grabbable: false, selectable: false, locked: true,
    data: { id: '__lane', label: 'GENERAL EDUCATION', w: laneX2 - laneX1, h: 84 },
    position: { x: (laneX1 + laneX2) / 2, y }
  });
  cy.add(bundles.map((b, i) => ({
    group: 'nodes', classes: b.s.season === 'Summer' ? 'ge summer' : 'ge', grabbable: false,
    data: {
      id: `GE-S${b.s.index}`, sem: b.s.index,
      label: `GE · ${sum(b.items.map(c => c.units))} units\n${shortTerm(b.s.term)} · ${plural(b.items.length, 'course')}`
    },
    position: { x: xs[i], y }
  })));
}

/* ---------------------------------------------------------------------
 * 6. GE REQUIREMENTS SIDE PANEL
 * ------------------------------------------------------------------- */
let panelReturnFocus = null;

function openGePanel(planId, sem) {
  const plan = state.plans[planId].data;
  const bundle = sem ? plan.semesters[sem - 1] : null;
  state.panel = {
    planId,
    term: bundle ? bundle.term : null,
    // fixed at open so a course stays visible after it's checked off (and leaves the bundle)
    itemCodes: bundle ? bundle.courses.filter(c => c.is_ge).map(c => c.code) : [],
    allOpen: !bundle
  };
  renderGePanel();
  panelReturnFocus = document.activeElement;
  $('gePanel').classList.add('open');
  $('gePanel').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  setTimeout(() => $('geClose').focus(), 60);
}

function closeGePanel() {
  if (!state.panel) return;
  state.panel = null;
  $('gePanel').classList.remove('open');
  $('gePanel').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (panelReturnFocus && panelReturnFocus.focus && document.contains(panelReturnFocus)) panelReturnFocus.focus();
}

$('geClose').addEventListener('click', closeGePanel);
$('geBackdrop').addEventListener('click', closeGePanel);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeGePanel(); });

$('geBody').addEventListener('change', e => {
  const cb = e.target.closest('input[data-code]');
  if (cb && state.panel && !state.loading) toggleCompleted(cb.dataset.code);
});

const CHIP = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ring-1';

/** Everything the panel knows about a course code in this plan. */
function courseInfo(p, code) {
  const node = p.graph.nodes.find(n => n.data.label === code);
  if (node) return node.data;
  for (const r of p.requirements) {
    const c = r.courses.find(x => x.code === code);
    if (c) return { label: c.code, title: c.title, status: c.status, satisfied_by: c.satisfied_by, term: c.term,
                    is_ge: c.kind === 'ge', units: null, satisfies: [r.name] };
  }
  return { label: code, title: '', status: 'completed', term: null, is_ge: true, units: null, satisfies: [] };
}

function statusChip(info) {
  if (info.status === 'completed') return `<span class="${CHIP} bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30">
    <i class="fa-solid fa-check"></i> ${info.satisfied_by ? `Prerequisite met · required by ${esc(info.satisfied_by)}` : 'Completed'}</span>`;
  if (!info.is_ge) return `<span class="${CHIP} bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30">
    <i class="fa-solid fa-link"></i> Major prep · ${esc(info.term)}</span>`;
  const summer = String(info.term).startsWith('Summer');
  return `<span class="${CHIP} ${summer
    ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30'
    : 'bg-teal-50 text-teal-700 ring-teal-200 dark:bg-teal-500/10 dark:text-teal-300 dark:ring-teal-500/30'}">
    <i class="fa-${summer ? 'solid fa-sun' : 'regular fa-clock'}"></i> Planned · ${esc(info.term)}</span>`;
}

function courseItemHTML(info) {
  const done = info.status === 'completed';
  const editable = info.is_ge && !info.satisfied_by;   // major prep is toggled from the graph; implied courses follow their course
  const control = editable
    ? `<input type="checkbox" data-code="${esc(info.label)}" ${done ? 'checked' : ''}
         class="mt-0.5 w-4 h-4 shrink-0 accent-teal-600 cursor-pointer" aria-label="Mark ${esc(info.label)} completed" />`
    : `<span class="mt-0.5 w-4 h-4 shrink-0 grid place-items-center text-[11px] ${done
        ? 'text-emerald-600 dark:text-emerald-400' : 'text-blue-600 dark:text-blue-400'}">
         <i class="fa-solid ${done ? 'fa-circle-check' : 'fa-link'}"></i></span>`;
  const Tag = editable ? 'label' : 'div';
  return `
  <${Tag} class="flex gap-3 rounded-lg border p-3 transition-colors ${done
    ? 'border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/40'
    : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900'} ${editable ? 'cursor-pointer hover:border-teal-400 dark:hover:border-teal-500/60' : ''}">
    ${control}
    <div class="flex-1 min-w-0">
      <div class="flex items-baseline justify-between gap-2">
        <div class="text-sm font-medium ${done ? 'text-slate-500 dark:text-slate-400 line-through decoration-slate-400/70' : ''}">
          ${esc(info.label)} <span class="font-normal text-slate-500 dark:text-slate-400">${esc(info.title)}</span></div>
        ${info.units ? `<span class="text-xs text-slate-500 dark:text-slate-400 shrink-0">${info.units}u</span>` : ''}
      </div>
      <div class="mt-1.5">${statusChip(info)}</div>
      ${info.satisfies.length ? `<div class="mt-2 flex flex-wrap gap-1">${info.satisfies.map(s =>
        `<span class="px-1.5 py-0.5 rounded text-[11px] bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">${esc(s)}</span>`).join('')}</div>` : ''}
    </div>
  </${Tag}>`;
}

function renderGePanel() {
  const { planId, term, itemCodes, allOpen } = state.panel;
  const plan = state.plans[planId];
  if (!plan) return closeGePanel();
  const p = plan.data;
  if (p.ge) return renderGeStatusPanel(p, term, itemCodes);
  const pw = state.response.pathway;
  const geReqs = p.requirements.filter(r => r.category === 'ge');
  const reqState = r => r.courses.every(c => c.status === 'completed') ? 'done'
    : r.courses.every(c => c.status === 'completed' || c.kind === 'major') ? 'major' : 'pending';
  const counts = { done: 0, major: 0, pending: 0 };
  geReqs.forEach(r => counts[reqState(r)]++);
  const total = geReqs.length || 1;
  const pct = n => `${(n / total) * 100}%`;
  const pendingUnits = sum(p.semesters.flatMap(s => s.courses.filter(c => c.is_ge).map(c => c.units)));

  $('geTitle').textContent = term ? `GE bundle · ${term}` : 'GE requirements';
  $('geSubtitle').textContent = `${p.name} · ${pw.ge_pathway} · ${pw.university}`;

  const bundle = itemCodes.map(code => courseInfo(p, code));
  const stillPlanned = bundle.filter(i => i.status !== 'completed');
  const bundleHTML = bundle.length ? `
    <section>
      <div class="flex items-center justify-between mb-1">
        <h3 class="text-xs font-semibold uppercase tracking-wider ${term.startsWith('Summer') ? 'text-amber-600 dark:text-amber-300' : 'text-teal-700 dark:text-teal-300'}">This bundle</h3>
        <span class="text-xs text-slate-500 dark:text-slate-400">${plural(bundle.length, 'course')}</span>
      </div>
      <p class="text-sm text-slate-600 dark:text-slate-300 mb-3">${stillPlanned.length
        ? `<span class="font-semibold text-slate-900 dark:text-white">Planned for ${esc(term)}:</span> ${stillPlanned.map(i => esc(i.label)).join(', ')}`
        : '<i class="fa-solid fa-circle-check text-emerald-500 mr-1"></i>Everything in this bundle is complete.'}</p>
      <div class="space-y-2">${bundle.map(courseItemHTML).join('')}</div>
    </section>` : '';

  const stateChip = st => ({
    done: `<span class="${CHIP} bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30">Complete</span>`,
    major: `<span class="${CHIP} bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30">Via major prep</span>`,
    pending: `<span class="${CHIP} bg-teal-50 text-teal-700 ring-teal-200 dark:bg-teal-500/10 dark:text-teal-300 dark:ring-teal-500/30">In plan</span>`
  })[st];

  $('geBody').innerHTML = `
    <section class="rounded-lg bg-slate-50 dark:bg-slate-800/50 ring-1 ring-slate-200 dark:ring-slate-800 p-4">
      <p class="text-sm text-slate-600 dark:text-slate-300">${esc(pw.ge_pathway)} (demo data)</p>
      <div class="mt-3 h-2 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden flex">
        <div class="h-full bg-emerald-500" style="width:${pct(counts.done)}"></div>
        <div class="h-full bg-blue-500" style="width:${pct(counts.major)}"></div>
      </div>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300">
        <span><span class="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1"></span>${counts.done} complete</span>
        <span><span class="inline-block w-2 h-2 rounded-full bg-blue-500 mr-1"></span>${counts.major} via major prep</span>
        <span><span class="inline-block w-2 h-2 rounded-full bg-slate-300 dark:bg-slate-600 mr-1"></span>${counts.pending} in plan · ${pendingUnits} GE units scheduled</span>
      </div>
    </section>
    ${bundleHTML}
    <details id="geAll" class="group" ${allOpen ? 'open' : ''}>
      <summary class="cursor-pointer list-none flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
        <span>All ${esc(pw.ge_pathway)} requirements</span>
        <i class="fa-solid fa-chevron-down transition-transform group-open:rotate-180"></i>
      </summary>
      <div class="mt-4 space-y-5">
        ${geReqs.map(r => `
        <div>
          <div class="flex items-center justify-between gap-2 mb-2">
            <h4 class="text-xs font-semibold text-slate-600 dark:text-slate-300">${esc(r.name)}${r.choose > 1 ? ` <span class="font-normal text-slate-400">(choose ${r.choose})</span>` : ''}</h4>
            ${stateChip(reqState(r))}
          </div>
          <div class="space-y-2">${r.courses.map(c => courseItemHTML(courseInfo(p, c.code))).join('')}</div>
        </div>`).join('')}
      </div>
    </details>`;
  $('geAll').addEventListener('toggle', e => { if (state.panel) state.panel.allOpen = e.target.open; });
}

/* ---------------------------------------------------------------------
 * 4c. GE PATHWAY — plan.ge (sep_engine/ge.py): every requirement of the chosen pathway with what
 *     fills it (completed course, AP exam, planned major prep, or the student's own choice) and,
 *     while open, the college's courses that count. Summer cards are the GE workspace: one course
 *     picker per open requirement, limited to the term's free units (9 in summer) and to courses
 *     whose catalog prerequisite is done or planned earlier. Fall/Spring cards offer the same under
 *     "Add a GE course". A pick is sent back as ge_choices and both plans are rebuilt; the backend
 *     re-checks it and drops (with the reason) anything that no longer counts.
 * ------------------------------------------------------------------- */
const TERM_ORDER = { Spring: 0, Summer: 1, Fall: 2 };
const termOrd = label => { const [season, year] = String(label).split(' '); return Number(year) * 3 + TERM_ORDER[season]; };
const codeKey = code => String(code).replace(/^.*:/, '').toUpperCase().replace(/[^A-Z0-9]/g, '');
const isChoice = code => state.geChoices.some(c => c.course === code);
const geReqOf = (p, code) => p.ge?.requirements.find(r => r.filled.some(f => f.kind === 'choice' && f.code === code));

const GE_STATE = {
  satisfied: ['Satisfied', CHIP_STYLE.done, 'fa-circle-check'],
  planned: ['Planned', CHIP_STYLE.planned, 'fa-calendar-check'],
  in_progress: ['In progress', CHIP_STYLE.partial, 'fa-circle-half-stroke'],
  open: ['Open', CHIP_STYLE.other, 'fa-circle']
};

function geBannerHTML(ge, pw) {
  if (!ge.requirements.length) return `GE (${esc(ge.name)}): ${esc(ge.message)}`;
  if (ge.all_satisfied) return `GE (${esc(ge.name)}): <b>GE pathway requirements satisfied.</b>`;
  const s = ge.summary;
  const where = ge.case !== 'courses_available' ? ' Course lists aren\'t loaded for this pathway yet.'
    : pw.include_summer ? ' Choose GE courses in your Summer terms below.' : ' Choose GE courses in a term below.';
  return `GE (${esc(ge.name)}): <b>${s.met}</b> satisfied, <b>${s.planned}</b> planned, <b>${s.open}</b> open of ${s.requirements} requirements.${where}`;
}

/** Why a course can't be added to term s (null = it can). */
function geBlockReason(p, s, c) {
  const free = s.max_units - s.units;
  if (c.units > free + 1e-9) {
    return s.season === 'Summer' ? `only ${fmtUnits(free)} of the ${s.max_units}-unit summer limit left`
      : `only ${fmtUnits(free)} of ${s.max_units} units left`;
  }
  if (c.last_term && termOrd(s.term) > termOrd(c.last_term)) return `not on the list after ${c.last_term}`;
  if (c.first_term && termOrd(s.term) < termOrd(c.first_term)) return `on the list from ${c.first_term}`;
  if (c.prereq_blocking && c.prereq_any.length && !prereqMet(p, s, c)) {
    return `needs ${c.prereq_any[0]} ${c.prereq_concurrent ? 'earlier or in the same term' : 'first'}`;
  }
  return null;
}
const fmtUnits = n => String(Math.round(n * 100) / 100);

/** At least one of the catalog prerequisite's courses is done, or planned before (or with) term s. */
function prereqMet(p, s, c) {
  const pw = state.response.pathway;
  const have = new Set([...pw.completed_courses, ...pw.waived_courses, ...pw.inferred_courses,
    ...p.ge.requirements.flatMap(r => r.filled.filter(f => f.kind === 'completed').map(f => f.code))].map(codeKey));
  const when = termOrd(s.term);
  p.semesters.forEach(t => {
    const o = termOrd(t.term);
    if (o < when || (c.prereq_concurrent && o === when)) t.courses.forEach(x => have.add(codeKey(x.code)));
  });
  return c.prereq_any.some(code => have.has(codeKey(code)));
}

/** The term card: major prep, the GE courses in it (a choice can be removed), and GE pickers. */
function termCardHTML(p, s, isLast, major, ge) {
  const summer = s.season === 'Summer';
  const frame = s.is_padding
    ? 'border-dashed border-emerald-300 dark:border-emerald-500/40 bg-emerald-50/60 dark:bg-emerald-500/5'
    : summer ? 'border-amber-200 dark:border-amber-500/30 bg-amber-50/70 dark:bg-amber-500/5'
      : 'border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/50';
  const iconColor = { Fall: 'text-orange-500', Spring: 'text-emerald-500', Summer: 'text-amber-500' }[s.season];
  const geList = ge.map(c => {
    const r = geReqOf(p, c.code);
    return `
      <li class="rounded-md bg-teal-50 dark:bg-teal-500/10 ring-1 ${summer ? 'ring-amber-300 dark:ring-amber-500/40' : 'ring-teal-200 dark:ring-teal-500/30'} px-2 py-1.5">
        <div class="flex items-start justify-between gap-2">
          <span class="min-w-0"><i class="fa-solid fa-check text-teal-600 dark:text-teal-300 mr-1" aria-hidden="true"></i><span class="font-medium">${esc(c.code)}</span>
            <span class="text-slate-500 dark:text-slate-400">${esc(c.title)}</span></span>
          ${isChoice(c.code) ? `<button type="button" data-ge-remove="${esc(c.code)}" aria-label="Remove ${esc(c.code)} from ${esc(s.term)}"
            class="shrink-0 w-6 h-6 -mr-1 rounded grid place-items-center text-slate-400 hover:text-red-600 hover:bg-white dark:hover:bg-slate-800 dark:hover:text-red-400 focus:outline-none focus:ring-2 focus:ring-blue-500">
            <i class="fa-solid fa-xmark" aria-hidden="true"></i></button>` : ''}
        </div>
        <div class="text-[11px] text-teal-800 dark:text-teal-200">${c.units} units · Satisfies: ${esc(r ? `${p.ge.name} ${r.label}` : c.satisfies.join('; '))}</div>
      </li>`;
  }).join('');
  const majorList = major.map(c => `
      <li class="flex justify-between gap-2"><span class="font-medium whitespace-nowrap">${esc(c.code)}</span>
        <span class="text-slate-500 dark:text-slate-400 truncate">${esc(c.title)}</span></li>`).join('');
  const picker = s.is_padding ? '' : gePickerHTML(p, s);
  const body = s.is_padding && !s.courses.length
    ? `<p class="text-sm text-emerald-700 dark:text-emerald-300 leading-snug">
        <i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready — no courses required. Ending on this Spring
        lines you up for <b>Fall ${s.year}</b> admission.</p>`
    : `${majorList ? `<ul class="space-y-1 text-sm">${majorList}</ul>` : ''}
       ${geList ? `<div class="${majorList ? 'mt-2' : ''} text-[11px] font-semibold uppercase tracking-wider text-teal-700 dark:text-teal-300">GE courses</div>
         <ul class="mt-1 space-y-1.5 text-sm">${geList}</ul>` : ''}
       ${!majorList && !geList && !picker ? '<p class="text-sm text-slate-400 dark:text-slate-500">No courses this term.</p>' : ''}
       ${picker}
       ${isLast && s.courses.length ? `<p class="mt-2 text-xs text-emerald-600 dark:text-emerald-400"><i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready for Fall ${s.year}</p>` : ''}`;
  return `
    <div data-term-card="${esc(s.term)}" class="rounded-lg border ${frame} p-3 flex flex-col">
      <div class="flex justify-between gap-2 text-xs font-semibold text-slate-500 dark:text-slate-400 mb-2">
        <span class="inline-flex items-center gap-1.5 tracking-wide"><i class="fa-solid ${SEASON_ICON[s.season]} ${iconColor}"></i>${esc(s.term.toUpperCase())}</span>
        <span class="whitespace-nowrap">${s.units}/${s.max_units} units</span>
      </div>
      ${body}
    </div>`;
}

/** Summer: the remaining GE requirements with a course picker each. Fall/Spring: the same, collapsed. */
function gePickerHTML(p, s) {
  const ge = p.ge;
  const summer = s.season === 'Summer';
  const note = (text, icon = 'fa-circle-info') =>
    `<p class="mt-2 text-sm text-slate-500 dark:text-slate-400"><i class="fa-solid ${icon} mr-1"></i>${esc(text)}</p>`;
  const open = ge.requirements.filter(r => r.open > 0);
  if (!summer && (ge.case !== 'courses_available' || !open.length)) return '';
  if (ge.case === 'not_loaded' || ge.case === 'not_applicable') return note(ge.message);
  if (!open.length) {
    return ge.all_satisfied ? note('Your GE pathway requirements are satisfied.', 'fa-circle-check')
      : note(`No remaining ${ge.name} requirements for Summer: the rest are planned in other terms.`, 'fa-circle-check');
  }
  if (ge.case === 'requirements_only') {
    return `<div class="mt-2 text-sm">
      <div class="text-[11px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-300">Remaining ${esc(ge.name)} requirements</div>
      <ul class="mt-1 space-y-0.5 text-xs text-slate-600 dark:text-slate-300">${open.map(r => `<li>${esc(r.label)}${r.open > 1 ? ` (${r.open})` : ''}</li>`).join('')}</ul>
      <p class="mt-1.5 text-xs text-slate-500 dark:text-slate-400">${esc(open[0].options_note || ge.message)}</p></div>`;
  }
  const free = s.max_units - s.units;
  const rows = open.map(r => geRowHTML(p, s, r)).join('');
  const hint = `${fmtUnits(free)} of ${s.max_units} units left${summer ? '' : ' in this term'} · picks apply to both plans`;
  const inner = `<div class="space-y-2 mt-1.5">${rows}</div>
    <p data-ge-notice role="alert" class="hidden mt-1.5 text-xs font-medium text-red-600 dark:text-red-400"></p>
    <p class="mt-1.5 text-[11px] text-slate-500 dark:text-slate-400">${hint}</p>`;
  return summer
    ? `<div data-ge-picker class="mt-2 pt-2 border-t border-amber-200 dark:border-amber-500/30">
        <div class="text-[11px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-300">Remaining ${esc(ge.name)} options</div>${inner}</div>`
    : `<details data-ge-picker class="mt-2 group">
        <summary class="cursor-pointer list-none text-xs font-medium text-teal-700 dark:text-teal-300 hover:underline">
          <i class="fa-solid fa-plus mr-1" aria-hidden="true"></i>Add a GE course
          <span class="text-slate-400 dark:text-slate-500 font-normal">· ${plural(open.length, 'open requirement')}</span></summary>${inner}</details>`;
}

function geRowHTML(p, s, r) {
  const courses = r.options.map(code => state.response.ge_courses[code]).filter(Boolean);
  const opts = courses.map(c => {
    const why = geBlockReason(p, s, c);
    return `<option value="${esc(c.code)}"${why ? ' disabled' : ''}>${esc(c.code)} · ${esc(c.title)} (${c.units}u)${why ? ` — ${esc(why)}` : ''}</option>`;
  }).join('');
  const usable = courses.filter(c => !geBlockReason(p, s, c)).length;
  const done = r.courses_required - r.open;
  const count = [done ? `${done} of ${r.courses_required} done` : r.open > 1 ? `${r.open} courses` : '',
    r.min_disciplines > 1 ? `${r.min_disciplines} disciplines` : r.min_distinct_areas > 1 ? `${r.min_distinct_areas}+ areas` : '']
    .filter(Boolean).join(' · ');
  return `
    <label class="block">
      <span class="block text-[11px] font-medium text-slate-600 dark:text-slate-300" title="${esc(r.rule)}">${esc(r.label)}${count
        ? ` <span class="font-normal text-slate-400 dark:text-slate-500">(${count})</span>` : ''}</span>
      <select data-ge-pick data-plan="${p.id}" data-term="${esc(s.term)}" data-req="${esc(r.id)}" ${usable ? '' : 'disabled'}
        class="mt-0.5 w-full rounded-md border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-60 disabled:cursor-not-allowed">
        <option value="">${usable ? `Choose a course (${usable})…` : courses.length ? 'No course fits this term' : 'No eligible course left'}</option>${opts}
      </select>
    </label>`;
}

/** A picked course joins that term (both plans are rebuilt); one that doesn't fit is refused here. */
function pickGeCourse(select) {
  if (state.loading) return;
  const p = state.plans[select.dataset.plan].data;
  const s = p.semesters.find(x => x.term === select.dataset.term) || firstTermOf(p);
  const c = state.response.ge_courses[select.value];
  const why = c && geBlockReason(p, s, c);
  if (!c || why) {
    const box = select.closest('[data-ge-picker]').querySelector('[data-ge-notice]');
    box.textContent = c ? `${c.code} can't be added to ${s.term}: ${why}.` : 'That course is not on the list.';
    box.classList.remove('hidden');
    select.value = '';
    return;
  }
  state.geChoices = [...state.geChoices.filter(x => x.course !== c.code),
    { requirement: select.dataset.req, course: c.code, term: s.term }];
  generatePlans({ refresh: true });
}

function removeGeChoice(code) {
  if (state.loading) return;
  state.geChoices = state.geChoices.filter(c => c.course !== code);
  generatePlans({ refresh: true });
}

function geFillText(f) {
  const what = f.kind === 'ap' ? `${f.label}${f.score ? ` (${f.score})` : ''}` : f.label;
  return f.kind === 'completed' ? `${what} · completed`
    : f.kind === 'ap' ? `${what} · AP credit`
    : f.kind === 'major' ? `${what} · ${f.term} (major prep)`
    : `${what} · ${f.term}`;
}

/** Every requirement of the pathway for this plan: status, what fills it, and what is still to choose. */
function geSectionHTML(p) {
  const ge = p.ge;
  const sources = [
    ge.standard && `Rules: ${ge.standard.source}.`,
    ge.course_list && `Courses: ${ge.course_list.institution_name}, ${ge.course_list.academic_year || 'undated'} list.`,
    ge.course_list?.note, ge.ap_source && `AP: ${ge.ap_source}.`, ge.ap_note
  ].filter(Boolean);
  const rows = ge.requirements.map(r => {
    const [label, cls, icon] = GE_STATE[r.status];
    const chip = r.status === 'in_progress' ? `${r.courses_required - r.open} of ${r.courses_required}` : label;
    const still = !r.open ? '' : r.options_note
      ? `<div class="text-xs text-amber-700 dark:text-amber-300">${esc(r.options_note)}</div>`
      : `<div class="text-xs text-slate-500 dark:text-slate-400">${r.open > 1 ? `${r.open} more courses` : 'Choose a course'} in a term above · ${plural(r.options.length, 'option')}</div>`;
    return `
      <li class="rounded-md border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-3 py-2">
        <div class="flex items-start justify-between gap-2">
          <span class="text-sm font-medium min-w-0">${esc(r.label)}${r.courses_required > 1 ? ` <span class="font-normal text-slate-400 dark:text-slate-500">(${r.courses_required} courses)</span>` : ''}</span>
          <span class="${CHIP} ${cls} shrink-0"><i class="fa-solid ${icon}" aria-hidden="true"></i>${esc(chip)}</span>
        </div>
        ${r.filled.map(f => `<div class="text-xs text-slate-600 dark:text-slate-300"><i class="fa-solid fa-check text-emerald-500 mr-1" aria-hidden="true"></i>${esc(geFillText(f))}</div>`).join('')}
        ${still}
      </li>`;
  }).join('');
  const s = ge.summary;
  return `
    <section class="rounded-lg border border-teal-200 dark:border-teal-500/30 bg-teal-50/40 dark:bg-teal-500/5 p-4 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h3 class="font-semibold text-sm"><i class="fa-solid fa-layer-group text-teal-600 dark:text-teal-300 mr-1.5" aria-hidden="true"></i>General education · ${esc(ge.name)}</h3>
        ${ge.requirements.length ? `<span class="text-xs text-slate-600 dark:text-slate-300">${s.met} satisfied · ${s.planned} planned · ${s.open} open</span>` : ''}
      </div>
      <p class="text-sm ${ge.all_satisfied ? 'text-emerald-700 dark:text-emerald-300 font-medium' : 'text-slate-600 dark:text-slate-300'}">${esc(ge.message)}</p>
      ${rows ? `<ul class="grid gap-2 sm:grid-cols-2">${rows}</ul>` : ''}
      ${sources.length ? `<p class="text-[11px] text-slate-500 dark:text-slate-400">${sources.map(esc).join(' ')}</p>` : ''}
    </section>`;
}

/** The side panel for a plan with a GE block: the term's GE courses (if opened from one), then every requirement. */
function renderGeStatusPanel(p, term, itemCodes) {
  const ge = p.ge;
  $('geTitle').textContent = term ? `GE courses · ${term}` : `${ge.name} requirements`;
  $('geSubtitle').textContent = `${p.name} · ${ge.name} · ${state.response.pathway.university}`;
  const bundle = itemCodes.map(code => courseInfo(p, code));
  const s = ge.summary;
  const total = s.requirements || 1;
  const pct = n => `${(n / total) * 100}%`;
  $('geBody').innerHTML = `
    <section class="rounded-lg bg-slate-50 dark:bg-slate-800/50 ring-1 ring-slate-200 dark:ring-slate-800 p-4">
      <p class="text-sm text-slate-600 dark:text-slate-300">${esc(ge.message)}</p>
      ${ge.requirements.length ? `
      <div class="mt-3 h-2 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden flex">
        <div class="h-full bg-emerald-500" style="width:${pct(s.met)}"></div>
        <div class="h-full bg-blue-500" style="width:${pct(s.planned)}"></div>
      </div>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300">
        <span><span class="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1"></span>${s.met} satisfied</span>
        <span><span class="inline-block w-2 h-2 rounded-full bg-blue-500 mr-1"></span>${s.planned} planned</span>
        <span><span class="inline-block w-2 h-2 rounded-full bg-slate-300 dark:bg-slate-600 mr-1"></span>${s.open} open · ${plural(s.courses_open, 'course')} to choose</span>
      </div>` : ''}
    </section>
    ${bundle.length ? `<section>
      <h3 class="text-xs font-semibold uppercase tracking-wider ${term.startsWith('Summer') ? 'text-amber-600 dark:text-amber-300' : 'text-teal-700 dark:text-teal-300'} mb-2">In ${esc(term)}</h3>
      <div class="space-y-2">${bundle.map(info => courseItemHTML(info) + (isChoice(info.label)
        ? `<button type="button" data-ge-remove="${esc(info.label)}" class="ml-7 -mt-1 text-xs text-red-600 dark:text-red-400 hover:underline">Remove ${esc(info.label)} from ${esc(term)}</button>` : '')).join('')}</div>
    </section>` : ''}
    <section class="space-y-3">
      <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">All ${esc(ge.name)} requirements</h3>
      ${ge.requirements.map(r => {
        const [label, cls] = GE_STATE[r.status];
        return `<div class="rounded-lg border border-slate-200 dark:border-slate-800 p-3">
          <div class="flex items-start justify-between gap-2">
            <h4 class="text-sm font-medium">${esc(r.label)}</h4>
            <span class="${CHIP} ${cls} shrink-0">${r.status === 'in_progress' ? `${r.courses_required - r.open} of ${r.courses_required}` : label}</span>
          </div>
          <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">${esc(r.rule)}</p>
          ${r.note ? `<p class="mt-1 text-xs text-slate-500 dark:text-slate-400"><i class="fa-solid fa-circle-info mr-1"></i>${esc(r.note)}</p>` : ''}
          ${r.filled.map(f => `<p class="mt-1 text-xs text-slate-700 dark:text-slate-200"><i class="fa-solid fa-check text-emerald-500 mr-1"></i>${esc(geFillText(f))}</p>`).join('')}
          ${r.open ? `<p class="mt-1 text-xs ${r.options_note ? 'text-amber-700 dark:text-amber-300' : 'text-slate-500 dark:text-slate-400'}">${esc(r.options_note
            || `${r.open} to choose from ${plural(r.options.length, 'course')} (pick them in the plan's terms).`)}</p>` : ''}
        </div>`;
      }).join('')}
    </section>`;
  $('geBody').querySelectorAll('[data-ge-remove]').forEach(b => b.addEventListener('click', () => removeGeChoice(b.dataset.geRemove)));
}
