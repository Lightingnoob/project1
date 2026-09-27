import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { availableNow } from './graph.js';
import { evaluateApWaivers, completedWithWaivers, validateArticulation } from './apWaiver.js';
import { parseArgs, main } from './check_ap_waivers.mjs';

const load = path => JSON.parse(readFileSync(new URL(path, import.meta.url)));
const campusData = load('./data/ap_campus_score_requirements.json');
const chabot = load('./data/chabot_ap_waivers.json');
const run = (scores, target, ccWaivers = chabot) => evaluateApWaivers({ scores, ccWaivers, campusData, target });
const course = (result, id) => result.courses.find(c => c.courseId === id);
const withArticulation = (examId, campusId, codes) => ({ ...chabot,
  rules: chabot.rules.map(r => r.exam_id === examId ? { ...r, uc_equivalents: { [campusId]: codes } } : r) });

// A small slice of Chabot's real prerequisite chain, for graph.js integration.
const chabotCourses = [
  { id: 'MTH 1', title: 'Calculus I', units: 5, prereqs: [] },
  { id: 'MTH 2', title: 'Calculus II', units: 5, prereqs: ['MTH 1'] },
  { id: 'HIS 7', title: 'U.S. History Through Reconstruction', units: 3, prereqs: [] },
  { id: 'HIS 8', title: 'U.S. History Since Reconstruction', units: 3, prereqs: [] },
];

test('Chabot data matches the catalog AP chart and the campus exam ids', () => {
  const examIds = new Set(campusData.exams.map(e => e.id));
  assert.equal(chabot.default_min_score, 3);
  for (const rule of chabot.rules) {
    assert.ok(examIds.has(rule.exam_id), rule.exam_id);
    assert.equal(rule.min_score, undefined, `${rule.exam_id} should use the catalog-wide 3+`);
    assert.equal(rule.uc_equivalents, undefined, 'UC equivalents must come from ASSIST, not the catalog');
    if (rule.choose_one) assert.ok(rule.waives.length >= 2);
    for (const id of rule.waives) assert.match(id, /^[A-Z]+ C?\d+[A-Z]?$/);
    for (const old of rule.former_ids ?? []) assert.ok(!rule.waives.includes(old), 'waive the renumbered course');
  }
  const waives = exam => chabot.rules.find(r => r.exam_id === exam).waives;
  assert.deepEqual(waives('ap:calculus_ab'), ['MTH 1']);
  assert.deepEqual(waives('ap:calculus_bc'), ['MTH 2']);
  assert.deepEqual(waives('ap:computer_science_a'), ['CSCI 14']);
  assert.deepEqual(waives('ap:statistics'), ['STAT C1000']);
  assert.deepEqual(waives('ap:english_literature_and_composition'), ['ENGL C1000']);
});

test('community college side: every mapped course waives at 3 and not at 2, at any campus', () => {
  const mapped = chabot.rules.filter(r => r.waives.length);
  for (const score of [2, 3]) {
    const scores = [...new Set(mapped.map(r => r.exam_id))].map(examId => ({ examId, score }));
    const result = run(scores, { campusId: 'davis' });
    for (const rule of mapped) {
      for (const id of rule.waives) {
        const c = result.courses.find(x => x.examId === rule.exam_id && x.courseId === id);
        assert.equal(c.communityCollegeEligible, score >= 3, `${rule.exam_id} -> ${id} at ${score}`);
        if (score < 3) assert.equal(c.recommendation, 'not_eligible');
      }
    }
  }
});

test('UCSD Calc AB 3: without ASSIST articulation MTH 1 is flagged, with MATH 20A it must be taken', () => {
  const plain = course(run([{ examId: 'ap:calculus_ab', score: 3 }], { campusId: 'san_diego' }), 'MTH 1');
  assert.equal(plain.communityCollegeEligible, true);
  assert.equal(plain.recommendation, 'waive'); // UCSD awards MATH 10A at 3
  assert.equal(plain.manualReview, true);
  assert.ok(plain.reasons.some(r => /No articulated campus course/.test(r)));
  // Articulation supplied here only for the test; real values come from ASSIST.
  const waivers = withArticulation('ap:calculus_ab', 'san_diego', ['MATH 20A']);
  const three = course(run([{ examId: 'ap:calculus_ab', score: 3 }], { campusId: 'san_diego' }, waivers), 'MTH 1');
  assert.equal(three.recommendation, 'take_course_for_transfer');
  assert.equal(three.requiredTransferScore, 4);
  const four = course(run([{ examId: 'ap:calculus_ab', score: 4 }], { campusId: 'san_diego' }, waivers), 'MTH 1');
  assert.equal(four.recommendation, 'waive');
});

test('UC Davis: Calc AB needs 4 for MTH 1, CS A clears CSCI 14 at 3, Statistics needs 4', () => {
  const at3 = run(['calculus_ab', 'computer_science_a', 'statistics'].map(e => ({ examId: `ap:${e}`, score: 3 })),
    { campusId: 'davis' });
  assert.equal(course(at3, 'MTH 1').recommendation, 'take_course_for_transfer');
  assert.equal(course(at3, 'MTH 1').requiredTransferScore, 4);
  assert.equal(course(at3, 'CSCI 14').recommendation, 'waive');
  assert.equal(course(at3, 'STAT C1000').recommendation, 'take_course_for_transfer');
  const at4 = run([{ examId: 'ap:calculus_ab', score: 4 }], { campusId: 'davis' });
  assert.equal(course(at4, 'MTH 1').recommendation, 'waive');
});

