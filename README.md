# TransferPath SEP Planner

TransferPath SEP Planner helps community college students build a Student Educational Plan (SEP) that maps out the courses they need to transfer to their target four-year university.

## Run it

One server runs both the backend and the frontend. From this folder:

```
pip install -r requirements.txt
uvicorn main:app --reload        # app at http://127.0.0.1:8000/, API docs at /docs
python -m unittest discover -s tests -v
```

AP engine tests (Node 18+, no `npm install` needed): `cd ap_engine` then `npm test`.

## Project layout

```
main.py            FastAPI backend; also serves the frontend and ap_engine/
pathway_data.py    loads ASSIST agreement JSON into planner pathways (discovery, prerequisites, matching)
ge_data.py         loads GE data: pathway registry, statewide GE standards, college GE course lists, AP rules
institutions.py    canonical institution ids ("Cal State East Bay" = "CSUEB" = csueb)
sep_engine/        Python scheduling engine (plans, graph, critical path)
tests/             Python API + engine tests
index.html         frontend page
api.js             every backend call the frontend makes
ui.js              frontend logic and rendering
assets/            prebuilt Tailwind CSS, fonts, icons, Cytoscape
ap_engine/         AP waiver engine (browser JS: apWaiver.js, graph.js), AP data, Chabot catalog,
                   Chabot prerequisites + 4 CSUEB major agreements, Node tests
ap_engine/data/ge/ GE standards (IGETC, UC 7-course pattern) and college GE course lists (Chabot, Porterville)
```

## AP credit

AP scores entered in the form are checked by `ap_engine/apWaiver.js`, which runs in the browser:

1. `ui.js` calls `fetchApData(college)` (`GET /colleges/{college}/ap-data`) and `loadApEngine()` (imports `/ap_engine/apWaiver.js`).
2. Each score is checked against the college's AP chart (only Chabot College so far) and the target university's AP rules.
3. The results appear in an **AP credit** card under the plans: Waive, Take the course, Ask a counselor, or Score too low.
4. Courses marked **Waive** for transfer are sent as `waived_courses` in `POST /generate-sep`, but only if they exist in the pathway's course catalog. No AP score means no AP credit, so the course stays in the plan; the student is never asked to mark AP courses completed. Unlike completed courses, AP credit does not imply the course's prerequisites. A waived course is not taken, though, so its prerequisites are planned only if another planned course needs them. For example, for Chabot → CSU East Bay Computer Science, AP Computer Science A (3+) waives CSCI 14, and MTH 55, needed only for CSCI 14, drops out of the plan. The mock pathway uses illustrative codes (`MATH 1`, `CS 1`), not the real Chabot ones (`MTH 1`, `CSCI 14`). With mock data, the card therefore reports that the plan is unchanged.

