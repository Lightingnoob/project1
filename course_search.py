"""
Course search for the "Completed courses" autocomplete (GET /colleges/{college}/courses in main.py).

Two sources, merged by course code:
  catalog  - the college's published catalog, ap_engine/data/catalog/ (parsed from the catalog PDF)
  pathway  - courses in uploaded transfer-pathway data (main.STORE); the only courses the plan generator knows

TWO-LEVEL SEARCH
  Level 1 - resolve the SUBJECT the user means. Each college gets a subject index built from its
            own data: department code (MTH) -> name (Mathematics, read from the catalog's table of
            contents) -> aliases (the code, the name, and SUBJECT_ALIASES below, e.g. "math").
            Subject match priority:
              1 exact subject code     "mth", "MTH"        -> MTH
              2 exact subject name     "mathematics"       -> MTH
              3 subject alias          "math", "bio"       -> MTH, BIOS (Life Sciences)
              4 subject code prefix    "cs"                -> CSCI
              5 subject name prefix    "comp sci"          -> Computer Science
              6 subject name word      "science"           -> Computer Science, Life Sciences ...
  Level 2 - list COURSES:
              a. courses of the resolved subjects, best subject first, in course-number order;
                 a typed number filters inside the subject ("math 4" -> MTH 4 first, then MTH 41 ...)
              b. exact course code                "engl c1000" -> ENGL C1000
              c. course-code prefix / number      "C1000"      -> ENGL C1000, STAT C1000 ...
              d. course title (lowest)            "calculus"   -> MTH 1 Calculus I ...
            b-d from other subjects are skipped when the subject is clear (a code, name or
            alias hit), so "math" lists MTH courses, not every title containing "math".
            Results are grouped by subject for display, best group first.

SUBJECT FILTER: search_in_subject() searches one subject only (the UI's "[Mathematics x]" chip):
  ""  -> all its courses   "1" -> MTH 1, MTH 15 ...   "calculus" -> its Calculus courses

Matching ignores case and spacing ("mth1" == "MTH 1"). Official course codes are never renamed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from institutions import REGISTRY

CATALOG_DIR = Path(__file__).parent / "ap_engine" / "data" / "catalog"

# canonical institution id (institutions.py) -> its catalog files and display name.
# Any spelling of the college finds it: "Chabot College", "Chabot", "chabot".
CATALOGS = {
    "chabot": {"name": "Chabot College", "label": "2025-2026 catalog",
               "courses": "chabot_2025_2026_courses.json", "pages": "chabot_2025_2026_pages.json"},
}
for _id, _cfg in CATALOGS.items():
    REGISTRY.register(_id, _cfg["name"])
PATHWAY_LABEL = "transfer pathway data"

# Everyday names for subjects, keyed by the subject NAME (not its code), so they work for any
# college: every college's "Mathematics" department (MTH, MATH or MAT) answers to "math".
# Only needed where the name itself can't be typed naturally (codes, full names and name
# prefixes are matched automatically).
SUBJECT_ALIASES = {
    "mathematics": ["math", "maths"],
    "statistics": ["stats"],
    "computer science": ["cs", "comp sci", "programming"],
    "computer information systems": ["cis"],
    "life sciences": ["biology", "bio", "life science"],
    "biology": ["bio"],
    "chemistry": ["chem"],
    "physics": ["phys"],
    "english": ["eng", "writing"],
    "english as a second language": ["esl"],
    "economics": ["econ"],
    "psychology": ["psych"],
    "political science": ["poli sci", "government"],
    "history": ["hist"],
    "sociology": ["soc"],
    "anthropology": ["anthro"],
    "philosophy": ["phil"],
    "geography": ["geog"],
    "geological sciences": ["geology"],
    "communication studies": ["speech", "communications"],
    "theater arts": ["theatre", "drama"],
    "physical education": ["pe", "p e"],
    "administration of justice": ["criminal justice"],
}

SUBJECT_MATCH = {1: "code", 2: "name", 3: "alias", 4: "code prefix", 5: "name prefix", 6: "name word"}
MULTI_SUBJECT_MIN = 4       # when several subjects match, each shows at least this many courses


def compact(text: str) -> str:
    """'cs 1' -> 'CS1': uppercase, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _prefix_match(query_words: list[str], words: list[str] | tuple[str, ...]) -> bool:
    """Every typed word starts some word of the text."""
    return bool(query_words) and all(any(w.startswith(q) for w in words) for q in query_words)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CourseEntry:
    code: str                     # "CSCI 14" (official code, never renamed)
    title: str
    units: str | None             # "4 units", "0.5-2 units", "54 hours (noncredit)"
    subject: str | None           # "Computer Science"
    sources: tuple[str, ...]
    # Precomputed for matching
    key: str = field(init=False)
    department: str = field(init=False)
    number: str = field(init=False)
    title_words: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        department, _, number = self.code.partition(" ")
        object.__setattr__(self, "key", compact(self.code))
        object.__setattr__(self, "department", department.upper())
        object.__setattr__(self, "number", compact(number))
        object.__setattr__(self, "title_words", tuple(_words(self.title)))

    def number_key(self) -> tuple:
        """Course-number order: MTH 1, MTH 2 ... MTH 44, MTH 104; C1000 sorts as 1000."""
        digits = re.search(r"\d+", self.number)
        return (int(digits.group()) if digits else 0, self.number)


