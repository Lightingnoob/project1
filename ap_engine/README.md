# Articulate DAG project folder

This folder includes the four graph functions, their tests, synthetic example data, and all four previously extracted Chabot-to-CSU East Bay major agreements. Original articulation mappings are preserved; the major files now include separately sourced Chabot prerequisite records.

## Start in VS Code

This folder is now `ap_engine/` inside the TransferPath project. `main.py` serves it at `/ap_engine/`, and the web UI runs `apWaiver.js` in the browser (see the top-level README).

1. Open Terminal > New Terminal and `cd ap_engine`.
2. Run `npm test`. No dependency installation is needed for the tests.

## Folder contents

```text
articulate-dag/
├── graph.js
├── graph.test.js
├── apWaiver.js                      AP waiver eligibility (community college vs. transfer campus)
├── apWaiver.test.js
├── chabotAp.test.js                 real Chabot courses against UC campuses
├── check_ap_waivers.mjs             command-line AP waiver checker
├── ap_cc_waivers.example.json       synthetic demo waivers for courses.example.json
├── check_prerequisites.py
├── courses.example.json
├── package.json
├── README.md
├── PREREQUISITE_REVIEW.md
├── prerequisite_validation.json
├── requirements-validation.txt
├── catalog.test.js
├── tools/parse_catalog.py           catalog PDF text -> JSON
└── data/
    ├── catalog/chabot_2025_2026_courses.json   every course in the 2025-2026 catalog
    ├── catalog/chabot_2025_2026_pages.json     every catalog page as text
    ├── ap_campus_score_requirements.json   UC/CSUEB AP score rules
    ├── chabot_ap_waivers.json              Chabot 2025-2026 catalog AP chart
    ├── chabot_assist_articulation.json     template to fill from ASSIST
    ├── chabot_prerequisites.json           sourced Chabot prerequisite registry
    ├── computer_science.json               Chabot -> CSUEB agreement
    ├── computer_engineering.json           Chabot -> CSUEB agreement
    ├── biochemistry.json                   Chabot -> CSUEB agreement
    └── physics.json                        Chabot -> CSUEB agreement
```

To run the structural prerequisite check from this folder (Windows: `python` instead of `python3`):

```bash
python3 -m pip install -r requirements-validation.txt
python3 check_prerequisites.py        # writes prerequisite_validation.json
```

| File | Major | Agreement year | JSON schema |
|---|---|---|---|
| `data/computer_science.json` | Computer Science | 2026-2027 | 1.0.0 |
| `data/computer_engineering.json` | Computer Engineering | 2026-2027 | 1.0.0 |
| `data/biochemistry.json` | Biochemistry — Chemistry Education concentration | 2025-2026 | 1.1.0 |
| `data/physics.json` | Physics | 2026-2027 | 1.2.0 |

## What is ready, and what remains

The graph algorithms and tests are ready to run. The four major files contain articulation mappings and requirement groups, including AND/OR bundles and elective thresholds. Their `prerequisite_data` now contains rich, sourced rules, including user-clarified entries and policy caveats. The files cannot be passed directly to `graph.js`.

For a real course schedule, review the user-clarified rules and remaining policy caveats, selected pathway and concurrent-enrollment constraints before adapting to the graph functions. Use `courses.example.json` for the included synthetic demo tests. The Biochemistry agreement is for 2025–2026; the other three are for 2026–2027.

This package contains the code and data created so far. A frontend (`index.html`, Cytoscape rendering, and the semester board) has not yet been built.

---

# Articulate DAG: graph.js

Four browser-compatible ES module exports, with no runtime dependencies:

| Function | Returns |
|---|---|
| `topoOrder(courses)` | Course IDs in Kahn topological order |
| `criticalPath(courses, completed = [])` | `{ path, semesters, semesterByCourse }` |
| `availableNow(courses, completed = [])` | Available, unfinished course IDs |
| `validatePlan(courses, plan)` | `{ valid, violations, unscheduled }` |

## Run the tests

Open this folder in VS Code and run in its terminal:

```bash
npm test
```

Node.js 18 or newer is required for the test runner. No `npm install` is needed. `npm test` runs all 43 tests (14 graph, 12 AP waiver, 14 Chabot AP, 3 catalog). Only the tests use Node APIs; `graph.js` can be imported directly into a browser module.

## Input contract

```js
const courses = [
  { id: 'A', prereqs: [] },
  { id: 'B', prereqs: ['A'] },
  { id: 'C', prereqs: ['A'] },
  { id: 'D', prereqs: ['B', 'C'] }
];
```

