"""
Step 0 — which courses are already done (runs before course selection, sorting and scheduling).

A completed course proves its requisites were met, so

    satisfied = completed + every recursive prerequisite/co-requisite of a completed course

    e.g.  CS 1 -> CS 2 -> CS 20 (arrows point from a prerequisite to the course that needs it)
          completed = {CS 20}  ->  satisfied = {CS 20, CS 2, CS 1}

Every later step (select_pathway, topological_sort, schedule_courses) receives `satisfied`,
so those courses are never planned and never add terms, units or ordering constraints.
The student's own list is not changed: prerequisites found this way are reported
separately as `inferred`.

Prerequisites in the catalog are AND-only (Course.prereqs: "ALL must be completed"), so
every listed prerequisite of a completed course is satisfied. Co-requisites count too: they
had to be taken in the same term or earlier.

Waived courses (AP credit / credit by exam) count as done themselves but do NOT imply their
prerequisites: the AP chart gives credit for a course, not proof of its prerequisite chain
(Chabot's chart: Calculus BC -> MTH 2, "ask a counselor whether MTH 1 is also cleared").

Bad data is tolerated: codes are matched ignoring case, spacing and an institution prefix
("cs 20", "chabot:CS20" -> "CS 20"), a cross-listed code resolves to its one catalog course
(Course.aliases: "CSCI 28" -> "MTH 8", never counted twice), duplicate requisites are visited
once, a cycle cannot loop forever, and a requisite that is not in the catalog is skipped with
a warning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .models import Course


def _key(code: str) -> str:
    """Comparison key for course codes: 'cs  20', 'CS 20' and 'chabot:CS20' -> 'CS20'."""
    return re.sub(r"[^A-Z0-9]", "", str(code).rpartition(":")[2].upper())


@dataclass(frozen=True)
class Completion:
    explicit: frozenset[str]            # completed courses the student entered (catalog codes)
    waived: frozenset[str]              # credit by exam (catalog codes)
    inferred: frozenset[str]            # prerequisites implied by `explicit`, not entered or waived
    satisfied: frozenset[str]           # explicit | waived | inferred: never planned
    implied_by: dict[str, str] = field(default_factory=dict)   # inferred course -> completed course needing it
    unknown: tuple[str, ...] = ()       # entered codes that are not in the catalog (ignored)
    warnings: tuple[str, ...] = ()


def resolve_completed(catalog: Mapping[str, Course], completed: Iterable[str] = (),
                      waived: Iterable[str] = ()) -> Completion:
    """Match entered codes to the catalog and add every course a completed course required."""
    index: dict[str, str] = {}
    for code in catalog:
        index.setdefault(_key(code), code)
    for code, course in catalog.items():
        for alias in course.aliases:               # cross-listed code -> its catalog course
            index.setdefault(_key(alias), code)
    lookup = lambda raw: raw if raw in catalog else index.get(_key(raw))   # noqa: E731

    def resolve(codes: Iterable[str]) -> tuple[list[str], list[str]]:
        found, missing = [], []
        for raw in codes:
            code = lookup(raw)
            (found if code else missing).append(code or raw)
        return found, missing

    explicit_list, unknown = resolve(completed)
    waived_list, unknown_waived = resolve(waived)
    explicit, waived_set = frozenset(explicit_list), frozenset(waived_list)
    warnings: list[str] = []

    # Depth-first walk down each completed course's requisite chain
    seen = set(explicit)
    implied_by: dict[str, str] = {}
    stack = [(code, code) for code in sorted(explicit)]
    while stack:
        code, root = stack.pop()
        course = catalog[code]
        for req in dict.fromkeys((*course.prereqs, *course.coreqs)):   # duplicates visited once
            target = lookup(req)
            if target is None:
                warnings.append(f"{code} lists requisite '{req}', which is not in the catalog; it was skipped.")
                continue
            if target in seen:                                         # already satisfied (also ends cycles)
                continue
            seen.add(target)
            implied_by[target] = root
            stack.append((target, root))

    inferred = frozenset(seen - explicit - waived_set)
    return Completion(explicit=explicit, waived=waived_set, inferred=inferred,
                      satisfied=explicit | waived_set | inferred,
                      implied_by={c: implied_by[c] for c in sorted(inferred)},
                      unknown=tuple(dict.fromkeys(unknown + unknown_waived)), warnings=tuple(warnings))
