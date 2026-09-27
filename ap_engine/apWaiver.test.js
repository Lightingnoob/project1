import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { availableNow } from './graph.js';
import { evaluateApWaivers, completedWithWaivers, classifyCredit, mentionsCourse } from './apWaiver.js';

const load = path => JSON.parse(readFileSync(new URL(path, import.meta.url)));
const campusData = load('./data/ap_campus_score_requirements.json');
const courses = load('./courses.example.json');
const ccWaivers = load('./ap_cc_waivers.example.json');
const run = (scores, target, waivers = ccWaivers) =>
  evaluateApWaivers({ scores, ccWaivers: waivers, campusData, target });
const course = (result, id) => result.courses.find(c => c.courseId === id);

test('classifyCredit separates specific courses from subject/elective credit', () => {
  assert.equal(classifyCredit('MATH 20A OR MATH 10A'), 'course');
  assert.equal(classifyCredit('ECON Unassigned'), 'subject_or_elective');
  assert.equal(classifyCredit('elective_only'), 'subject_or_elective');
  assert.equal(classifyCredit('none_listed'), 'none');
  assert.equal(classifyCredit(null), 'none');
  assert.equal(classifyCredit('placement_only; no university units'), 'review');
});
test('mentionsCourse matches whole codes and ignores zero padding', () => {
  assert.ok(mentionsCourse('MATH 20A OR MATH 10A', 'math 20a'));
  assert.ok(mentionsCourse('PHY 001A', 'PHY 1A'));
  assert.ok(!mentionsCourse('BIOLOGY 1AL', 'BIOLOGY 1A'));
  assert.ok(!mentionsCourse('MATH 10A', 'MATH 1'));
});
test('Calc AB 3: community college waives, UCSD only awards MATH 10A, so take the course', () => {
  const result = run([{ examId: 'ap:calculus_ab', score: 3 }], { campusId: 'san_diego' });
  const c = course(result, 'C');
  assert.equal(c.communityCollegeEligible, true);
  assert.equal(c.recommendation, 'take_course_for_transfer');
  assert.equal(c.transferStatus, 'units_or_ge_only');
  assert.equal(c.requiredTransferScore, 4);
  // Exam-level view still shows the MATH 10A award.
  assert.equal(result.exams[0].transfer.status, 'course_credit');
});
test('Calc AB 4 at UCSD earns MATH 20A: waive', () => {
  const c = course(run([{ examId: 'ap:calculus_ab', score: 4 }], { campusId: 'san_diego' }), 'C');
  assert.equal(c.recommendation, 'waive');
  assert.equal(c.basis, 'transfer_course_credit');
});
test('score below the community college minimum is not eligible, but still reported', () => {
  const result = run([{ examId: 'ap:calculus_ab', score: 2 }], { campusId: 'san_diego' });
  assert.equal(result.exams[0].communityCollege.eligible, false);
  assert.equal(course(result, 'C').recommendation, 'not_eligible');
  assert.deepEqual(completedWithWaivers(courses, [], result), []);
});
test('UCLA needs a college context; The College gives only title credit for Calc AB 3', () => {
  const noContext = run([{ examId: 'ap:calculus_ab', score: 3 }], { campusId: 'los_angeles' });
  assert.equal(course(noContext, 'C').transferStatus, 'context_required');
  assert.equal(course(noContext, 'C').recommendation, 'verify_before_waiving');
  const waivers = { rules: [{ exam_id: 'ap:calculus_ab', waives: ['C'], transfer_role: 'major_prep' }] };
  const college = run([{ examId: 'ap:calculus_ab', score: 3 }],
    { campusId: 'los_angeles', contexts: ['The College'] }, waivers);
  assert.equal(course(college, 'C').recommendation, 'take_course_for_transfer');
  assert.equal(course(college, 'C').requiredTransferScore, 5);
});
test('UCLA Macro splits at May 2021; unknown date takes the stricter outcome', () => {
  const waivers = { rules: [{ exam_id: 'ap:macroeconomics', waives: ['A'], transfer_role: 'major_prep' }] };
  const target = { campusId: 'los_angeles', contexts: ['The College'] };
  const older = course(run([{ examId: 'ap:macroeconomics', score: 4, examDate: '2020-05' }], target, waivers), 'A');
  assert.equal(older.recommendation, 'waive');
  const newer = course(run([{ examId: 'ap:macroeconomics', score: 4, examDate: '2024-05' }], target, waivers), 'A');
  assert.equal(newer.recommendation, 'take_course_for_transfer');
  assert.equal(newer.requiredTransferScore, 5);
  const unknown = course(run([{ examId: 'ap:macroeconomics', score: 4 }], target, waivers), 'A');
  assert.equal(unknown.recommendation, 'take_course_for_transfer');
  assert.equal(unknown.requiredTransferScore, 5);
  assert.ok(unknown.reasons.some(r => /exam date/.test(r)));
});
test('Merced ECON 001 needs both Micro and Macro at 4+', () => {
  const waivers = { rules: [{ exam_id: 'ap:microeconomics', waives: ['A'], transfer_role: 'major_prep',
    uc_equivalents: { merced: ['ECON 001'] } }] };
  const one = course(run([{ examId: 'ap:microeconomics', score: 4 }], { campusId: 'merced' }, waivers), 'A');
  assert.equal(one.recommendation, 'take_course_for_transfer');
  const both = course(run([{ examId: 'ap:microeconomics', score: 4 }, { examId: 'ap:macroeconomics', score: 5 }],
    { campusId: 'merced' }, waivers), 'A');
  assert.equal(both.recommendation, 'waive');
  assert.equal(both.manualReview, true);
});
test('GE and elective roles waive on the community college rule alone', () => {
  const waivers = { rules: [
    { exam_id: 'ap:psychology', waives: ['A'], transfer_role: 'ge' },
    { exam_id: 'ap:art_history', waives: ['B'], transfer_role: 'elective' }] };
  const result = run([{ examId: 'ap:psychology', score: 3 }, { examId: 'ap:art_history', score: 3 }],
    { campusId: 'davis' }, waivers);
  assert.equal(course(result, 'A').basis, 'cal_getc_certification');
  assert.equal(course(result, 'A').manualReview, true);
  assert.equal(course(result, 'B').basis, 'elective_units');
});
test('exam not listed at the campus is unresolved, not denied', () => {
  const waivers = { rules: [{ exam_id: 'ap:cybersecurity', waives: ['A'], transfer_role: 'major_prep' }] };
  const c = course(run([{ examId: 'ap:cybersecurity', score: 5 }], { campusId: 'davis' }, waivers), 'A');
  assert.equal(c.transferStatus, 'not_listed');
  assert.equal(c.recommendation, 'verify_before_waiving');
});
test('completedWithWaivers feeds graph.js; transfer mode keeps only transfer-safe waivers', () => {
  const result = run([{ examId: 'ap:computer_science_a', score: 4 }, { examId: 'ap:calculus_ab', score: 3 }],
    { campusId: 'san_diego' });
  assert.equal(course(result, 'A').recommendation, 'waive');
  const local = completedWithWaivers(courses, [], result);
  assert.deepEqual(local.sort(), ['A', 'C']);
  assert.deepEqual(availableNow(courses, local), ['B']);
  const transfer = completedWithWaivers(courses, [], result, { mode: 'transfer' });
  assert.deepEqual(transfer, ['A']);
  assert.deepEqual(availableNow(courses, transfer), ['B', 'C']);
});
test('rejects malformed input', () => {
  const target = { campusId: 'san_diego' };
  assert.throws(() => run([{ examId: 'ap:nope', score: 3 }], target), /Unknown AP exam/);
  assert.throws(() => run([{ examId: 'ap:calculus_ab', score: 6 }], target), /1 to 5/);
  assert.throws(() => run([{ examId: 'ap:calculus_ab', score: 3 }, { examId: 'ap:calculus_ab', score: 4 }], target), /Duplicate/);
  assert.throws(() => run([], { campusId: 'ucsf' }), /Unknown transfer campus/);
  assert.throws(() => run([], { campusId: 'los_angeles', contexts: ['Nope'] }), /Unknown context/);
  assert.throws(() => run([{ examId: 'ap:calculus_ab', score: 3, examDate: '2024' }], target), /YYYY-MM/);
  assert.throws(() => run([], target, { rules: [{ exam_id: 'ap:calculus_ab' }] }), /waives/);
  const result = run([{ examId: 'ap:calculus_ab', score: 3 }], target,
    { rules: [{ exam_id: 'ap:calculus_ab', waives: ['Z'] }] });
  assert.throws(() => completedWithWaivers(courses, [], result), /not in the course list/);
});
