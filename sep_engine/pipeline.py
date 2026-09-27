"""
generate_sep(): resolve what is already done -> Dijkstra (pick courses) -> topological sort
(order them) -> CSP (place them in terms).

Step 0 (completion.resolve_completed) turns the student's completed courses into the set of
SATISFIED courses: the completed ones plus every prerequisite in their chains. Every later
step uses that set, so a course whose later course is already done is never planned again.
Runs once per PlanConfig and returns a JSON-serializable dict in the /generate-sep response shape
(see the Pydantic models in main.py).

General education (optional `ge` context, see ge.py): after Dijkstra picks the plan's major prep,
GE is evaluated for that plan (completed courses, AP, major prep the college's GE list approves,
and the student's own GE choices). The chosen courses are scheduled in the terms the student picked
(pinned), and each plan returns a `ge` block: every requirement with what fills it and, while open,
the college courses that could.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .completion import Completion, resolve_completed
from .ge import GEContext, GEPlanner
from .models import Course, PlanConfig, StudentProfile, Term, TransferAgreement, index_catalog
from .pathway import select_pathway
from .scheduler import parse_term_label, schedule_courses, term_sequence
from .toposort import chain_metrics, critical_path, topological_sort

DEFAULT_PLANS: tuple[PlanConfig, ...] = (
    PlanConfig(id="A", name="Plan A", label="Fastest Route", weight="units",
               description="Heavy loads (up to 18 units, 9 in summer) to transfer as early as possible.",
               max_units=18, max_summer_units=9, value_order="earliest"),
    PlanConfig(id="B", name="Plan B", label="Balanced Workload", weight="difficulty",
               description="Capped at 13 units per term (9 in summer), spread evenly, favoring easier courses.",
               max_units=13, max_summer_units=9, value_order="balanced"),
)


def _num(x: float) -> float | int:
    return int(x) if float(x).is_integer() else round(x, 2)


def node_id(code: str) -> str:
    """Cytoscape-safe id: 'MATH 1' -> 'MATH1'."""
    return re.sub(r"[^A-Za-z0-9_]", "", code)


def generate_sep(
    catalog: Mapping[str, Course] | Iterable[Course],
    agreement: TransferAgreement,
    profile: StudentProfile | None = None,
    plans: Sequence[PlanConfig] | None = None,
    ge: GEContext | None = None,
) -> dict[str, Any]:
    """
    Build every plan for one College + University + Major agreement (+ GE pathway, when `ge` is given).

    Raises a SEPError subclass (CycleError, UnknownCourseError,
    InfeasibleRequirementError, SchedulingError) when the data or the
    constraints make a plan impossible.
    """
    if not isinstance(catalog, Mapping):
        catalog = index_catalog(list(catalog))
    profile = profile or StudentProfile()
    first = term_sequence(profile.start_term, profile.start_year, 1, profile.include_summer)[0]

    # 0. Satisfied = completed + waived + every prerequisite in a completed course's chain.
    #    Everything below plans only what is NOT satisfied.
    completion = resolve_completed(catalog, profile.completed_courses, profile.waived_courses)
    plans = list(plans or DEFAULT_PLANS)
    planner = None
    if ge is not None:
        planner = GEPlanner(ge, completed=profile.completed_courses, satisfied=completion.satisfied,
                            aliases={code: c.aliases for code, c in catalog.items() if c.aliases},
                            start_term=first.label, include_summer=profile.include_summer,
                            summer_cap=min(min(p.max_units, p.max_summer_units) for p in plans))

    # A completed course that isn't major prep may still count toward GE (then it isn't "ignored")
    warnings = [f"Completed course '{c}' is not part of this pathway's course data and was ignored."
                for c in sorted(completion.unknown) if not (planner and planner.recognizes(c))]
    warnings += list(completion.warnings)
    if planner:
        warnings += _ge_warnings(planner)
    built = [_build_plan(catalog, agreement, profile, completion, cfg, planner) for cfg in plans]
    result = {
        "pathway": {
            "college": agreement.college,
            "university": agreement.university,
            "major": agreement.major,
            "degree": agreement.degree or agreement.major,
            "ge_pathway": agreement.ge_pattern,
            "start_term": first.label,
            "include_summer": profile.include_summer,
            "completed_courses": sorted(set(profile.completed_courses)),
            "waived_courses": sorted(completion.waived - completion.explicit),
            "inferred_courses": sorted(completion.inferred),
        },
        "warnings": warnings + [w for plan in built for w in plan.pop("_warnings", [])],
        "plans": built,
    }
    if planner:
        result["pathway"]["ge_pathway"] = ge.pathway_name
        result["pathway"]["ge_pathway_id"] = ge.pathway
        result["ge_choices"] = {
            "accepted": [{"requirement": ch.requirement, "course": code, "term": ch.term}
                         for ch, code in planner.choices],
            "dropped": [{"requirement": ch.requirement, "course": ch.course, "term": ch.term, "reason": reason}
                        for ch, reason in planner.dropped]}
        result["ge_courses"] = {code: _ge_course(c) for code, c in sorted(planner.referenced.items())}
    return result


def _ge_warnings(planner: GEPlanner) -> list[str]:
    ctx = planner.ctx
    out = []
    if ctx.case in ("not_loaded", "not_applicable"):
        out.append(ctx.message)
    if ctx.map_note:
        out.append(ctx.map_note)
    if ctx.ap_note:
        out.append(ctx.ap_note)
    out += [f"GE choice {ch.course}{f' ({ch.term})' if ch.term else ''} was removed: {reason}."
            for ch, reason in planner.dropped]
    return out


def _ge_course(c) -> dict[str, Any]:
    return {"code": c.code, "title": c.title, "units": _num(c.units), "areas": sorted(c.areas),
            "discipline": c.discipline, "same_as": list(c.same_as), "prereq_any": list(c.prereq_any),
            "prereq_text": c.prereq_text, "prereq_blocking": c.prereq_blocking,
            "prereq_concurrent": c.prereq_concurrent, "first_term": c.first_term, "last_term": c.last_term}


def _ge_catalog(catalog: Mapping[str, Course], part, keep: set[str]) -> dict[str, Course]:
    """The plan's catalog plus the chosen GE courses. A chosen course keeps only requisites that are
    done or in the plan; the rest of its catalog prerequisite is checked when it is offered and warned
    about after scheduling (ge.GEPlanner.prerequisite_warnings), never enforced as a hidden course."""
    extra = {}
    for code, c in part.courses.items():
        known = catalog.get(code)
        extra[code] = (known.model_copy(update={"prereqs": [p for p in known.prereqs if p in keep],
                                                "coreqs": [q for q in known.coreqs if q in keep]})
                       if known else Course(code=code, title=c.title, units=c.units))
    return {**catalog, **extra}


def _build_plan(catalog: Mapping[str, Course], agreement: TransferAgreement, profile: StudentProfile,
                completion: Completion, cfg: PlanConfig, planner: GEPlanner | None = None) -> dict[str, Any]:
    completed = completion.satisfied        # done for planning: entered, waived, or implied by a completed course
    # 1. Dijkstra: cheapest combination of requirement choices under this plan's metric
    selection = select_pathway(catalog, agreement, completed, cfg.weight)
    major = {c for c in selection.courses if selection.kinds.get(c) == "major"}

    # 1b. GE for this plan: what its major prep already covers, and the student's chosen GE courses
    part = planner.plan(sorted(selection.courses)) if planner else None
    courses = set(selection.courses)
    pins: dict[str, str] = {}
    released: dict[str, str] = {}
    if part and part.courses:
        catalog = _ge_catalog(catalog, part, courses | set(completed))
        courses |= set(part.courses)
        caps = {Term.FALL: cfg.max_units, Term.SPRING: cfg.max_units,
                Term.SUMMER: min(cfg.max_units, cfg.max_summer_units)}
        load: dict[str, float] = {}
        for code, term in part.pins.items():               # in the order the student chose them
            cap = caps[parse_term_label(term)[0]]
            if load.get(term, 0) + catalog[code].units > cap + 1e-9:
                released[code] = f"{term} can't hold it within {cfg.name}'s {_num(cap)}-unit limit"
                continue
            load[term] = load.get(term, 0) + catalog[code].units
            pins[code] = term

    # 2. Topological sort (first pass validates/detects cycles, second puts long chains first)
    order = topological_sort(catalog, courses, completed)
    _, height = chain_metrics(order, catalog)
    order = topological_sort(catalog, courses, completed, key=lambda c: (-height[c], c))

    # 3. CSP backtracking: place courses into real terms, ending in a Spring (chosen GE terms pinned)
    schedule = schedule_courses(catalog, order, profile, cfg.max_units, cfg.max_term_difficulty,
                                max_summer_units=cfg.max_summer_units, major_courses=major,
                                value_order=cfg.value_order, pinned=pins)

    term_of = {c: schedule.slots[t].label for c, t in schedule.assignment.items()}
    for code, term in schedule.released.items():
        released[code] = f"{term} is not one of this plan's terms"
    plan_warnings = [f"{cfg.name}: {code} is scheduled in {term_of[code]} instead of {part.pins[code]}: {why}."
                     for code, why in released.items() if code in term_of] if part else []
    if part:
        plan_warnings += planner.prerequisite_warnings(part, term_of, cfg.name)
    position = {c: i for i, c in enumerate(order)}
    ge_codes = set(part.courses) if part else set()
    is_ge = lambda code: selection.kinds.get(code) == "ge" or code in ge_codes   # noqa: E731
    if part:
        labels = {r.id: r.label for r in planner.ctx.requirements}
        for code, rid in part.requirement_of.items():
            selection.reasons.setdefault(code, [f"{planner.ctx.pathway_name} {labels[rid]}"])

    # --- semesters -----------------------------------------------------------
    semesters = []
    terms = schedule.terms()
    for n, (slot, codes) in enumerate(terms):
        courses = [catalog[c] for c in sorted(codes, key=position.__getitem__)]
        semesters.append({
            "index": slot.index + 1,
            "term": slot.label,
            "season": slot.term.value,
            "year": slot.year,
            "units": _num(sum(c.units for c in courses)),
            "max_units": _num(schedule.caps[slot.index]),
            "is_padding": not courses and n == len(terms) - 1,
            "courses": [{"id": node_id(c.code), "code": c.code, "title": c.title,
                         "units": _num(c.units), "is_ge": is_ge(c.code),
                         "satisfies": selection.reasons.get(c.code, [])} for c in courses],
        })

    # --- graph (planned courses + completed courses that matter to this plan) --
    chosen = set(agreement.required_courses) | {c for picks in selection.choices.values() for c in picks}
    shown_completed = {c for c in chosen if c in completed}
    for c in order:
        shown_completed |= {r for r in (*catalog[c].prereqs, *catalog[c].coreqs) if r in completed}
    graph_codes = [*order, *sorted(shown_completed)]
    in_graph = set(graph_codes)
    nodes = [{"data": {
        "id": node_id(code), "label": code, "title": catalog[code].title, "units": _num(catalog[code].units),
        "is_ge": is_ge(code), "status": "completed" if code in completed else "planned",
        "satisfied_by": completion.implied_by.get(code),
        "term": term_of.get(code), "satisfies": selection.reasons.get(code, []),
    }} for code in graph_codes]
    edges: dict[str, dict] = {}
    for code in graph_codes:
        for relation, reqs in (("prereq", catalog[code].prereqs), ("coreq", catalog[code].coreqs)):
            for r in reqs:
                if r in in_graph:
                    eid = f"e_{node_id(r)}_{node_id(code)}"
                    edges.setdefault(eid, {"data": {"id": eid, "source": node_id(r), "target": node_id(code),
                                                    "relation": relation}})
    cp = [node_id(c) for c in critical_path(order, catalog)]

    # --- requirements ----------------------------------------------------------
    def entry(code: str) -> dict[str, Any]:
        return {"id": node_id(code), "code": code, "title": catalog[code].title,
                "kind": selection.kinds.get(code, "major"),
                "status": "completed" if code in completed else "planned",
                "satisfied_by": completion.implied_by.get(code), "term": term_of.get(code)}

    requirements = [{
        "id": "major-prep", "name": "Major preparation (required)", "category": "major",
        "choose": len(agreement.required_courses),
        "courses": [entry(c) for c in agreement.required_courses],
    }] + [{
        "id": g.id, "name": g.name, "category": g.category.value, "choose": g.choose,
        "courses": [entry(c) for c in selection.choices[g.id]],
    } for g in agreement.requirement_groups]

    total_units = sum(catalog[c].units for c in order)
    last = schedule.slots[-1] if schedule.slots else None
    return {
        "id": cfg.id,
        "name": cfg.name,
        "label": cfg.label,
        "description": cfg.description,
        "selection_metric": cfg.weight,
        "max_units_regular": _num(cfg.max_units),
        "max_units_summer": _num(min(cfg.max_units, cfg.max_summer_units)),
        "summary": {
            "terms_needed": len(semesters),
            "min_terms_required": schedule.lower_bound,
            "prereq_chain_length": len(cp),
            "unit_load_terms": math.ceil(total_units / cfg.max_units) if total_units else 0,
            "total_units": _num(total_units),
            "transfer_admission_term": f"Fall {last.year}" if last and last.term is Term.SPRING else None,
            "schedule_proven_minimal": schedule.proven_minimal,
            "major_prep_in_summer": schedule.major_in_summer,
        },
        "graph": {"nodes": nodes, "edges": list(edges.values())},
        "critical_path": cp,
        "critical_path_edges": [f"e_{a}_{b}" for a, b in zip(cp, cp[1:])],
        "requirements": requirements,
        "semesters": semesters,
        **({"ge": planner.status(part, term_of, order, released), "_warnings": plan_warnings} if planner else {}),
    }
