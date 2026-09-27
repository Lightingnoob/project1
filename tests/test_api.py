"""API tests for main.py. Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import main
from pathway_data import PathwayStore, academic_year_of, split_major

BODY = {"college": "Chabot College", "university": "UC Berkeley", "major": "Computer Science",
        "completed_courses": ["CS 1", "ENGL 1A"], "no_completed_courses": False, "ap_scores": [], "no_ap_scores": True,
        "ge_pathway": "CAL-GETC", "start_term": "Fall 2026", "include_summer": False}

# Exactly what ui.js sends for Chabot College -> Cal State East Bay -> Computer Science, no courses, no AP
CSUEB = {"college": "Chabot College", "university": "Cal State East Bay", "major": "Computer Science",
         "completed_courses": [], "no_completed_courses": True, "ap_scores": [], "no_ap_scores": True,
         "ge_pathway": "CAL-GETC", "start_term": "Fall 2026", "include_summer": False}
AP_CS_A = {"subject": "AP Computer Science A", "score": 4}


def answered(base: dict, **overrides) -> dict:
    """A complete request. Unless a test sets them, the explicit "none" answers follow the lists, and AP
    waivers come with the AP score that ui.js would have evaluated (validation itself is tested separately)."""
    body = {**base, **overrides}
    if "no_completed_courses" not in overrides:
        body["no_completed_courses"] = not body["completed_courses"]
    if body.get("waived_courses") and "ap_scores" not in overrides:
        body["ap_scores"], body["no_ap_scores"] = [AP_CS_A], False
    return body


class APITests(unittest.TestCase):
    """Mock-data tests start from an EMPTY store (no ASSIST files), so they don't depend on ap_engine/data."""

    def setUp(self):
        main.STORE.clear()
        self.client = TestClient(main.app)

    def post(self, **overrides):
        return self.client.post("/generate-sep", json=answered(BODY, **overrides))

    def test_empty_store_has_no_pathways(self):
        r = self.post()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], main.NOT_UPLOADED)
        self.assertEqual(self.client.get("/colleges/Chabot College/catalog").status_code, 404)
        self.assertFalse(self.client.get("/dev/mock-data").json()["loaded"])

    def test_inject_then_generate(self):
        self.assertTrue(self.client.post("/dev/mock-data").json()["loaded"])
        for ge in ("CAL-GETC", "7-Course Pattern"):
            r = self.post(ge_pathway=ge)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([p["label"] for p in r.json()["plans"]], ["Fastest Route", "Balanced Workload"])
        self.assertEqual(self.post(major="Computer Engineering").status_code, 404)
        self.client.delete("/dev/mock-data")
        self.assertEqual(self.post().status_code, 404)

    def test_real_terms_start_term_and_spring_end(self):
        self.client.post("/dev/mock-data")
        for start, summer in [("Fall 2026", False), ("Spring 2027", False), ("Spring 2027", True), ("Summer 2027", True)]:
            data = self.post(start_term=start, include_summer=summer).json()
            self.assertEqual(data["pathway"]["start_term"], start)
            for plan in data["plans"]:
                terms = [s["term"] for s in plan["semesters"]]
                self.assertEqual(terms[0], start)
                self.assertTrue(terms[-1].startswith("Spring"), terms)
                self.assertEqual(any(t.startswith("Summer") for t in terms), summer and len(terms) > 1 or start.startswith("Summer"))
                for s in plan["semesters"]:
                    if s["season"] == "Summer":
                        self.assertLessEqual(s["units"], 9)
                        self.assertTrue(all(c["is_ge"] for c in s["courses"]))

    def test_completed_course_satisfies_its_prerequisite_chain(self):
        self.client.post("/dev/mock-data")
        data = self.post(completed_courses=["CS 20"]).json()           # CS 1 -> CS 2 -> CS 20
        self.assertEqual(data["pathway"]["completed_courses"], ["CS 20"])
        self.assertEqual(data["pathway"]["inferred_courses"], ["CS 1", "CS 2"])
        for plan in data["plans"]:
            planned = {c["code"] for s in plan["semesters"] for c in s["courses"]}
            self.assertFalse(planned & {"CS 1", "CS 2", "CS 20"})
        # AP credit (waived) counts as done but does not imply its prerequisites
        waived = self.post(waived_courses=["CS 2"], completed_courses=[]).json()
        self.assertEqual(waived["pathway"]["waived_courses"], ["CS 2"])
        planned = {c["code"] for s in waived["plans"][0]["semesters"] for c in s["courses"]}
        self.assertIn("CS 1", planned)
        self.assertNotIn("CS 2", planned)

    def test_summer_start_requires_summer(self):
        self.client.post("/dev/mock-data")
        self.assertEqual(self.post(start_term="Summer 2027", include_summer=False).status_code, 422)
        self.assertEqual(self.post(start_term="Autumn 2026").status_code, 422)

    def test_ap_data_and_engine_are_served(self):
        data = self.client.get("/colleges/chabot college/ap-data").json()
        self.assertEqual(set(data), {"cc_waivers", "articulation", "campus_data"})
        self.assertEqual(data["cc_waivers"]["institution_id"], "chabot")
        # ui.js maps the target-university names to AP campus ids by name
        names = {c["name"] for c in data["campus_data"]["campuses"]}
        self.assertTrue({"UC Berkeley", "UCLA", "UC San Diego"} <= names)
        self.assertEqual(self.client.get("/colleges/Laney College/ap-data").status_code, 404)
        for path in ("/ap_engine/apWaiver.js", "/ap_engine/graph.js"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("javascript", r.headers["content-type"])

    def test_request_matches_ui_js(self):
        # ui.js buildRequest() sends exactly these fields (the AP answer too); anything else is rejected
        self.client.post("/dev/mock-data")
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post(ap_scores=[AP_CS_A], no_ap_scores=False).status_code, 200)
        self.assertEqual(self.post(apScores=[]).status_code, 422)

    def search(self, q, college="Chabot College", limit=20):
        return self.client.get(f"/colleges/{college}/courses", params={"q": q, "limit": limit})

    def codes(self, q, **kw):
        return [c["code"] for g in self.search(q, **kw).json()["groups"] for c in g["courses"]]

    def subjects(self, q):
        return [(s["code"], s["match"]) for s in self.search(q).json()["subjects"]]

    def test_course_search_resolves_the_subject_first(self):
        # Level 1: mth / math / mathematics (any case) all name Mathematics (MTH)
        self.assertEqual(self.subjects("mth"), [("MTH", "code")])
        self.assertEqual(self.subjects("mathematics"), [("MTH", "name")])
        for q in ("math", "MATH", "Math"):
            self.assertEqual(self.subjects(q), [("MTH", "alias")], q)
        # Level 2: the same Mathematics courses in course-number order, not "math" in titles
        first = ["MTH 1", "MTH 2", "MTH 3", "MTH 4", "MTH 6", "MTH 8"]
        for q in ("mth", "math", "mathematics"):
            self.assertEqual(self.codes(q)[:6], first, q)
            self.assertTrue(all(c.startswith("MTH ") for c in self.codes(q)), q)   # no ECD 32 / LNSK 119
        # One header per subject, with the subject's full course count
        group = self.search("math").json()["groups"][0]
        self.assertEqual((group["code"], group["name"], group["count"]), ("MTH", "Mathematics", 49))
        self.assertEqual(len(group["courses"]), 20)
        # Subject + course number, any spacing
        for q in ("mth 1", "mth1", "MTH1", "MTH 1", "math 1", "mathematics 1"):
            self.assertEqual(self.codes(q)[0], "MTH 1", q)
        self.assertEqual(self.codes("math 2")[0], "MTH 2")
        self.assertEqual(self.codes("math 4")[0], "MTH 4")
        # Every subject works the same way, with names read from the catalog
        self.assertEqual(self.subjects("physics"), [("PHYS", "name")])
        self.assertEqual(self.codes("phys"), self.codes("physics"))
        self.assertTrue(all(c.startswith("PHYS ") for c in self.codes("phys")))
        self.assertEqual(self.subjects("chemistry"), [("CHEM", "name")])
        self.assertEqual(self.subjects("biology"), [("BIOS", "alias")])          # Chabot calls it Life Sciences
        self.assertEqual(self.subjects("cs"), [("CSCI", "alias")])
        self.assertEqual(self.codes("cs 1"), ["CSCI 10", "CSCI 14", "CSCI 15", "CSCI 19A"])
        self.assertEqual(self.subjects("comp sci"), [("CSCI", "alias")])
        self.assertEqual(self.subjects("computer")[0], ("CSCI", "name prefix"))   # before Computer Application Systems

    def test_course_search_titles_and_codes_rank_below_subjects(self):
        # No subject named "calculus": title search is used
        self.assertEqual(self.subjects("calculus"), [])
        self.assertEqual(self.codes("calculus")[:3], ["MTH 1", "MTH 2", "MTH 3"])
        self.assertTrue(all(g["courses"][0]["match"] == "title" for g in self.search("calculus").json()["groups"]))
        # Course-number search across subjects, grouped by subject
        data = self.search("C1000").json()
        self.assertEqual([g["code"] for g in data["groups"]], ["COMM", "ENGL", "POLS", "PSYC", "STAT"])
        self.assertEqual(self.codes("engl c1000"), ["ENGL C1000"])
        # Several subjects: each gets its own group, fairly shared
        groups = self.search("science").json()["groups"]
        self.assertGreater(len(groups), 3)
        # Real catalog fields; junk, empty text and colleges without a catalog
        course = self.search("CSCI 14").json()["groups"][0]["courses"][0]
        self.assertEqual(course, {"code": "CSCI 14", "title": "Introduction to Structured Programming In C++",
                                  "units": "4 units", "college": "Chabot College",
                                  "sources": ["2025-2026 catalog"], "match": "code"})
        self.assertEqual(self.search("SDESDEDSF").json()["total"], 0)
        self.assertEqual(self.search("").json()["groups"], [])
        self.assertEqual(self.search("CS", college="Laney College").status_code, 404)

    def test_several_categories_and_the_subject_filter(self):
        # "comp" / "computer": two separate official departments, each its own category
        for q in ("comp", "computer"):
            groups = self.search(q).json()["groups"]
            self.assertEqual([(g["code"], g["name"]) for g in groups],
                             [("CSCI", "Computer Science"), ("CAS", "Computer Application Systems")], q)
        self.assertEqual(self.subjects("chem"), [("CHEM", "code")])
        self.assertEqual(self.search("cis").json()["groups"], [])       # Chabot has no CIS department
        # Filter to one subject: q='' lists it, a number or a title searches inside it
        inside = lambda q, s="MTH": self.client.get("/colleges/Chabot College/courses",
                                                    params={"q": q, "subject": s, "limit": 100}).json()
        everything = inside("")
        self.assertEqual(everything["filter"], {"code": "MTH", "name": "Mathematics", "count": 49})
        self.assertEqual(len(everything["groups"][0]["courses"]), 49)
        self.assertEqual([c["code"] for c in inside("1")["groups"][0]["courses"]][:2], ["MTH 1", "MTH 15"])
        self.assertEqual(inside("1")["groups"][0]["courses"][0]["match"], "code")
        self.assertEqual([c["code"] for c in inside("calculus")["groups"][0]["courses"]][:3], ["MTH 1", "MTH 2", "MTH 3"])
        self.assertEqual(inside("math 1")["groups"][0]["courses"][0]["code"], "MTH 1")   # subject words ignored
        self.assertEqual(inside("zzz")["groups"], [])
        self.assertEqual(self.search("1", college="Chabot College").status_code, 200)
        bad = self.client.get("/colleges/Chabot College/courses", params={"q": "", "subject": "NOPE"})
        self.assertEqual(bad.status_code, 422)

    def test_subject_aliases_follow_the_name_not_the_code(self):
        # Another college whose Mathematics department uses a different code keeps its own codes
        import course_search as cs
        courses = [cs.CourseEntry(code=c, title=t, units="3 units", subject=None, sources=("test",))
                   for c, t in [("MAT 1", "Calculus I"), ("MAT 2", "Calculus II"), ("ECD 32", "Math for Kids")]]
        other = cs.Catalog("Other College", courses,
                           cs.build_subjects(courses, {"MAT": "Mathematics", "ECD": "Early Childhood"}))
        found = cs.search(other, "math 2", 20)
        self.assertEqual([(t, s.code) for t, s in found["subjects"]], [(3, "MAT")])
        self.assertEqual([c.code for c, _ in found["results"]], ["MAT 2"])

    def test_course_search_includes_uploaded_pathway_courses(self):
        self.client.post("/dev/mock-data")
        self.assertEqual(self.codes("CS 1")[0], "CS 1")                             # mock-only course
        both = self.search("PHYS 4A").json()["groups"][0]["courses"][0]
        self.assertEqual(both["sources"], ["2025-2026 catalog", "transfer pathway data"])

    def test_frontend_is_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("javascript", self.client.get("/ui.js").headers["content-type"])
        html = self.client.get("/").text
        # Styles, fonts, icons and graph libs are self-hosted so the page never renders unstyled offline
        for path in ("assets/tailwind.css", "assets/vendor/inter/inter.css",
                     "assets/vendor/fontawesome/css/all.min.css", "assets/vendor/cytoscape/cytoscape.min.js"):
            self.assertIn(path, html)
            self.assertEqual(self.client.get("/" + path).status_code, 200, path)
        self.assertLess(html.index("fontawesome/css/all.min.css"), html.index("assets/tailwind.css"),
                        "Tailwind must load after Font Awesome so utilities like `hidden` win")


