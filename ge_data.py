"""
GE pathway data:  data files -> adapters -> GEStandard / CollegeGEMap -> GERegistry -> /generate-sep.

Two layers, kept apart (sep_engine/ge.py plans with them):
  GE STANDARD   what a pathway requires: areas, course counts, UC/CSU differences, AP rules. Statewide.
  COLLEGE MAP   which of ONE college's courses count toward which area, for one academic year.
Only the selected college's own map is used: a Porterville list never supplies Chabot courses.

DISCOVERY. Every *.json in the GE data folders (ap_engine/data/ge/ and ap_engine/data/) is recognized
by its SHAPE, never its name:
  IGETC standards    {"traditional_igetc": {"areas"}, "external_exam_credit": {"ap"}}           -> standard
  GE standard        {"kind": "ge_standard", "ge_pathway": {"id", "requirements"}, "ap_rules"}  -> standard
  college GE list    {"institution": {"id"}, "ge_pathway": {"id", "requirements"}, "courses"}   -> college map
                     (ASSIST's layout). Its area rules are the statewide pattern, so when no standard file
                     exists for that pathway they become its standard (without the college's courses).
  college AP chart   {"institution_id", "rules": [{"exam_id", "waives", "cal_getc": "3A or 3B"}]}
                     -> the college's published AP -> area rules, one per pathway column
  AP exam index      {"exams": [{"id", "name"}]}                                               -> exam names
Anything else is ignored (ASSIST agreements and prerequisites belong to pathway_data.py).
GET /ge-pathways lists each file's role, and POST /pathways/reload re-reads the folders.

LOOKUP (context): the pathway (any alias: "CAL-GETC" = "cal_getc"), its standard, the target system
(UC / CSU), the college's map for the plan's academic year (the exact year, else the nearest one, with a
note), and AP credit from the standard's own AP rules, else the college's published rules for that
pathway, else none (with a note).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from institutions import REGISTRY, InstitutionRegistry
from pathway_data import _year_distance, academic_year_of
from sep_engine.ge import (SYSTEMS, APCredit, APRule, CollegeGEMap, GEChoice, GEContext, GECourse, GERequirement,
                           GEStandard)


def _pkey(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def ap_exam_key(text: str) -> str:
    """'AP U.S. History', 'ap:united_states_history', 'U.S. History' -> 'united states history'."""
    t = str(text).lower().strip()
    t = re.sub(r"^ap[:\s]+", "", t).replace("&", " and ").replace("/", " ").replace("_", " ")
    t = re.sub(r"\bu\.?\s*s\.?(?=\s|$)", "united states", t)
    return " ".join(w for w in re.findall(r"[a-z0-9]+", t) if w not in ("and", "exam"))


def _systems(values: Iterable[str] | None) -> frozenset[str]:
    """['CSU', 'UC'] or ['California State University (CSU)', ...] -> {'CSU', 'UC'}."""
    out = set()
    for v in values or ():
        v = str(v).upper()
        out |= {s for s in SYSTEMS if re.search(rf"\b{s}\b", v)}
    return frozenset(out)


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_SEASONS = {"F": "Fall", "FA": "Fall", "SP": "Spring", "S": "Spring", "SU": "Summer"}


def _term(code: str | None) -> str | None:
    """ASSIST effective terms: 'F2025' -> 'Fall 2025', 'Su2026' -> 'Summer 2026'."""
    m = re.fullmatch(r"\s*([A-Za-z]{1,2})\s*(\d{4})\s*", code or "")
    return f"{_SEASONS[m[1].upper()]} {m[2]}" if m and m[1].upper() in _SEASONS else None


def _prerequisite(text: str | None, ids: Iterable[str] | None) -> dict:
    """Catalog prerequisite wording -> the GECourse fields. The ids mention every course named, AND or OR,
    so the check is 'at least one of them'; a placement alternative makes it advisory only."""
    ids = tuple(dict.fromkeys(i for i in ids or () if i))
    if not text or not ids:
        return {"prereq_text": text or None}
    return {"prereq_any": ids, "prereq_text": text,
            "prereq_blocking": not re.search(r"placement|assessment|multiple measures", text, re.I),
            "prereq_concurrent": bool(re.search(r"concurrent", text, re.I))}


def _discipline(course: dict) -> str:
    """Subject for "courses from two disciplines". Common Course Numbering renamed some subjects
    (PSYC C1000 was PSY 1), so a former code's subject wins: PSYC C1000 and PSY 2 are one discipline."""
    former = [f for f in course.get("former_codes") or [] if f and f.split()]
    return former[0].split()[0] if former else course.get("subject") or course["code"].split()[0]