test('UCLA The College: MTH 1 needs a 5; English and Psychology 3 waive through Cal-GETC', () => {
  const target = { campusId: 'los_angeles', contexts: ['The College'] };
  const calc = course(run([{ examId: 'ap:calculus_ab', score: 4 }], target), 'MTH 1');
  assert.equal(calc.recommendation, 'take_course_for_transfer');
  assert.equal(calc.requiredTransferScore, 5);
  const ge = run([{ examId: 'ap:english_language_and_composition', score: 3 }, { examId: 'ap:psychology', score: 3 }], target);
  const engl = course(ge, 'ENGL C1000');
  assert.equal(engl.basis, 'cal_getc_certification');
  assert.equal(engl.calGetc, '1A');
  assert.equal(engl.manualReview, false);
  assert.deepEqual(engl.reasons, ['Counts toward Cal-GETC Area 1A (community college AP chart).']);
  assert.equal(course(ge, 'PSYC C1000').calGetc, '4');
});

test('Berkeley Engineering: Physics C Mechanics needs a 5 for either PHYS 4A or PHYS 7A', () => {
  const result = run([{ examId: 'ap:physics_c_mechanics', score: 4 }],
    { campusId: 'berkeley', contexts: ['College of Engineering'] });
  for (const id of ['PHYS 4A', 'PHYS 7A']) {
    const c = course(result, id);
    assert.equal(c.recommendation, 'take_course_for_transfer');
    assert.equal(c.requiredTransferScore, 5);
    assert.deepEqual(c.alternatives, ['PHYS 4A', 'PHYS 7A']);
  }
});

test('"one of" rules need an explicit choice before marking a course completed', () => {
  const result = run([{ examId: 'ap:united_states_history', score: 3 }], { campusId: 'davis' });
  assert.equal(course(result, 'HIS 7').choiceGroup, 'ap:united_states_history');
  assert.ok(course(result, 'HIS 8').reasons.some(r => /one of HIS 7, HIS 8/.test(r)));
  assert.throws(() => completedWithWaivers(chabotCourses, [], result), /pass choices/);
  assert.throws(() => completedWithWaivers(chabotCourses, [], result,
    { choices: { 'ap:united_states_history': 'HIS 1' } }), /not one of/);
  const done = completedWithWaivers(chabotCourses, [], result, { choices: { 'ap:united_states_history': 'HIS 8' } });
  assert.deepEqual(done, ['HIS 8']);
});

test('courseRoles lets a non-STEM student use Cal-GETC Area 2; a course with no Cal-GETC area stays unresolved', () => {
  const target = { campusId: 'san_diego', courseRoles: { 'STAT C1000': 'ge', 'CSCI 14': 'ge' } };
  const result = run([{ examId: 'ap:statistics', score: 3 }, { examId: 'ap:computer_science_a', score: 3 }], target);
  assert.equal(course(result, 'STAT C1000').recommendation, 'waive');
  assert.equal(course(result, 'STAT C1000').basis, 'cal_getc_certification');
  assert.equal(course(result, 'CSCI 14').recommendation, 'verify_before_waiving');
  assert.ok(course(result, 'CSCI 14').reasons.some(r => /no Cal-GETC area/.test(r)));
  // Default role for STAT C1000 is major_prep, and UCSD lists no course for AP Statistics.
  const major = run([{ examId: 'ap:statistics', score: 3 }], { campusId: 'san_diego' });
  assert.equal(course(major, 'STAT C1000').recommendation, 'take_course_for_transfer');
  assert.throws(() => run([], { campusId: 'davis', courseRoles: { 'MTH 1': 'core' } }), /Unknown role/);
});

test('GE-only exams report the Cal-GETC area without waiving a Chabot course', () => {
  const result = run([{ examId: 'ap:environmental_science', score: 3 }, { examId: 'ap:drawing', score: 4 }],
    { campusId: 'davis' });
  const env = result.exams.find(e => e.examId === 'ap:environmental_science').communityCollege;
  assert.deepEqual(env.waives, []);
  assert.deepEqual(env.calGetc, ['5A and 5C']);
  const drawing = result.exams.find(e => e.examId === 'ap:drawing').communityCollege;
  assert.ok(drawing.conditions.some(c => /Portfolio review/.test(c)));
  assert.equal(result.courses.length, 0);
});

test('graph.js: Davis Calc AB 3 clears MTH 1 locally but not for transfer', () => {
  const result = run([{ examId: 'ap:calculus_ab', score: 3 }], { campusId: 'davis' });
  const local = completedWithWaivers(chabotCourses, [], result);
  assert.deepEqual(local, ['MTH 1']);
  assert.ok(availableNow(chabotCourses, local).includes('MTH 2'));
  const transfer = completedWithWaivers(chabotCourses, [], result, { mode: 'transfer' });
  assert.deepEqual(transfer, []);
  assert.ok(!availableNow(chabotCourses, transfer).includes('MTH 2'));
});