class AssistPathwayTests(unittest.TestCase):
    """The real ASSIST agreements in ap_engine/data/, loaded exactly as at server startup."""

    def setUp(self):
        main.STORE.clear()
        main.load_pathways()
        self.client = TestClient(main.app)

    def tearDown(self):
        main.STORE.clear()

    def post(self, **overrides):
        return self.client.post("/generate-sep", json=answered(CSUEB, **overrides))

    def ok(self, **overrides) -> dict:
        r = self.post(**overrides)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def planned(plan) -> list[str]:
        return [c["code"] for s in plan["semesters"] for c in s["courses"]]

    @staticmethod
    def term_of(plan) -> dict[str, int]:
        return {c["code"]: s["index"] for s in plan["semesters"] for c in s["courses"]}

    def test_ui_request_finds_the_chabot_csueb_cs_agreement(self):
        data = self.ok()
        self.assertEqual(data["pathway"]["university"], "California State University, East Bay")
        self.assertEqual(data["pathway"]["degree"], "Computer Science, B.S.")
        a = data["articulation"]
        self.assertEqual(a["dataset_id"], "chabot_csueb_computer_science_bs_2026_2027")
        self.assertEqual((a["sending"]["id"], a["receiving"]["id"]), ("chabot", "csueb"))
        self.assertEqual((a["scope"], a["complete_degree_requirements"], a["program_total_units"]),
                         ("lower_division_core", False, 120))
        self.assertEqual([r["targets"][0]["code"] for r in a["requirements"]],
                         ["CS 101", "CS 201", "CS 211", "CS 221", "MATH 130", "MATH 131", "PHYS 135"])
        for plan in data["plans"]:
            # Chabot courses only, never CSUEB course numbers
            self.assertTrue(set(self.planned(plan)) <= set(main.STORE.catalog_for("Chabot College")))
            self.assertTrue(set(self.planned(plan)) >= {"CSCI 20", "MTH 8", "CSCI 21", "MTH 1", "MTH 2"})

    def test_every_spelling_resolves_to_the_same_pathway(self):
        for college, university, major in [("Chabot", "CSUEB", "Computer Science, B.S."),
                                           ("chabot", "California State University, East Bay", "computer science bs"),
                                           ("Chabot College", "CSU East Bay", "Computer Science (B.S.)"),
                                           ("  CHABOT COLLEGE ", "csueb", "Computer  Science")]:
            data = self.ok(college=college, university=university, major=major)
            self.assertEqual(data["articulation"]["dataset_id"], "chabot_csueb_computer_science_bs_2026_2027")
        self.assertEqual(self.post(major="Computer Science, B.A.").status_code, 404)   # degree must match
        self.assertEqual(self.post(university="UC Berkeley").status_code, 404)
        self.assertEqual(self.post(major="Mathematics").status_code, 404)
        self.assertEqual(split_major("Computer Science, B.S."), ("Computer Science", "B.S."))
        self.assertEqual(split_major("Computer Science"), ("Computer Science", None))

    def test_or_requirements_take_exactly_one_option(self):
        for plan in self.ok()["plans"]:
            planned = set(self.planned(plan))
            self.assertEqual(len(planned & {"CSCI 15", "CSCI 19A"}), 1, planned)
            self.assertEqual(len(planned & {"PHYS 4A", "PHYS 7A"}), 1, planned)
            groups = {r["id"]: r for r in plan["requirements"]}
            self.assertEqual((groups["requirement:CS101"]["choose"], len(groups["requirement:CS101"]["courses"])), (1, 1))
            self.assertEqual(groups["requirement:PHYS135"]["choose"], 1)

    def test_completed_mth2_is_not_scheduled_and_implies_mth1(self):
        base = self.ok(completed_courses=["MTH 2"])
        self.assertEqual(base["pathway"]["inferred_courses"], ["MTH 1"])
        for plan in base["plans"]:
            planned = set(self.planned(plan))
            self.assertFalse(planned & {"MTH 1", "MTH 2"})
            nodes = {n["data"]["label"]: n["data"] for n in plan["graph"]["nodes"]}
            self.assertEqual((nodes["MTH 2"]["status"], nodes["MTH 1"]["satisfied_by"]), ("completed", "MTH 2"))
        for spelling in ("MTH2", "mth 2", "chabot:MTH2"):          # same canonical course
            data = self.ok(completed_courses=[spelling])
            self.assertEqual(data["pathway"]["inferred_courses"], ["MTH 1"], spelling)
            self.assertEqual([self.planned(p) for p in data["plans"]], [self.planned(p) for p in base["plans"]])

    def test_cross_listed_mth8_and_csci28_are_one_course(self):
        for entered in (["CSCI 28"], ["MTH 8"], ["MTH 8", "CSCI 28"]):
            data = self.ok(completed_courses=entered)
            self.assertFalse(any("ignored" in w for w in data["warnings"]), data["warnings"])
            for plan in data["plans"]:
                self.assertFalse({"MTH 8", "CSCI 28"} & set(self.planned(plan)), entered)
                codes = [c["code"] for r in plan["requirements"] for c in r["courses"]]
                self.assertEqual(codes.count("MTH 8"), 1)
                self.assertNotIn("CSCI 28", codes)
        catalog = self.client.get("/colleges/Chabot College/catalog").json()
        self.assertEqual(catalog["CSCI 28"]["title"], catalog["MTH 8"]["title"])   # UI tag counts as known

    def test_no_course_articulated_requirements_are_left_out(self):
        # CS 230 and MATH 225 have no Chabot course: not planned, not listed, not mentioned
        data = self.ok()
        text = json.dumps(data)
        self.assertNotIn("CS 230", text)
        self.assertNotIn("MATH 225", text)
        self.assertNotIn("No Course Articulated", text)
        self.assertTrue(all(r["options"] for r in data["articulation"]["requirements"]))

    def test_ap_scores_decide_supporting_courses_without_asking(self):
        # No AP score: no AP credit, so CSCI 14 and its prerequisite MTH 55 are planned; nothing asks about AP
        plain = self.ok()
        for plan in plain["plans"]:
            self.assertTrue({"CSCI 14", "MTH 55"} <= set(self.planned(plan)))
        self.assertTrue(any("MTH 55 (for CSCI 14)" in w for w in plain["warnings"]))
        self.assertFalse(any("mark it completed" in w for w in plain["warnings"]))
        # AP Computer Science A: ap_engine waives CSCI 14, which ui.js sends as waived_courses
        ap = self.ok(waived_courses=["CSCI 14"])
        self.assertEqual(ap["pathway"]["waived_courses"], ["CSCI 14"])
        for plan in ap["plans"]:
            planned = set(self.planned(plan))
            self.assertFalse({"CSCI 14", "MTH 55"} & planned)             # MTH 55 was only needed for CSCI 14
            self.assertTrue({"CSCI 15", "CSCI 21"} <= planned)
        self.assertFalse(any("MTH 55" in w or "(for )" in w for w in ap["warnings"]))

    def test_prerequisites_come_from_the_chabot_registry(self):
        for plan in self.ok()["plans"]:
            t = self.term_of(plan)
            self.assertLess(t["MTH 1"], t["MTH 2"])
            self.assertLess(t["MTH 1"], t["MTH 8"])
            self.assertLess(t["MTH 55"], t["CSCI 14"])
            self.assertLess(t["CSCI 14"], t["CSCI 15"])
            self.assertLess(t["CSCI 15"], t["CSCI 20"])
            physics = "PHYS 4A" if "PHYS 4A" in t else "PHYS 7A"
            self.assertLess(t["MTH 1"], t[physics])
            self.assertLessEqual(t["MTH 2"], t[physics])                # MTH 2 may be concurrent
            edges = {e["data"]["id"]: e["data"]["relation"] for e in plan["graph"]["edges"]}
            self.assertEqual(edges[f"e_MTH2_{physics.replace(' ', '')}"], "coreq")

    def test_missing_prerequisite_data_is_not_a_missing_pathway(self):
        # The agreement as exported from ASSIST: prerequisite_data {"status": "not_provided_in_source", "edges": null}
        doc = json.loads((main.DATA_DIRS[0] / "computer_science.json").read_text(encoding="utf-8"))
        doc["prerequisite_data"] = {"status": "not_provided_in_source", "edges": None}
        registry = (main.DATA_DIRS[0] / "chabot_prerequisites.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "chabot_csueb_computer_science_2026_2027(1).json").write_text(json.dumps(doc), encoding="utf-8")
            alone = PathwayStore()
            alone.load_directory(Path(tmp))                              # no registry at all: still available
            match = alone.find("Chabot College", "Cal State East Bay", "Computer Science", "CAL-GETC", "Fall 2026")
            self.assertIsNotNone(match)
            self.assertIn("CSCI 20", match.pathway.advisories)          # flagged, not silently prerequisite-free
            Path(tmp, "chabot_prerequisites.json").write_text(registry, encoding="utf-8")
            both = PathwayStore()
            both.load_directory(Path(tmp))                               # with the Chabot registry: same courses as ours
            ours = main.STORE.find("Chabot", "CSUEB", "Computer Science", "CAL-GETC", "Fall 2026").pathway
            theirs = both.find("Chabot", "CSUEB", "Computer Science", "CAL-GETC", "Fall 2026").pathway
            self.assertEqual({c: (x.prereqs, x.coreqs) for c, x in theirs.catalog.items()},
                             {c: (x.prereqs, x.coreqs) for c, x in ours.catalog.items()})

    def test_fall_2026_uses_the_2026_2027_agreement(self):
        self.assertEqual([academic_year_of(t) for t in ("Fall 2026", "Spring 2027", "Summer 2027", "Fall 2027")],
                         ["2026-2027", "2026-2027", "2026-2027", "2027-2028"])
        data = self.ok(start_term="Fall 2026")
        self.assertEqual((data["articulation"]["academic_year"], data["articulation"]["requested_academic_year"]),
                         ("2026-2027", "2026-2027"))
        self.assertFalse(any("agreement is used" in w for w in data["warnings"]))
        later = self.ok(start_term="Fall 2027")                          # nearest loaded year, with a warning
        self.assertEqual(later["articulation"]["academic_year"], "2026-2027")
        self.assertTrue(any("2027-2028" in w for w in later["warnings"]))

    def test_any_ge_pattern_finds_the_major_prep_agreement(self):
        # The agreement is major prep only; GE comes from the chosen pathway's own data (GEPathwayTests)
        for ge, pid in (("CAL-GETC", "cal_getc"), ("7-Course Pattern", "seven_course_pattern"), ("IGETC", "igetc")):
            data = self.ok(ge_pathway=ge)
            self.assertEqual(data["pathway"]["ge_pathway_id"], pid)
            self.assertEqual(data["plans"][0]["ge"]["pathway"], pid)
            self.assertFalse([r for r in data["plans"][0]["requirements"] if r["category"] == "ge"])
            self.assertFalse(any("General education is not scheduled" in w for w in data["warnings"]))

    def test_unsupported_agreements_explain_themselves(self):
        listing = {(r["major"], r["status"]) for r in self.client.get("/pathways").json()["pathways"]}
        self.assertTrue({("Computer Science", "available"), ("Computer Engineering", "available"),
                         ("Physics", "unsupported"), ("Biochemistry", "unsupported")} <= listing)
        r = self.post(major="Physics")
        self.assertEqual(r.status_code, 422)
        self.assertIn("Select A or B", r.json()["detail"])
        self.assertEqual(self.post(major="Computer Engineering").status_code, 200)
        # The dropdown keeps one entry per institution: "Cal State East Bay" already is csueb
        unis = [u for names in self.client.get("/institutions").json()["universities"].values() for u in names]
        self.assertIn("Cal State East Bay", unis)
        self.assertNotIn("California State University, East Bay", unis)

    def test_new_assist_files_are_found_by_shape_and_reloaded(self):
        doc = json.loads((main.DATA_DIRS[0] / "computer_science.json").read_text(encoding="utf-8"))
        doc["dataset_id"], doc["agreement"]["academic_year"] = "test_2027_2028", "2027-2028"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "any file name.json").write_text(json.dumps(doc), encoding="utf-8")
            Path(tmp, "duplicate.json").write_text(json.dumps(doc), encoding="utf-8")
            Path(tmp, "broken.json").write_text("{not json", encoding="utf-8")
            store = PathwayStore()
            store.load_directory(Path(tmp))
            match = store.find("Chabot College", "Cal State East Bay", "Computer Science", "CAL-GETC", "Fall 2027")
            self.assertEqual((match.pathway.academic_year, match.warnings), ("2027-2028", []))
            self.assertEqual(sorted(s["file"] for s in store.skipped), ["broken.json", "duplicate.json"])

    def test_mock_data_leaves_real_pathways_loaded(self):
        self.client.post("/dev/mock-data")
        self.ok()
        self.assertEqual(self.client.post("/generate-sep", json=BODY).status_code, 200)
        self.client.delete("/dev/mock-data")
        self.ok()
        self.assertEqual(self.client.post("/generate-sep", json=BODY).status_code, 404)