To add another college, put its AP chart and ASSIST articulation JSON in `ap_engine/data/` and add it to `AP_CHARTS` in `main.py` (keyed by the college's canonical id, e.g. `chabot`).

The UI loads its CSS, fonts, icons and graph libraries from `assets/`, not from CDNs. It is fully styled offline, on networks that block CDNs, and in editor preview panes. `assets/tailwind.css` is prebuilt from the classes in `index.html` and `ui.js`. If you add new Tailwind classes, rebuild it (the Tailwind CDN script in `<head>` covers new classes in the meantime when online):

```
npx tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.input.css -o assets/tailwind.css --minify
```

## Transfer pathway data (`pathway_data.py`)

At startup the backend loads every ASSIST agreement in `ap_engine/data/`: data file → loader → pathway → store → `POST /generate-sep` → `sep_engine`. Today that is Chabot College → CSU East Bay for **Computer Science, B.S.** and **Computer Engineering, B.S.** (2026-2027). Physics and Biochemistry are found but reported as *unsupported*: their "Option A or B" bundles and minimum-unit electives can't be expressed by the engine yet, so `/generate-sep` answers 422 with that reason instead of planning them wrongly. The server log and `GET /pathways` list what loaded and why anything didn't.

- **Discovery.** Files are recognized by their JSON shape, not their name. An ASSIST agreement has `agreement`, `institutions`, `courses` and `requirements`. A prerequisite registry has `institution_id`, `semantics` and `courses`. A campus list has `campuses`. To add a pathway, copy the ASSIST JSON into `ap_engine/data/`, then restart or call `POST /pathways/reload`. A second file with an existing `dataset_id` is skipped as a duplicate.
- **Institutions** match by canonical id taken from the data (`chabot`, `csueb`). Everyday spellings are derived from the official name, so "Cal State East Bay", "CSU East Bay", "CSUEB" and "California State University, East Bay" are all `csueb` (`institutions.py`).
- **Majors** match by name, with the degree as a separate field: "Computer Science", "Computer Science, B.S." and "Computer Science BS" find the same agreement. "Computer Science, B.A." does not.
- **Academic year** comes from the start term: Fall 2026 → 2026-2027, and Spring/Summer 2027 → 2026-2027 too. When that year isn't loaded, the nearest agreement is used and the plan says so.
- **GE.** ASSIST major agreements have no GE requirements, so one agreement serves every GE pathway. The chosen pathway's GE is planned from the GE data (next section).
- **Articulation → engine.** A single course or an AND bundle becomes required major prep. An OR ("CSCI 15 or CSCI 19A") becomes a choose-one group, so only one option is planned. Cross-listed codes (MTH 8 = CSCI 28) become one course with aliases. A *No Course Articulated* requirement has nothing to take at the college, so it is left out: not planned, listed or mentioned.
- **Prerequisites** come from `chabot_prerequisites.json`, overlaid by records embedded in the agreement. "Before" becomes a prerequisite; "before or concurrent" becomes a co-requisite. An OR is never flattened: it resolves to an alternative the pathway already requires, or else it becomes an advisory warning. Placement and other conditions are advisories. An agreement without prerequisite data (`"prerequisite_data": {"status": "not_provided_in_source"}`) still loads, and its courses are flagged instead.
- **Scope.** Each plan returns `articulation` with the agreement's scope. For example, `lower_division_core` with `complete_degree_requirements: false` means the plan covers the agreement's lower-division core, not the full 120-unit degree. The UI shows this in a **Transfer agreement** card.

For a demo of the GE features, click **Developer: Inject Mock Data** (or `POST /dev/mock-data`). This adds a mock pathway, Chabot College -> UC Berkeley -> Computer Science, with both CAL-GETC and the 7-Course Pattern. Clearing it leaves the real pathways loaded. Any other combination shows "N/A - Transfer data for this specific pathway has not been uploaded yet."

## General education (`ge_data.py`, `sep_engine/ge.py`)

The GE pathway the student picks (Cal-GETC, IGETC or the UC 7-Course Pattern) decides which GE requirements a plan contains. Two kinds of data are kept apart:

- A **GE standard** is a pathway's statewide rules: areas, course counts, UC/CSU differences and AP rules.
- A **college GE course list** says which of one college's courses count toward each area, for one academic year. Only the selected college's own list is used: Porterville's list never supplies Chabot courses.

Files in `ap_engine/data/ge/` (and `ap_engine/data/`) are recognized by their contents, not their names. `GET /ge-pathways` lists each file's role.

| File | Role |
|---|---|
| `igetc_standards_v2_0_2019.json` | IGETC standard (2019): areas 1–6, CSU-only 1C, UC-only 6A, one-area rule, AP rules. No courses. |
| `uc_seven_course_pattern.json` | UC 7-Course Pattern standard, transcribed from the Chabot catalog (p. 35; AP areas pp. 41–43). UC only. |
| `chabot_cal_getc_2025_2026.json` | Chabot's Cal-GETC course list (199 courses), extracted from the catalog's Cal-GETC certificate by `ap_engine/tools/extract_chabot_cal_getc.py`. |
| `porterville_cal_getc_2025_2026.json` | Porterville's Cal-GETC course list (ASSIST). Used only for Porterville College. |
| `../chabot_ap_waivers.json` | Chabot's AP chart: its Cal-GETC column gives the Cal-GETC AP rules for Chabot students. |

**Cal-GETC's area rules** come with the college lists (ASSIST publishes them there). When no standard file exists for a pathway, the loader uses those rules, without the courses. **AP rules** come from the standard itself (IGETC, 7-course), or else from the college's AP chart for that pathway (Cal-GETC at Chabot), or else there are none and the plan says so.

For each plan, `sep_engine/ge.py` fills every requirement from four sources:

- courses the student completed (old codes such as `ENGL 1` count as `ENGL C1000`);
- AP exams;
- major prep the plan already schedules that the list approves (e.g. MTH 1 → Cal-GETC Area 2);
- GE courses the student picked.

Each course counts toward one area only, except a 5C lab listed with its 5A/5B course and IGETC's 6A language. "Two disciplines" and "at least two areas" minimums are enforced. Each plan's `ge` block has a `case`:

- `courses_available`: rules and a course list; open requirements list the college's eligible courses.
- `requirements_only`: rules only (e.g. IGETC at Chabot); areas are shown with "Course selection unavailable: Chabot College IGETC course list not loaded."
- `not_applicable`: the pathway doesn't apply to the target (UC 7-Course Pattern for a CSU).
- `not_loaded`: "GE pathway data is not loaded."

When every requirement is done, the message is "GE pathway requirements satisfied." and no GE is scheduled.

**Summer** cards are the GE workspace. They offer a course picker for each open requirement, with only courses that fit the 9-unit summer limit and whose catalog prerequisite is done or planned earlier. Fall/Spring cards offer the same under "Add a GE course". A pick is sent back as `ge_choices` and scheduled in that term (pinned), for both plans. Picks that stop counting (another pathway, a completed course, a full summer) are dropped with the reason.

**To add a college's GE list:** save its ASSIST GE list JSON (same layout as Porterville's: `institution`, `ge_pathway.requirements`, `courses` with the area codes) into `ap_engine/data/ge/`, then restart or `POST /pathways/reload`. Its `institution.id` must match the college's canonical id, and `source.academic_year` picks the right year. A list for another year is used only when no exact one exists, and the plan says so. A new pathway's standard can use the layout of `uc_seven_course_pattern.json` (`"kind": "ge_standard"`).

