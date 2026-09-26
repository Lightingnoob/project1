"""
TransferPath SEP Planner — API + static frontend.

Run:   uvicorn main:app --reload
App:   http://127.0.0.1:8000/          (index.html, served by this app)
Docs:  http://127.0.0.1:8000/docs

PATHWAY DATA: at startup every ASSIST agreement JSON in ap_engine/data/ is loaded into the
pathway store (pathway_data.py: file -> loader -> Pathway -> store -> /generate-sep). Institutions
match by canonical id ("Cal State East Bay" = "California State University, East Bay" = csueb),
so the dropdown spelling doesn't matter. GET /pathways lists what loaded (and what was found but
can't be planned, and why); POST /pathways/reload picks up new files without a restart.
/generate-sep returns 404 ("N/A - ... not uploaded yet") only when no agreement exists for the
college + university + major. For demos, POST /dev/mock-data adds one mock pathway
(Chabot College -> UC Berkeley -> Computer Science, CAL-GETC and 7-Course Pattern).

GE DATA: the chosen GE pathway (Cal-GETC, IGETC, UC 7-Course Pattern) is planned from ap_engine/data/ge/
(ge_data.py): the pathway's statewide rules, the selected college's own course list for it, and AP rules.
GET /ge-pathways lists what loaded. Each plan returns a `ge` block (requirements satisfied / planned / open,
with the college's eligible courses), and the student's GE picks come back in as `ge_choices`.

Test:  POST /generate-sep   (every section is required; "none" answers are explicit flags)
       {"college": "Chabot College", "university": "Cal State East Bay", "major": "Computer Science",
        "completed_courses": ["MTH 2"], "ap_scores": [], "no_ap_scores": true,
        "ge_pathway": "cal_getc", "start_term": "Fall 2026", "include_summer": true,
        "ge_choices": [{"requirement": "3A", "course": "ARTH 1", "term": "Summer 2027"}]}

AP credit: GET /colleges/Chabot College/ap-data returns the data that the browser feeds to
ap_engine/apWaiver.js (served at /ap_engine/apWaiver.js). AP-waived courses reach
/generate-sep as waived_courses: done, but (unlike completed_courses) their prerequisites are not implied.

The response models below validate every payload's internal consistency (graph
references, critical path chain, unit totals and caps, real consecutive terms,
plans ending in Spring), so a bad payload fails loudly with a 500 instead of
breaking Cytoscape in the browser.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, field_validator,
                      model_validator)
from pydantic_core import PydanticCustomError

import course_search
from ge_data import GERegistry
from institutions import REGISTRY, normalize
from pathway_data import PathwayStore, split_major
from sep_engine import SEPError, StudentProfile, generate_sep
from sep_engine import mock_data
from sep_engine.ge import GEChoice
from sep_engine.scheduler import parse_term_label, term_sequence

# Some Windows installs map .js to text/plain, which browsers refuse for ES modules (ap_engine/*.js).
mimetypes.add_type("text/javascript", ".js")

Season = Literal["Fall", "Spring", "Summer"]
TERM_PATTERN = r"^(Fall|Spring|Summer) \d{4}$"
NOT_UPLOADED = "N/A - Transfer data for this specific pathway has not been uploaded yet."


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =====================================================================
# REQUEST — every section of the form needs an explicit answer (ui.js checks the same rules)
# =====================================================================
def _ask(message: str) -> dict:
    """What a MISSING field's 422 error says, instead of Pydantic's generic "Field required"."""
    return {"x-missing-message": message}


# field -> (GET /institutions list, what the student is asked to select)
LISTED_OPTIONS = {"college": ("colleges", "a community college"),
                  "university": ("universities", "a target university"),
                  "major": ("majors", "an intended major")}


def _listed(field: str, value: str) -> str:
    """College, university and major must name an option the planner lists (GET /institutions,
    which includes every loaded pathway). Any spelling of the same institution counts (csueb)."""
    text = " ".join(value.split())
    kind, what = LISTED_OPTIONS[field]
    if not text:
        raise PydanticCustomError("answer_required", f"Please select {what}.")
    options = institutions()[kind]
    if kind == "universities":
        options = [u for names in options.values() for u in names]
    listed = (normalize(split_major(text)[0]) in {normalize(m) for m in options} if kind == "majors"
              else any(REGISTRY.same(text, o) for o in options))
    if not listed:
        raise PydanticCustomError("unknown_option", f"“{{value}}” is not in the list. Please select {what} "
                                  "from GET /institutions.", {"value": text})
    return text


def _answered(none: bool, entries: list | None, what: str, none_label: str) -> bool:
    """Entries or an explicit "none": never both, and never neither (an empty list alone is unanswered)."""
    if entries is None:                   # the list itself was invalid; that error is already reported
        return none
    if none and entries:
        raise PydanticCustomError("answer_conflict", f"Choose either {what}s or “{none_label}”, not both.")
    if not none and not entries:
        raise PydanticCustomError("answer_required", f"Add at least one {what} or choose “{none_label}.”")
    return none


CourseCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class APScore(StrictModel):
    subject: str = Field(min_length=1, examples=["AP Computer Science A"])
    score: int = Field(ge=1, le=5, examples=[4])


class GEChoiceIn(StrictModel):
    """A GE course the student picked for one requirement of their GE pathway, in a term of the plan."""
    requirement: str = Field(min_length=1, examples=["3A"], description="Requirement id from a plan's `ge` block.")
    course: CourseCode = Field(examples=["ARTH 1"])
    term: str | None = Field(default=None, pattern=TERM_PATTERN, examples=["Summer 2027"],
                             description="Term to take it in; null lets the scheduler place it (Summer first).")


class GenerateSEPRequest(StrictModel):
    """One planning request. Nothing has a stand-in default: an unanswered section is a 422, never a guess."""

    college: str = Field(examples=["Chabot College"], json_schema_extra=_ask("Please select a community college."))
    university: str = Field(examples=["Cal State East Bay"],
                            json_schema_extra=_ask("Please select a target university."))
    major: str = Field(examples=["Computer Science"], json_schema_extra=_ask("Please select an intended major."))
    completed_courses: list[CourseCode] = Field(
        default_factory=list, examples=[["MTH 2"]],
        description="Courses the student passed. Every prerequisite in their chains counts as done too.")
    no_completed_courses: bool = Field(
        default=False, validate_default=True,
        description="The explicit answer \"No completed courses\". Without it, an empty completed_courses "
                    "is unanswered and rejected.")
    ap_scores: list[APScore] = Field(
        default_factory=list, examples=[[{"subject": "AP Computer Science A", "score": 4}]],
        description="The AP scores the student entered. The browser evaluates them (ap_engine); the courses "
                    "they waive arrive as waived_courses.")
    no_ap_scores: bool = Field(
        default=False, validate_default=True,
        description="The explicit answer \"No AP scores\". Without it, an empty ap_scores is unanswered and rejected.")
    waived_courses: list[CourseCode] = Field(
        default_factory=list, examples=[["CSCI 14"]],
        description="Credit by exam (AP waivers). Counts as done, but does not imply the course's prerequisites.")
    ge_pathway: str = Field(examples=["cal_getc"], json_schema_extra=_ask("Please select a GE pathway."),
                            description="A GE pathway from GET /ge-pathways, by id or name ('cal_getc' = 'CAL-GETC').")
    start_term: str = Field(pattern=TERM_PATTERN, description="First term of the plan.",
                            examples=["Fall 2026"], json_schema_extra=_ask("Please select a starting semester."))
    include_summer: bool = Field(description="Allow Summer terms (max 9 units, GE first). Must be sent: true or false.",
                                 json_schema_extra=_ask("Please choose whether to include summer classes."))
    ge_choices: list[GEChoiceIn] = Field(
        default_factory=list, description="GE courses the student picked (optional). Invalid or unneeded ones "
                                          "come back in ge_choices.dropped with the reason.")

    @field_validator("ge_pathway")
    @classmethod
    def _known_ge_pathway(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise PydanticCustomError("answer_required", "Please select a GE pathway.")
        pathway = GE.resolve(text)
        if pathway is None:
            raise PydanticCustomError("unknown_option", "“{value}” is not a GE pathway. Please select one from "
                                      "GET /ge-pathways.", {"value": text})
        return pathway

    @field_validator("college", "university", "major")
    @classmethod
    def _listed_option(cls, value: str, info: ValidationInfo) -> str:
        return _listed(info.field_name, value)

    @field_validator("no_completed_courses")
    @classmethod
    def _completed_answered(cls, none: bool, info: ValidationInfo) -> bool:
        return _answered(none, info.data.get("completed_courses"), "completed course", "No completed courses")

    @field_validator("no_ap_scores")
    @classmethod
    def _ap_answered(cls, none: bool, info: ValidationInfo) -> bool:
        return _answered(none, info.data.get("ap_scores"), "AP score", "No AP scores")

    @field_validator("waived_courses")
    @classmethod
    def _waivers_need_ap_scores(cls, waived: list[str], info: ValidationInfo) -> list[str]:
        if waived and info.data.get("no_ap_scores"):
            raise PydanticCustomError("answer_conflict", "AP waivers come from AP scores, but “No AP scores” was chosen.")
        return waived

    @field_validator("include_summer")
    @classmethod
    def _summer_start_needs_summer(cls, include_summer: bool, info: ValidationInfo) -> bool:
        if not include_summer and str(info.data.get("start_term", "")).startswith("Summer"):
            raise PydanticCustomError("summer_start", "A Summer starting semester needs summer classes included.")
        return include_summer


MISSING_MESSAGES = {name: f.json_schema_extra["x-missing-message"] for name, f in GenerateSEPRequest.model_fields.items()
                    if isinstance(f.json_schema_extra, dict) and "x-missing-message" in f.json_schema_extra}


# =====================================================================
# RESPONSE — graph (Cytoscape.js `elements` format: { nodes: [...], edges: [...] })
# =====================================================================
class NodeData(StrictModel):
    id: str = Field(description="Cytoscape id; course code without spaces, e.g. 'MATH1'.",
                    pattern=r"^[A-Za-z0-9_]+$", examples=["MATH1"])
    label: str = Field(description="Course code shown on the node.", examples=["MATH 1"])
    title: str = Field(examples=["Calculus I"])
    units: float = Field(gt=0)
    is_ge: bool = Field(description="True for GE courses, False for major prep (and its prerequisites).")
    status: Literal["completed", "planned"] = Field(
        description="'completed' = done for planning: entered, waived, or a prerequisite of a completed course.")
    satisfied_by: str | None = Field(
        default=None, examples=["CS 20"],
        description="Set when the student did not enter this course but completed a course that required it.")
    term: str | None = Field(description="Planned term, e.g. 'Spring 2027'; null when completed.")
    satisfies: list[str] = Field(default_factory=list, description="Requirements this course counts toward.")


class CyNode(StrictModel):
    data: NodeData


class EdgeData(StrictModel):
    id: str = Field(description="'e_<source>_<target>'", examples=["e_MATH1_MATH2"])
    source: str = Field(description="Prerequisite / co-requisite course id.")
    target: str = Field(description="Course that requires `source`.")
    relation: Literal["prereq", "coreq"] = Field(
        default="prereq", description="prereq: source in an earlier term. coreq: same term or earlier.")


class CyEdge(StrictModel):
    data: EdgeData


class GraphElements(StrictModel):
    nodes: list[CyNode]
    edges: list[CyEdge]


# =====================================================================
# RESPONSE — term timeline (accordion cards) + requirements
# =====================================================================
class PlannedCourse(StrictModel):
    id: str = Field(description="Matches a graph node id.")
    code: str
    title: str
    units: float = Field(gt=0)
    is_ge: bool
    satisfies: list[str] = Field(default_factory=list)


class Semester(StrictModel):
    index: int = Field(ge=1, description="1-based position in the plan.")
    term: str = Field(description="Real term name.", examples=["Fall 2026"])
    season: Season
    year: int
    units: float = Field(ge=0)
    max_units: float = Field(gt=0, description="Unit cap for this term (lower in Summer).")
    is_padding: bool = Field(description="Empty final Spring added so the plan ends in Spring.")
    courses: list[PlannedCourse]

    @model_validator(mode="after")
    def _consistent(self) -> Semester:
        if self.term != f"{self.season} {self.year}":
            raise ValueError(f"term {self.term!r} does not match season/year")
        total = sum(c.units for c in self.courses)
        if abs(total - self.units) > 1e-6:
            raise ValueError(f"{self.term}: units={self.units} but courses sum to {total}")
        if self.units > self.max_units + 1e-6:
            raise ValueError(f"{self.term}: {self.units} units exceeds the {self.max_units}-unit cap")
        if self.is_padding and self.courses:
            raise ValueError(f"{self.term}: a padding term cannot contain courses")
        return self


class RequirementCourse(StrictModel):
    id: str
    code: str
    title: str
    kind: Literal["major", "ge"] = Field(description="'major' when the course is (also) major prep.")
    status: Literal["completed", "planned"]
    satisfied_by: str | None = Field(default=None, description="Completed course that required this one.")
    term: str | None


class Requirement(StrictModel):
    id: str
    name: str
    category: Literal["major", "ge"]
    choose: int = Field(ge=0)
    courses: list[RequirementCourse]

    @model_validator(mode="after")
    def _complete(self) -> Requirement:
        if len(self.courses) != self.choose:
            raise ValueError(f"requirement {self.id!r} lists {len(self.courses)} courses but needs {self.choose}")
        return self


class GEFill(StrictModel):
    kind: Literal["completed", "ap", "major", "choice"] = Field(
        description="completed: a course the student entered; ap: an AP exam; major: major prep this plan "
                    "schedules; choice: a GE course the student picked.")
    code: str | None = Field(description="Course code; null for an AP exam.")
    label: str = Field(examples=["PSYC C1000", "AP Psychology"])
    title: str
    units: float | None
    score: int | None = Field(description="AP score.")
    area: str = Field(description="The listed area it counts as, e.g. '3A'.")
    term: str | None = Field(description="Planned term; null when done.")


class GERequirementStatus(StrictModel):
    id: str = Field(examples=["3A"])
    label: str = Field(examples=["Area 3A · Arts"])
    name: str
    area_id: str
    area_name: str
    courses_required: int = Field(ge=1)
    status: Literal["satisfied", "planned", "in_progress", "open"] = Field(
        description="satisfied: done (courses or AP); planned: filled, partly by planned courses; "
                    "in_progress: partly filled; open: nothing counts yet.")
    open: int = Field(ge=0, description="Courses still to choose.")
    rule: str
    note: str | None
    shares_course: bool = Field(description="May use a course counted elsewhere (a 5C lab, IGETC 6A).")
    min_disciplines: int = Field(ge=1)
    min_distinct_areas: int = Field(ge=1)
    min_units: float | None
    filled: list[GEFill]
    options: list[str] = Field(description="While open: the college's courses that can fill it (details in "
                                           "SEPResponse.ge_courses), minus done, planned and already-used ones.")
    options_note: str | None = Field(description="Why there are no options, e.g. no college course list loaded.")

    @model_validator(mode="after")
    def _counts(self) -> GERequirementStatus:
        if len(self.filled) + self.open != self.courses_required:
            raise ValueError(f"GE {self.id}: {len(self.filled)} filled + {self.open} open != {self.courses_required}")
        return self


class GEChoiceState(StrictModel):
    requirement: str
    course: str
    term: str | None = Field(description="The term the student chose.")
    status: Literal["planned", "not_needed"]
    placed_term: str | None = Field(description="Where this plan schedules it.")
    note: str | None


class GEStandardRef(StrictModel):
    dataset_id: str
    source: str
    historical: bool
    derived_from: list[str] = Field(description="College lists the area rules were published with, if any.")
    notes: list[str]


class GECourseListRef(StrictModel):
    dataset_id: str
    institution: str
    institution_name: str
    academic_year: str | None
    requested_academic_year: str | None
    source: str
    note: str | None = Field(description="Set when the list is for another academic year.")


class GESummary(StrictModel):
    requirements: int = Field(ge=0)
    met: int = Field(ge=0)
    planned: int = Field(ge=0)
    open: int = Field(ge=0)
    courses_needed: int = Field(ge=0)
    courses_open: int = Field(ge=0)


class GEPlanStatus(StrictModel):
    pathway: str = Field(examples=["cal_getc"])
    name: str = Field(examples=["Cal-GETC"])
    case: Literal["not_loaded", "not_applicable", "requirements_only", "courses_available"] = Field(
        description="not_loaded: no rules for this pathway; not_applicable: its rules are for other destinations; "
                    "requirements_only: rules but no course list for this college; courses_available: both.")
    message: str
    target_system: str
    standard: GEStandardRef | None
    course_list: GECourseListRef | None
    ap_source: str | None
    ap_note: str | None
    requirements: list[GERequirementStatus]
    all_satisfied: bool
    choices: list[GEChoiceState]
    summary: GESummary


class GECourseInfo(StrictModel):
    code: str
    title: str
    units: float = Field(gt=0)
    areas: list[str] = Field(description="Every area the college lists it under for this pathway.")
    discipline: str
    same_as: list[str]
    prereq_any: list[str] = Field(description="Catalog prerequisite: at least one of these, earlier (or the same "
                                              "term when prereq_concurrent).")
    prereq_text: str | None
    prereq_blocking: bool = Field(description="False: the catalog allows another route (placement); advisory only.")
    prereq_concurrent: bool
    first_term: str | None
    last_term: str | None = Field(description="Last term the course is on the list, if it is being retired.")


class GEChoiceDropped(StrictModel):
    requirement: str
    course: str
    term: str | None
    reason: str


class GEChoicesResult(StrictModel):
    accepted: list[GEChoiceIn]
    dropped: list[GEChoiceDropped]


class PlanSummary(StrictModel):
    terms_needed: int = Field(ge=0)
    min_terms_required: int = Field(ge=0, description="Lower bound from prereq chain and unit capacity.")
    prereq_chain_length: int = Field(ge=0)
    unit_load_terms: int = Field(ge=0, description="ceil(total units / regular-term cap).")
    total_units: float = Field(ge=0)
    transfer_admission_term: str | None = Field(examples=["Fall 2028"])
    schedule_proven_minimal: bool
    major_prep_in_summer: list[str] = Field(description="Major prep that could only fit in Summer.")


class SemesterPlan(StrictModel):
    id: str = Field(examples=["A"])
    name: str = Field(examples=["Plan A"])
    label: str = Field(examples=["Fastest Route"])
    description: str
    selection_metric: Literal["units", "difficulty"]
    max_units_regular: float = Field(gt=0)
    max_units_summer: float = Field(gt=0)
    summary: PlanSummary
    graph: GraphElements
    critical_path: list[str] = Field(description="Node ids in chain order; highlight these in red.")
    critical_path_edges: list[str] = Field(description="Edge ids joining consecutive critical_path nodes.")
    requirements: list[Requirement]
    semesters: list[Semester]
    ge: GEPlanStatus | None = Field(default=None, description="The GE pathway's requirements for this plan "
                                                              "(null for mock data, whose GE is in `requirements`).")

    @model_validator(mode="after")
    def _consistent(self) -> SemesterPlan:
        where = f"Plan {self.id}"
        nodes = {n.data.id: n.data for n in self.graph.nodes}
        edges = {e.data.id: e.data for e in self.graph.edges}
        if len(nodes) != len(self.graph.nodes) or len(edges) != len(self.graph.edges):
            raise ValueError(f"{where}: duplicate node or edge ids")
        for e in edges.values():
            if e.source not in nodes or e.target not in nodes:
                raise ValueError(f"{where}: edge {e.id} references a missing node")

        if any(n not in nodes for n in self.critical_path):
            raise ValueError(f"{where}: critical_path references missing nodes")
        expected = [f"e_{a}_{b}" for a, b in zip(self.critical_path, self.critical_path[1:])]
        if self.critical_path_edges != expected or any(e not in edges for e in expected):
            raise ValueError(f"{where}: critical_path must be a connected chain; edges should be {expected}")

        if self.summary.terms_needed != len(self.semesters):
            raise ValueError(f"{where}: terms_needed does not match semesters")
        if [s.index for s in self.semesters] != list(range(1, len(self.semesters) + 1)):
            raise ValueError(f"{where}: semester indexes must be 1..n in order")
        if abs(sum(s.units for s in self.semesters) - self.summary.total_units) > 1e-6:
            raise ValueError(f"{where}: total_units does not match the semesters")
        for s in self.semesters:
            cap = self.max_units_summer if s.season == "Summer" else self.max_units_regular
            if s.max_units != cap:
                raise ValueError(f"{where}: {s.term} cap should be {cap}")
        if self.semesters:
            last = self.semesters[-1]
            if last.season != "Spring":
                raise ValueError(f"{where}: plans must end in a Spring term (ends {last.term})")
            if self.summary.transfer_admission_term != f"Fall {last.year}":
                raise ValueError(f"{where}: transfer_admission_term should be Fall {last.year}")

        planned = {i for i, d in nodes.items() if d.status == "planned"}
        term_index = {c.id: s.index for s in self.semesters for c in s.courses}
        scheduled = [c.id for s in self.semesters for c in s.courses]
        if len(scheduled) != len(set(scheduled)) or set(scheduled) != planned:
            raise ValueError(f"{where}: every planned node must be scheduled exactly once")
        term_label = {c.id: s.term for s in self.semesters for c in s.courses}
        for i, d in nodes.items():
            if d.term != term_label.get(i):
                raise ValueError(f"{where}: node {i} term {d.term!r} does not match the timeline")
        for e in edges.values():
            if e.source in term_index and e.target in term_index:
                a, b = term_index[e.source], term_index[e.target]
                if (e.relation == "prereq" and not a < b) or (e.relation == "coreq" and not a <= b):
                    raise ValueError(f"{where}: violates {e.relation} {e.source} -> {e.target}")
        by_code = {c.code: (s.term, c.is_ge) for s in self.semesters for c in s.courses}
        for r in (self.ge.requirements if self.ge else []):
            for f in r.filled:
                if f.kind in ("major", "choice") and (f.code not in by_code or by_code[f.code][0] != f.term):
                    raise ValueError(f"{where}: GE {r.id} counts {f.code} ({f.term}), which is not scheduled there")
                if f.kind == "choice" and not by_code[f.code][1]:
                    raise ValueError(f"{where}: GE choice {f.code} must be scheduled as GE")
        return self


# =====================================================================
# RESPONSE — top level
# =====================================================================
class PathwayInfo(StrictModel):
    college: str
    university: str
    major: str
    degree: str
    ge_pathway: str = Field(examples=["Cal-GETC"])
    ge_pathway_id: str | None = Field(default=None, examples=["cal_getc"])
    start_term: str
    include_summer: bool
    completed_courses: list[str] = Field(description="Exactly what the student entered.")
    waived_courses: list[str] = Field(default_factory=list, description="AP / credit-by-exam courses counted as done.")
    inferred_courses: list[str] = Field(
        default_factory=list, description="Not entered, but done for planning: prerequisites of completed courses.")


class ArticulatedCourse(StrictModel):
    code: str = Field(examples=["CSCI 15"])
    title: str
    units: float | None


class ArticulationRequirement(StrictModel):
    id: str = Field(examples=["requirement:CS101"])
    targets: list[ArticulatedCourse] = Field(description="The university course(s) this requirement is.")
    options: list[list[ArticulatedCourse]] = Field(
        min_length=1, description="Community-college ways to meet it: any ONE inner list, all of its courses.")


class InstitutionRef(StrictModel):
    id: str = Field(examples=["csueb"])
    name: str = Field(examples=["California State University, East Bay"])
    short_name: str | None = Field(default=None, examples=["CSUEB"])


class ArticulationInfo(StrictModel):
    source: str = Field(examples=["ASSIST"])
    dataset_id: str
    document_title: str | None
    file: str
    academic_year: str | None = Field(examples=["2026-2027"])
    requested_academic_year: str | None = Field(description="Academic year of the start term (Fall 2026 -> 2026-2027).")
    sending: InstitutionRef
    receiving: InstitutionRef
    major: str
    degree: str | None = Field(examples=["B.S."])
    scope: str = Field(examples=["lower_division_core"], description="Which requirements the agreement data covers.")
    scope_label: str = Field(examples=["Lower-division core"])
    complete_degree_requirements: bool = Field(description="False: this is not every requirement of the degree.")
    program_total_units: float | None
    ge_included: bool
    requirements: list[ArticulationRequirement] = Field(
        description="Requirements with a community-college course; ones with none articulated are left out.")
    notes: list[str] = Field(description="Transfer policies and recommendations stated in the agreement.")


class SEPResponse(StrictModel):
    pathway: PathwayInfo
    warnings: list[str] = Field(default_factory=list)
    plans: list[SemesterPlan]
    articulation: ArticulationInfo | None = Field(
        default=None, description="The articulation agreement behind the plan; null for injected mock data.")
    ge_courses: dict[str, GECourseInfo] = Field(
        default_factory=dict, description="Every college course a plan's GE block offers or schedules, by code.")
    ge_choices: GEChoicesResult | None = Field(
        default=None, description="The request's GE choices: accepted, or dropped with the reason.")

    @model_validator(mode="after")
    def _real_calendar(self) -> SEPResponse:
        season, year = parse_term_label(self.pathway.start_term)
        for plan in self.plans:
            expected = [s.label for s in term_sequence(season, year, len(plan.semesters), self.pathway.include_summer)]
            actual = [s.term for s in plan.semesters]
            if actual != expected:
                raise ValueError(f"Plan {plan.id}: terms {actual} are not the consecutive terms "
                                 f"from {self.pathway.start_term} (summer {'on' if self.pathway.include_summer else 'off'})")
            missing = {o for r in (plan.ge.requirements if plan.ge else []) for o in r.options} - set(self.ge_courses)
            if missing:
                raise ValueError(f"Plan {plan.id}: GE options without course details: {sorted(missing)}")
        return self


class CatalogEntry(StrictModel):
    title: str
    units: float


class CourseMatch(StrictModel):
    code: str = Field(examples=["CSCI 14"])
    title: str = Field(examples=["Introduction to Structured Programming In C++"])
    units: str | None = Field(examples=["4 units"])
    college: str = Field(examples=["Chabot College"])
    sources: list[str] = Field(description="'2025-2026 catalog' and/or 'transfer pathway data'.")
    match: Literal["code", "subject", "course code", "title"] = Field(
        description="Why it matched: exact course ('mth 1' or 'math 1' -> MTH 1), resolved subject, "
                    "part of a course code, or title.")


class SubjectMatch(StrictModel):
    code: str = Field(examples=["MTH"])
    name: str | None = Field(examples=["Mathematics"], description="Null for pathway-only departments.")
    count: int = Field(ge=1, description="Courses in this subject.")
    match: Literal["code", "name", "alias", "code prefix", "name prefix", "name word"] = Field(
        description="How the query named this subject ('math' -> alias of Mathematics).")


class CourseGroup(StrictModel):
    code: str = Field(examples=["MTH"], description="Subject (department) code.")
    name: str | None = Field(examples=["Mathematics"])
    count: int = Field(ge=1, description="Courses in this subject (not just those shown).")
    courses: list[CourseMatch]


class SubjectInfo(StrictModel):
    code: str = Field(examples=["MTH"])
    name: str | None = Field(examples=["Mathematics"])
    count: int = Field(ge=1)


class CourseSearchResult(StrictModel):
    college: str
    query: str
    filter: SubjectInfo | None = Field(description="The subject the search was limited to (`subject=`), if any.")
    subjects: list[SubjectMatch] = Field(description="Level 1: the subjects the query refers to, best first.")
    total: int = Field(ge=0, description="All matching courses; `groups` holds only the best ones.")
    groups: list[CourseGroup] = Field(description="Level 2: courses grouped by subject, best group first.")


class MockDataStatus(StrictModel):
    loaded: bool
    pathway: dict[str, str] | None = None
    suggested_completed: list[str] = Field(default_factory=list)


# =====================================================================
# DATA STORE — every ASSIST agreement in ap_engine/data/, loaded at startup (pathway_data.py)
# =====================================================================
ROOT = Path(__file__).parent
DATA_DIRS = [ROOT / "ap_engine" / "data"]
GE_DIRS = [ROOT / "ap_engine" / "data" / "ge", ROOT / "ap_engine" / "data"]   # GE standards, college GE lists, AP charts
log = logging.getLogger("uvicorn.error")

GE = GERegistry()
STORE = PathwayStore(ge_key=GE.key)


def load_pathways() -> None:
    """(Re)read the data folders: GE data and ASSIST agreements. Injected mock pathways are kept."""
    GE.load_directory(*GE_DIRS)
    for f in GE.files:
        log.info("GE data %s: %s (%s)%s", f["status"], f["file"], f.get("role"), f" - {f['reason']}" if f.get("reason") else "")
    STORE.load_directory(*DATA_DIRS)
    for row in STORE.listing():
        if row["file"]:
            log.info("Pathway %s: %s -> %s, %s %s (%s) from %s%s", row["status"], row["college_id"],
                     row["university_id"], row["major"], row["degree"] or "", row["academic_year"], row["file"],
                     f" - {row['reason']}" if row["reason"] else "")
    for skipped in STORE.skipped:
        log.warning("Pathway file skipped: %s - %s", skipped["file"], skipped["reason"])


load_pathways()

# Selection lists for the dropdowns (NOT transfer data)
INSTITUTIONS = {
    "colleges": [
        "Chabot College", "Diablo Valley College", "Las Positas College", "Ohlone College",
        "De Anza College", "Foothill College", "Mission College", "City College of San Francisco",
        "Laney College", "College of Alameda", "Berkeley City College", "Evergreen Valley College",
        "Santa Monica College",
    ],
    "universities": {
        "UC": ["UC Berkeley", "UC Davis", "UC Irvine", "UCLA", "UC Merced",
               "UC Riverside", "UC San Diego", "UC Santa Barbara", "UC Santa Cruz"],
        "CSU": ["Cal Poly Humboldt", "Cal Poly Pomona", "Cal Poly San Luis Obispo", "Cal State Bakersfield",
                "Cal State Channel Islands", "Cal State Dominguez Hills", "Cal State East Bay",
                "Cal State Fullerton", "Cal State LA", "Cal State Long Beach", "Cal State Monterey Bay",
                "Cal State Northridge", "Cal State San Bernardino", "Cal State San Marcos", "Chico State",
                "Fresno State", "Sacramento State", "San Diego State University",
                "San Francisco State University", "San José State University", "Sonoma State University",
                "Stanislaus State"],
    },
    "majors": [
        "Computer Science", "Computer Engineering", "Electrical Engineering", "Mechanical Engineering",
        "Data Science", "Mathematics", "Statistics", "Physics", "Chemistry", "Biology",
        "Cognitive Science", "Economics", "Business Administration", "Psychology",
    ],
}


# =====================================================================
# APP
# =====================================================================
app = FastAPI(title="TransferPath SEP Planner API", version="0.2.0")

# Hackathon setting: allow any origin so the frontend also works from file:// or another dev server.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
async def explain_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    """The standard 422 body, except that a missing request field says what to answer
    ("Please select a GE pathway.") using the message declared on that field."""
    errors = [{**e, "msg": MISSING_MESSAGES[e["loc"][1]]}
              if e["type"] == "missing" and len(e["loc"]) == 2 and e["loc"][0] == "body" and e["loc"][1] in MISSING_MESSAGES
              else e for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/api.js", include_in_schema=False)
def api_js() -> FileResponse:
    return FileResponse(ROOT / "api.js", media_type="text/javascript")


@app.get("/ui.js", include_in_schema=False)
def ui_js() -> FileResponse:
    return FileResponse(ROOT / "ui.js", media_type="text/javascript")


# Prebuilt Tailwind CSS + self-hosted fonts, icons and graph libraries
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")

# AP waiver engine (browser ES modules: apWaiver.js imports ./graph.js). ui.js loads it via api.js.
app.mount("/ap_engine", StaticFiles(directory=ROOT / "ap_engine"), name="ap_engine")

# AP credit charts per community college, by canonical id (ap_engine/data). Only Chabot College has one so far.
AP_DATA = ROOT / "ap_engine" / "data"
AP_CHARTS = {"chabot": ("chabot_ap_waivers.json", "chabot_assist_articulation.json")}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _system(university: str) -> str:
    name = normalize(university)
    if name.startswith(("university of california", "uc ")):
        return "UC"
    return "CSU" if name.startswith(("california state university", "cal state", "csu ")) else "Other"


def system_of(university: str) -> str:
    """'UC' or 'CSU' from the dropdown's campus groups (any spelling of the campus), else by name."""
    for system, names in INSTITUTIONS["universities"].items():
        if any(REGISTRY.same(university, n) for n in names):
            return system
    return _system(university)


