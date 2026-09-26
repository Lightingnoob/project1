/* =====================================================================
 * api.js — HTTP client for the TransferPath FastAPI backend (main.py)
 *
 * The UI talks to the backend ONLY through these hooks:
 *
 *   fetchInstitutions()        -> { colleges, universities: { UC, CSU }, majors }
 *   fetchGePathways()          -> { pathways: [{ id, name, loaded, systems, courses_by_system, historical,
 *                                                 standard, college_lists: [{ institution, academic_year }] }], files }
 *   fetchCollegeCatalog(cc)    -> { [code]: { title, units } } | null      (404 -> null)
 *   searchCourses(cc, q, limit, subject?) -> { college, query, total, filter,        // subject: 'MTH' = only Mathematics
 *                                    subjects: [{ code, name, count, match }],        // "math" -> MTH (alias)
 *                                    groups: [{ code, name, count, courses: [{ code, title, units, college, sources, match }] }] }
 *                                  | null (404 -> null = no course catalog for this college)
 *   generateSep(request)       -> SEPResponse | null                        (404 -> null = "not uploaded yet";
 *                                 422 -> ApiError: invalid, infeasible, or an agreement the planner can't use yet)
 *        SEPResponse.articulation: the ASSIST agreement behind the plan (scope, requirement table), null for mock data
 *        request = { college, university, major, completed_courses, no_completed_courses, ap_scores, no_ap_scores,
 *                    waived_courses, ge_pathway: 'cal_getc', start_term: 'Fall 2026', include_summer: false,
 *                    ge_choices: [{ requirement: '3A', course: 'ARTH 1', term: 'Summer 2027' }] }
 *        Every section is required (ui.js section 3b; the backend re-checks and answers 422):
 *        college / university / major are listed options; completed_courses and ap_scores are
 *        non-empty OR their no_* flag is true (an empty list alone = unanswered).
 *        completed_courses: what the student passed; their prerequisite chains count as done too
 *        waived_courses: transfer-safe AP waivers; done, but their prerequisites are NOT implied
 *        ge_choices: GE courses the student picked for a requirement and term (optional)
 *        SEPResponse.plans[].ge: the GE pathway for that plan: case (not_loaded | not_applicable |
 *          requirements_only | courses_available), message, requirements [{ id, label, status, open,
 *          filled, options: [code] }]; SEPResponse.ge_courses: { code: { title, units, prereq_any, ... } };
 *          SEPResponse.ge_choices: { accepted, dropped: [{ course, reason }] }
 *   fetchApData(cc)            -> { cc_waivers, articulation, campus_data } | null   (404 -> null = no AP chart)
 *   loadApEngine()             -> ap_engine/apWaiver.js module { evaluateApWaivers, ... }
 *   fetchMockDataStatus() / injectMockData() / clearMockData()  (dev-only injector)
 *
 * Errors: network failure -> ApiError { offline: true }; other HTTP errors ->
 * ApiError with the server's `detail` message.
 *
 * The backend loads every ASSIST agreement in ap_engine/data/ at startup (GET /pathways lists them);
 * other combinations are "not uploaded yet". The Developer: Inject Mock Data button adds a demo pathway.
 * ===================================================================== */

// Always the local FastAPI server, however the page itself is served (uvicorn :8000,
// VS Code Live Server :5500, another dev server, or file://). CORS in main.py allows all origins.
// Override by setting window.SEP_API_BASE before this script loads.
const API_BASE = window.SEP_API_BASE ?? 'http://127.0.0.1:8000';

class ApiError extends Error {
  constructor(message, { status = 0, offline = false } = {}) {
    super(message);
    this.status = status;
    this.offline = offline;
  }
}

async function apiRequest(path, { method = 'GET', body, allow404 = false } = {}) {
  const url = API_BASE + path;
  console.log(`[TransferPath] ${method} ${url}`, body ?? '');   // proves the request is actually firing
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  }).catch(error => {
    // fetch only rejects when no HTTP response arrives: server not running, wrong port, or CORS blocked
    console.error('Fetch failed:', error, `\n→ Is the API running? Open ${API_BASE}/health — it should show {"status":"ok"}.`);
    return null;
  });
  if (!res) {
    throw new ApiError(`Can't reach the TransferPath API at ${API_BASE}.`, { offline: true });
  }
  if (allow404 && res.status === 404) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(d => d.msg).join('; ')
      : `HTTP ${res.status}`;
    throw new ApiError(msg, { status: res.status });
  }
  return data;
}

const fetchInstitutions = () => apiRequest('/institutions');

/* GE pathways the form offers (Cal-GETC, IGETC, UC 7-Course Pattern) and whether their data is loaded */
const fetchGePathways = () => apiRequest('/ge-pathways');

const fetchCollegeCatalog = cc =>
  apiRequest(`/colleges/${encodeURIComponent(cc)}/catalog`, { allow404: true });

/* Completed-courses autocomplete: subject first, then its courses, from the college's catalog (+ pathway courses) */
const searchCourses = (cc, q, limit = 20, subject = null) =>
  apiRequest(`/colleges/${encodeURIComponent(cc)}/courses?q=${encodeURIComponent(q)}&limit=${limit}` +
    (subject ? `&subject=${encodeURIComponent(subject)}` : ''), { allow404: true });

const generateSep = request =>
  apiRequest('/generate-sep', { method: 'POST', body: request, allow404: true });

/* AP credit: the college's AP chart + campus score rules, evaluated in the browser by ap_engine/apWaiver.js */
const fetchApData = cc =>
  apiRequest(`/colleges/${encodeURIComponent(cc)}/ap-data`, { allow404: true });

let apEngine = null;
const loadApEngine = () => (apEngine ??= import(`${API_BASE}/ap_engine/apWaiver.js`)
  .catch(error => {
    console.error('AP engine import failed:', error);
    apEngine = null;   // allow a retry once the server is back
    throw new ApiError(`Can't load the AP engine from ${API_BASE}/ap_engine/.`, { offline: true });
  }));

/* Dev-only: hackathon data injector (backend keeps the injected data until cleared or restarted) */
const fetchMockDataStatus = () => apiRequest('/dev/mock-data');
const injectMockData = () => apiRequest('/dev/mock-data', { method: 'POST' });
const clearMockData = () => apiRequest('/dev/mock-data', { method: 'DELETE' });