## Scheduling engine (`sep_engine/`)

Python 3.10+ package that turns articulation data into SEP plans. `python -m sep_engine [--summer]` prints plans for the mock data.

Pipeline in `generate_sep()` (runs once per plan: Plan A "Fastest Route", Plan B "Balanced Workload"):

1. **Dijkstra** (`pathway.py`) picks courses for each multiple-choice requirement group with the lowest total weight: units for Plan A, historical difficulty for Plan B.
2. **Topological sort** (`toposort.py`, Kahn's algorithm) orders the selected courses by prerequisites and reports any prerequisite cycle.
3. **CSP backtracking** (`scheduler.py`) assigns courses to real terms ("Fall 2026", "Summer 2027", ...) starting from the student's start term. It uses DFS with MRV and forward checking. Rules:
   - Fall/Spring: up to 18 units (13 for Plan B). Summer: only when enabled, up to 9 units.
   - Plans always **end in a Spring** term, for Fall transfer admission. If the courses finish in a Fall, the plan is padded with the next Spring.
   - GE courses are placed in Summer first; a GE course the student picked for a term is pinned there (released, with a warning, only if that term isn't in the plan). Major prep stays out of Summer unless no plan of the same length exists without it.
   - Fall-/Spring-only offerings, co-requisites and meeting-time conflicts are respected.

Real data reaches the engine through `pathway_data.py` (see above). To plug in another source directly, produce JSON matching `SEPRequest` in `sep_engine/models.py` (`catalog`, `agreement`, `profile`).

## API (`main.py`)

`GET /colleges/{college}/courses?q=cs1&limit=12` powers the **Completed courses** search box (`course_search.py`). It searches the college's catalog file (`ap_engine/data/catalog/`, Chabot College only for now) plus any uploaded pathway courses. The search has two levels:

1. **Subject.** `mth`, `math` and `Mathematics` all resolve to **Mathematics (MTH)**. Subject names are read from the catalog's table of contents. Priority: exact code, exact name, alias, code prefix, name prefix, then a word of the name. Aliases such as `math`, `bio` and `cs` live in `SUBJECT_ALIASES` in `course_search.py`, keyed by subject *name*, so another college's Mathematics department (MATH, MAT…) gets them too.
2. **Courses.** The subject's courses are listed in course-number order, and a typed number filters inside the subject (`math 4` → MTH 4 first). Course-code and title matches (`C1000`, `calculus`) come only after subject matches, and are skipped when the subject is clear. Matching ignores case and spacing (`mth1` = `MTH 1`).

In the dropdown, each subject is a **category** option followed by its courses. Choosing a category (click, or ↑ ↓ + Enter) adds nothing. Instead it shows a `[Mathematics ×]` chip and searches only that subject (`&subject=MTH`): an empty box lists all its courses, `1` → MTH 1 first, and `calculus` → its calculus courses. The chip's × or Backspace goes back to all subjects. Only courses become completed-course tags. Only a course picked from these results can be added as completed. To add another college, put its catalog JSON in `ap_engine/data/catalog/` and list it in `CATALOGS` in `course_search.py`, keyed by the college's canonical id.

`GET /ge-pathways` lists the GE pathways (loaded or not, courses per UC/CSU, college lists, AP rules) and every GE file's role. `GET /pathways` lists every loaded pathway (`available` or `unsupported` with a `reason`) and any skipped files. `POST /pathways/reload` re-reads `ap_engine/data/` without a restart.

`POST /generate-sep` takes `college`, `university`, `major`, `completed_courses` + `no_completed_courses`, `ap_scores` + `no_ap_scores`, `waived_courses` (AP credit), `ge_pathway` (`cal_getc`, `igetc` or `seven_course_pattern`, or their names), `start_term` (e.g. `"Fall 2026"`), `include_summer`, and optional `ge_choices` (`[{"requirement": "3A", "course": "ARTH 1", "term": "Summer 2027"}]`). **Every section is required**, and nothing has a stand-in default:

- `college`, `university` and `major` must be options from `GET /institutions` (any spelling of the same institution counts).
- `completed_courses` and `ap_scores` need at least one entry *or* their explicit `no_*` flag set to `true`. An empty list without the flag is unanswered, and entries plus the flag is a conflict.
- `ge_pathway`, `start_term` and `include_summer` must be sent. `false` is a valid summer answer.

An incomplete request gets a 422 that names each missing answer with the same wording as the form ("Please select a GE pathway."). The form (`ui.js`, section 3b) checks the same rules first: fields marked * show inline errors, and **Generate SEP Plans** stays inactive until every section is answered. Clicking it early shows every error and moves focus to the first one.

It returns 404 when no agreement exists for the college + university + major, and 422 when one exists but can't be planned yet. Completed courses match however they are written (`MTH 2`, `mth2`, `chabot:MTH2`), and a cross-listed code counts as its course (`CSCI 28` → `MTH 8`). Otherwise it returns `articulation` (the ASSIST agreement: scope, and each articulated university requirement with its community-college options) and `plans`, and each plan has:

**Completed courses satisfy their prerequisite chains.** Before choosing, ordering or scheduling anything, `sep_engine/completion.py` works out the satisfied set: completed courses plus every prerequisite and co-requisite they required, followed recursively. For example, completing CS 20 also satisfies CS 2 and CS 1. Satisfied courses are never planned, so they don't add terms, units or ordering constraints. The student's list is not changed; the implied courses come back as `pathway.inferred_courses`, and graph nodes carry `satisfied_by`. AP waivers count as done but don't imply their prerequisites.

- `semesters`: real term names, unit caps and padding flags
- `graph`: Cytoscape `{nodes, edges}`
- `critical_path` / `critical_path_edges`
- `requirements` and `summary`
- `ge`: the GE pathway for that plan (see General education). The response also has `ge_courses` (details of every GE course offered) and `ge_choices` (accepted, or dropped with the reason).

Pydantic response models validate the whole payload, including consecutive real terms and the Spring ending.