GE_DIR = main.ROOT / "ap_engine" / "data" / "ge"
# A test-only Chabot IGETC course list (no real one exists): the ASSIST course-list layout, ge_pathway igetc
CHABOT_IGETC_FIXTURE = {
    "schema_version": "1.0.0", "dataset_id": "test_chabot_igetc", "source": {"academic_year": "2026-2027"},
    "institution": {"id": "chabot", "name": "Chabot College"},
    "ge_pathway": {"id": "igetc", "requirements": [{"id": "1", "name": "English Communication", "minimum_courses": 3,
                                                    "subareas": [{"id": "1A", "name": "English Composition",
                                                                  "minimum_courses": 1, "eligible_course_codes": []}]}]},
    "courses": [{"code": "ENGL C1000", "subject": "ENGL", "title": "Academic Reading and Writing", "semester_units": 4,
                 "areas": ["1A"], "former_codes": ["ENGL 1"]},
                {"code": "PSYC C1000", "subject": "PSYC", "title": "Introduction to Psychology", "semester_units": 3,
                 "areas": ["4"]},
                {"code": "SOCI 1", "subject": "SOCI", "title": "Principles of Sociology", "semester_units": 3,
                 "areas": ["4"]}],
}


class GEPathwayTests(unittest.TestCase):
    """The GE pathway system (ge_data.py + sep_engine/ge.py) with the data in ap_engine/data/ge/."""

    def setUp(self):
        main.STORE.clear()
        main.load_pathways()
        self.client = TestClient(main.app)

    def tearDown(self):
        main.STORE.clear()

    def ok(self, **overrides) -> dict:
        body = answered(CSUEB, **overrides)
        if body.get("ap_scores"):
            body["no_ap_scores"] = False
        r = self.client.post("/generate-sep", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def reqs(data, plan=0) -> dict[str, dict]:
        return {r["id"]: r for r in data["plans"][plan]["ge"]["requirements"]}

    def engine(self, pathway, system="UC", registry=None, **profile):
        """GE through the engine for the real Chabot -> CSUEB CS agreement, but any target system
        (no UC agreement is loaded, and GE rules depend on UC vs CSU)."""
        from sep_engine import StudentProfile, Term, generate_sep
        pathway_row = main.STORE.find("Chabot", "CSUEB", "Computer Science", pathway, "Fall 2026").pathway
        ctx = (registry or main.GE).context(pathway, "chabot", "UC Berkeley" if system == "UC" else "CSUEB", system,
                                            "Fall 2026", profile.pop("ap", ()), profile.pop("choices", ()))
        return generate_sep(pathway_row.catalog, pathway_row.agreement,
                            StudentProfile(start_term=Term.FALL, start_year=2026, **profile), ge=ctx)

    def test_every_ge_file_is_recognized_by_its_contents(self):
        listing = self.client.get("/ge-pathways").json()
        roles = {f["dataset_id"]: (f["role"], f["scope"], f.get("institution"), f.get("academic_year"), f["courses"])
                 for f in listing["files"] if f["status"] == "loaded"}
        self.assertEqual(roles["igetc_standards_v2_0_may_2019"], ("GE standard (requirement rules)", "statewide", None, None, 0))
        self.assertEqual(roles["porterville_cal_getc_2025_2026"], ("college GE course list", "college-specific", "porterville", "2025-2026", 132))
        self.assertEqual(roles["chabot_cal_getc_2025_2026"], ("college GE course list", "college-specific", "chabot", "2025-2026", 199))
        paths = {p["id"]: p for p in listing["pathways"]}
        self.assertEqual(set(paths), {"cal_getc", "igetc", "seven_course_pattern"})
        self.assertEqual(paths["igetc"]["courses_by_system"], {"CSU": 12, "UC": 11})
        self.assertEqual(paths["seven_course_pattern"]["systems"], ["UC"])
        self.assertEqual({m["institution"] for m in paths["cal_getc"]["college_lists"]}, {"chabot", "porterville"})
        self.assertEqual(paths["igetc"]["college_lists"], [])          # no college has an IGETC list loaded

    def test_1_igetc_uses_igetc_rules_only(self):
        data = self.ok(ge_pathway="IGETC")
        ge = data["plans"][0]["ge"]
        self.assertEqual((ge["pathway"], ge["standard"]["dataset_id"]), ("igetc", "igetc_standards_v2_0_may_2019"))
        # CSU target: 1C is required, 6A (UC language) is not; IGETC numbers math 2A and needs 3 Area 4 courses
        self.assertEqual(list(self.reqs(data)), ["1A", "1B", "1C", "2A", "3A", "3B", "3", "4", "5A", "5B", "5C"])
        self.assertEqual(self.reqs(data)["4"]["courses_required"], 3)
        uc = {r["id"]: r for r in self.engine("igetc", "UC")["plans"][0]["ge"]["requirements"]}
        self.assertIn("6A", uc)
        self.assertNotIn("1C", uc)

    def test_2_seven_course_pattern_uses_its_own_rules(self):
        ge = self.engine("seven_course_pattern", "UC")["plans"][0]["ge"]
        self.assertEqual([(r["id"], r["courses_required"]) for r in ge["requirements"]], [("E", 2), ("M", 1), ("BR", 4)])
        self.assertEqual(ge["requirements"][2]["min_distinct_areas"], 2)
        self.assertEqual(ge["case"], "requirements_only")                # no Chabot UC-E/-M/-H/-B/-S list loaded
        # A UC admission requirement: nothing is required for a CSU target
        csu = self.ok(ge_pathway="7-Course Pattern")["plans"][0]["ge"]
        self.assertEqual((csu["case"], csu["requirements"]), ("not_applicable", []))

    def test_3_completed_course_satisfies_its_igetc_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "igetc_list.json").write_text(json.dumps(CHABOT_IGETC_FIXTURE), encoding="utf-8")
            reg = main.GERegistry()
            reg.load_directory(Path(tmp), *main.GE_DIRS)
            self.assertEqual(reg.standards["igetc"].dataset_id, "igetc_standards_v2_0_may_2019")   # rules stay IGETC's
            ge = self.engine("igetc", "CSU", reg, completed_courses=["ENGL 1", "PSYC C1000"])["plans"][0]["ge"]
        reqs = {r["id"]: r for r in ge["requirements"]}
        self.assertEqual(ge["case"], "courses_available")
        self.assertEqual((reqs["1A"]["status"], reqs["1A"]["filled"][0]["code"]), ("satisfied", "ENGL C1000"))  # ENGL 1 = former code
        self.assertEqual((reqs["4"]["status"], reqs["4"]["open"]), ("in_progress", 2))
        self.assertEqual(reqs["4"]["options"], ["SOCI 1"])                # another discipline, never PSYC again

    def test_4_ap_scores_follow_the_pathways_own_ap_rules(self):
        ap = [{"subject": "AP English Language and Composition", "score": 4}, {"subject": "AP Biology", "score": 5},
              {"subject": "AP Art History", "score": 3}, {"subject": "AP Psychology", "score": 2}]
        reqs = self.reqs(self.ok(ge_pathway="igetc", ap_scores=ap))
        self.assertEqual(reqs["1A"]["filled"][0]["label"], "AP English Language and Composition")
        self.assertEqual(reqs["1B"]["status"], "open")                   # IGETC: no AP exam satisfies 1B
        self.assertEqual((reqs["5B"]["status"], reqs["5C"]["status"]), ("satisfied", "satisfied"))   # 5B + its lab
        art = [r for r in ("3A", "3B", "3") if reqs[r]["filled"]]
        self.assertEqual(len(art), 1)                                    # "3A or 3B": one area only
        self.assertEqual(reqs["4"]["filled"], [])                        # a 2 earns nothing
        # Cal-GETC at Chabot: AP rules come from Chabot's chart (Cal-GETC column)
        cal = self.ok(ap_scores=[{"subject": "AP Psychology", "score": 3}])
        self.assertEqual(self.reqs(cal)["4"]["filled"][0]["kind"], "ap")
        self.assertIn("Advanced Placement", cal["plans"][0]["ge"]["ap_source"])
        self.assertIn("Cal-GETC column", cal["plans"][0]["ge"]["ap_source"])

    def test_5_the_colleges_course_list_supplies_the_options(self):
        data = self.ok()
        reqs = self.reqs(data)
        self.assertEqual(reqs["1C"]["options"], ["COMM C1000"])
        self.assertIn("ARTH 1", reqs["3A"]["options"])
        self.assertEqual(data["ge_courses"]["ARTH 1"]["title"], "Introduction to Art")
        # Major prep the list approves counts, in its planned term: MTH 1 -> Area 2
        self.assertEqual((reqs["2"]["status"], reqs["2"]["filled"][0]["kind"]), ("planned", "major"))
        self.assertNotIn("MTH 1", data["ge_courses"])                    # planned already: never offered again

    def test_6_no_course_list_means_no_invented_courses(self):
        data = self.ok(ge_pathway="igetc")
        ge = data["plans"][0]["ge"]
        self.assertEqual(ge["case"], "requirements_only")
        self.assertTrue(all(r["options"] == [] for r in ge["requirements"]))
        self.assertEqual(ge["requirements"][0]["options_note"],
                         "Course selection unavailable: Chabot College IGETC course list not loaded.")
        self.assertEqual(data["ge_courses"], {})

    def test_7_porterville_courses_never_appear_for_chabot(self):
        porterville = json.loads((GE_DIR / "porterville_cal_getc_2025_2026.json").read_text(encoding="utf-8"))
        only_porterville = {c["code"] for c in porterville["courses"]} - set(main.GE.college_map("chabot", "cal_getc", None)[0].courses)
        data = self.ok(ge_choices=[{"requirement": "1B", "course": "ENGL P101B", "term": "Summer 2027"}], include_summer=True)
        text = json.dumps(data["plans"]) + json.dumps(data["ge_courses"])
        self.assertFalse([c for c in only_porterville if f'"{c}"' in text])
        self.assertEqual(data["ge_choices"]["dropped"][0]["reason"], "ENGL P101B is not on Chabot College's Cal-GETC course list")
        # ... while Porterville itself uses its own list
        ctx = main.GE.context("cal_getc", "porterville", "CSUEB", "CSU", "Fall 2026")
        self.assertEqual(ctx.college_map.dataset_id, "porterville_cal_getc_2025_2026")

    def test_8_summer_holds_chosen_ge_up_to_9_units(self):
        picks = [{"requirement": "3A", "course": "ARTH 1", "term": "Summer 2027"},
                 {"requirement": "3B", "course": "PHIL 50", "term": "Summer 2027"},
                 {"requirement": "1A", "course": "ENGL C1000", "term": "Summer 2027"}]     # 3 + 3 + 4 > 9
        data = self.ok(include_summer=True, ge_choices=picks)
        self.assertEqual([c["course"] for c in data["ge_choices"]["accepted"]], ["ARTH 1", "PHIL 50"])
        self.assertIn("the summer limit is 9", data["ge_choices"]["dropped"][0]["reason"])
        for plan in data["plans"]:
            summer = next(s for s in plan["semesters"] if s["term"] == "Summer 2027")
            self.assertEqual(([c["code"] for c in summer["courses"]], summer["units"]), (["ARTH 1", "PHIL 50"], 6))
            self.assertTrue(all(c["is_ge"] for c in summer["courses"]))   # major prep stays in Fall/Spring
            reqs = {r["id"]: r for r in plan["ge"]["requirements"]}
            self.assertEqual((reqs["3A"]["status"], reqs["3A"]["filled"][0]["term"]), ("planned", "Summer 2027"))
            self.assertEqual(plan["summary"]["major_prep_in_summer"], [])

    def test_8b_a_requirement_planned_elsewhere_is_not_offered_again_in_any_summer(self):
        data = self.ok(include_summer=True, start_term="Summer 2026",
                       ge_choices=[{"requirement": "3A", "course": "ARTH 1", "term": "Summer 2026"}])
        plan = data["plans"][0]
        self.assertEqual([s["term"] for s in plan["semesters"] if s["season"] == "Summer"], ["Summer 2026", "Summer 2027"])
        reqs = self.reqs(data)
        self.assertEqual((reqs["3A"]["open"], reqs["3A"]["options"]), (0, []))   # so Summer 2027 offers no 3A course
        self.assertNotIn("ARTH 1", reqs["3B"]["options"])                # ARTH 1 (3A and 3B) is already used
        fall = self.ok(ge_choices=[{"requirement": "4", "course": "PSYC C1000", "term": "Fall 2026"}], include_summer=True)
        self.assertEqual(self.reqs(fall)["4"]["open"], 1)                # one of two Area 4 courses planned in Fall
        self.assertNotIn("PSY 2", self.reqs(fall)["4"]["options"])       # same discipline as PSYC C1000

    def test_9_switching_pathways_recalculates_and_drops_old_choices(self):
        pick = [{"requirement": "3A", "course": "ARTH 1", "term": "Summer 2027"}]
        cal = self.ok(include_summer=True, ge_choices=pick)
        self.assertEqual(self.reqs(cal)["3A"]["status"], "planned")
        igetc = self.ok(include_summer=True, ge_choices=pick, ge_pathway="igetc")
        self.assertEqual(igetc["ge_choices"]["accepted"], [])
        self.assertIn("course list is loaded", igetc["ge_choices"]["dropped"][0]["reason"])
        self.assertIn("2A", self.reqs(igetc))                            # IGETC's areas, not Cal-GETC's
        self.assertNotIn("6", self.reqs(igetc))
        self.assertNotIn("ARTH 1", [c["code"] for s in igetc["plans"][0]["semesters"] for c in s["courses"]])
        self.assertTrue(any("ARTH 1" in w and "removed" in w for w in igetc["warnings"]))

    def test_10_satisfied_pathway_schedules_no_ge(self):
        done = ["ENGL C1000", "ENGL C1001", "COMM C1000", "MTH 1", "ARTH 1", "PHIL 50", "PSYC C1000", "ECN 1",
                "CHEM 1A", "BIOS 1", "ES 1"]
        data = self.ok(completed_courses=done, include_summer=True)
        for plan in data["plans"]:
            ge = plan["ge"]
            self.assertTrue(ge["all_satisfied"])
            self.assertEqual(ge["message"], "GE pathway requirements satisfied.")
            self.assertFalse([c for s in plan["semesters"] for c in s["courses"] if c["is_ge"]])
            self.assertTrue(all(r["options"] == [] for r in ge["requirements"]))
        self.assertFalse(any("ignored" in w for w in data["warnings"]))  # GE courses are not "unknown" courses

    def test_ge_choice_in_fall_and_prerequisite_order(self):
        # ENGL C1001 needs ENGL C1000 (catalog): chosen without it, the plan warns instead of hiding it
        data = self.ok(ge_choices=[{"requirement": "1B", "course": "ENGL C1001", "term": "Fall 2026"}])
        self.assertTrue(any("ENGL C1001" in w and "prerequisite" in w for w in data["warnings"]))
        self.assertEqual(self.ok()["ge_courses"]["ENGL C1001"]["prereq_any"][0], "ENGL C1000")   # what the UI checks
        both = self.ok(ge_choices=[{"requirement": "1A", "course": "ENGL C1000", "term": "Fall 2026"},
                                   {"requirement": "1B", "course": "ENGL C1001", "term": "Spring 2027"}])
        self.assertFalse(any("ENGL C1001" in w for w in both["warnings"]))