@dataclass(frozen=True)
class Subject:
    code: str                     # "MTH"
    name: str | None              # "Mathematics" (None for a pathway-only department)
    count: int
    names: tuple[str, ...]        # lowercase name forms: "mathematics"; "apprenticeship: roofing", "roofing"
    aliases: frozenset[str]       # compact lowercase: "math", "maths"

    def match_tier(self, letters: str) -> int | None:
        """How well typed letters ('math', 'comp sci') name this subject: 1 (best) .. 6, or None."""
        text = " ".join(_words(letters))
        flat = text.replace(" ", "")
        if not flat:
            return None
        words = text.split()
        name_words = [_words(n) for n in self.names]
        if flat.upper() == self.code:
            return 1
        if any(" ".join(w) == text for w in name_words):
            return 2
        if flat in self.aliases:
            return 3
        if self.code.startswith(flat.upper()):
            return 4
        if any(w and len(words) <= len(w) and all(nw.startswith(q) for q, nw in zip(words, w)) for w in name_words):
            return 5                   # "math" -> Mathematics, "comp sci" -> Computer Science
        if any(_prefix_match(words, w) for w in name_words):
            return 6                   # "science" -> Computer Science
        return None


@dataclass(frozen=True)
class Catalog:
    name: str                          # "Chabot College"
    courses: list[CourseEntry]
    subjects: dict[str, Subject]       # "MTH" -> Subject


def build_subjects(courses: list[CourseEntry], names: dict[str, str]) -> dict[str, Subject]:
    """Subject index from the courses themselves: code, name, course count, aliases."""
    counts: dict[str, int] = {}
    for c in courses:
        counts[c.department] = counts.get(c.department, 0) + 1
    subjects = {}
    for code, count in counts.items():
        name = names.get(code)
        forms = []
        if name:
            forms.append(name.lower())
            if ":" in name:                         # "Apprenticeship: Roofing" also answers to "roofing"
                forms.append(name.split(":", 1)[1].strip().lower())
        aliases = {compact(a).lower() for form in forms for a in SUBJECT_ALIASES.get(form, [])}
        subjects[code] = Subject(code, name, count, tuple(forms), frozenset(aliases))
    return subjects


# Subject names from the catalog text
TOC_ENTRY = re.compile(r"^(.+?)\s*\(([A-Z]{2,5})\)\s+\d+$")           # "Mathematics (MTH) 303"
LIST_HEADING = re.compile(r"^(.*?)\(([A-Z]{2,5})\)\s*COURSES$")       # "PHYSICS (PHYS) COURSES"
CAPS_NAME = re.compile(r"^[A-Z][A-Z &,/'\-:]*[A-Z]$")
SMALL_WORDS = {"and", "of", "as", "a", "the", "for", "in", "to"}


def _title_case(caps: str) -> str:
    """'MUSIC RECORDING TECHNOLOGY' -> 'Music Recording Technology'; short words like PE stay."""
    return " ".join(w.lower() if i and w.lower() in SMALL_WORDS else w if len(w) <= 2 else w.capitalize()
                    for i, w in enumerate(caps.split()))


