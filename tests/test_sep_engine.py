"""Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import itertools
import json
import random
import unittest

from sep_engine import (Course, CycleError, GroupCategory, InfeasibleRequirementError, MeetingTime,
                        RequirementGroup, SchedulingError, StudentProfile, Term, TransferAgreement,
                        generate_sep, schedule_courses, select_pathway, topological_sort)
from sep_engine.mock_data import MOCK_AGREEMENT, MOCK_CATALOG, MOCK_PROFILE
from sep_engine.models import index_catalog
from sep_engine.pathway import WEIGHT_FUNCTIONS, requisite_closure
from sep_engine.scheduler import parse_term_label, term_sequence

F, S, SU = Term.FALL, Term.SPRING, Term.SUMMER


def cat(*courses: Course) -> dict[str, Course]:
    return index_catalog(list(courses))


def course(code, units=3, difficulty=3, prereqs=(), coreqs=(), terms=(F, S), meetings=()):
    return Course(code=code, title=code, units=units, difficulty=difficulty, prereqs=list(prereqs),
                  coreqs=list(coreqs), terms_offered=frozenset(terms),
                  meetings=[MeetingTime(days=d, start=s, end=e) for d, s, e in meetings])


def assert_valid_schedule(tc: unittest.TestCase, catalog, courses, completed, schedule, max_units,
                          max_difficulty=None):
    a = schedule.assignment
    tc.assertEqual(set(a), set(courses) - set(completed))
    for c, t in a.items():
        crs = catalog[c]
        tc.assertIn(schedule.slots[t].term, crs.terms_offered, f"{c} placed in a term it isn't offered")
        for p in crs.prereqs:
            tc.assertTrue(p in completed or a[p] < t, f"{c} scheduled before prereq {p}")
        for q in crs.coreqs:
            tc.assertTrue(q in completed or a[q] <= t, f"{c} scheduled before coreq {q}")
    if schedule.slots:
        tc.assertEqual(schedule.slots[-1].term, S, "plan must end in a Spring term")
    for slot, codes in schedule.terms():
        tc.assertLessEqual(sum(catalog[c].units for c in codes), min(max_units, schedule.caps[slot.index]) + 1e-9)
        if max_difficulty is not None:
            tc.assertLessEqual(sum(catalog[c].difficulty for c in codes), max_difficulty + 1e-9)
        for x, y in itertools.combinations(codes, 2):
            tc.assertFalse(catalog[x].conflicts_with(catalog[y]), f"{x} and {y} overlap in {slot.label}")


class TopologicalSortTests(unittest.TestCase):
    def test_prereqs_come_first(self):
        c = cat(course("A"), course("B", prereqs=["A"]), course("C", prereqs=["A", "B"]), course("D"))
        order = topological_sort(c, c.keys())
        for code in order:
            for p in c[code].prereqs:
                self.assertLess(order.index(p), order.index(code))

    def test_completed_courses_are_dropped(self):
        c = cat(course("A"), course("B", prereqs=["A"]))
        self.assertEqual(topological_sort(c, ["A", "B"], completed=["A"]), ["B"])

    def test_cycle_is_reported_with_path(self):
        c = cat(course("A", prereqs=["C"]), course("B", prereqs=["A"]), course("C", prereqs=["B"]), course("D"))
        with self.assertRaises(CycleError) as ctx:
            topological_sort(c, c.keys())
        cycle = ctx.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"A", "B", "C"})
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_two_course_cycle(self):
        c = cat(course("A", prereqs=["B"]), course("B", prereqs=["A"]))
        with self.assertRaises(CycleError):
            topological_sort(c, c.keys())


class DijkstraTests(unittest.TestCase):
    def test_picks_cheapest_options_and_counts_prereqs(self):
        # X is cheap on its own but drags in a 5-unit prereq; Y is the true minimum.
        c = cat(course("P", units=5), course("X", units=2, prereqs=["P"]), course("Y", units=3), course("Z", units=4))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["X", "Y", "Z"])])
        sel = select_pathway(c, ag, metric="units")
        self.assertEqual(sel.choices["g"], ["Y"])
        self.assertEqual(sel.courses, {"Y"})

    def test_double_count_major_into_ge_but_not_ge_into_ge(self):
        c = cat(course("M", units=5), course("G1", units=3), course("G2", units=3))
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=["M"], requirement_groups=[
            RequirementGroup(id="a", name="A", category=GroupCategory.GE, options=["M", "G1"]),
            RequirementGroup(id="b", name="B", category=GroupCategory.GE, options=["M", "G2"])])
        sel = select_pathway(c, ag)
        # M is free for one GE group, but may not fill both -> M (5) + one 3-unit GE course
        self.assertEqual((sel.choices["a"] + sel.choices["b"]).count("M"), 1)
        self.assertEqual(sel.cost[0], 8)

    def test_completed_course_is_free(self):
        c = cat(course("A", units=3, difficulty=1), course("B", units=4, difficulty=5))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["A", "B"])])
        sel = select_pathway(c, ag, completed=["B"])
        self.assertEqual(sel.choices["g"], ["B"])
        self.assertEqual(sel.courses, set())

    def test_metrics_disagree(self):
        c = cat(course("SHORT", units=2, difficulty=4), course("EASY", units=3, difficulty=1))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.GE, options=["SHORT", "EASY"])])
        self.assertEqual(select_pathway(c, ag, metric="units").choices["g"], ["SHORT"])
        self.assertEqual(select_pathway(c, ag, metric="difficulty").choices["g"], ["EASY"])

    def test_infeasible_group(self):
        c = cat(course("A"), course("B"))
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g1", name="First", category=GroupCategory.GE, choose=2, options=["A", "B"]),
            RequirementGroup(id="g2", name="Second", category=GroupCategory.GE, options=["A"])])
        with self.assertRaises(InfeasibleRequirementError) as ctx:
            select_pathway(c, ag)
        self.assertIn("Second", str(ctx.exception))

    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(7)
        for trial in range(150):
            n = rng.randint(4, 9)
            courses = [course(f"C{i}", units=rng.randint(1, 5), difficulty=rng.randint(1, 5),
                              prereqs=rng.sample([f"C{j}" for j in range(i)], k=min(i, rng.choice([0, 0, 1, 2]))))
                       for i in range(n)]
            c = cat(*courses)
            codes = list(c)
            groups = []
            for gi in range(rng.randint(1, 4)):
                opts = rng.sample(codes, k=rng.randint(1, min(5, n)))
                groups.append(RequirementGroup(id=f"g{gi}", name=f"G{gi}", choose=rng.randint(1, len(opts)),
                                               category=rng.choice([GroupCategory.GE, GroupCategory.MAJOR]),
                                               options=opts))
            required = rng.sample(codes, k=rng.randint(0, 2))
            completed = rng.sample(codes, k=rng.randint(0, 2))
            ag = TransferAgreement(college="c", university="u", major="m",
                                   required_courses=required, requirement_groups=groups)
            for metric in ("units", "difficulty"):
                expected = brute_force_cost(c, ag, completed, metric)
                if expected is None:
                    with self.assertRaises(InfeasibleRequirementError):
                        select_pathway(c, ag, completed, metric)
                else:
                    sel = select_pathway(c, ag, completed, metric)
                    self.assertEqual(sel.cost, expected, f"trial {trial} metric {metric}")


def brute_force_cost(catalog, agreement, completed, metric):
    w = WEIGHT_FUNCTIONS[metric]
    done = set(completed)
    per_group = [itertools.combinations(g.options, g.choose) for g in agreement.requirement_groups]
    best = None
    for combo in itertools.product(*per_group):
        used = set()
        ok = True
        for g, picks in zip(agreement.requirement_groups, combo):
            for p in picks:
                if (g.category, p) in used:
                    ok = False
                used.add((g.category, p))
        if not ok:
            continue
        chosen = set(agreement.required_courses) | {p for picks in combo for p in picks}
        full = set()
        for x in chosen:                     # a done (or waived) course needs none of its requisites
            full |= {x, *requisite_closure(catalog, x, done)}
        cost = (0.0, 0.0)
        for x in full - done:
            cost = (cost[0] + w(catalog[x])[0], cost[1] + w(catalog[x])[1])
        best = cost if best is None or cost < best else best
    return best


class SchedulerTests(unittest.TestCase):
    def test_term_sequence(self):
        labels = [s.label for s in term_sequence(F, 2026, 4, include_summer=False)]
        self.assertEqual(labels, ["Fall 2026", "Spring 2027", "Fall 2027", "Spring 2028"])
        labels = [s.label for s in term_sequence(S, 2027, 4, include_summer=True)]
        self.assertEqual(labels, ["Spring 2027", "Summer 2027", "Fall 2027", "Spring 2028"])
        labels = [s.label for s in term_sequence(SU, 2027, 3, include_summer=True)]
        self.assertEqual(labels, ["Summer 2027", "Fall 2027", "Spring 2028"])
        with self.assertRaises(SchedulingError):
            term_sequence(SU, 2027, 3, include_summer=False)

    def test_parse_term_label(self):
        self.assertEqual(parse_term_label("Fall 2026"), (F, 2026))
        self.assertEqual(parse_term_label(" summer 2027 "), (SU, 2027))
        with self.assertRaises(ValueError):
            parse_term_label("Autumn 2026")

    def test_fall_only_course_is_pushed(self):
        # B needs A; B is Fall-only, so starting in Fall: A (Fall), gap (Spring), B (Fall), then end on Spring
        c = cat(course("A"), course("B", prereqs=["A"], terms=[F]))
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=F))
        self.assertEqual(sched.assignment, {"A": 0, "B": 2})
        self.assertEqual([s.label for s in sched.slots], ["Fall 2026", "Spring 2027", "Fall 2027", "Spring 2028"])

    def test_ends_in_spring_with_padding(self):
        # Everything fits in Fall 2026, but transfers happen after a Spring -> pad to Spring 2027
        c = cat(course("A"), course("B"))
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=F, start_year=2026))
        self.assertEqual([s.label for s in sched.slots], ["Fall 2026", "Spring 2027"])
        self.assertEqual(set(sched.assignment.values()), {0})
        # Starting in Spring, a one-term plan already ends in Spring
        sched = schedule_courses(c, ["A", "B"], StudentProfile(start_term=S, start_year=2027))
        self.assertEqual([s.label for s in sched.slots], ["Spring 2027"])

    def test_unit_cap_forces_extra_term(self):
        c = cat(*(course(f"X{i}", units=5) for i in range(4)))
        sched = schedule_courses(c, list(c), StudentProfile(), max_units=12)
        self.assertEqual(len(sched.slots), 2)
        assert_valid_schedule(self, c, c.keys(), [], sched, 12)

    def test_time_conflict_separates_courses(self):
        c = cat(course("A", meetings=[("MW", "09:00", "10:15")]), course("B", meetings=[("W", "10:00", "11:00")]))
        sched = schedule_courses(c, ["A", "B"], StudentProfile())
        self.assertNotEqual(sched.assignment["A"], sched.assignment["B"])

    def test_coreqs_can_share_a_term(self):
        c = cat(course("LEC"), course("LAB", units=1, coreqs=["LEC"]))
        sched = schedule_courses(c, ["LEC", "LAB"], StudentProfile(start_term=S))
        self.assertEqual(len(sched.slots), 1)
        self.assertEqual(sched.assignment["LEC"], sched.assignment["LAB"])

    def test_summer_only_when_enabled_and_capped(self):
        c = cat(*(course(f"G{i}", units=3, terms=(F, S, SU)) for i in range(6)))
        off = schedule_courses(c, list(c), StudentProfile(start_term=S), max_units=18)
        self.assertFalse(any(s.is_summer for s in off.slots))
        on = schedule_courses(c, list(c), StudentProfile(start_term=S, include_summer=True), max_units=18)
        for slot, codes in on.terms():
            if slot.is_summer:
                self.assertLessEqual(sum(c[x].units for x in codes), 9)
        # a course bigger than the summer cap never lands in summer
        big = cat(course("BIG", units=10, terms=(F, S, SU)), course("G", units=3, terms=(F, S, SU)))
        sched = schedule_courses(big, ["BIG", "G"], StudentProfile(start_term=SU, include_summer=True))
        self.assertFalse(sched.slots[sched.assignment["BIG"]].is_summer)

    def test_ge_goes_to_summer_major_stays_out(self):
        # Start in Spring with summer: GE (offered any term) fills the summer; major prep stays in Fall/Spring
        major = [course("M1", units=4, terms=(F, S, SU)), course("M2", units=4, prereqs=["M1"], terms=(F, S, SU))]
        ge = [course(f"G{i}", units=3, terms=(F, S, SU)) for i in range(3)]
        c = cat(*major, *ge)
        order = topological_sort(c, c.keys())
        sched = schedule_courses(c, order, StudentProfile(start_term=S, include_summer=True),
                                 major_courses={"M1", "M2"})
        summer = [codes for slot, codes in sched.terms() if slot.is_summer]
        self.assertTrue(summer and set(summer[0]) == {"G0", "G1", "G2"}, sched.terms())
        self.assertEqual(sched.major_in_summer, [])
        assert_valid_schedule(self, c, c.keys(), [], sched, 18)

    def test_major_in_summer_only_when_necessary(self):
        # A 5-course chain from Fall 2026 can only end by Spring 2028 if one link runs in Summer 2027
        chain = [course(f"M{i}", units=4, prereqs=[f"M{i-1}"] if i else [], terms=(F, S, SU)) for i in range(5)]
        c = cat(*chain)
        order = [f"M{i}" for i in range(5)]
        sched = schedule_courses(c, order, StudentProfile(start_term=F, include_summer=True), major_courses=order)
        self.assertEqual(sched.slots[-1].label, "Spring 2028")
        self.assertEqual(sched.major_in_summer, ["M2"])

    def test_backtracking_needed(self):
        # D and E are Fall-only and clash, C is Spring-only after A, and the cap fits two courses:
        # the search has to undo early placements to find the minimum.
        c = cat(course("A", units=6), course("B", units=6), course("C", units=6, prereqs=["A"], terms=[S]),
                course("D", units=6, terms=[F], meetings=[("MW", "09:00", "10:00")]),
                course("E", units=6, terms=[F], meetings=[("MW", "09:30", "10:30")]))
        order = topological_sort(c, c.keys())
        sched = schedule_courses(c, order, StudentProfile(start_term=F), max_units=12)
        assert_valid_schedule(self, c, c.keys(), [], sched, 12)
        self.assertEqual(len(sched.slots), brute_force_min_terms(c, order, 12, F))

    def test_minimal_terms_match_brute_force(self):
        rng = random.Random(11)
        for trial in range(80):
            summer = trial % 2 == 1
            n = rng.randint(2, 5 if summer else 6)
            offered = [(F, S, SU), (F, S, SU), (F, S), (F,), (S,)] if summer else [(F, S), (F, S), (F,), (S,)]
            courses = []
            for i in range(n):
                prereqs = rng.sample([f"C{j}" for j in range(i)], k=min(i, rng.choice([0, 0, 1])))
                meetings = [rng.choice([("MW", "09:00", "10:15"), ("TR", "09:00", "10:15"), ("MW", "10:00", "11:00")])] \
                    if rng.random() < 0.4 else []
                courses.append(course(f"C{i}", units=rng.randint(2, 6), prereqs=prereqs,
                                      terms=rng.choice(offered), meetings=meetings))
            c = cat(*courses)
            order = topological_sort(c, c.keys())
            start = rng.choice([F, S])
            major = set(rng.sample(order, k=rng.randint(0, len(order))))
            profile = StudentProfile(start_term=start, include_summer=summer, max_terms=8)
            expected = brute_force_min_terms(c, order, 9, start, limit=8, include_summer=summer, summer_cap=5)
            for value_order in ("earliest", "balanced"):
                try:
                    sched = schedule_courses(c, order, profile, max_units=9, max_summer_units=5,
                                             major_courses=major, value_order=value_order)
                except SchedulingError:
                    self.assertIsNone(expected, f"trial {trial}")
                    continue
                assert_valid_schedule(self, c, c.keys(), [], sched, 9)
                self.assertEqual(len(sched.slots), expected, f"trial {trial} {value_order}")

    def test_unit_cap_violation_raises(self):
        c = cat(course("BIG", units=20))
        with self.assertRaises(SchedulingError):
            schedule_courses(c, ["BIG"], StudentProfile(), max_units=18)


def brute_force_min_terms(catalog, order, max_units, start, limit=8, include_summer=False, summer_cap=9):
    """Fewest terms of any valid, Spring-ending schedule (exhaustive search)."""
    for H in range(1, limit + 1):
        slots = term_sequence(start, 2026, H, include_summer=include_summer)
        if slots[-1].term is not S:
            continue
        caps = [min(max_units, summer_cap) if s.is_summer else max_units for s in slots]
        for combo in itertools.product(range(H), repeat=len(order)):
            a = dict(zip(order, combo))
            if all(slots[t].term in catalog[c].terms_offered
                   and all(a[p] < t for p in catalog[c].prereqs if p in a)
                   and all(a[q] <= t for q in catalog[c].coreqs if q in a)
                   for c, t in a.items()) \
               and all(sum(catalog[c].units for c in a if a[c] == t) <= caps[t] for t in range(H)) \
               and not any(a[x] == a[y] and catalog[x].conflicts_with(catalog[y])
                           for x, y in itertools.combinations(order, 2)):
                return H
    return None


class CompletedPrerequisiteTests(unittest.TestCase):
    """A completed course satisfies its whole prerequisite chain, so none of it is planned again."""

    def plan(self, catalog, required, completed=(), waived=()):
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=list(required))
        result = generate_sep(catalog, ag, StudentProfile(completed_courses=list(completed), waived_courses=list(waived)))
        planned = [{c["code"] for s in p["semesters"] for c in s["courses"]} for p in result["plans"]]
        self.assertEqual(planned[0], planned[1], "both plans schedule the same courses here")
        return planned[0], result

    def test_1_direct_prerequisite(self):                 # A -> B, completed B
        c = cat(course("A"), course("B", prereqs=["A"]))
        planned, result = self.plan(c, ["A", "B"], completed=["B"])
        self.assertEqual(planned, set())
        self.assertEqual(result["pathway"]["inferred_courses"], ["A"])
        self.assertEqual(result["pathway"]["completed_courses"], ["B"])     # the student's list is unchanged

    def test_2_recursive_chain(self):                     # A -> B -> C, completed C
        c = cat(course("A"), course("B", prereqs=["A"]), course("C", prereqs=["B"]))
        planned, result = self.plan(c, ["A", "B", "C"], completed=["C"])
        self.assertEqual(planned, set())
        self.assertEqual(result["pathway"]["inferred_courses"], ["A", "B"])
        self.assertEqual(result["plans"][0]["summary"]["terms_needed"], 0)

    def test_3_shared_prerequisite(self):                 # A -> B, A -> C, completed B
        c = cat(course("A"), course("B", prereqs=["A"]), course("C", prereqs=["A"]))
        planned, result = self.plan(c, ["A", "B", "C"], completed=["B"])
        self.assertEqual(planned, {"C"})
        first = result["plans"][0]["semesters"][0]
        self.assertEqual([x["code"] for x in first["courses"]], ["C"])     # C can start at once: A is satisfied

    def test_4_multiple_prerequisites(self):              # A and B -> D, C -> B, completed D
        c = cat(course("A"), course("C"), course("B", prereqs=["C"]), course("D", prereqs=["A", "B"]))
        planned, result = self.plan(c, ["A", "B", "C", "D"], completed=["D"])
        self.assertEqual(planned, set())
        self.assertEqual(result["pathway"]["inferred_courses"], ["A", "B", "C"])

    def test_5_no_completed_courses_is_unchanged(self):
        # Same result as before this change (recorded from the unchanged planner)
        result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, StudentProfile())
        a = result["plans"][0]
        self.assertEqual(result["pathway"]["inferred_courses"], [])
        self.assertEqual((a["summary"]["terms_needed"], a["summary"]["total_units"]), (4, 60))
        self.assertEqual([x["code"] for s in a["semesters"] for x in s["courses"] if x["code"].split()[0] in ("CS", "MATH")],
                         ["CS 1", "MATH 1", "CS 2", "MATH 2", "CS 7", "MATH 6", "CS 20"])
        self.assertTrue(all(n["data"]["satisfied_by"] is None for n in a["graph"]["nodes"]))

    def test_6_completed_course_without_prerequisites(self):
        c = cat(course("A"), course("B", prereqs=["A"]), course("X"))
        baseline, _ = self.plan(c, ["A", "B", "X"])
        planned, result = self.plan(c, ["A", "B", "X"], completed=["X"])
        self.assertEqual(baseline - planned, {"X"})
        self.assertEqual(result["pathway"]["inferred_courses"], [])

    def test_mock_pathway_cs_20_and_math_6(self):
        result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, StudentProfile(completed_courses=["CS 20", "MATH 6"]))
        for plan in result["plans"]:
            planned = {x["code"] for s in plan["semesters"] for x in s["courses"]}
            self.assertFalse(planned & {"CS 1", "CS 2", "CS 20", "MATH 1", "MATH 2", "MATH 6"})
            self.assertIn("CS 7", planned)                                      # still required, not implied
            nodes = {n["data"]["label"]: n["data"] for n in plan["graph"]["nodes"]}
            self.assertEqual((nodes["CS 1"]["status"], nodes["CS 1"]["satisfied_by"]), ("completed", "CS 20"))
            self.assertEqual((nodes["CS 20"]["status"], nodes["CS 20"]["satisfied_by"]), ("completed", None))
            major = next(r for r in plan["requirements"] if r["id"] == "major-prep")
            self.assertEqual({x["code"]: x["satisfied_by"] for x in major["courses"] if x["status"] == "completed"},
                             {"MATH 1": "MATH 6", "MATH 2": "MATH 6", "MATH 6": None,
                              "CS 1": "CS 20", "CS 2": "CS 20", "CS 20": None})
        self.assertEqual(result["pathway"]["inferred_courses"], ["CS 1", "CS 2", "MATH 1", "MATH 2"])
        self.assertLess(result["plans"][0]["summary"]["terms_needed"], 4)

    def test_waived_ap_course_does_not_imply_its_prerequisites(self):
        c = cat(course("A"), course("B", prereqs=["A"]))
        planned, result = self.plan(c, ["A", "B"], waived=["B"])
        self.assertEqual(planned, {"A"})
        self.assertEqual(result["pathway"]["waived_courses"], ["B"])
        self.assertEqual(result["pathway"]["inferred_courses"], [])

    def test_prerequisites_of_a_waived_course_are_not_planned_for_it(self):
        # AP credit for B: B is never taken, so A (needed only to enroll in B) isn't planned either
        c = cat(course("A"), course("B", prereqs=["A"]), course("C", prereqs=["B"]), course("D", prereqs=["A"]))
        planned, result = self.plan(c, ["C"], waived=["B"])
        self.assertEqual(planned, {"C"})
        self.assertEqual(result["pathway"]["inferred_courses"], [])
        # ...but A is still planned when a course the student takes needs it
        planned, _ = self.plan(c, ["C", "D"], waived=["B"])
        self.assertEqual(planned, {"A", "C", "D"})
        # The same holds for a waived option of a choose-one group
        ag = TransferAgreement(college="c", university="u", major="m", requirement_groups=[
            RequirementGroup(id="g", name="G", category=GroupCategory.MAJOR, options=["C"])])
        self.assertEqual(select_pathway(c, ag, completed={"C"}).courses, set())

    def test_corequisites_of_a_completed_course_are_satisfied(self):
        c = cat(course("LEC"), course("LAB", units=1, coreqs=["LEC"]))
        planned, _ = self.plan(c, ["LEC", "LAB"], completed=["LAB"])
        self.assertEqual(planned, set())

    def test_bad_data(self):
        from sep_engine.completion import resolve_completed
        # Different capitalization / spacing resolves to the catalog's own codes
        c = cat(course("CS 1"), course("CS 2", prereqs=["CS 1"]))
        done = resolve_completed(c, ["cs  2"])
        self.assertEqual((done.explicit, done.inferred), ({"CS 2"}, {"CS 1"}))
        # Duplicate prerequisite entries are visited once
        c = cat(course("A"), course("B", prereqs=["A", "A"]))
        self.assertEqual(resolve_completed(c, ["B"]).inferred, {"A"})
        # A prerequisite missing from the catalog is skipped with a warning
        c = cat(course("A"), course("B", prereqs=["A", "GONE 9"]))
        done = resolve_completed(c, ["B"])
        self.assertEqual(done.inferred, {"A"})
        self.assertIn("GONE 9", done.warnings[0])
        # A cycle in the data cannot loop forever, and completing one course clears the loop
        c = cat(course("A", prereqs=["B"]), course("B", prereqs=["A"]))
        self.assertEqual(resolve_completed(c, ["A"]).satisfied, {"A", "B"})
        planned, _ = self.plan(c, ["A", "B"], completed=["A"])
        self.assertEqual(planned, set())
        # Unknown completed codes are reported, not planned around
        _, result = self.plan(cat(course("A")), ["A"], completed=["ZZZ 1"])
        self.assertIn("ZZZ 1", result["warnings"][0])

    def test_institution_prefix_and_cross_listed_codes(self):
        from sep_engine.completion import resolve_completed
        c = cat(course("MTH 1"), course("MTH 2", prereqs=["MTH 1"]),
                Course(code="MTH 8", title="Discrete Mathematics", units=3, prereqs=["MTH 1"], aliases=["CSCI 28"]))
        # 'chabot:MTH2' is the dataset id of MTH 2
        self.assertEqual(resolve_completed(c, ["chabot:MTH2"]).explicit, {"MTH 2"})
        # A cross-listed code is the same course, counted once however it is entered
        for entered in (["CSCI 28"], ["csci28"], ["MTH 8", "CSCI 28"]):
            done = resolve_completed(c, entered)
            self.assertEqual((done.explicit, done.inferred, done.unknown), ({"MTH 8"}, {"MTH 1"}, ()), entered)
        with self.assertRaises(ValueError):
            Course(code="MTH 8", title="x", units=3, aliases=["MTH 8"])


class PipelineTests(unittest.TestCase):
    def check_plans(self, result, profile):
        json.dumps(result)   # must be JSON-serializable
        catalog = index_catalog(MOCK_CATALOG)
        completed = set(profile.completed_courses)
        calendar = [s.label for s in term_sequence(profile.start_term, profile.start_year, 30, profile.include_summer)]
        for plan in result["plans"]:
            sems = plan["semesters"]
            self.assertEqual([s["term"] for s in sems], calendar[:len(sems)])
            self.assertEqual(sems[-1]["season"], "Spring")
            self.assertEqual(plan["summary"]["transfer_admission_term"], f"Fall {sems[-1]['year']}")
            scheduled = [c["code"] for s in sems for c in s["courses"]]
            self.assertEqual(len(scheduled), len(set(scheduled)))
            self.assertFalse(completed & set(scheduled))
            for s in sems:
                cap = plan["max_units_summer"] if s["season"] == "Summer" else plan["max_units_regular"]
                self.assertEqual(s["max_units"], cap)
                self.assertLessEqual(s["units"], cap)
            for req in plan["requirements"]:
                self.assertEqual(len(req["courses"]), req["choose"])
            term_of = {c["code"]: s["index"] for s in sems for c in s["courses"]}
            for code, t in term_of.items():
                for p in catalog[code].prereqs:
                    self.assertTrue(p in completed or term_of[p] < t)

    def test_mock_pipeline(self):
        result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, MOCK_PROFILE)
        self.assertEqual([p["label"] for p in result["plans"]], ["Fastest Route", "Balanced Workload"])
        self.check_plans(result, MOCK_PROFILE)

    def test_mock_pipeline_all_start_terms_and_summer(self):
        for summer in (False, True):
            for term, year in [(F, 2026), (S, 2027), (SU, 2027)]:
                if term is SU and not summer:
                    continue
                profile = MOCK_PROFILE.model_copy(update={"start_term": term, "start_year": year, "include_summer": summer})
                result = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, profile)
                self.check_plans(result, profile)
                for plan in result["plans"]:
                    for s in plan["semesters"]:
                        if s["season"] == "Summer":
                            self.assertTrue(all(c["is_ge"] for c in s["courses"]),
                                            f"major prep in {s['term']}: {s['courses']}")

    def test_plan_a_is_not_slower_than_plan_b(self):
        a, b = generate_sep(MOCK_CATALOG, MOCK_AGREEMENT, MOCK_PROFILE)["plans"]
        self.assertLessEqual(a["summary"]["terms_needed"], b["summary"]["terms_needed"])
        self.assertLessEqual(a["summary"]["total_units"], b["summary"]["total_units"])

    def test_cycle_in_data_surfaces_as_cycle_error(self):
        c = cat(course("A", prereqs=["B"]), course("B", prereqs=["A"]))
        ag = TransferAgreement(college="c", university="u", major="m", required_courses=["A"])
        with self.assertRaises(CycleError):
            generate_sep(c, ag, StudentProfile())


class GERequirementTests(unittest.TestCase):
    """sep_engine/ge.py: credits fill requirement slots by the pathway's own rules (synthetic data)."""

    @staticmethod
    def req(rid, n=1, accepts=None, **kw):
        from sep_engine.ge import GERequirement
        return GERequirement(id=rid, label=f"Area {rid}", name=rid, area_id=rid[0], area_name=rid[0], courses=n,
                             accepts=frozenset(accepts or {rid}), **kw)

    @staticmethod
    def credit(label, areas, kind="completed", discipline=None, options=()):
        from sep_engine.ge import _Credit
        return _Credit(kind, label, label, label, frozenset(areas), frozenset(options),
                       discipline=discipline or label.split()[0], code=label)

    def assign(self, reqs, credits):
        from sep_engine.ge import assign
        return {rid: [c.label for c, _ in f] for rid, f in assign(reqs, credits).items()}

    def test_a_course_counts_toward_one_area_only(self):
        reqs = [self.req("3B"), self.req("4")]
        self.assertEqual(self.assign(reqs, [self.credit("HIS 1", {"3B", "4"})]), {"3B": ["HIS 1"], "4": []})
        # two such courses: each area gets one (the search finds it, whichever order they come in)
        two = self.assign(reqs, [self.credit("HIS 1", {"3B", "4"}), self.credit("SOCI 1", {"4"})])
        self.assertEqual(two, {"3B": ["HIS 1"], "4": ["SOCI 1"]})

    def test_a_lab_rides_on_its_lecture_course(self):
        reqs = [self.req("5A"), self.req("5B"), self.req("5C", shares_course=True)]
        got = self.assign(reqs, [self.credit("BIOS 1", {"5B", "5C"}), self.credit("GEO 1", {"5A"})])
        self.assertEqual(got, {"5A": ["GEO 1"], "5B": ["BIOS 1"], "5C": ["BIOS 1"]})

    def test_discipline_and_distinct_area_minimums(self):
        area4 = self.req("4", 2, min_disciplines=2)
        got = self.assign([area4], [self.credit("PSY 2", {"4"}), self.credit("PSY 3", {"4"})])
        self.assertEqual(len(got["4"]), 1)                               # a second PSY course can't complete it
        breadth = self.req("BR", 4, accepts={"UC-H", "UC-B", "UC-S"}, min_distinct_areas=2)
        humanities = [self.credit(f"ART {i}", {"UC-H"}) for i in range(4)]
        self.assertEqual(len(self.assign([breadth], humanities)["BR"]), 3)   # the 4th must be B or S
        self.assertEqual(len(self.assign([breadth], humanities + [self.credit("PSY 1", {"UC-B"})])["BR"]), 4)

    def test_done_credits_win_over_planned_ones(self):
        got = self.assign([self.req("2")], [self.credit("MTH 1", {"2"}, kind="major"),
                                            self.credit("STAT C1000", {"2"}, kind="completed")])
        self.assertEqual(got["2"], ["STAT C1000"])

    def test_ap_either_or_fills_one_area(self):
        from sep_engine.ge import _Credit
        art = _Credit("ap", "ap:art", "AP Art History", "AP", frozenset(), frozenset({"3A", "3B"}), "art")
        got = self.assign([self.req("3A"), self.req("3B")], [art, self.credit("PHIL 50", {"3B"})])
        self.assertEqual(got, {"3A": ["AP Art History"], "3B": ["PHIL 50"]})

    def test_pinned_courses_keep_their_term_or_are_released(self):
        # MAJ 1 -> MAJ 2 -> MAJ 3 runs Fall 2026 .. Fall 2027 (+ Spring 2028), so Summer 2027 is in the plan
        c = cat(course("MAJ 1", 4), course("MAJ 2", 4, prereqs=["MAJ 1"]), course("MAJ 3", 4, prereqs=["MAJ 2"]),
                course("GE 1", 3, terms=(F, S, SU)))
        order, majors = ["MAJ 1", "MAJ 2", "MAJ 3", "GE 1"], {"MAJ 1", "MAJ 2", "MAJ 3"}
        profile = StudentProfile(start_term=F, start_year=2026, include_summer=True)
        for pin in ("Summer 2027", "Fall 2026", "Spring 2028"):
            s = schedule_courses(c, order, profile, pinned={"GE 1": pin}, major_courses=majors)
            self.assertEqual((s.slots[s.assignment["GE 1"]].label, s.released), (pin, {}))
            self.assertEqual(s.slots[-1].label, "Spring 2028")           # a pin never lengthens the plan
        # a term this plan doesn't reach: placed like any other course, and reported
        s = schedule_courses(c, order, profile, pinned={"GE 1": "Summer 2031"}, major_courses=majors)
        self.assertEqual(s.released, {"GE 1": "Summer 2031"})
        self.assertEqual(s.slots[-1].label, "Spring 2028")


if __name__ == "__main__":
    unittest.main()
