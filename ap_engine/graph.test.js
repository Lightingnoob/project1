import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { topoOrder, criticalPath, availableNow, validatePlan } from './graph.js';

const courses = JSON.parse(readFileSync(new URL('./courses.example.json', import.meta.url)));

test('topoOrder: A before B/C, both before D', () => {
  assert.deepEqual(topoOrder(courses), ['A', 'B', 'C', 'D']);
});
test('criticalPath: completing A leaves two semesters and one tied path', () => {
  assert.deepEqual(criticalPath(courses, ['A']), {
    path: ['B', 'D'], semesters: 2,
    semesterByCourse: { A: 0, B: 1, C: 1, D: 2 }
  });
  assert.equal(criticalPath(courses).semesters, 3);
});
test('availableNow: completing A unlocks B/C but not D', () => {
  assert.deepEqual(availableNow(courses, new Set(['A'])), ['B', 'C']);
  assert.deepEqual(availableNow(courses), ['A']);
});
test('validatePlan: correct three-semester plan', () => {
  assert.deepEqual(validatePlan(courses, [['A'], ['B', 'C'], ['D']]), {
    valid: true, violations: [], unscheduled: []
  });
});
test('validatePlan: rejects prerequisites in the same semester', () => {
  const result = validatePlan(courses, [['A', 'B'], ['C', 'D']]);
  assert.equal(result.valid, false);
  assert.deepEqual(result.violations.map(v => [v.courseId, v.prerequisiteId, v.semester]),
    [['B', 'A', 1], ['D', 'C', 2]]);
});
test('validatePlan: checks every AND prerequisite', () => {
  const result = validatePlan(courses, { completed: ['A', 'B'], semesters: [['D']] });
  assert.equal(result.valid, false);
  assert.equal(result.violations[0].prerequisiteId, 'C');
});
test('validatePlan: completed courses and partial plans', () => {
  assert.deepEqual(validatePlan(courses, { completed: ['A'], semesters: [['B', 'C']] }),
    { valid: true, violations: [], unscheduled: ['D'] });
});
test('validatePlan: unknown, duplicate, and already-completed entries', () => {
  const result = validatePlan(courses, { completed: ['A'], semesters: [['A', 'X', 'B', 'B']] });
  assert.deepEqual(result.violations.map(v => v.code),
    ['ALREADY_COMPLETED', 'UNKNOWN_COURSE', 'DUPLICATE_COURSE']);
  assert.deepEqual(result.unscheduled, ['C', 'D']);
});
test('empty graph and all-completed graph', () => {
  assert.deepEqual(topoOrder([]), []);
  assert.deepEqual(criticalPath([]), { path: [], semesters: 0, semesterByCourse: {} });
  assert.deepEqual(availableNow([]), []);
  assert.deepEqual(validatePlan([], []), { valid: true, violations: [], unscheduled: [] });
  assert.deepEqual(criticalPath(courses, ['A', 'B', 'C', 'D']),
    { path: [], semesters: 0, semesterByCourse: { A: 0, B: 0, C: 0, D: 0 } });
});
test('Kahn ordering handles input out of order and disconnected courses', () => {
  const graph = [courses[3], courses[2], courses[1], courses[0], { id: 'E', prereqs: [] }];
  const order = topoOrder(graph);
  for (const c of graph) for (const p of c.prereqs) assert.ok(order.indexOf(p) < order.indexOf(c.id));
  assert.equal(criticalPath(graph).semesters, 3);
});
test('all four functions reject cycles', () => {
  const cyclic = [{ id: 'A', prereqs: ['B'] }, { id: 'B', prereqs: ['A'] }];
  for (const fn of [() => topoOrder(cyclic), () => criticalPath(cyclic),
    () => availableNow(cyclic), () => validatePlan(cyclic, [])]) {
    assert.throws(fn, error => error.code === 'CYCLE_DETECTED');
  }
  assert.throws(() => topoOrder([{ id: 'A', prereqs: ['A'] }]), /cycle/);
});
test('reject bad course data instead of silently creating edges', () => {
  assert.throws(() => topoOrder([{ id: 'A' }]), /explicit prereqs/);
  assert.throws(() => topoOrder([{ id: 'A', prereqs: null }]), /explicit prereqs/);
  assert.throws(() => topoOrder([{ id: 'A', prereqs: { anyOf: ['B', 'C'] } }]), /explicit prereqs/);
  assert.throws(() => topoOrder([{ id: 'A', prereqs: ['X'] }]), /Unknown prerequisite/);
  assert.throws(() => topoOrder([{ id: 'A', prereqs: [] }, { id: 'A', prereqs: [] }]), /Duplicate/);
});
test('reject malformed completion/plan data', () => {
  assert.throws(() => availableNow(courses, ['X']), /Unknown completed/);
  assert.throws(() => criticalPath(courses, 'A'), /array or Set/);
  assert.throws(() => validatePlan(courses, null), /plan must/);
  assert.throws(() => validatePlan(courses, ['A']), /Semester 1/);
});
test('does not mutate caller-owned data', () => {
  const frozen = courses.map(c => Object.freeze({ ...c, prereqs: Object.freeze([...c.prereqs]) }));
  Object.freeze(frozen);
  const done = Object.freeze(['A']);
  const plan = Object.freeze([Object.freeze(['A']), Object.freeze(['B', 'C']), Object.freeze(['D'])]);
  topoOrder(frozen); criticalPath(frozen, done); availableNow(frozen, done); validatePlan(frozen, plan);
  assert.deepEqual(done, ['A']);
});