@app.get("/institutions")
def institutions() -> dict:
    """Dropdown lists, plus any college, university or major that only a loaded pathway names."""
    out = {"colleges": list(INSTITUTIONS["colleges"]), "majors": list(INSTITUTIONS["majors"]),
           "universities": {system: list(names) for system, names in INSTITUTIONS["universities"].items()}}
    for p in STORE.pathways:
        if not any(REGISTRY.same(c, p.college_ref) for c in out["colleges"]):
            out["colleges"].append(p.college)
        if not any(REGISTRY.same(u, p.university_ref) for names in out["universities"].values() for u in names):
            out["universities"].setdefault(_system(p.university), []).append(p.university)
        if not any(normalize(m) == normalize(p.major) for m in out["majors"]):
            out["majors"].append(p.major)
    return out


@app.get("/pathways", tags=["data"])
def pathways() -> dict:
    """Every pathway the planner knows: 'available', or 'unsupported' (found, but its structure can't be
    planned yet; `reason` says why). `skipped_files` are data files that could not be read."""
    return {"data_dirs": [str(d) for d in DATA_DIRS], "pathways": STORE.listing(), "skipped_files": STORE.skipped}


@app.post("/pathways/reload", tags=["data"])
def reload_pathways() -> dict:
    """Re-read the data folders, e.g. after adding an ASSIST or GE JSON file (no server restart needed)."""
    load_pathways()
    return pathways()