class RequiredFieldTests(unittest.TestCase):
    """POST /generate-sep rejects a request with any unanswered section (the form's rules, enforced again)."""

    def setUp(self):
        main.STORE.clear()
        main.load_pathways()
        self.client = TestClient(main.app)

    def tearDown(self):
        main.STORE.clear()

    def status(self, body) -> int:
        return self.client.post("/generate-sep", json=body).status_code

    def errors(self, body) -> dict[str, str]:
        r = self.client.post("/generate-sep", json=body)
        self.assertEqual(r.status_code, 422, r.text)
        return {".".join(map(str, e["loc"][1:])): e["msg"] for e in r.json()["detail"]}

    def test_empty_request_lists_every_missing_answer(self):
        self.assertEqual(self.errors({}), {
            "college": "Please select a community college.",
            "university": "Please select a target university.",
            "major": "Please select an intended major.",
            "no_completed_courses": "Add at least one completed course or choose “No completed courses.”",
            "no_ap_scores": "Add at least one AP score or choose “No AP scores.”",
            "ge_pathway": "Please select a GE pathway.",
            "start_term": "Please select a starting semester.",
            "include_summer": "Please choose whether to include summer classes."})

    def test_college_university_and_major_must_be_listed_options(self):
        errors = self.errors({**CSUEB, "college": "  ", "university": "Hogwarts", "major": "Wizardry"})
        self.assertEqual(errors["college"], "Please select a community college.")
        self.assertIn("“Hogwarts” is not in the list", errors["university"])
        self.assertIn("“Wizardry” is not in the list", errors["major"])
        self.assertEqual(self.status({**CSUEB, "college": "chabot", "university": "CSUEB"}), 200)   # same options
        self.assertEqual(self.status({**CSUEB, "university": "UC Davis"}), 404)                     # listed, no data

    def test_completed_courses_need_courses_or_an_explicit_none(self):
        self.assertEqual(self.status(CSUEB), 200)                                                # explicit none
        self.assertEqual(self.status({**CSUEB, "completed_courses": ["MTH 2"], "no_completed_courses": False}), 200)
        self.assertEqual(list(self.errors({**CSUEB, "no_completed_courses": False})), ["no_completed_courses"])
        self.assertIn("not both", self.errors({**CSUEB, "completed_courses": ["MTH 2"]})["no_completed_courses"])
        self.assertIn("completed_courses.0", self.errors({**CSUEB, "completed_courses": [" "], "no_completed_courses": False}))

    def test_ap_scores_need_scores_or_an_explicit_none(self):
        self.assertEqual(self.status(CSUEB), 200)                                                # explicit none
        self.assertEqual(self.status({**CSUEB, "ap_scores": [AP_CS_A], "no_ap_scores": False,
                                      "waived_courses": ["CSCI 14"]}), 200)
        self.assertEqual(list(self.errors({**CSUEB, "no_ap_scores": False})), ["no_ap_scores"])
        self.assertIn("not both", self.errors({**CSUEB, "ap_scores": [AP_CS_A]})["no_ap_scores"])
        self.assertIn("No AP scores", self.errors({**CSUEB, "waived_courses": ["CSCI 14"]})["waived_courses"])
        bad_score = {**CSUEB, "ap_scores": [{"subject": "AP Calculus AB", "score": 7}], "no_ap_scores": False}
        self.assertIn("ap_scores.0.score", self.errors(bad_score))

    def test_ge_pathway_start_term_and_summer_must_be_sent(self):
        for field, message in [("ge_pathway", "Please select a GE pathway."),
                               ("start_term", "Please select a starting semester."),
                               ("include_summer", "Please choose whether to include summer classes.")]:
            self.assertEqual(self.errors({k: v for k, v in CSUEB.items() if k != field}), {field: message})
        self.assertIn("ge_pathway", self.errors({**CSUEB, "ge_pathway": ""}))
        self.assertIn("start_term", self.errors({**CSUEB, "start_term": ""}))
        self.assertEqual(self.status({**CSUEB, "include_summer": False}), 200)      # "No" is an answer
        self.assertIn("include_summer", self.errors({**CSUEB, "start_term": "Summer 2027"}))


if __name__ == "__main__":
    unittest.main()