def _count_words(text: str | None, noun: str) -> int | None:
    m = re.search(rf"\b(one|two|three|four|five|\d)\s+(?:different\s+)?(?:academic\s+)?{noun}", text or "", re.I)
    return None if not m else int(m[1]) if m[1].isdigit() else _NUMBERS[m[1].lower()]


def _rule(count: int, units=None, disciplines: int = 1, extra: str = "") -> str:
    text = f"{count} course{'s' if count != 1 else ''}"
    if disciplines > 1:
        text += f" from at least {disciplines} disciplines"
    if units:
        text += f" ({units:g} semester units minimum)"
    return text + (f"; {extra}" if extra else "") + "."


@dataclass(frozen=True)
class PathwayIdentity:
    id: str
    name: str
    aliases: tuple[str, ...] = ()


# Names only (the rules always come from data files): lets requests use any familiar spelling and lets
# the form list a pathway whose data is missing ("GE pathway data is not loaded.").
KNOWN_PATHWAYS = (
    PathwayIdentity("cal_getc", "Cal-GETC", ("CAL-GETC", "Cal GETC", "CALGETC",
                                             "California General Education Transfer Curriculum")),
    PathwayIdentity("igetc", "IGETC", ("Intersegmental General Education Transfer Curriculum",)),
    PathwayIdentity("seven_course_pattern", "UC 7-Course Pattern", ("7-Course Pattern", "Seven-Course Pattern",
                                                                     "UC seven-course pattern", "7 course pattern")),
)