@app.get("/ge-pathways", tags=["data"])
def ge_pathways() -> dict:
    """Every GE pathway the form offers: whether its rules are loaded (standard), which colleges have a
    course list for it, and where its AP rules come from. `files` reports the role of every GE data file."""
    return {"data_dirs": [str(d) for d in GE_DIRS], **GE.listing()}


@app.get("/colleges/{college}/courses", response_model=CourseSearchResult,
         responses={404: {"description": "No course catalog for this college."}})
def search_college_courses(college: str, q: str = Query("", max_length=80),
                           subject: str | None = Query(None, max_length=10, examples=["MTH"],
                                                       description="Only search this subject (category filter)."),
                           limit: int = Query(20, ge=1, le=100)) -> dict:
    """Completed-courses autocomplete over the college's catalog file plus any uploaded pathway courses.
    Two levels (see course_search.py): resolve the subject ('math' -> Mathematics / MTH), then list
    its courses, then course-code and title matches. Case- and space-insensitive ('mth1' == 'MTH 1').
    With `subject=MTH`, only Mathematics is searched: q='' lists it, q='1' -> MTH 1 first."""
    catalog = course_search.courses_for(college, STORE.catalog_for(college))
    if catalog is None:
        raise HTTPException(404, f"No course catalog has been uploaded for {college}.")
    chosen = catalog.subjects.get(subject.strip().upper()) if subject else None
    if subject and chosen is None:
        raise HTTPException(422, f"{catalog.name} has no subject with code {subject}.")
    found = (course_search.search_in_subject(catalog, chosen.code, q, limit) if chosen
             else course_search.search(catalog, q, limit))
    groups: list[dict] = []
    for course, how in found["results"]:
        subject = catalog.subjects[course.department]
        if not groups or groups[-1]["code"] != subject.code:
            groups.append({"code": subject.code, "name": subject.name, "count": subject.count, "courses": []})
        groups[-1]["courses"].append({"code": course.code, "title": course.title, "units": course.units,
                                      "college": catalog.name, "sources": list(course.sources), "match": how})
    return {"college": catalog.name, "query": q, "total": found["total"], "groups": groups,
            "filter": {"code": chosen.code, "name": chosen.name, "count": chosen.count} if chosen else None,
            "subjects": [{"code": s.code, "name": s.name, "count": s.count,
                          "match": course_search.SUBJECT_MATCH[tier]} for tier, s in found["subjects"]]}