This is a synthetic example, not verified Chabot catalog data. Optional metadata such as `title` and `units` is allowed and is not modified.

- `id` must be unique. Every prerequisite must refer to an included course.
- Every course must provide `prereqs`. Use `[]` only when no prerequisites is verified. Missing or null data throws an error.
- `prereqs` means AND: every listed prerequisite is required.
- These algorithms operate on selected courses. Resolve elective/OR options into an explicit selected pathway before calling them; the module deliberately rejects expression objects rather than flattening OR into AND.
- `completed` accepts an array or Set of known IDs. Completion is accepted as an input fact; the user need not separately mark all ancestors of an already-completed course.
- Each unfinished course takes one semester; prerequisites must finish in an earlier semester. The semester result is a lower bound that assumes unlimited parallel enrollment, availability every semester, and no unit cap. Corequisites, grades, placement rules, and equivalent-course credit decisions are outside this module.
- Do not pass an ASSIST articulation graph as a prerequisite graph. The four extracted agreement files do not contain `prereqs`. Use the new sourced registry as research input; resolve its review items and richer constraints before adapting it for real scheduling. Articulation AND/OR rules and elective thresholds remain separate from these prerequisite algorithms.

## Four small examples

```js
import {
  topoOrder, criticalPath, availableNow, validatePlan
} from './graph.js';

const courses = [
  { id: 'A', prereqs: [] },
  { id: 'B', prereqs: ['A'] },
  { id: 'C', prereqs: ['A'] },
  { id: 'D', prereqs: ['B', 'C'] }
];

topoOrder(courses);
// ['A', 'B', 'C', 'D']

criticalPath(courses, ['A']);
// {
//   path: ['B', 'D'],
//   semesters: 2,
//   semesterByCourse: { A: 0, B: 1, C: 1, D: 2 }
// }
// B and C run in parallel. C -> D is an equally long path;
// the function returns one deterministic critical path.

availableNow(courses, ['A']);
// ['B', 'C']

validatePlan(courses, [['A'], ['B', 'C'], ['D']]);
// { valid: true, violations: [], unscheduled: [] }

validatePlan(courses, [['A', 'B'], ['C', 'D']]);
// valid: false
// B requires A before semester 1.
// D requires C before semester 2.
```

Completed courses can be supplied to the plan checker:

```js
validatePlan(courses, {
  completed: ['A'],
  semesters: [['B', 'C'], ['D']]
});
// { valid: true, violations: [], unscheduled: [] }
```

Plan semesters are numbered from 1 in violation objects. Empty semesters are allowed. A valid partial plan can still have `unscheduled` courses; `valid` does not mean degree completion. Course IDs in `violations` can be used to mark semester-board cards red.

## Error handling

Invalid course data or a cycle throws an Error. A cycle error has `code === 'CYCLE_DETECTED'` and `blockedCourseIds`; that list may include descendants blocked by the cycle, not just cycle members. Invalid plan shape also throws. For a well-formed plan, unknown scheduled IDs, duplicates, already-completed courses, and unmet prerequisites are returned as violation objects.

All functions reject cyclic graphs. Kahn's algorithm and the course-graph computations take O(V + E) time. Plan validation additionally scans scheduled courses and their prerequisites. Inputs are never mutated.

## Files

- `graph.js`: the four functions and shared validation helpers.
- `graph.test.js`: 14 executable tests, including a small example for each function.
- `courses.example.json`: synthetic demo data.
- `package.json`: ES module and test-runner configuration.


## Prerequisite research update (2026-09-24)

See `PREREQUISITE_REVIEW.md` for all 35 Chabot courses and the screenshot clarifications. Each major JSON now embeds its course records in `prerequisite_data.records`; the shared `data/chabot_prerequisites.json` adds supporting courses and explicit semantics. These rich records are not direct input to the AND-only graph functions. Original articulation mappings and agreement years are preserved. Run `python3 check_prerequisites.py` after installing the version in `requirements-validation.txt` for structural checks.

Three previously ambiguous rules (CHEM 1A, BIOS 21B, BIOS 21C) now use the user-supplied screenshot groupings, labeled `user_clarified`. This is not independent official verification. CHEM 201 policy applicability remains open.


## AP waivers (apWaiver.js)

Two separate decisions are made for every AP score:

