"""
Transfer-pathway data:  data file -> loader -> Pathway -> PathwayStore -> /generate-sep -> sep_engine.

DISCOVERY. PathwayStore.load_directory() reads every *.json directly inside the data folder
(ap_engine/data/) and recognizes each file by its SHAPE, never by its name:
  ASSIST agreement       {"agreement", "institutions", "courses", "requirements", ...}  -> one Pathway
  prerequisite registry  {"institution_id", "semantics", "courses": {id: record}}        -> prerequisites
  campus list            {"campuses": [{"id", "name"}]}                                   -> institution ids
Anything else (AP charts, templates) is ignored. To add a pathway, drop its ASSIST JSON into the
folder and restart or POST /pathways/reload. GET /pathways lists what loaded and why anything didn't.

ASSIST -> ENGINE. The requirement tree must be an AND of requirements, each articulated as
  COURSE            -> required course           (CSUEB CS 201 <- CSCI 20)
  AND of COURSEs    -> required courses          (all of them)
  OR of COURSEs     -> RequirementGroup choose=1 (CSUEB CS 101 <- CSCI 15 OR CSCI 19A: take ONE)
  no_course_articulated -> left out: nothing to take at the college, so it is neither planned nor listed
Choices between whole groups or bundles ("Option A or B") and minimum-unit electives can't be
expressed by the engine, so such a file is reported as unsupported rather than planned wrongly.
Cross-listed codes (MTH 8 = CSCI 28) become Course.aliases of ONE course.

PREREQUISITES come from the sending college's registry (chabot_prerequisites.json), overlaid by
records embedded in the agreement file. The registry is optional: without it courses are planned
without prerequisite edges and flagged, and the pathway is still available. Record -> engine:
  COURSE before                       -> Course.prereqs (an earlier term)
  COURSE before_or_concurrent/same_term -> Course.coreqs (same term or earlier)
  AND                                 -> all of its parts
  OR   never flattened into AND. Cross-listed alternatives are one course; otherwise the alternative
       the pathway already requires is used (ENGR 36 needs PHYS 7A OR PHYS 4A; the pathway requires
       PHYS 7A), else nothing is enforced and the course gets an advisory warning
  CONDITION, unresearched             -> advisory warning only (placement, Math Course Selection ...)

MATCHING (PathwayStore.find): institutions by canonical id (institutions.py); majors by name with
the degree as a separate field ("Computer Science, B.S." = "Computer Science" + "B.S."); the GE
pattern only when the data has one (ASSIST agreements have none, so they fit every GE choice, and
ge_data.py plans the chosen pathway's GE); the academic year of the start term (Fall 2026 ->
2026-2027), else the nearest loaded year, with a warning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import course_search
from institutions import REGISTRY, STOP_WORDS, InstitutionRegistry, name_variants, normalize
from sep_engine.models import Course, GroupCategory, RequirementGroup, TransferAgreement, index_catalog


# =====================================================================
# Majors, degrees and academic years
# =====================================================================
_DEGREE = re.compile(r"[\s,(\-]+(bachelor of science|bachelor of arts|b\.?\s?f\.?\s?a\.?|b\.?\s?s\.?|b\.?\s?a\.?"
                     r"|a\.?\s?s\.?\s?-\s?t\.?|a\.?\s?a\.?\s?-\s?t\.?)\s*\)?$", re.IGNORECASE)
_DEGREE_KEYS = {"bachelorofscience": "bs", "bachelorofarts": "ba"}


def split_major(text: str) -> tuple[str, str | None]:
    """'Computer Science, B.S.' -> ('Computer Science', 'B.S.');  'Computer Science' -> ('Computer Science', None)."""
    text = text.strip()
    m = _DEGREE.search(text)
    return (text[:m.start()].strip(), m[1]) if m and m.start() else (text, None)


def degree_key(degree: str) -> str:
    """'B.S.', 'BS', 'Bachelor of Science' -> 'bs'."""
    letters = re.sub(r"[^a-z]", "", degree.casefold())
    return _DEGREE_KEYS.get(letters, letters)


def academic_year_of(term: str | None) -> str | None:
    """The agreement year a term belongs to: 'Fall 2026' -> '2026-2027'; 'Spring 2027', 'Summer 2027' -> '2026-2027'."""
    m = re.fullmatch(r"\s*(fall|spring|summer)\s+(\d{4})\s*", term or "", re.IGNORECASE)
    if not m:
        return None
    start = int(m[2]) if m[1].lower() == "fall" else int(m[2]) - 1
    return f"{start}-{start + 1}"


def _year_distance(have: str | None, wanted: str | None) -> tuple[int, int]:
    """Exact year first, then the latest earlier agreement, then the nearest later one."""
    if not have or not wanted:
        return (1, 0)
    a, w = int(have[:4]), int(wanted[:4])
    return (0, 0) if a == w else (1, w - a) if a < w else (2, a - w)


# =====================================================================
# Pathways and the store
# =====================================================================
@dataclass
class Pathway:
    college: str                      # display names (the official ones for ASSIST data)
    university: str
    college_ref: str                  # canonical id, or a name the registry resolves
    university_ref: str
    major: str                        # "Computer Science"
    degree: str | None                # "B.S."
    ge_pattern: str | None            # None: the data has no GE requirements, so it fits every GE choice
    academic_year: str | None         # "2026-2027"
    agreement: TransferAgreement
    catalog: dict[str, Course]
    source: str = "mock"              # "assist" or "mock"
    file: str | None = None           # data file it came from
    articulation: dict | None = None  # ASSIST summary returned with every plan (scope, requirement table)
    articulated: frozenset[str] = frozenset()               # sending-college codes the agreement names
    advisories: dict[str, str] = field(default_factory=dict)  # course -> prerequisite the planner can't enforce
    notes: list[str] = field(default_factory=list)            # warnings returned with every plan

    def warnings_for(self, result: dict) -> list[str]:
        """Pathway warnings that apply to the plans in a generate_sep() result (major prep only: GE courses
        come from the GE data and are explained in each plan's `ge` block)."""
        planned = {c["code"] for plan in result["plans"] for s in plan["semesters"] for c in s["courses"]
                   if c["code"] in self.catalog and not c["is_ge"]}
        out = list(self.notes) + [self.advisories[c] for c in sorted(planned) if c in self.advisories]
        # AP credit arrives as waived courses, so a course planned here has no AP waiver: nothing to ask the student
        support = sorted(c for c in planned if self.articulated and c not in self.articulated)
        if support:
            def needed_by(code: str) -> str:
                return ", ".join(sorted(c for c in planned if code in (*self.catalog[c].prereqs, *self.catalog[c].coreqs)))
            out.append("Planned only because a course in this pathway requires them (they are not articulated "
                       "themselves): " + "; ".join(f"{s} (for {needed_by(s)})" for s in support) + ".")
        return out


@dataclass
class Unsupported:
    """An agreement that was found but can't be planned, and why."""
    college_ref: str
    university_ref: str
    college: str
    university: str
    major: str
    degree: str | None
    academic_year: str | None
    file: str
    reason: str

    @property
    def message(self) -> str:
        degree = f", {self.degree}" if self.degree else ""
        return (f"Articulation data for {self.college} -> {self.university}, {self.major}{degree} "
                f"({self.academic_year}) is loaded from {self.file}, but the planner can't use it yet: {self.reason}.")


class UnsupportedAgreement(Exception):
    def __init__(self, record: Unsupported) -> None:
        super().__init__(record.reason)
        self.record = record


@dataclass
class Match:
    pathway: Pathway
    academic_year: str | None          # the start term's academic year
    warnings: list[str]


class PathwayStore:
    def __init__(self, registry: InstitutionRegistry = REGISTRY,
                 ge_key: Callable[[str], str] | None = None) -> None:
        self.registry = registry
        self.ge_key = ge_key or (lambda text: text.strip().casefold())   # GE pattern names: "CAL-GETC" = "cal_getc"
        self.pathways: list[Pathway] = []
        self.unsupported: list[Unsupported] = []
        self.skipped: list[dict[str, str]] = []     # files that look like agreements but could not be read

    # --- adding ---------------------------------------------------------------
    def add(self, catalog: list[Course], agreements: list[TransferAgreement], source: str = "mock") -> None:
        """Register ready-made engine data (the dev mock injector)."""
        for ag in agreements:
            major, degree = split_major(ag.degree or ag.major)
            self.pathways.append(Pathway(
                college=ag.college, university=ag.university, college_ref=ag.college, university_ref=ag.university,
                major=ag.major, degree=degree, ge_pattern=ag.ge_pattern, academic_year=None, agreement=ag,
                catalog=index_catalog(catalog), source=source))

    def remove_source(self, source: str) -> None:
        self.pathways = [p for p in self.pathways if p.source != source]

    def clear(self) -> None:
        self.pathways, self.unsupported, self.skipped = [], [], []

    def load_directory(self, *dirs: Path) -> None:
        """(Re)load every data file in `dirs`; injected (mock) pathways are kept."""
        agreements: list[tuple[str, dict]] = []
        registries: dict[str, dict] = {}
        self.unsupported, self.skipped = [], []
        for folder in dirs:
            for path in sorted(Path(folder).glob("*.json")):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    self.skipped.append({"file": path.name, "reason": f"not readable JSON ({e})"})
                    continue
                kind = _kind(doc)
                if kind == "campuses":
                    for campus in doc["campuses"]:
                        self.registry.register(campus["id"], campus.get("name"))
                elif kind == "prerequisites":
                    registries.setdefault(doc["institution_id"], doc)
                elif kind == "assist":
                    agreements.append((path.name, doc))
        for _, doc in agreements:                   # every institution is known before any matching
            for inst in doc.get("institutions") or []:
                self.registry.register(inst["id"], inst.get("name"))

        loaded: dict[str, str] = {}
        new: list[Pathway] = []
        for name, doc in agreements:
            dataset = doc.get("dataset_id") or name
            if dataset in loaded:
                self.skipped.append({"file": name, "reason": f"same dataset_id as {loaded[dataset]} ({dataset}), "
                                                             "which is already loaded"})
                continue
            loaded[dataset] = name
            try:
                sending = doc["agreement"]["from_institution_id"]
                new.append(build_pathway(doc, name, registries.get(sending), self.registry))
            except UnsupportedAgreement as e:
                self.unsupported.append(e.record)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                self.skipped.append({"file": name, "reason": f"not a usable ASSIST agreement ({type(e).__name__}: {e})"})
        self.pathways = [p for p in self.pathways if p.file is None] + new

    # --- lookup ---------------------------------------------------------------
    def _same(self, pathway, college: str, university: str, major: str) -> bool:
        return (self.registry.same(pathway.college_ref, college)
                and self.registry.same(pathway.university_ref, university)
                and normalize(pathway.major) == normalize(major))

    def find(self, college: str, university: str, major: str, ge: str, start_term: str | None = None) -> Match | None:
        name, degree = split_major(major)
        wanted = academic_year_of(start_term)
        found = [p for p in self.pathways
                 if self._same(p, college, university, name)
                 and (degree is None or p.degree is None or degree_key(p.degree) == degree_key(degree))
                 and (p.ge_pattern is None or self.ge_key(p.ge_pattern) == self.ge_key(ge))]
        if not found:
            return None
        best = min(found, key=lambda p: (p.ge_pattern is None, _year_distance(p.academic_year, wanted)))
        warnings = []
        if wanted and best.academic_year and best.academic_year != wanted:
            warnings.append(f"No {wanted} agreement (the year {start_term} falls in) is loaded for this pathway, so "
                            f"the {best.academic_year} agreement is used. Articulation can change from year to year.")
        others = sorted({p.degree for p in found if p.degree and p.degree != best.degree})
        if others:
            warnings.append(f"Planning the {best.degree} degree; also available: {', '.join(others)}. "
                            f"Add the degree to the major (e.g. \"{name}, {others[0]}\") to choose it.")
        return Match(best, wanted, warnings)

    def find_unsupported(self, college: str, university: str, major: str) -> Unsupported | None:
        name, _ = split_major(major)
        return next((u for u in self.unsupported if self._same(u, college, university, name)), None)

    def catalog_for(self, college: str) -> dict[str, Course] | None:
        """Every course the loaded pathways know for a college (first definition of a code wins)."""
        merged: dict[str, Course] = {}
        for p in self.pathways:
            if self.registry.same(p.college_ref, college):
                for code, course in p.catalog.items():
                    merged.setdefault(code, course)
        return merged or None

    def listing(self) -> list[dict]:
        """GET /pathways: what is available, and what was found but can't be planned."""
        rows = [{"status": "available", "college": p.college, "college_id": self.registry.resolve(p.college_ref),
                 "university": p.university, "university_id": self.registry.resolve(p.university_ref),
                 "major": p.major, "degree": p.degree, "academic_year": p.academic_year,
                 "ge_pattern": p.ge_pattern, "source": p.source, "file": p.file,
                 "scope": (p.articulation or {}).get("scope"), "reason": None} for p in self.pathways]
        rows += [{"status": "unsupported", "college": u.college, "college_id": u.college_ref,
                  "university": u.university, "university_id": u.university_ref, "major": u.major,
                  "degree": u.degree, "academic_year": u.academic_year, "ge_pattern": None, "source": "assist",
                  "file": u.file, "scope": None, "reason": u.reason} for u in self.unsupported]
        return rows


def _kind(doc: object) -> str | None:
    if not isinstance(doc, dict):
        return None
    if {"agreement", "institutions", "courses", "requirements"} <= doc.keys():
        return "assist"
    if isinstance(doc.get("institution_id"), str) and isinstance(doc.get("courses"), dict) and "semantics" in doc:
        return "prerequisites"
    if isinstance(doc.get("campuses"), list):
        return "campuses"
    return None


# =====================================================================
# ASSIST agreement -> Pathway
# =====================================================================
def display_title(title: str) -> str:
    """'CALCULUS II' -> 'Calculus II'; mixed-case titles are kept."""
    if not title.isupper():
        return title
    return " ".join(w if re.fullmatch(r"[IVX]+", w) else w.lower() if i and w.lower() in STOP_WORDS
                    else w.capitalize() for i, w in enumerate(title.split()))


def short_name(name: str) -> str:
    """'California State University, East Bay' -> 'CSUEB'; no acronym -> the name itself."""
    acronyms = sorted((v for v in name_variants(name) if " " not in v and v != normalize(name)), key=len)
    return acronyms[0].upper() if acronyms else name


def scope_label(scope: str) -> str:
    """'lower_division_core' -> 'Lower-division core'."""
    text = scope.replace("_", " ").replace("lower division", "lower-division").replace("upper division", "upper-division")
    return text[:1].upper() + text[1:]


def build_pathway(doc: dict, file: str, prerequisites: dict | None, registry: InstitutionRegistry,
                  catalog_rows: Callable[[str], Iterable[dict]] = course_search.catalog_rows) -> Pathway:
    ag = doc["agreement"]
    institutions = {i["id"]: i for i in doc["institutions"]}
    sending, receiving = ag["from_institution_id"], ag["to_institution_id"]
    college = institutions.get(sending, {}).get("name") or sending
    university = institutions.get(receiving, {}).get("name") or receiving
    major, degree, year = ag["major"], ag.get("degree"), ag.get("academic_year")

    def unsupported(reason: str) -> UnsupportedAgreement:
        return UnsupportedAgreement(Unsupported(sending, receiving, college, university, major, degree, year, file, reason))

    courses = {c["id"]: c for c in doc["courses"]}
    requirements = {r["id"]: r for r in doc["requirements"]}
    groups = {g["id"]: g for g in doc.get("requirement_groups") or []}

    # --- course codes, cross-listings -------------------------------------------------
    records = {**(prerequisites or {}).get("courses", {}), **((doc.get("prerequisite_data") or {}).get("records") or {})}
    external = {e["course_id"]: e for e in (prerequisites or {}).get("external_references", [])}
    rows: dict[str, dict] = {}
    for row in sorted(catalog_rows(sending), key=lambda r: r.get("type") != "credit"):   # credit records first
        rows.setdefault(course_search.compact(row["id"]), row)

    def code_of(cid: str) -> str:
        local = cid.rpartition(":")[2]
        if cid in courses:
            return courses[cid]["code"]
        if records.get(cid, {}).get("code"):
            return records[cid]["code"]
        if local in rows:
            return rows[local]["id"]
        m = re.fullmatch(r"([A-Za-z]+)(\d.*)", local)
        return f"{m[1]} {m[2]}" if m else local

    canonical: dict[str, str] = {}                      # cross-listed course id -> the one id used for it
    aliases: dict[str, list[str]] = {}                  # that id -> its other codes

    def link(other: str, head: str, code: str) -> None:
        if other != head and other not in canonical:
            canonical[other] = head
            aliases.setdefault(head, []).append(code)

    for c in doc["courses"]:
        if c.get("institution_id") == sending:
            for code in c.get("cross_listed_codes") or []:          # "MTH 8 is the same as CSCI 28"
                link(f"{sending}:{course_search.compact(code)}", c["id"], code)
    for group in (prerequisites or {}).get("aliases", []):          # ENGR 25 = MTH 25 = PHYS 25
        head = next((c for c in group if c in courses), group[0])  # prefer the code the agreement uses
        for other in group:
            link(other, head, code_of(other))
    canon = lambda cid: canonical.get(cid, cid)        # noqa: E731

    # --- requirement tree: an AND of requirements --------------------------------------
    def walk(gid: str) -> Iterable[str]:
        g = groups[gid]
        op = g.get("operator", "AND")
        if op != "AND":
            how = f" (\"{g['source_instruction']}\")" if g.get("source_instruction") else ""
            raise unsupported(f"\"{g.get('title', gid)}\" " + (f"requires a minimum number of units/courses{how}"
                              if op == "MINIMUM" else f"asks to choose between groups of courses{how}"))
        yield from g.get("requirement_ids") or []
        for sub in g.get("group_ids") or []:
            yield from walk(sub)

    nested = {sub for g in groups.values() for sub in g.get("group_ids") or []}
    roots = [ag["root_requirement_group_id"]] if ag.get("root_requirement_group_id") else \
        [gid for gid in groups if gid not in nested]
    order = list(dict.fromkeys(rid for root in roots for rid in walk(root))) if groups else list(requirements)

    # --- articulation -> required courses + choose-one groups ---------------------------
    uni_short = short_name(university)

    def bundle(expr: dict) -> list[str] | None:
        if expr.get("operator") == "COURSE":
            return [canon(expr["course_id"])]
        if expr.get("operator") == "AND" and all(o.get("operator") == "COURSE" for o in expr["operands"]):
            return [canon(o["course_id"]) for o in expr["operands"]]
        return None

    def course_info(cid: str) -> dict:
        c = courses.get(cid, {})
        return {"code": c.get("code") or code_of(cid), "title": display_title(c.get("title") or ""),
                "units": c.get("units")}

    required: list[str] = []
    choice_groups: list[tuple[str, str, list[str]]] = []          # (requirement id, label, options)
    articulates_to: dict[str, list[str]] = {}
    table: list[dict] = []
    for rid in order:
        r = requirements[rid]
        status = r.get("articulation_status")
        if status == "no_course_articulated":
            continue                                    # nothing to take at the college: left out entirely
        if status != "articulated":
            raise unsupported(f"requirement {rid} has articulation status {status!r}")
        target = r.get("target_requirement") or {"operator": "COURSE", "course_id": r["target_course_id"]}
        target_info = [course_info(t) for t in bundle(target) or []]
        label = (f"{uni_short} " + " + ".join(t["code"] for t in target_info) + " · "
                 + " + ".join(t["title"] for t in target_info if t["title"]))
        expr = r["source_requirement"]
        options = [bundle(expr)] if bundle(expr) else \
            [bundle(o) for o in expr.get("operands", [])] if expr.get("operator") == "OR" else [None]
        if None in options:
            raise unsupported(f"the courses for {label} use a {expr.get('operator')} combination the planner can't read")
        options = [list(o) for o in dict.fromkeys(tuple(o) for o in options)]
        table.append({"id": rid, "targets": target_info, "options": [[course_info(c) for c in opt] for opt in options]})
        if len(options) == 1:
            for c in options[0]:
                required.append(c)
                articulates_to.setdefault(code_of(c), []).append(label)
        elif all(len(o) == 1 for o in options):
            choice_groups.append((rid, label, [o[0] for o in options]))
        else:
            raise unsupported(f"{label} can be met by different course bundles, which the planner can't choose between yet")
    required = list(dict.fromkeys(required))
    named = set(required) | {c for _, _, opts in choice_groups for c in opts}
    if not named:
        raise unsupported(f"it articulates no {college} course")

    # --- prerequisites -------------------------------------------------------------------
    def covered(item: dict, prefer: set[str]) -> bool:
        if item.get("type") == "COURSE":
            return canon(item["course_id"]) in prefer
        return item.get("type") == "AND" and all(covered(i, prefer) for i in item.get("items", []))

    def translate(expr: dict | None, prefer: set[str]) -> tuple[list[str], list[str], bool]:
        """(prereqs, coreqs, open): ids needed in an earlier term / by the same term; open = not fully enforced."""
        kind = (expr or {}).get("type")
        if kind == "NONE":
            return [], [], False
        if kind == "COURSE":
            timing = expr.get("timing", "before")
            if timing == "before":
                return [canon(expr["course_id"])], [], False
            if timing in ("before_or_concurrent", "same_term"):
                return [], [canon(expr["course_id"])], False
            return [], [], True
        if kind == "AND":
            pre, co, is_open = [], [], False
            for item in expr.get("items", []):
                p, c, o = translate(item, prefer)
                pre, co, is_open = pre + p, co + c, is_open or o
            return pre, co, is_open
        if kind == "OR":
            items = expr.get("items", [])
            if all(i.get("type") == "COURSE" for i in items) and len({canon(i["course_id"]) for i in items}) == 1:
                return translate(items[0], prefer)                      # cross-listed alternatives: one course
            chosen = next((i for i in items if covered(i, prefer)), None)
            return translate(chosen, prefer) if chosen else ([], [], True)
        return [], [], True                                              # CONDITION, unresolved, unknown

    def record(cid: str) -> dict | None:
        return records.get(cid) or external.get(cid)

    prefer = set(required)                  # courses every plan contains: the fixpoint of required + their requisites
    while True:
        grown = set(prefer)
        for cid in prefer:
            p, c, _ = translate((record(cid) or {}).get("requirement"), prefer)
            grown.update(p, c)
        if grown == prefer:
            break
        prefer = grown

    edges: dict[str, tuple[list[str], list[str], bool]] = {}
    queue = list(dict.fromkeys([*required, *(c for _, _, opts in choice_groups for c in opts)]))
    while queue:
        cid = queue.pop(0)
        if cid in edges:
            continue
        edges[cid] = translate((record(cid) or {}).get("requirement"), prefer)
        queue += [x for x in (*edges[cid][0], *edges[cid][1]) if x not in edges]

    def units_title(cid: str) -> tuple[float | None, str]:
        c = courses.get(cid) or {}
        row = rows.get(cid.rpartition(":")[2]) or {}
        units = c.get("units") or (row.get("units") or {}).get("max")
        return (float(units) if units else None), display_title(c.get("title") or row.get("title") or code_of(cid))

    missing = {cid for cid in edges if units_title(cid)[0] is None}
    if missing & named:
        raise unsupported("no units are known for " + ", ".join(sorted(code_of(c) for c in missing & named)))
    catalog: list[Course] = []
    advisories: dict[str, str] = {}
    for cid, (pre, co, is_open) in edges.items():
        if cid in missing:
            continue
        code = code_of(cid)
        units, title = units_title(cid)
        pre = [code_of(p) for p in dict.fromkeys(pre) if p != cid and p not in missing]
        co = [code_of(c) for c in dict.fromkeys(co) if c != cid and c not in missing and code_of(c) not in pre]
        rec = record(cid)
        dropped = sorted(code_of(x) for x in (*edges[cid][0], *edges[cid][1]) if x in missing)
        if rec is None:
            advisories[code] = f"{code}: no prerequisite research for this course is loaded, so none are enforced."
        elif is_open or rec.get("requirement") is None:
            summary = (rec.get("summary") or rec.get("note") or "requirement not fully resolved").rstrip(".")
            advisories[code] = (f"{code} prerequisite: {summary}. The planner can't check all of this "
                                "automatically; confirm it with a counselor.")
        if dropped:
            advisories[code] = (advisories.get(code, "") + f" {code} also requires {', '.join(dropped)}, which is "
                                f"not in {college}'s course data, so it isn't scheduled.").strip()
        catalog.append(Course(code=code, title=title, units=units, prereqs=pre, coreqs=co,
                              aliases=[a for a in aliases.get(cid, []) if a != code]))

    agreement = TransferAgreement(
        college=college, university=university, major=major, degree=f"{major}, {degree}" if degree else major,
        required_courses=[code_of(c) for c in required], articulates_to=articulates_to,
        requirement_groups=[RequirementGroup(id=rid, name=label, category=GroupCategory.MAJOR, choose=1,
                                             options=[code_of(c) for c in opts]) for rid, label, opts in choice_groups])

    # The agreement is major preparation only; the student's GE pathway is planned from the GE data
    # (ge_data.py), which says itself whether its rules and this college's course list are loaded.
    notes: list[str] = []
    source = doc.get("source") or {}
    articulation = {
        "source": source.get("publisher") or "ASSIST",
        "dataset_id": doc.get("dataset_id") or file,
        "document_title": source.get("document_title"),
        "file": file,
        "academic_year": year,
        "requested_academic_year": None,
        "sending": {"id": sending, "name": college},
        "receiving": {"id": receiving, "name": university, "short_name": uni_short},
        "major": major,
        "degree": degree,
        "scope": ag.get("extracted_requirement_scope") or "unspecified",
        "scope_label": scope_label(ag.get("extracted_requirement_scope") or "unspecified"),
        "complete_degree_requirements": bool(ag.get("complete_degree_requirements_in_source")),
        "program_total_units": ag.get("program_total_units"),
        "ge_included": False,
        "requirements": table,
        "notes": [f"{p['title'][:1].upper()}{p['title'][1:].lower()}" + (f" ({p['scope']})" if p.get("scope") else "") + "."
                  for p in doc.get("transfer_policies") or [] if p.get("title")]
                 + [r["action"].rstrip(".") + (f" ({r['strength']})" if r.get("strength") else "") + "."
                    for r in doc.get("recommendations") or [] if r.get("action")],
    }
    return Pathway(college=college, university=university, college_ref=sending, university_ref=receiving,
                   major=major, degree=degree, ge_pattern=None, academic_year=year, agreement=agreement,
                   catalog=index_catalog(catalog), source="assist", file=file, articulation=articulation,
                   articulated=frozenset(code_of(c) for c in named), advisories=advisories, notes=notes)
