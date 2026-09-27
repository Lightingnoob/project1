"""
Algorithm 3 — Semester scheduling as a Constraint Satisfaction Problem.

Calendar: terms run Spring -> (Summer) -> Fall -> Spring ... from the student's
start term, with real labels ("Fall 2026", "Summer 2027"). Summer only appears
when the student opts in.

Variables : each course still to take.
Domains   : term indices 0..H-1 in which the course is offered and fits the
            term's unit cap, narrowed by its prerequisite chain (it can't
            start before `depth` terms, and must leave room for its longest
            dependent chain).
Constraints:
  * prerequisite  — every in-plan prereq is in a strictly earlier term
  * co-requisite  — every in-plan coreq is in the same term or earlier
  * unit caps     — Fall/Spring <= max_units; Summer <= max_summer_units (half-length term)
  * difficulty cap (optional) — sum of difficulty scores per term <= cap
  * time conflict — no two courses in one term have overlapping meetings
  * term availability — Fall-only / Spring-only / no-summer courses stay put
  * transfer timeline — the plan's LAST term is always a Spring (transfer for
    Fall admission). If the courses would finish in a Fall, the plan is
    padded to end with the following Spring.

Summer bias: GE courses try Summer terms first; major prep tries Fall/Spring
first and, for each horizon, is only allowed into Summer at all if no
schedule of that length exists without it (a strict pass runs first with
major prep removed from Summer domains, then a relaxed pass).

Pinned courses (GE courses the student chose for a specific term) have that
term as their only value. A pin whose term is not among a horizon's terms, or
that the term can't hold, is released for that horizon: the course is placed
like any other and Schedule.released says so.

Search: depth-first backtracking. The next variable is the one with the
fewest remaining values (MRV), ties broken by topological order. Value order
applies the summer bias, then 'earliest' (pack early terms) or 'balanced'
(least-loaded term first). When a course doesn't fit, the search moves it to
the next candidate term, and if nothing works it backtracks and moves an
earlier course instead. After every placement, forward checking removes
now-impossible terms from the domains of unplaced courses and abandons the
branch as soon as any domain empties or the remaining units can't fit.

Minimising terms: Spring-ending horizons H are tried shortest first, so the
first feasible schedule is the earliest transfer (proven unless a horizon
hit the node budget).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .errors import SchedulingError
from .models import Course, StudentProfile, Term
from .toposort import chain_metrics

_EPS = 1e-9
DEFAULT_SUMMER_MAX_UNITS = 9


@dataclass(frozen=True)
class TermSlot:
    index: int
    term: Term
    year: int

    @property
    def label(self) -> str:
        return f"{self.term.value} {self.year}"

    @property
    def is_summer(self) -> bool:
        return self.term is Term.SUMMER


def parse_term_label(label: str) -> tuple[Term, int]:
    """'Fall 2026' -> (Term.FALL, 2026)."""
    m = re.fullmatch(r"\s*(fall|spring|summer)\s+(\d{4})\s*", label, re.IGNORECASE)
    if not m:
        raise ValueError(f"term must look like 'Fall 2026', got {label!r}")
    return Term(m.group(1).capitalize()), int(m.group(2))


def term_sequence(start_term: Term, start_year: int, count: int, include_summer: bool) -> list[TermSlot]:
    """The next `count` terms starting at start_term/start_year, in calendar order."""
    cycle = [Term.SPRING, Term.SUMMER, Term.FALL] if include_summer else [Term.SPRING, Term.FALL]
    if start_term not in cycle:
        raise SchedulingError("Starting in Summer requires include_summer=True.")
    i, year, out = cycle.index(start_term), start_year, []
    for n in range(count):
        out.append(TermSlot(n, cycle[i], year))
        i += 1
        if i == len(cycle):     # wrapped past Fall -> next calendar year
            i, year = 0, year + 1
    return out


@dataclass
class Schedule:
    slots: list[TermSlot]
    assignment: dict[str, int]           # course -> term index
    caps: list[float]                    # unit cap of each slot
    proven_minimal: bool                 # False if a shorter horizon ran out of search budget
    nodes_explored: int
    lower_bound: int                     # terms needed ignoring availability/conflicts/Spring-end
    major_in_summer: list[str] = field(default_factory=list)   # major prep that had to go in Summer
    released: dict[str, str] = field(default_factory=dict)     # pinned course -> the term it could not keep

    def terms(self) -> list[tuple[TermSlot, list[str]]]:
        return [(slot, [c for c, t in self.assignment.items() if t == slot.index]) for slot in self.slots]


class _BudgetExceeded(Exception):
    pass


class _Backtracker:
    def __init__(self, catalog: Mapping[str, Course], order: list[str], slots: list[TermSlot],
                 caps: list[float], max_difficulty: float | None, major: set[str],
                 balanced: bool, node_budget: int) -> None:
        in_plan = set(order)
        self.course = {c: catalog[c] for c in order}
        self.rank = {c: i for i, c in enumerate(order)}
        self.prereqs = {c: {p for p in catalog[c].prereqs if p in in_plan} for c in order}
        self.coreqs = {c: {q for q in catalog[c].coreqs if q in in_plan} for c in order}
        self.dependents: dict[str, set[str]] = {c: set() for c in order}
        self.coreq_of: dict[str, set[str]] = {c: set() for c in order}
        for c in order:
            for p in self.prereqs[c]:
                self.dependents[p].add(c)
            for q in self.coreqs[c]:
                self.coreq_of[q].add(c)

        self.slots = slots
        self.caps = caps
        self.max_difficulty = max_difficulty
        self.major = major
        self.balanced = balanced
        self.load = [0.0] * len(slots)
        self.difficulty = [0.0] * len(slots)
        self.members: list[list[str]] = [[] for _ in slots]
        self.assignment: dict[str, int] = {}
        self.nodes = 0
        self.node_budget = node_budget

    def initial_domains(self, depth: dict[str, int], height: dict[str, int],
                        major_out_of_summer: bool, pinned: Mapping[str, int] | None = None) -> dict[str, list[int]]:
        H = len(self.slots)
        domains = {}
        for c, course in self.course.items():
            banned_summer = major_out_of_summer and c in self.major
            domains[c] = [s for s in range(depth[c], H - height[c] + 1)
                          if self.slots[s].term in course.terms_offered
                          and course.units <= self.caps[s] + _EPS
                          and not (banned_summer and self.slots[s].is_summer)]
            if pinned and c in pinned and pinned[c] in domains[c]:
                domains[c] = [pinned[c]]             # the student's term; otherwise released
        return domains

    def solve(self, domains: dict[str, list[int]]) -> dict[str, int] | None:
        if any(not d for d in domains.values()):
            return None
        return dict(self.assignment) if self._search(domains) else None

    # --- constraint checks -------------------------------------------------
    def _fits(self, c: str, s: int) -> bool:
        """Unit cap, difficulty cap and time conflicts against what's already in term s."""
        course = self.course[c]
        if self.load[s] + course.units > self.caps[s] + _EPS:
            return False
        if self.max_difficulty is not None and self.difficulty[s] + course.difficulty > self.max_difficulty + _EPS:
            return False
        return not any(course.conflicts_with(self.course[o]) for o in self.members[s])

    def _consistent(self, c: str, s: int) -> bool:
        a = self.assignment
        return (self._fits(c, s)
                and all(a[p] < s for p in self.prereqs[c] if p in a)
                and all(a[d] > s for d in self.dependents[c] if d in a)
                and all(a[q] <= s for q in self.coreqs[c] if q in a)
                and all(a[d] >= s for d in self.coreq_of[c] if d in a))

    def _place(self, c: str, s: int) -> None:
        self.assignment[c] = s
        self.load[s] += self.course[c].units
        self.difficulty[s] += self.course[c].difficulty
        self.members[s].append(c)

    def _unplace(self, c: str, s: int) -> None:
        del self.assignment[c]
        self.load[s] -= self.course[c].units
        self.difficulty[s] -= self.course[c].difficulty
        self.members[s].remove(c)

    # --- search --------------------------------------------------------------
    def _value_order(self, c: str, domain: list[int]) -> list[int]:
        """GE: Summer first. Major prep: Fall/Spring first. Then earliest or least-loaded."""
        prefers_summer = c not in self.major

        def key(s: int) -> tuple:
            bias = 0 if self.slots[s].is_summer == prefers_summer else 1
            return (bias, self.load[s] / self.caps[s], s) if self.balanced else (bias, s)
        return sorted(domain, key=key)

    def _search(self, domains: dict[str, list[int]]) -> bool:
        if not domains:
            return True
        self.nodes += 1
        if self.nodes > self.node_budget:
            raise _BudgetExceeded
        c = min(domains, key=lambda x: (len(domains[x]), self.rank[x]))   # MRV
        rest = {x: d for x, d in domains.items() if x != c}
        for s in self._value_order(c, domains[c]):
            if not self._consistent(c, s):
                continue
            self._place(c, s)
            pruned = self._forward_check(c, s, rest)
            if pruned is not None and self._search(pruned):
                return True
            self._unplace(c, s)                                           # backtrack
        return False

    def _forward_check(self, c: str, s: int, domains: dict[str, list[int]]) -> dict[str, list[int]] | None:
        """Prune values made impossible by placing c in term s; None means a dead end."""
        pre, dep, co, co_of = self.prereqs[c], self.dependents[c], self.coreqs[c], self.coreq_of[c]
        pruned: dict[str, list[int]] = {}
        for x, dom in domains.items():
            keep = [v for v in dom
                    if not (x in pre and v >= s)
                    and not (x in dep and v <= s)
                    and not (x in co and v > s)
                    and not (x in co_of and v < s)
                    and (v != s or self._fits(x, s))]
            if not keep:
                return None
            pruned[x] = keep
        remaining = sum(self.course[x].units for x in pruned)
        capacity = sum(cap - load for cap, load in zip(self.caps, self.load))
        return pruned if remaining <= capacity + _EPS else None