1. **Chabot:** a score of 3, 4 or 5 waives the course listed in the 2025-2026 catalog AP chart (`data/chabot_ap_waivers.json`) and counts toward the listed Cal-GETC area.
2. **Transfer campus:** whether the intended UC (or CSU East Bay) awards credit for that course at that score (`data/ap_campus_score_requirements.json`). UC minimums are often higher.

Each Chabot course gets one recommendation:

| Recommendation | Meaning |
|---|---|
| `waive` | Safe to skip for this campus (campus course credit, or a GE/elective role covered by Cal-GETC). |
| `take_course_for_transfer` | Chabot waives it, but the campus needs a higher score; `requiredTransferScore` says which. |
| `verify_before_waiving` | Not resolvable from the data (no campus rule, no articulation, no Cal-GETC area). |
| `not_eligible` | Score below Chabot's minimum of 3. |

```js
import { evaluateApWaivers, completedWithWaivers, validateArticulation } from './apWaiver.js';

const result = evaluateApWaivers({
  scores: [{ examId: 'ap:calculus_ab', score: 3 }],
  ccWaivers: chabotApWaivers,            // data/chabot_ap_waivers.json
  campusData: apCampusRequirements,      // data/ap_campus_score_requirements.json
  target: {
    campusId: 'san_diego',
    contexts: ['campus_chart'],          // optional; UCLA/Berkeley need a college, e.g. 'The College'
    courseRoles: { 'STAT C1000': 'ge' }, // optional per-student override
  },
  articulation: {                        // from ASSIST, per campus and per Chabot course
    campuses: { san_diego: { 'MTH 1': ['MATH 20A'], 'CSCI 14': [] } },
  },
});
// result.courses[0] → { courseId: 'MTH 1', recommendation: 'take_course_for_transfer',
//                       requiredTransferScore: 4, targetCourses: ['MATH 20A'], reasons: [...] }

// Mark courses completed for graph.js (either/or rows need a choice):
completedWithWaivers(courses, [], result, { mode: 'transfer', choices: { 'ap:united_states_history': 'HIS 8' } });

// Check hand-entered ASSIST data first; throws on a bad shape or unknown campus,
// and lists courses no AP rule waives (often a typo such as 'MATH 1' for 'MTH 1'):
validateArticulation(articulation, chabotApWaivers, apCampusRequirements); // → { unmatchedCourses: [] }
```

Command line:

```bash
node check_ap_waivers.mjs --campus davis calculus_ab=3 psychology=4
node check_ap_waivers.mjs --campus los_angeles --context "The College" english_language_and_composition=3
```

Limits: UC course matches must come from ASSIST (the articulation template is empty until filled by hand). Without them, any campus course award counts and the result is flagged `manualReview`. A UC or CSU AP award never marks a Chabot course completed; only Chabot's own chart does. Course roles (`major_prep`/`ge`) are inferred defaults; override per student with `target.courseRoles`.

## Chabot 2025-2026 catalog as JSON

`data/catalog/chabot_2025_2026_courses.json` holds all 1,341 courses (1,093 credit, 96 noncredit, 152 apprenticeship) in 95 departments. Each record:

```json
{
  "id": "MTH 2", "department": "MTH", "number": "2", "title": "Calculus II",
  "type": "credit", "units": { "min": 5, "max": 5 }, "noncredit_hours": null, "page": 303,
  "description": "Continuation of differential and integral calculus, ...",
  "prerequisite": "MTH 1", "prerequisite_course_ids": ["MTH 1"],
  "hours": { "lecture": { "min": 90, "max": 90 } }, "see_also": [], "formerly": null
}
```

Optional fields when the catalog lists them: `corequisite`, `strongly_recommended`, `other_requirements` (unlabeled rules such as placement), each text field with a matching `*_course_ids` list. `*_course_ids` lists every course code mentioned, AND and OR alike, so read the text for the actual rule; these are not `graph.js` prereqs. Key records by `id` + `type`, since a department can list the same number as credit and noncredit.

`data/catalog/chabot_2025_2026_pages.json` keeps every page's text with its page number and running header, for anything not in the course records (programs, policies, the AP chart).

Regenerate from the PDF's text extraction:

```bash
python3 tools/parse_catalog.py 2025-2026_catalog_pdf.txt data/catalog
```

Note: some catalog prerequisites differ from the newer sources in `PREREQUISITE_REVIEW.md`. For example, the catalog still lists an algebra/trigonometry prerequisite for MTH 1, while the review records open access from Fall 2025.