def subject_names(pages: list[dict], codes: set[str]) -> dict[str, str]:
    """Department code -> name, read from the catalog's table of contents and course-list headings."""
    toc, headings = {}, {}
    for page in pages:
        lines = [line.strip() for line in page["text"].split("\n")]
        for i, line in enumerate(lines):
            if (m := TOC_ENTRY.match(line)) and m[2] in codes:
                toc.setdefault(m[2], m[1])
            elif (m := LIST_HEADING.match(line)) and m[2] in codes:
                name = " ".join(m[1].split())
                prev = re.sub(rf"\s*\({m[2]}\)$", "", lines[i - 1]) if i else ""
                if CAPS_NAME.match(prev) and "COURSE LISTING" not in prev and prev != name:
                    name = f"{prev} {name}".strip()          # heading wrapped onto two lines
                if CAPS_NAME.match(name):
                    headings.setdefault(m[2], _title_case(name))
    return {code: name for code in codes if (name := toc.get(code) or headings.get(code))}


def _range(r: dict | None) -> str | None:
    if not r:
        return None
    lo, hi = f"{r['min']:g}", f"{r['max']:g}"
    return lo if lo == hi else f"{lo}-{hi}"


@lru_cache(maxsize=None)
def catalog_rows(institution_id: str) -> tuple[dict, ...]:
    """Raw course records of a college's catalog file (empty when it has none). Read once."""
    cfg = CATALOGS.get(institution_id)
    if cfg is None:
        return ()
    return tuple(json.loads((CATALOG_DIR / cfg["courses"]).read_text(encoding="utf-8"))["courses"])


@lru_cache(maxsize=None)
def load_catalog(college: str) -> Catalog | None:
    """The college's catalog, or None when it has no catalog file. Read once, then cached."""
    cfg = CATALOGS.get(REGISTRY.resolve(college))
    if cfg is None:
        return None
    rows = catalog_rows(REGISTRY.resolve(college))
    pages = json.loads((CATALOG_DIR / cfg["pages"]).read_text(encoding="utf-8"))["pages"]
    names = subject_names(pages, {c["department"] for c in rows})
    courses = []
    for c in rows:
        units, hours = _range(c.get("units")), _range(c.get("noncredit_hours"))
        courses.append(CourseEntry(
            code=c["id"], title=c["title"], subject=names.get(c["department"]), sources=(cfg["label"],),
            units=f"{units} units" if units else f"{hours} hours (noncredit)" if hours else None))
    return Catalog(cfg["name"], courses, build_subjects(courses, names))


def courses_for(college: str, pathway_catalog: dict | None) -> Catalog | None:
    """All searchable courses for a college (catalog + pathway), or None when neither exists."""
    catalog = load_catalog(college)
    if catalog is None and not pathway_catalog:
        return None
    catalog = catalog or Catalog(college.strip(), [], {})
    if not pathway_catalog:
        return catalog

    names = {code: s.name for code, s in catalog.subjects.items() if s.name}
    courses = list(catalog.courses)
    position = {c.code: i for i, c in enumerate(courses)}
    for code, course in pathway_catalog.items():
        if code in position:          # in both: keep the catalog title, note that the planner knows it
            old = courses[position[code]]
            courses[position[code]] = CourseEntry(code=old.code, title=old.title, units=old.units,
                                                  subject=old.subject, sources=old.sources + (PATHWAY_LABEL,))
        else:
            courses.append(CourseEntry(code=code, title=course.title, units=f"{course.units:g} units",
                                       subject=names.get(code.partition(" ")[0].upper()),
                                       sources=(PATHWAY_LABEL,)))
    return Catalog(catalog.name, courses, build_subjects(courses, names))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def parse_query(query: str) -> tuple[str, str | None]:
    """Split into subject letters and an optional course number:
    'math 1' -> ('math', '1')   'mth1' -> ('mth', '1')   'engl c1000' -> ('engl', 'C1000')
    'computer science' -> ('computer science', None)   '1' -> ('', '1')   'c1000' -> ('', 'C1000')"""
    tokens = _words(query)
    if not tokens or not re.search(r"\d", tokens[-1]):
        return " ".join(tokens), None
    *head, last = tokens
    if re.fullmatch(r"[a-z]?\d[a-z0-9]*", last):
        return " ".join(head), last.upper()
    if m := re.fullmatch(r"([a-z]+)(\d[a-z0-9]*)", last):
        return " ".join(head + [m[1]]), m[2].upper()
    return "", None