def schedule_courses(
    catalog: Mapping[str, Course],
    order: list[str],
    profile: StudentProfile,
    max_units: float = 18,
    max_term_difficulty: float | None = None,
    *,
    max_summer_units: float = DEFAULT_SUMMER_MAX_UNITS,
    major_courses: Iterable[str] = (),
    value_order: str = "earliest",
    end_in_spring: bool = True,
    node_budget: int = 50_000,
    pinned: Mapping[str, str] | None = None,
) -> Schedule:
    """Assign topologically ordered courses to real terms, finishing (in a Spring) as early as possible.
    `pinned` maps a course to the term label it must be taken in ('Summer 2027')."""
    summer_cap = min(max_units, max_summer_units)
    cap_for = {Term.FALL: max_units, Term.SPRING: max_units, Term.SUMMER: summer_cap}
    cycle_terms = {Term.FALL, Term.SPRING} | ({Term.SUMMER} if profile.include_summer else set())
    major = set(major_courses) & set(order)

    for c in order:
        course = catalog[c]
        if course.units > max_units:
            raise SchedulingError(f"{c} is {course.units} units, above the {max_units}-unit term cap.")
        if max_term_difficulty is not None and course.difficulty > max_term_difficulty:
            raise SchedulingError(f"{c} alone exceeds the per-term difficulty cap of {max_term_difficulty}.")
        usable = [t for t in course.terms_offered & cycle_terms if course.units <= cap_for[t] + _EPS]
        if not usable:
            offered = ", ".join(sorted(t.value for t in course.terms_offered))
            raise SchedulingError(f"{c} is only offered in {offered}, and no such term in this plan can "
                                  f"hold its {course.units} units (Summer cap {summer_cap}, "
                                  f"summer {'on' if profile.include_summer else 'off'}).")

    if not order:
        return Schedule([], {}, [], True, 0, 0)

    def slots_for(h: int) -> list[TermSlot]:
        return term_sequence(profile.start_term, profile.start_year, h, profile.include_summer)

    depth, height = chain_metrics(order, catalog)
    total_units = sum(catalog[c].units for c in order)
    lower = max(height.values())
    while lower < profile.max_terms and sum(cap_for[s.term] for s in slots_for(lower)) < total_units - _EPS:
        lower += 1

    proven, explored = True, 0
    for horizon in range(lower, profile.max_terms + 1):
        slots = slots_for(horizon)
        if end_in_spring and slots[-1].term is not Term.SPRING:
            continue                         # transfers happen after a Spring: only Spring-ending plans
        caps = [cap_for[s.term] for s in slots]
        has_summer = any(s.is_summer for s in slots)
        label_index = {s.label: s.index for s in slots}
        pin_index = {c: label_index[t] for c, t in (pinned or {}).items() if c in set(order) and t in label_index}
        # Strict pass keeps major prep out of Summer; relax only if that finds nothing.
        for strict in ((True, False) if has_summer and major else (False,)):
            solver = _Backtracker(catalog, order, slots, caps, max_term_difficulty, major,
                                  value_order == "balanced", node_budget)
            try:
                result = solver.solve(solver.initial_domains(depth, height, major_out_of_summer=strict,
                                                             pinned=pin_index))
            except _BudgetExceeded:
                result, proven = None, False
            explored += solver.nodes
            if result is not None:
                in_summer = sorted(c for c in major if slots[result[c]].is_summer)
                released = {c: t for c, t in (pinned or {}).items() if c in result and slots[result[c]].label != t}
                return Schedule(slots, result, caps, proven, explored, lower, in_summer, released)

    raise SchedulingError(
        f"No valid schedule ending in a Spring term fits within {profile.max_terms} terms under a "
        f"{max_units}-unit cap" + (f" and a difficulty cap of {max_term_difficulty}" if max_term_difficulty else "")
        + ". Try allowing more terms, enabling summer, or relaxing the caps.")