class GERegistry:
    def __init__(self, institutions: InstitutionRegistry = REGISTRY) -> None:
        self.institutions = institutions
        self.clear()

    def clear(self) -> None:
        self.pathways: dict[str, PathwayIdentity] = {p.id: p for p in KNOWN_PATHWAYS}
        self.standards: dict[str, GEStandard] = {}
        self.maps: list[CollegeGEMap] = []
        self.ap_charts: dict[tuple[str, str], tuple[tuple[APRule, ...], str]] = {}
        self.disciplines: dict[str, dict[str, str]] = {}      # institution -> exam key -> subject
        self.exams: dict[str, str] = {}                       # exam key -> exam name
        self._exam_alias: dict[str, str] = {}
        self._derived: dict[str, list[tuple[str | None, str, tuple, GEStandard]]] = {}
        self.files: list[dict] = []

    # --- pathway names -------------------------------------------------------------------------
    def resolve(self, text: str | None) -> str | None:
        k = _pkey(text or "")
        if not k:
            return None
        for p in self.pathways.values():
            if k in {_pkey(p.id), _pkey(p.name), *map(_pkey, p.aliases)}:
                return p.id
        return None

    def key(self, text: str) -> str:
        """Comparison key for a GE pattern name (PathwayStore matches mock pathways with it)."""
        return self.resolve(text) or _pkey(text)

    def name(self, pathway: str) -> str:
        return self.pathways[pathway].name if pathway in self.pathways else pathway

    def _identity(self, pid: str, name: str | None) -> str:
        found = self.resolve(pid) or self.resolve(name)
        if found:
            return found
        self.pathways[pid] = PathwayIdentity(pid, name or pid)
        return pid

    # --- AP exam names ---------------------------------------------------------------------------
    def exam(self, text: str) -> str:
        """Canonical exam key for an exam named any way: exact name or id, else the one exam whose
        name contains every word ('World History' -> 'world history modern')."""
        k = ap_exam_key(text)
        if k in self.exams:
            return k
        if k in self._exam_alias:
            return self._exam_alias[k]
        words = set(k.split())
        wider = [e for e in self.exams if words <= set(e.split())]
        return wider[0] if len(wider) == 1 else k

    # --- loading ---------------------------------------------------------------------------------
    def load_directory(self, *dirs: Path) -> None:
        self.clear()
        docs: list[tuple[str, str, dict]] = []
        seen: dict[str, str] = {}
        for folder in dirs:
            for path in sorted(Path(folder).glob("*.json")):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    self.files.append({"file": path.name, "role": None, "status": "skipped",
                                       "reason": f"not readable JSON ({e})"})
                    continue
                kind = _kind(doc)
                if kind is None:
                    continue
                dataset = doc.get("dataset_id") or path.name
                if dataset in seen:
                    self.files.append({"file": path.name, "dataset_id": dataset, "role": kind, "status": "skipped",
                                       "reason": f"same dataset_id as {seen[dataset]}"})
                    continue
                seen[dataset] = path.name
                docs.append((kind, path.name, doc))
        order = ["exam_index", "ap_chart", "igetc_standard", "ge_standard", "college_list"]
        for kind, name, doc in sorted(docs, key=lambda d: order.index(d[0])):
            try:
                getattr(self, f"_load_{kind}")(doc, name)
            except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as e:
                self.files.append({"file": name, "dataset_id": doc.get("dataset_id"), "role": kind,
                                   "status": "skipped", "reason": f"{type(e).__name__}: {e}"})
        self._choose_derived_standards()

    def _report(self, file: str, doc: dict, role: str, **info) -> None:
        self.files.append({"file": file, "dataset_id": doc.get("dataset_id"), "role": role, "status": "loaded",
                           "reason": None, **info})

    def _load_exam_index(self, doc: dict, file: str) -> None:
        for e in doc["exams"]:
            key = ap_exam_key(e["id"])
            self.exams[key] = e["name"]
            self._exam_alias.setdefault(ap_exam_key(e["name"]), key)

    def _load_ap_chart(self, doc: dict, file: str) -> None:
        """A college's AP chart: exam -> the courses it waives, plus a GE-area column per pathway."""
        inst = self.institutions.resolve(doc["institution_id"])
        default = int(doc.get("default_min_score") or 3)
        columns: dict[str, list[APRule]] = {}
        subjects = self.disciplines.setdefault(inst, {})
        for rule in doc["rules"]:
            exam = self.exam(rule["exam_id"])
            waives = [*(rule.get("former_ids") or []), *(rule.get("waives") or [])]   # PSY 1 before PSYC C1000
            if waives:
                subjects[exam] = waives[0].split()[0]
            for column, value in rule.items():
                pathway = self.resolve(column)
                if not pathway or not isinstance(value, str) or not value.strip():
                    continue
                part = value.split(";")[0]                     # "3B; IGETC 6A": later parts name other patterns
                ids = tuple(re.findall(r"\b\d[A-Z]?\b", part))
                if not ids:
                    continue
                either = re.search(r"\bor\b", part, re.I) is not None
                columns.setdefault(pathway, []).append(APRule(
                    exam, self.exams.get(exam, rule["exam_id"]), int(rule.get("min_score") or default),
                    areas=() if either else ids, options=ids if either else (),
                    note="; ".join(rule.get("conditions") or []) or None))
        source = doc.get("catalog") or doc.get("source") or file
        for pathway, rules in columns.items():
            self.ap_charts[(inst, pathway)] = (tuple(rules), f"{source} ({self.name(pathway)} column)")
        self._report(file, doc, "college AP chart", pathway=sorted(columns), institution=inst, academic_year=None,
                     scope="college-specific", rules=True, courses=0)

    def _load_igetc_standard(self, doc: dict, file: str) -> None:
        scope = doc.get("scope") or {}
        pathway = self._identity(_pkey(scope.get("abbreviation") or "igetc"), scope.get("abbreviation"))
        general = doc.get("general_rules") or {}
        exception = str((general.get("multiple_area_use") or {}).get("exception") or "")
        reqs: list[GERequirement] = []
        for area in doc["traditional_igetc"]["areas"]:
            aid, aname = area["id"], area["name"]
            subs = area.get("subareas") or []
            scope_systems = _systems(area.get("transfer_scope")) or SYSTEMS
            per_system = {k.upper(): v for k, v in (area.get("requirements") or {}).items() if isinstance(v, dict)}
            dist = area.get("distribution_rule") or {}

            def req(rid, name, count, accepts, systems=scope_systems, **kw) -> GERequirement:
                return GERequirement(id=rid, label=f"Area {rid} · {name}", name=name, area_id=aid, area_name=aname,
                                     courses=count, accepts=frozenset(accepts), systems=frozenset(systems), **kw)

            if subs and all("courses_required" in s for s in subs):          # Area 1: a course in each subarea
                system_rules = "; ".join(f"{k}: {v.get('courses_required')} courses"
                                         + (f" ({', '.join(v['subareas_required'])})" if v.get("subareas_required") else "")
                                         for k, v in sorted(per_system.items()))
                for s in subs:
                    systems = _systems(s.get("required_for")) or _systems(s.get("transfer_scope")) or scope_systems
                    systems = {x for x in systems if "subareas_required" not in per_system.get(x, {})
                               or s["id"] in per_system[x]["subareas_required"]}
                    reqs.append(req(s["id"], s["name"], s["courses_required"], {s["id"]}, systems,
                                    min_units=s.get("minimum_semester_units"),
                                    rule=_rule(s["courses_required"], s.get("minimum_semester_units"),
                                               extra=f"Area {aid}: {system_rules}" if system_rules else ""),
                                    note=" ".join(s.get("notes") or []) or None))
            elif dist:                                                        # Areas 3 and 5
                named = {_pkey(s["name"]): s for s in subs}
                counted = 0
                for k, v in dist.items():
                    m = re.fullmatch(r"minimum_from_(\w+)|(\w+?)_courses", k)
                    if not m or not isinstance(v, int) or isinstance(v, bool):
                        continue
                    s = named.get(_pkey(m[1] or m[2]))
                    if s is None:
                        raise ValueError(f"Area {aid}: {k} names no subarea")
                    reqs.append(req(s["id"], s["name"], v, {s["id"]}, rule=_rule(v, extra=f"part of Area {aid}")))
                    counted += v
                rest = int(area.get("courses_required") or counted) - counted
                if rest > 0:
                    ids = re.findall(r"\b\d[A-Z]\b", str(dist.get("remaining_course") or ""))
                    if not ids:
                        raise ValueError(f"Area {aid}: {rest} more course(s) but no remaining_course areas")
                    names = " or ".join(next(s["name"] for s in subs if s["id"] == i) for i in ids)
                    reqs.append(req(aid, f"{names} (one more course)", rest, set(ids),
                                    rule=_rule(rest, extra=f"from {' or '.join(ids)}")))
                if dist.get("laboratory_required"):
                    lab = next(s for s in subs if "lab" in s["name"].lower())
                    reqs.append(req(lab["id"], lab["name"], 1, {lab["id"]}, shares_course=True,
                                    rule="A laboratory with one of the Area 5 courses.",
                                    note=str(dist.get("lab_must_correspond_to") or "").capitalize() or None))
            elif subs:                                                        # Area 6: language (UC)
                for s in subs:
                    systems = _systems(s.get("required_for")) or scope_systems
                    shares = bool(exception) and (_pkey(s["name"]) in _pkey(exception)
                                                  or _pkey(exception) in _pkey(s["name"]))
                    lote = doc.get("lote") or {}
                    note = s.get("proficiency_standard") or lote.get("proficiency_level")
                    if lote.get("accepted_methods_include"):
                        note = (f"{note} " if note else "") + "Can also be met without a college course: " \
                            + "; ".join(lote["accepted_methods_include"][2:6]) + "; and more (see a counselor)."
                    reqs.append(req(s["id"], s["name"], int(s.get("courses_required") or 1), {s["id"]}, systems,
                                    shares_course=shares, rule="Proficiency equal to two years of high-school study.",
                                    note=note))
            else:                                                             # Areas 2A and 4
                count = int(area["courses_required"])
                disciplines = int(area.get("minimum_academic_disciplines") or 1)
                units = area.get("minimum_semester_units")
                reqs.append(req(aid, aname, count, {aid}, min_disciplines=disciplines, min_units=None,
                                rule=_rule(count, units, disciplines)))
        ap = (doc.get("external_exam_credit") or {}).get("ap") or {}
        rules, unmatched = [], []
        for e in ap.get("exams") or []:
            areas = next((tuple(v) for k, v in e.items() if k.endswith("_areas")), ())
            options = next((tuple(v) for k, v in e.items() if k.endswith("_area_options")), ())
            exam = self.exam(e["exam"])
            if exam not in self.exams:
                unmatched.append(e["exam"])
            rules.append(APRule(exam, e["exam"], int(ap.get("minimum_score") or 3), areas, options))
        source = doc.get("source") or {}
        notes = [scope.get("note"), (doc.get("important_distinctions_for_software") or {}).get("historical_relevance"),
                 "IGETC for STEM is not planned: it applies only where a Transfer Model Curriculum authorizes it."]
        std = GEStandard(pathway=pathway, name=self.name(pathway), dataset_id=doc["dataset_id"],
                         source=f"{source.get('publisher', '')}, {source.get('document_title', '')} "
                                f"(version {source.get('version', '?')}, {source.get('date', '?')})",
                         systems=frozenset().union(*(r.systems for r in reqs)), requirements=tuple(reqs),
                         ap_rules=tuple(rules), ap_source=f"{doc['dataset_id']} (external_exam_credit.ap)",
                         historical=bool(scope.get("historical_standard")), notes=tuple(n for n in notes if n))
        self._add_standard(std, file, doc, extra={"ap_exams_without_a_current_exam": unmatched})

    def _load_ge_standard(self, doc: dict, file: str) -> None:
        gp = doc["ge_pathway"]
        pathway = self._identity(gp["id"], gp.get("display_name") or gp.get("name"))
        systems = _systems(gp.get("systems")) or SYSTEMS
        reqs = []
        for r in gp["requirements"]:
            numbered = re.match(r"^\d", r["id"]) is not None
            reqs.append(GERequirement(
                id=r["id"], label=r.get("label") or (f"Area {r['id']} · {r['name']}" if numbered else r["name"]),
                name=r["name"], area_id=r.get("area_id", r["id"]), area_name=r.get("area_name", r["name"]),
                courses=int(r["minimum_courses"]), accepts=frozenset(r.get("accepts") or [r["id"]]),
                systems=_systems(r.get("systems")) or systems, min_disciplines=int(r.get("minimum_disciplines") or 1),
                min_distinct_areas=int(r.get("minimum_distinct_areas") or 1),
                shares_course=bool(r.get("shares_course")),
                min_units=r.get("minimum_semester_units_each") or r.get("minimum_semester_units"),
                rule=r.get("rule", ""), note=r.get("note")))
        ap = doc.get("ap_rules")
        rules = None if ap is None else tuple(
            APRule(self.exam(e["exam_id"]), self.exams.get(self.exam(e["exam_id"]), e["exam_id"]),
                   int(e.get("minimum_score") or ap.get("minimum_score") or 3),
                   tuple(e.get("areas") or ()), tuple(e.get("area_options") or ()), e.get("note"))
            for e in ap.get("exams") or [])
        source = doc.get("source") or {}
        pages = source.get("catalog_pages")
        std = GEStandard(pathway=pathway, name=self.name(pathway), dataset_id=doc["dataset_id"],
                         source=(source.get("document_title") or source.get("publisher") or file)
                         + (f", pp. {', '.join(map(str, pages))}" if pages else ""),
                         systems=systems, requirements=tuple(reqs), ap_rules=rules,
                         ap_source=(ap or {}).get("source"), area_names=dict(gp.get("course_area_codes") or {}),
                         notes=tuple(doc.get("notes") or ()))
        self._add_standard(std, file, doc)

    def _add_standard(self, std: GEStandard, file: str, doc: dict, extra: dict | None = None) -> None:
        if std.pathway in self.standards:
            raise ValueError(f"a standard for {std.name} is already loaded ({self.standards[std.pathway].dataset_id})")
        self.standards[std.pathway] = std
        self._report(file, doc, "GE standard (requirement rules)", pathway=[std.pathway], institution=None,
                     academic_year=None, scope="statewide", rules=True, courses=0, **(extra or {}))

    def _load_college_list(self, doc: dict, file: str) -> None:
        inst_doc, gp, source = doc["institution"], doc["ge_pathway"], doc.get("source") or {}
        self.institutions.register(inst_doc["id"], inst_doc.get("name"))
        inst = self.institutions.resolve(inst_doc["id"])
        pathway = self._identity(gp["id"], gp.get("display_name") or gp.get("name"))
        year = source.get("academic_year")
        area_field = f"{gp['id']}_areas"
        listed: dict[str, set[str]] = {}
        for area in gp["requirements"]:
            for s in area.get("subareas") or []:
                for code in s.get("eligible_course_codes") or []:
                    listed.setdefault(code, set()).add(s["id"])
        courses: dict[str, GECourse] = {}
        for c in doc["courses"]:
            code = c["code"]
            areas = set(c.get("areas") or c.get(area_field) or []) | listed.pop(code, set())
            courses[code] = GECourse(
                code=code, title=c.get("title") or code, units=float(c.get("semester_units") or c.get("units") or 0),
                areas=frozenset(areas), discipline=_discipline(c),
                same_as=tuple(c.get("same_as") or ()), former_codes=tuple(c.get("former_codes") or ()),
                first_term=_term(c.get("effective_begin")), last_term=_term(c.get("effective_end")),
                **_prerequisite(c.get("prerequisite"), c.get("prerequisite_course_ids")))
        unlisted = sorted(listed)                        # in an area list but without a course record (no units)
        self.maps.append(CollegeGEMap(
            institution=inst, institution_name=inst_doc.get("name") or inst, pathway=pathway, academic_year=year,
            dataset_id=doc["dataset_id"],
            source=source.get("document_title") or source.get("publisher") or file, courses=courses))
        std = self._standard_from_list(doc, pathway, file)
        self._derived.setdefault(pathway, []).append((year, doc["dataset_id"], _signature(std), std))
        self._report(file, doc, "college GE course list", pathway=[pathway], institution=inst, academic_year=year,
                     scope="college-specific", rules=True, courses=len(courses),
                     **({"listed_without_course_record": unlisted} if unlisted else {}))

    def _standard_from_list(self, doc: dict, pathway: str, file: str) -> GEStandard:
        """The statewide area rules published with a college list (ASSIST layout), without its courses."""
        gp = doc["ge_pathway"]
        reqs = []
        for area in gp["requirements"]:
            subs = area.get("subareas") or []
            lab = area.get("laboratory_subarea") if area.get("laboratory_required") else None
            counted = [s for s in subs if s["id"] != lab]
            disciplines = int(area.get("minimum_disciplines") or _count_words(area.get("rule"), "disciplines") or 1)
            for s in subs:
                common = dict(label=f"Area {s['id']} · {s['name']}", name=s["name"], area_id=area["id"],
                              area_name=area["name"], accepts=frozenset({s["id"]}), systems=SYSTEMS,
                              rule=area.get("rule", ""))
                if s["id"] == lab:
                    reqs.append(GERequirement(id=s["id"], courses=1, shares_course=True, note=s.get("note"), **common))
                else:
                    count = int(s.get("minimum_courses") or area["minimum_courses"])
                    reqs.append(GERequirement(id=s["id"], courses=count,
                                              min_disciplines=disciplines if len(counted) == 1 else 1,
                                              min_units=s.get("minimum_semester_units"), **common))
        notes = [gp.get("transfer_scope_statement"),
                 "A course may count toward only one area, except a 5C lab listed with its 5A/5B course."]
        return GEStandard(pathway=pathway, name=self.name(pathway), dataset_id=f"{doc['dataset_id']}#area_rules",
                          source=f"Area rules published with {doc['dataset_id']}", systems=SYSTEMS,
                          requirements=tuple(reqs), notes=tuple(n for n in notes if n),
                          derived_from=(doc["dataset_id"],))

    def _choose_derived_standards(self) -> None:
        """A pathway with no standard file uses the area rules of its newest college list; lists that
        publish different rules are reported."""
        for pathway, found in self._derived.items():
            found.sort(key=lambda f: (-(int(f[0][:4]) if f[0] else 0), f[1]))
            year, dataset, signature, std = found[0]
            same = tuple(d for _, d, sig, _ in found if sig == signature)
            differs = [d for _, d, sig, _ in found if sig != signature]
            if pathway not in self.standards:
                self.standards[pathway] = GEStandard(**{**std.__dict__, "derived_from": same,
                                                        "source": f"Area rules published with {', '.join(same)}"})
            for row in self.files:
                if row.get("dataset_id") in differs:
                    row["reason"] = f"its area rules differ from {dataset}'s; {dataset}'s are used"

    # --- lookup ----------------------------------------------------------------------------------
    def college_map(self, institution: str, pathway: str, year: str | None) -> tuple[CollegeGEMap | None, str | None]:
        found = [m for m in self.maps if m.institution == institution and m.pathway == pathway]
        if not found:
            return None, None
        best = min(found, key=lambda m: _year_distance(m.academic_year, year))
        note = None
        if year and best.academic_year and best.academic_year != year:
            note = (f"{best.institution_name}'s {self.name(pathway)} course list is for {best.academic_year}; "
                    f"no {year} list is loaded, so that list is used. Approved courses can change each year.")
        return best, note

    def context(self, pathway: str, college: str, university: str, system: str, start_term: str | None,
                ap_scores: Iterable[tuple[str, int]] = (), choices: Iterable[GEChoice] = ()) -> GEContext:
        pid = self.resolve(pathway) or pathway
        name = self.name(pid)
        standard = self.standards.get(pid)
        inst = self.institutions.resolve(college)
        college_name = self.institutions.name(inst) or college
        year = academic_year_of(start_term)
        cmap, note = self.college_map(inst, pid, year)
        if standard and standard.ap_rules is not None:
            rules, ap_source = standard.ap_rules, standard.ap_source
        else:
            rules, ap_source = self.ap_charts.get((inst, pid), (None, None))
        credits, ap_note = [], None
        scores = list(ap_scores)
        if scores and standard is not None and rules is None:
            ap_note = (f"No AP rules for {name} are loaded for {college_name}, so AP scores don't reduce "
                       f"the {name} requirements.")
        for subject, score in scores if rules else ():
            exam = self.exam(subject)
            for rule in rules:
                if rule.exam == exam and score >= rule.min_score:
                    credits.append(APCredit(subject, score, rule.areas, rule.options,
                                            self.disciplines.get(inst, {}).get(exam, exam)))
        return GEContext(pathway=pid, pathway_name=name, target_system=system, target_name=university,
                         college_name=college_name, standard=standard, college_map=cmap, requested_year=year,
                         map_note=note, ap_credits=tuple(credits), ap_source=ap_source if rules else None,
                         ap_note=ap_note, choices=tuple(choices))

    def listing(self) -> dict:
        """GET /ge-pathways: every pathway (loaded or not), its rules and college lists, and every file's role."""
        rows = []
        for p in self.pathways.values():
            std = self.standards.get(p.id)
            counts = {s: sum(r.courses for r in std.requirements_for(s) if not r.shares_course)
                      for s in sorted(std.systems)} if std else {}
            rows.append({
                "id": p.id, "name": p.name, "aliases": list(p.aliases), "loaded": std is not None,
                "systems": sorted(std.systems) if std else [], "courses_by_system": counts,
                "historical": bool(std and std.historical),
                "standard": None if std is None else {"dataset_id": std.dataset_id, "source": std.source,
                                                      "derived_from": list(std.derived_from),
                                                      "ap_rules": std.ap_rules is not None},
                "college_lists": [{"institution": m.institution, "institution_name": m.institution_name,
                                   "academic_year": m.academic_year, "dataset_id": m.dataset_id,
                                   "courses": len(m.courses)} for m in self.maps if m.pathway == p.id],
                "college_ap_rules": sorted(inst for inst, pid in self.ap_charts if pid == p.id),
            })
        return {"pathways": rows, "files": self.files}


def _kind(doc: object) -> str | None:
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("traditional_igetc"), dict):
        return "igetc_standard"
    gp = doc.get("ge_pathway")
    if isinstance(gp, dict) and isinstance(gp.get("requirements"), list):
        if doc.get("kind") == "ge_standard":
            return "ge_standard"
        if isinstance(doc.get("institution"), dict) and isinstance(doc.get("courses"), list):
            return "college_list"
    rules = doc.get("rules")
    if isinstance(doc.get("institution_id"), str) and isinstance(rules, list) \
            and any(isinstance(r, dict) and "exam_id" in r for r in rules):
        return "ap_chart"
    exams = doc.get("exams")
    if isinstance(exams, list) and exams and all(isinstance(e, dict) and {"id", "name"} <= e.keys() for e in exams):
        return "exam_index"
    return None


def _signature(std: GEStandard) -> tuple:
    return tuple(sorted((r.id, r.courses, tuple(sorted(r.accepts)), r.shares_course, r.min_disciplines)
                        for r in std.requirements))