def resolve_subjects(catalog: Catalog, letters: str) -> list[tuple[int, Subject]]:
    """Level 1: subjects the letters refer to, best first."""
    found = [(tier, s) for s in catalog.subjects.values() if (tier := s.match_tier(letters)) is not None]
    best = min((tier for tier, _ in found), default=None)
    # A clear code/name/alias hit (tiers 1-3) hides weaker guesses: "math" -> Mathematics only
    if best is not None and best <= 3:
        found = [(tier, s) for tier, s in found if tier <= 3]
    # Same tier: shorter name first ("Computer Science" before "Computer Application Systems"),
    # then the bigger department (PSY, 13 courses, before PSYC, 1 course)
    found.sort(key=lambda f: (f[0], len(f[1].name or f[1].code), -f[1].count, f[1].name or f[1].code))
    return found


def search(catalog: Catalog, query: str, limit: int) -> dict:
    """{total, subjects: [(tier, Subject)], results: [(CourseEntry, how)]} grouped by subject, best first.
    how: 'code' (exact course code), 'subject', 'course code' or 'title'."""
    q = compact(query)
    if not q:
        return {"total": 0, "subjects": [], "results": []}
    letters, number = parse_query(query)
    subjects = resolve_subjects(catalog, letters) if letters else []
    rank = {s.code: i for i, (_, s) in enumerate(subjects)}
    # A clear subject (code, name or alias) is the answer: skip other subjects' code/title matches,
    # so "phys" lists Physics, not every course with "Physical" in its title
    clear = bool(subjects) and subjects[0][0] <= 3
    words = _words(query)

    scored = []
    for c in catalog.courses:
        if c.department in rank and (number is None or c.number.startswith(number)):
            # Level-1 subjects first. "mth 1" and "math 1" both name MTH 1 exactly: it leads its subject
            score = (1, rank[c.department], c.number != number, c.number_key())
            how = "code" if c.number == number else "subject"
        elif clear:
            continue
        elif c.key == q:                                     # exact course code
            score, how = (2, c.number_key()), "code"
        elif c.key.startswith(q) or c.number.startswith(q):   # "C1000" -> ENGL C1000 (never "DANC SAL1" for "cs")
            score = (3, not c.key.startswith(q), c.number != q, c.department, c.number_key())
            how = "course code"
        elif _prefix_match(words, c.title_words):            # lowest: title words
            score, how = (4, not c.title_words[0].startswith(words[0]), c.department, c.number_key()), "title"
        else:
            continue
        scored.append((score, c, how))
    scored.sort(key=lambda s: s[0])

    # Several subjects matched: give each a fair share of the list instead of letting the first fill it
    cap = limit if len(subjects) <= 1 else max(MULTI_SUBJECT_MIN, limit // len(subjects))
    shown, per_subject = [], {}
    for score, c, how in scored:
        if len(shown) == limit:
            break
        if how == "subject":
            per_subject[c.department] = per_subject.get(c.department, 0) + 1
            if per_subject[c.department] > cap:
                continue
        shown.append((c, how))

    # Group by subject for display, keeping the best group first and relevance order inside groups
    group_order: dict[str, int] = {}
    for c, _ in shown:
        group_order.setdefault(c.department, len(group_order))
    shown.sort(key=lambda r: group_order[r[0].department])
    # Report only subjects that actually contributed courses ("c1000" names no subject)
    listed = {c.department for c, how in shown if how in ("subject", "code")}
    return {"total": len(scored), "subjects": [(t, s) for t, s in subjects if s.code in listed],
            "results": shown}


def search_in_subject(catalog: Catalog, code: str, query: str, limit: int) -> dict:
    """Search one subject only (the category filter). Same result shape as search().
    ''         -> every course in the subject, in course-number order
    '1', '2a'  -> course numbers starting with it, the exact number first (MTH 1, MTH 15 ...)
    'calculus' -> its courses with a title word starting with 'calculus'
    'math 1'   -> letters that name the subject itself are ignored, so this equals '1'"""
    subject = catalog.subjects[code]
    letters, number = parse_query(query)
    if letters and subject.match_tier(letters) is not None and subject.match_tier(letters) <= 5:
        letters = ""                                          # "math" / "mth" inside Mathematics
    words = letters.split()
    scored = []
    for c in catalog.courses:
        if c.department != code or (number and not c.number.startswith(number)):
            continue
        if words and not _prefix_match(words, c.title_words):
            continue
        exact = number is not None and c.number == number
        title_first = bool(words) and c.title_words[0].startswith(words[0])
        scored.append(((not exact, not title_first, c.number_key()), c,
                       "code" if exact else "title" if words else "subject"))
    scored.sort(key=lambda s: s[0])
    return {"total": len(scored), "subjects": [], "results": [(c, how) for _, c, how in scored[:limit]]}
