"""
General education for one plan: which requirements are satisfied, planned or still open, and which of
the selected college's courses can fill what is open.

Two data layers, loaded separately (ge_data.py) and never mixed:
  GEStandard    one GE pathway's REQUIREMENTS: areas, course counts, UC/CSU differences, AP rules
                (statewide: IGETC standards, Cal-GETC area rules, the UC 7-course pattern).
  CollegeGEMap  one college's COURSE ELIGIBILITY for one pathway and academic year:
                course -> the areas it is approved for (Chabot's Cal-GETC list).
A standard alone is enough to know what is required; course options also need the college's map.

Credits fill requirement slots:
  completed  courses the student entered, looked up in the map (implied prerequisites are not GE credit)
  ap         AP exams under the pathway's AP rules (exam -> areas; "or" = one of them; minimum score)
  major      major-prep courses this plan already schedules that the map lists (MTH 1 -> Cal-GETC Area 2)
  choice     courses the student picked for one requirement (bound to it, pinned to a term)
Each credit fills at most ONE requirement ("a course may be applied to only one area"), except
requirements marked shares_course: a lab (5C) listed with its 5A/5B course, and IGETC's language
requirement (6A). A requirement with a discipline or distinct-area minimum ("two courses from two
disciplines", "four courses from at least two areas") only takes credits that keep that minimum
reachable. The assignment is a small branch and bound: fill as many slots as possible, preferring
done credits (completed, AP) over planned ones (major prep before the student's choices).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .completion import _key as course_key

SYSTEMS = frozenset({"UC", "CSU"})
COMPLETED, AP, MAJOR, CHOICE = "completed", "ap", "major", "choice"
DONE_KINDS = frozenset({COMPLETED, AP})
_PRIORITY = {COMPLETED: 3, AP: 3, MAJOR: 2, CHOICE: 1}
_GAIN = 1000                                  # per filled slot; the priority only breaks ties
_SEASON_ORDER = {"spring": 0, "summer": 1, "fall": 2}


def term_ordinal(label: str | None) -> int | None:
    """'Fall 2026' -> a number that orders terms (Spring < Summer < Fall within a year)."""
    m = re.fullmatch(r"\s*(spring|summer|fall)\s+(\d{4})\s*", label or "", re.IGNORECASE)
    return int(m[2]) * 3 + _SEASON_ORDER[m[1].lower()] if m else None


# =====================================================================
# Data (built by ge_data.py from the JSON files)
# =====================================================================
@dataclass(frozen=True)
class GERequirement:
    """One requirement row: `courses` courses whose listed areas include one of `accepts`."""
    id: str                                   # "3A", "4", "5C", "E"
    label: str                                # "Area 3A · Arts"
    name: str                                 # "Arts"
    area_id: str                              # "3"
    area_name: str                            # "Arts and Humanities"
    courses: int
    accepts: frozenset[str]                   # course-list area codes that fill it: {"3A"}, {"3A", "3B"}, {"UC-H", ...}
    systems: frozenset[str] = SYSTEMS         # where it is required ("UC", "CSU")
    min_disciplines: int = 1                  # distinct subjects among its courses
    min_distinct_areas: int = 1               # distinct `accepts` areas among its courses
    shares_course: bool = False               # may use a course already counted elsewhere (lab, LOTE)
    min_units: float | None = None            # semester units per course, as the source states it
    rule: str = ""                            # the source's wording
    note: str | None = None


@dataclass(frozen=True)
class APRule:
    exam: str                                 # exam key (ge_data.ap_exam_key)
    label: str                                # as the source names the exam
    min_score: int = 3
    areas: tuple[str, ...] = ()               # every one of these (a science exam: 5B and its lab 5C)
    options: tuple[str, ...] = ()             # or ONE of these (Art History: 3A or 3B)
    note: str | None = None


@dataclass(frozen=True)
class GEStandard:
    pathway: str                              # "igetc"
    name: str                                 # "IGETC"
    dataset_id: str
    source: str
    systems: frozenset[str]                   # destinations it serves
    requirements: tuple[GERequirement, ...]
    ap_rules: tuple[APRule, ...] | None = None    # None: the standard states no AP rules of its own
    ap_source: str | None = None
    area_names: Mapping[str, str] = field(default_factory=dict)
    historical: bool = False
    notes: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()        # college course lists the area rules were published with

    def requirements_for(self, system: str) -> tuple[GERequirement, ...]:
        return tuple(r for r in self.requirements if system in r.systems)


@dataclass(frozen=True)
class GECourse:
    code: str
    title: str
    units: float
    areas: frozenset[str]                     # every area the list shows the course under
    discipline: str                           # subject code: "PSYC"
    same_as: tuple[str, ...] = ()             # cross-listed codes: the same course
    former_codes: tuple[str, ...] = ()
    prereq_any: tuple[str, ...] = ()          # catalog prerequisite: at least one of these, before (or with)
    prereq_text: str | None = None
    prereq_blocking: bool = False             # False: an alternative such as placement exists (advisory only)
    prereq_concurrent: bool = False           # "may be taken concurrently"
    first_term: str | None = None             # on the list from / through these terms (ASSIST effective dates)
    last_term: str | None = None


@dataclass
class CollegeGEMap:
    institution: str                          # canonical id
    institution_name: str
    pathway: str
    academic_year: str | None
    dataset_id: str
    source: str
    courses: dict[str, GECourse]

    def __post_init__(self) -> None:
        self._index: dict[str, str] = {}
        for code in self.courses:
            self._index.setdefault(course_key(code), code)
        for code, c in self.courses.items():
            for old in c.former_codes:
                self._index.setdefault(course_key(old), code)

    def lookup(self, code: str) -> str | None:
        return self._index.get(course_key(code))


@dataclass(frozen=True)
class GEChoice:
    """The student picked `course` for requirement `requirement`, to take in `term`."""
    requirement: str
    course: str
    term: str | None = None


@dataclass(frozen=True)
class APCredit:
    exam: str                                 # "AP Psychology"
    score: int
    areas: tuple[str, ...]
    options: tuple[str, ...]
    discipline: str


@dataclass
class GEContext:
    """Everything one request plans GE with (ge_data.GERegistry.context builds it)."""
    pathway: str
    pathway_name: str
    target_system: str                        # "UC", "CSU" or "Other"
    target_name: str
    college_name: str
    standard: GEStandard | None
    college_map: CollegeGEMap | None
    requested_year: str | None = None
    map_note: str | None = None
    ap_credits: tuple[APCredit, ...] = ()
    ap_source: str | None = None
    ap_note: str | None = None
    choices: tuple[GEChoice, ...] = ()

    @property
    def case(self) -> str:
        """not_loaded (no rules) | not_applicable (rules for other destinations) |
        requirements_only (rules, no college course list) | courses_available (both)."""
        if self.standard is None:
            return "not_loaded"
        if not self.standard.requirements_for(self.target_system):
            return "not_applicable"
        return "courses_available" if self.college_map else "requirements_only"

    @property
    def requirements(self) -> tuple[GERequirement, ...]:
        return self.standard.requirements_for(self.target_system) if self.standard else ()

    @property
    def message(self) -> str:
        name, case = self.pathway_name, self.case
        if case == "not_loaded":
            return "GE pathway data is not loaded."
        if case == "not_applicable":
            where = " and ".join(sorted(self.standard.systems))
            return (f"{name} applies to {where} transfer, so it sets no requirements for {self.target_name}. "
                    f"Choose a GE pathway that {self.target_name} accepts.")
        if case == "requirements_only":
            return (f"Showing the {name} requirements. Course selection is unavailable: no {self.college_name} "
                    f"{name} course list is loaded, so completed courses can't be matched to {name} areas yet"
                    f"{' (AP scores still count)' if self.standard.ap_rules is not None or self.ap_credits else ''}.")
        return f"{name} requirements with the courses {self.college_name} lists for each area."


# =====================================================================
# Assignment
# =====================================================================
@dataclass(eq=False)
class _Credit:
    kind: str
    identity: str                             # dedupe key: one course (with its cross-listings) or one exam
    label: str                                # "PSYC C1000", "AP Psychology"
    title: str
    areas: frozenset[str]                     # counts toward all of these (non-sharing: one of them)
    options: frozenset[str] = frozenset()     # AP "or"
    discipline: str = ""
    code: str | None = None
    units: float | None = None
    score: int | None = None
    bound: str | None = None                  # a choice counts only toward the requirement it was picked for
    choice: GEChoice | None = None

    @property
    def usable(self) -> frozenset[str]:
        return self.areas | self.options


Fill = list[tuple[_Credit, str]]              # (credit, the accepted area it counts as)


def _feasible(r: GERequirement, fills: Fill) -> bool:
    open_slots = r.courses - len(fills)
    return (open_slots >= 0
            and len({c.discipline for c, _ in fills}) + open_slots >= r.min_disciplines
            and len({a for _, a in fills}) + open_slots >= r.min_distinct_areas)


def _targets(c: _Credit, main: list[GERequirement]) -> list[tuple[str, str]]:
    out = []
    for r in main:
        if c.bound is not None and c.bound != r.id:
            continue
        usable = sorted(c.usable & r.accepts)
        if usable:
            out += [(r.id, a) for a in (usable if r.min_distinct_areas > 1 else usable[:1])]
    return out


def assign(requirements: Iterable[GERequirement], credits: Iterable[_Credit]) -> dict[str, Fill]:
    """Requirement id -> the credits counted toward it (see the module docstring)."""
    reqs, credits = list(requirements), list(credits)
    main = [r for r in reqs if not r.shares_course]
    by_id = {r.id: r for r in main}
    cands = [(c, t) for c in credits if (t := _targets(c, main))]
    cands.sort(key=lambda ct: (-_PRIORITY[ct[0].kind], len(ct[1]), ct[0].label))
    gains = [_GAIN + _PRIORITY[c.kind] for c, _ in cands]
    suffix = [0] * (len(cands) + 1)
    for i in reversed(range(len(cands))):
        suffix[i] = suffix[i + 1] + gains[i]
    capacity = sum(r.courses for r in main)
    fill: dict[str, Fill] = {r.id: [] for r in main}
    best: list = [-1, {rid: [] for rid in fill}]

    def dfs(i: int, score: int, filled: int) -> None:
        if score + min(suffix[i], (capacity - filled) * (_GAIN + max(_PRIORITY.values()))) <= best[0]:
            return
        if i == len(cands):
            best[0], best[1] = score, {rid: list(f) for rid, f in fill.items()}
            return
        c, targets = cands[i]
        for rid, area in targets:
            f = fill[rid]
            if len(f) < by_id[rid].courses and _feasible(by_id[rid], f + [(c, area)]):
                f.append((c, area))
                dfs(i + 1, score + gains[i], filled + 1)
                f.pop()
        dfs(i + 1, score, filled)

    dfs(0, 0, 0)
    out: dict[str, Fill] = best[1]
    counted = {c for f in out.values() for c, _ in f}
    for r in reqs:                            # labs / LOTE: any eligible credit in use, done ones first
        if not r.shares_course:
            continue
        # a choice counts here if it was picked for this requirement or is counted toward its own one
        eligible = [c for c in credits if (c.bound is None or c.bound == r.id or c in counted)
                    and c.usable & r.accepts]
        eligible.sort(key=lambda c: (-_PRIORITY[c.kind], c.bound == r.id, c.label))
        out[r.id] = [(c, min(c.usable & r.accepts)) for c in eligible[:r.courses]]
    return out


# =====================================================================
# Planner: one per request; plan() once per plan (A, B)
# =====================================================================
@dataclass
class GEPlanPart:
    """GE for one plan before scheduling: what the student's choices add, and the slot assignment."""
    fills: dict[str, Fill]
    courses: dict[str, GECourse]              # choices to schedule: code -> course
    pins: dict[str, str]                      # code -> term the student chose
    requirement_of: dict[str, str]            # choice code -> requirement id
    unused: list[tuple[GEChoice, str]]        # valid choices this plan doesn't need, with the reason