@app.get("/colleges/{college}/catalog", response_model=dict[str, CatalogEntry],
         responses={404: {"description": "No catalog uploaded for this college."}})
def college_catalog(college: str) -> dict:
    catalog = STORE.catalog_for(college)
    if catalog is None:
        raise HTTPException(404, f"No course catalog has been uploaded for {college}.")
    out = {code: {"title": c.title, "units": c.units} for code, c in catalog.items()}
    for c in catalog.values():                  # cross-listed codes are the same course (CSCI 28 = MTH 8)
        for alias in c.aliases:
            out.setdefault(alias, {"title": c.title, "units": c.units})
    return out


@app.get("/colleges/{college}/ap-data",
         responses={404: {"description": "No AP credit chart uploaded for this college."}})
def college_ap_data(college: str) -> dict:
    """Input for ap_engine/apWaiver.js evaluateApWaivers(): the college's AP chart, ASSIST
    articulation, and every campus's AP score rules."""
    chart = AP_CHARTS.get(REGISTRY.resolve(college))
    if chart is None:
        raise HTTPException(404, f"No AP credit chart has been uploaded for {college}.")
    load = lambda name: json.loads((AP_DATA / name).read_text(encoding="utf-8"))
    waivers, articulation = chart
    return {"cc_waivers": load(waivers), "articulation": load(articulation),
            "campus_data": load("ap_campus_score_requirements.json")}