// Articulation values below are test inputs, not verified ASSIST data.
test('ASSIST articulation per course: UCSD MTH 1 -> MATH 20A needs a 4', () => {
  const articulation = { campuses: { san_diego: { 'MTH 1': ['MATH 20A'] } } };
  const at = score => course(evaluateApWaivers({ scores: [{ examId: 'ap:calculus_ab', score }],
    ccWaivers: chabot, campusData, target: { campusId: 'san_diego' }, articulation }), 'MTH 1');
  assert.equal(at(3).recommendation, 'take_course_for_transfer');
  assert.equal(at(3).requiredTransferScore, 4);
  assert.deepEqual(at(3).targetCourses, ['MATH 20A']);
  assert.equal(at(4).recommendation, 'waive');
  assert.ok(!at(4).reasons.some(r => /No articulated campus course/.test(r)));
});

test('articulation is per course, so "one of" alternatives can map differently', () => {
  const articulation = { campuses: { berkeley: { 'PHYS 7A': ['PHYSICS 7A'], 'PHYS 4A': [] } } };
  const result = evaluateApWaivers({ scores: [{ examId: 'ap:physics_c_mechanics', score: 5 }],
    ccWaivers: chabot, campusData, target: { campusId: 'berkeley', contexts: ['College of Engineering'] }, articulation });
  assert.equal(course(result, 'PHYS 7A').recommendation, 'waive');
  assert.equal(course(result, 'PHYS 4A').transferStatus, 'no_articulation');
  assert.equal(course(result, 'PHYS 4A').recommendation, 'verify_before_waiving');
  assert.ok(course(result, 'PHYS 4A').reasons.some(r => /ASSIST lists no berkeley course/.test(r)));
});

test('validateArticulation rejects bad data and reports courses no AP rule waives', () => {
  assert.deepEqual(validateArticulation({ campuses: { davis: { 'MTH 1': ['MAT 021A'], 'MATH 1': [] } } },
    chabot, campusData).unmatchedCourses, ['MATH 1']);
  assert.throws(() => validateArticulation({ campuses: { ucsf: {} } }, chabot, campusData), /Unknown campus/);
  assert.throws(() => validateArticulation({ campuses: { davis: { 'MTH 1': 'MAT 021A' } } }, chabot, campusData), /list of course codes/);
  assert.throws(() => validateArticulation({}, chabot, campusData), /campuses/);
  const template = load('./data/chabot_assist_articulation.json');
  assert.deepEqual(validateArticulation(template, chabot, campusData).unmatchedCourses, []);
  for (const id of template.courses_to_check) assert.ok(chabot.rules.some(r => r.waives.includes(id)), id);
});

test('command-line checker parses options and prints a readable result', () => {
  const opts = parseArgs(['--campus', 'los_angeles', '--context', 'The College', '--role', 'STAT C1000=ge',
    '--date', 'macroeconomics=2020-05', 'macroeconomics=4', 'ap:statistics=3']);
  assert.equal(opts.campusId, 'los_angeles');
  assert.deepEqual(opts.courseRoles, { 'STAT C1000': 'ge' });
  assert.deepEqual(opts.scores, [{ examId: 'ap:macroeconomics', score: 4, examDate: '2020-05' },
    { examId: 'ap:statistics', score: 3 }]);
  assert.throws(() => parseArgs(['calculus_ab=3']), /--campus/);
  assert.throws(() => parseArgs(['--campus', 'davis', 'calculus_ab=A']), /1-5/);
  const text = main(['--campus', 'davis', 'calculus_ab=3', 'environmental_science=3']);
  assert.match(text, /MTH 1\s+TAKE THE COURSE - campus needs 4/);
  assert.match(text, /GE\/unit credit only/);
});

test('README example: UCSD Calc AB 3 with ASSIST MTH 1 -> MATH 20A', () => {
  const articulation = { campuses: { san_diego: { 'MTH 1': ['MATH 20A'], 'CSCI 14': [] } } };
  assert.deepEqual(validateArticulation(articulation, chabot, campusData), { unmatchedCourses: [] });
  const result = evaluateApWaivers({
    scores: [{ examId: 'ap:calculus_ab', score: 3 }],
    ccWaivers: chabot, campusData,
    target: { campusId: 'san_diego', contexts: ['campus_chart'], courseRoles: { 'STAT C1000': 'ge' } },
    articulation,
  });
  const [mth1] = result.courses;
  assert.equal(mth1.courseId, 'MTH 1');
  assert.equal(mth1.recommendation, 'take_course_for_transfer');
  assert.equal(mth1.requiredTransferScore, 4);
  assert.deepEqual(mth1.targetCourses, ['MATH 20A']);
  assert.ok(mth1.reasons.length > 0);
  assert.deepEqual(completedWithWaivers(chabotCourses, [], result,
    { mode: 'transfer', choices: { 'ap:united_states_history': 'HIS 8' } }), []);
});