class GEPlanner:
    """Validates the student's choices once, then evaluates GE for each plan."""

    def __init__(self, ctx: GEContext, *, completed: Iterable[str], satisfied: Iterable[str],
                 aliases: Mapping[str, Iterable[str]] | None = None, start_term: str | None = None,
                 include_summer: bool = False, summer_cap: float = 9) -> None:
        self.ctx = ctx
        self.map = ctx.college_map if ctx.case == "courses_available" else None
        self.reqs = {r.id: r for r in ctx.requirements} if ctx.case in ("courses_available", "requirements_only") else {}
        self.start_term, self.include_summer, self.summer_cap = start_term, include_summer, summer_cap
        self._identity = self._identities(aliases or {})
        entered = list(dict.fromkeys(completed))
        self.completed = [code for code in (self.lookup(c) for c in entered) if code]
        self.done = {self._id(c) for c in [*satisfied, *entered]}      # never offered or planned again
        self.done_credits = [self._course_credit(COMPLETED, code) for code in dict.fromkeys(self.completed)]
        self.done_credits = list({c.identity: c for c in self.done_credits}.values())
        self.done_credits += [_Credit(AP, f"ap:{a.exam}", a.exam, "AP exam", frozenset(a.areas), frozenset(a.options),
                                      a.discipline, score=a.score) for a in ctx.ap_credits]
        self.referenced: dict[str, GECourse] = {}
        self.choices, self.dropped = self._validate(ctx.choices)

    # --- identities: a course, its former codes and its cross-listings are one course ------------
    def _identities(self, aliases: Mapping[str, Iterable[str]]) -> dict[str, str]:
        parent: dict[str, str] = {}

        def find(k: str) -> str:
            while parent.setdefault(k, k) != k:
                k = parent[k]
            return k

        def union(a: str, b: str) -> None:
            parent[find(course_key(a))] = find(course_key(b))

        for code, others in aliases.items():
            for o in others:
                union(o, code)
        if self.map:
            for code, c in self.map.courses.items():
                for o in (*c.same_as, *c.former_codes):
                    union(o, code)
        return {k: find(k) for k in list(parent)}

    def _id(self, code: str) -> str:
        k = course_key(code)
        return self._identity.get(k, k)

    def lookup(self, code: str) -> str | None:
        return self.map.lookup(code) if self.map else None

    def recognizes(self, code: str) -> bool:
        return self.lookup(code) is not None

    def _course_credit(self, kind: str, code: str, choice: GEChoice | None = None) -> _Credit:
        c = self.map.courses[code]
        return _Credit(kind, self._id(code), code, c.title, c.areas, discipline=c.discipline, code=code,
                       units=c.units, bound=choice.requirement if choice else None, choice=choice)

    # --- the student's choices ------------------------------------------------------------------
    def _validate(self, choices: Iterable[GEChoice]) -> tuple[list[tuple[GEChoice, str]], list[tuple[GEChoice, str]]]:
        """(valid (choice, map code), dropped (choice, reason)). Reasons that hold for every plan."""
        ctx, valid, dropped, seen = self.ctx, [], [], set()
        summer_units: dict[str, float] = defaultdict(float)
        start = term_ordinal(self.start_term)
        for ch in choices:
            r = self.reqs.get(ch.requirement)
            code = self.lookup(ch.course)
            c = self.map.courses[code] if code else None
            when = term_ordinal(ch.term)
            summer = bool(ch.term) and ch.term.strip().lower().startswith("summer")
            if not self.map:
                reason = ctx.message
            elif r is None:
                reason = f"{ch.requirement} is not a {ctx.pathway_name} requirement for {ctx.target_system} transfer"
            elif c is None:
                reason = f"{ch.course} is not on {ctx.college_name}'s {ctx.pathway_name} course list"
            elif not c.areas & r.accepts:
                reason = f"{code} does not count toward {r.label}"
            elif self._id(code) in self.done:
                reason = f"{code} is already completed"
            elif self._id(code) in seen:
                reason = f"{code} was chosen twice"
            elif ch.term and when is None:
                reason = f"“{ch.term}” is not a term"
            elif when is not None and start is not None and when < start:
                reason = f"{ch.term} is before the plan starts ({self.start_term})"
            elif summer and not self.include_summer:
                reason = "summer classes are turned off"
            elif c.last_term and when is not None and when > term_ordinal(c.last_term):
                reason = f"{code} is on the list only through {c.last_term}"
            elif c.first_term and when is not None and when < term_ordinal(c.first_term):
                reason = f"{code} is on the list only from {c.first_term}"
            elif summer and summer_units[ch.term] + c.units > self.summer_cap + 1e-9:
                reason = (f"{ch.term} would have {summer_units[ch.term] + c.units:g} units; "
                          f"the summer limit is {self.summer_cap:g}")
            else:
                reason = None
            if reason:
                dropped.append((ch, reason))
                continue
            seen.add(self._id(code))
            if summer:
                summer_units[ch.term] += c.units
            valid.append((ch, code))
        # Not needed at all: done credits (or earlier choices) already fill the requirement
        credits = self.done_credits + [self._course_credit(CHOICE, code, ch) for ch, code in valid]
        fills = assign(self.reqs.values(), credits)
        used = {c.choice for f in fills.values() for c, _ in f if c.choice}
        kept = []
        for ch, code in valid:
            if ch in used:
                kept.append((ch, code))
                continue
            r = self.reqs[ch.requirement]
            done_only = assign([r], self.done_credits)[r.id]
            if len(done_only) >= r.courses:
                what = ", ".join(c.label for c, _ in done_only)
                dropped.append((ch, f"{r.label} is already satisfied by {what}"))
            elif len(fills.get(r.id, [])) >= r.courses:
                others = ", ".join(c.label for c, _ in fills[r.id])
                dropped.append((ch, f"{r.label} needs only {r.courses} course{'s' if r.courses > 1 else ''} "
                                    f"({others} already chosen)"))
            elif r.min_disciplines > 1 or r.min_distinct_areas > 1:
                dropped.append((ch, f"{r.label} needs courses from at least "
                                    f"{max(r.min_disciplines, r.min_distinct_areas)} different "
                                    f"{'disciplines' if r.min_disciplines > 1 else 'areas'}"))
            else:
                dropped.append((ch, f"{r.label} needs only {r.courses} course{'s' if r.courses > 1 else ''}"))
        return kept, dropped

    # --- one plan -------------------------------------------------------------------------------
    def plan(self, planned: Iterable[str]) -> GEPlanPart:
        """GE for a plan whose major prep schedules `planned` (catalog codes)."""
        planned = list(planned)
        in_plan = {self._id(c) for c in planned}
        credits = list(self.done_credits)
        for code in planned:                               # major prep that the list also approves
            listed = self.lookup(code)
            if listed and self._id(listed) not in self.done:
                credits.append(self._course_credit(MAJOR, listed))
        credits = list({c.identity: c for c in credits}.values())
        choice_credits = [self._course_credit(CHOICE, code, ch) for ch, code in self.choices
                          if self._id(code) not in in_plan]
        fills = assign(self.reqs.values(), credits + choice_credits)
        used = {c.choice for f in fills.values() for c, _ in f if c.choice}
        courses, pins, requirement_of, unused = {}, {}, {}, []
        for ch, code in self.choices:
            if ch in used:
                courses[code] = self.map.courses[code]
                requirement_of[code] = ch.requirement
                if ch.term:
                    pins[code] = ch.term
            elif self._id(code) in in_plan:
                unused.append((ch, f"{code} is already in this plan as major preparation"))
            else:
                r = self.reqs[ch.requirement]
                by = ", ".join(c.label for c, _ in fills.get(r.id, []) if not c.choice)
                unused.append((ch, f"{r.label} is covered in this plan" + (f" by {by}" if by else "")))
        return GEPlanPart(fills, courses, pins, requirement_of, unused)

    # --- after scheduling -----------------------------------------------------------------------
    def status(self, part: GEPlanPart | None, term_of: Mapping[str, str], scheduled: Iterable[str],
               released: Mapping[str, str] | None = None) -> dict:
        """The plan's GE block: every requirement with what fills it, and options for what is open."""
        ctx = self.ctx
        released = released or {}
        base = {
            "pathway": ctx.pathway, "name": ctx.pathway_name, "case": ctx.case, "message": ctx.message,
            "target_system": ctx.target_system,
            "standard": None if ctx.standard is None else {
                "dataset_id": ctx.standard.dataset_id, "source": ctx.standard.source,
                "historical": ctx.standard.historical, "derived_from": list(ctx.standard.derived_from),
                "notes": list(ctx.standard.notes)},
            "course_list": None if self.map is None else {
                "dataset_id": self.map.dataset_id, "institution": self.map.institution,
                "institution_name": self.map.institution_name, "academic_year": self.map.academic_year,
                "requested_academic_year": ctx.requested_year, "source": self.map.source, "note": ctx.map_note},
            "ap_source": ctx.ap_source, "ap_note": ctx.ap_note,
            "requirements": [], "all_satisfied": False, "choices": [],
            "summary": {"requirements": 0, "met": 0, "planned": 0, "open": 0, "courses_needed": 0, "courses_open": 0},
        }
        if ctx.case not in ("courses_available", "requirements_only"):
            return base
        fills = part.fills if part else assign(self.reqs.values(), self.done_credits)
        busy = {self._id(c) for c in scheduled} | self.done
        used = {c.identity for f in fills.values() for c, _ in f}
        rows, summary = [], base["summary"]
        for r in ctx.requirements:
            f = sorted(fills.get(r.id, []), key=lambda ca: (-_PRIORITY[ca[0].kind], ca[0].label))
            open_slots = r.courses - len(f)
            if open_slots <= 0:
                state = "satisfied" if all(c.kind in DONE_KINDS for c, _ in f) else "planned"
            else:
                state = "in_progress" if f else "open"
            options: list[str] = []
            if self.map and open_slots > 0:
                for code, course in self.map.courses.items():
                    usable = sorted(course.areas & r.accepts)
                    ident = self._id(code)
                    if not usable or ident in busy or ident in used:
                        continue
                    probe = _Credit(CHOICE, ident, code, course.title, course.areas, discipline=course.discipline)
                    if any(_feasible(r, f + [(probe, a)]) for a in usable):
                        options.append(code)
                        self.referenced[code] = course
                options.sort(key=_natural)
            summary["requirements"] += 1
            if not r.shares_course:                    # a lab / LOTE rides on another course: not an extra one
                summary["courses_needed"] += r.courses
                summary["courses_open"] += max(open_slots, 0)
            summary["met" if state == "satisfied" else "planned" if state == "planned" else "open"] += 1
            rows.append({
                "id": r.id, "label": r.label, "name": r.name, "area_id": r.area_id, "area_name": r.area_name,
                "courses_required": r.courses, "status": state, "open": max(open_slots, 0),
                "rule": r.rule, "note": r.note, "shares_course": r.shares_course,
                "min_disciplines": r.min_disciplines, "min_distinct_areas": r.min_distinct_areas,
                "min_units": r.min_units,
                "filled": [{"kind": c.kind, "code": c.code, "label": c.label, "title": c.title, "units": c.units,
                            "score": c.score, "area": area, "term": term_of.get(c.code) if c.code else None}
                           for c, area in f],
                "options": options,
                "options_note": None if self.map or open_slots <= 0 else
                f"Course selection unavailable: {ctx.college_name} {ctx.pathway_name} course list not loaded.",
            })
        base["requirements"] = rows
        base["all_satisfied"] = all(row["status"] == "satisfied" for row in rows)
        if base["all_satisfied"]:
            base["message"] = "GE pathway requirements satisfied."
        elif rows and not summary["open"]:
            base["message"] = f"Every {ctx.pathway_name} requirement is satisfied or planned; nothing is left to choose."
        for ch, code in self.choices:
            placed = term_of.get(code)
            if part and code in part.courses:
                note = released.get(code)
                base["choices"].append({"requirement": ch.requirement, "course": code, "term": ch.term,
                                        "status": "planned", "placed_term": placed, "note": note})
        for ch, reason in (part.unused if part else []):
            base["choices"].append({"requirement": ch.requirement, "course": self.lookup(ch.course) or ch.course,
                                    "term": ch.term, "status": "not_needed", "placed_term": None, "note": reason})
        return base

    def prerequisite_warnings(self, part: GEPlanPart, term_of: Mapping[str, str], plan_name: str) -> list[str]:
        """A chosen course whose catalog prerequisite is neither completed nor planned before it."""
        out = []
        earlier = defaultdict(set)                      # term ordinal -> course keys by then
        for code, term in term_of.items():
            earlier[term_ordinal(term)].add(course_key(code))
        done = {course_key(c) for c in self.done} | set(self.done)
        for code, course in part.courses.items():
            if not (course.prereq_blocking and course.prereq_any) or code not in term_of:
                continue
            when = term_ordinal(term_of[code])
            before = set(done)
            for t, keys in earlier.items():
                if t is not None and (t < when or (course.prereq_concurrent and t == when)):
                    before |= keys
            if not any(course_key(p) in before or self._id(p) in before for p in course.prereq_any):
                out.append(f"{plan_name}: {code} ({term_of[code]}) lists “{course.prereq_text}” as its "
                           f"prerequisite, which is not completed or planned before it.")
        return out


def _natural(code: str) -> tuple:
    """'MTH 2' < 'MTH 10' < 'MTH 10A'."""
    subject, _, number = code.partition(" ")
    m = re.match(r"([A-Z]?)(\d+)(.*)", number)
    return (subject, m[1], int(m[2]), m[3]) if m else (subject, number, 0, "")