@app.post("/generate-sep", response_model=SEPResponse,
          responses={404: {"description": NOT_UPLOADED},
                     422: {"description": "Invalid request, infeasible plan, or an agreement the planner can't use yet."}})
def generate(request: GenerateSEPRequest) -> dict:
    match = STORE.find(request.college, request.university, request.major, request.ge_pathway, request.start_term)
    if match is None:
        if unsupported := STORE.find_unsupported(request.college, request.university, request.major):
            raise HTTPException(422, unsupported.message)
        raise HTTPException(404, NOT_UPLOADED)
    pathway = match.pathway
    ge = None
    if pathway.ge_pattern:                  # mock data carries its own GE requirement groups
        agreement = pathway.agreement
    else:
        # An ASSIST agreement is major prep only: GE comes from the chosen pathway's GE data (ge_data.py)
        agreement = pathway.agreement.model_copy(update={"ge_pattern": GE.name(request.ge_pathway)})
        ge = GE.context(request.ge_pathway, pathway.college_ref, pathway.university, system_of(request.university),
                        request.start_term, [(a.subject, a.score) for a in request.ap_scores],
                        [GEChoice(c.requirement, c.course, c.term) for c in request.ge_choices])
    season, year = parse_term_label(request.start_term)
    profile = StudentProfile(completed_courses=request.completed_courses, waived_courses=request.waived_courses,
                             start_term=season, start_year=year, include_summer=request.include_summer)
    try:
        result = generate_sep(pathway.catalog, agreement, profile, ge=ge)
    except SEPError as e:
        raise HTTPException(422, str(e)) from e
    result["pathway"]["ge_pathway_id"] = request.ge_pathway
    result["warnings"] = [*match.warnings, *pathway.warnings_for(result), *result["warnings"]]
    if pathway.articulation:
        result["articulation"] = {**pathway.articulation, "requested_academic_year": match.academic_year}
    return result


# --- Dev-only: hackathon data injector ---------------------------------
def _mock_status() -> dict:
    ag = mock_data.MOCK_AGREEMENT
    loaded = any(p.source == "mock" for p in STORE.pathways)
    return {"loaded": loaded,
            "pathway": {"college": ag.college, "university": ag.university, "major": ag.major} if loaded else None,
            "suggested_completed": list(mock_data.MOCK_PROFILE.completed_courses) if loaded else []}


@app.get("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def mock_data_status() -> dict:
    return _mock_status()


@app.post("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def inject_mock_data() -> dict:
    STORE.remove_source("mock")                 # injecting twice doesn't duplicate it
    STORE.add(mock_data.MOCK_CATALOG, mock_data.MOCK_AGREEMENTS, source="mock")
    return _mock_status()


@app.delete("/dev/mock-data", response_model=MockDataStatus, tags=["dev"])
def clear_mock_data() -> dict:
    STORE.remove_source("mock")                 # real (ASSIST) pathways stay loaded
    return _mock_status()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
