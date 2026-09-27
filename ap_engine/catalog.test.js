import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const load = path => JSON.parse(readFileSync(new URL(path, import.meta.url)));
const catalog = load('./data/catalog/chabot_2025_2026_courses.json');
const pages = load('./data/catalog/chabot_2025_2026_pages.json');
const byId = new Map(catalog.courses.map(c => [`${c.id}|${c.type}`, c]));
const credit = id => byId.get(`${id}|credit`);

test('catalog JSON has every course section and unique ids per type', () => {
  assert.equal(catalog.course_count, catalog.courses.length);
  assert.ok(catalog.courses.length > 1300);
  assert.equal(byId.size, catalog.courses.length);
  const types = new Set(catalog.courses.map(c => c.type));
  assert.deepEqual([...types].sort(), ['apprenticeship', 'credit', 'noncredit']);
  for (const c of catalog.courses) {
    assert.equal(c.id, `${c.department} ${c.number}`);
    assert.ok(c.title && c.description, c.id);
    assert.ok(c.type === 'noncredit' ? c.noncredit_hours : c.units, `${c.id} needs units or hours`);
  }
  assert.equal(pages.page_count, pages.pages.length);
});

test('key courses match the catalog text', () => {
  const mth1 = credit('MTH 1');
  assert.equal(mth1.title, 'Calculus I');
  assert.deepEqual(mth1.units, { min: 5, max: 5 });
  assert.deepEqual(mth1.hours, { lecture: { min: 90, max: 90 } });
  assert.equal(credit('MTH 2').prerequisite, 'MTH 1');
  assert.equal(credit('CSCI 14').title, 'Introduction to Structured Programming In C++');
  assert.equal(credit('CSCI 15').prerequisite, 'CSCI 14');
  assert.deepEqual(credit('PHYS 7C').prerequisite_course_ids, ['PHYS 7A', 'MTH 3', 'MTH 4', 'MTH 6']);
  assert.equal(credit('ENGL C1000').formerly, 'ENGL 1');
  assert.equal(credit('STAT C1000').formerly, 'MTH 43');
  assert.deepEqual(credit('MTH 25').see_also, ['ENGR 25', 'PHYS 25']);
  assert.equal(credit('ART 23').title, '2-D Foundations');
  assert.deepEqual(credit('PEAC 5K1').units, { min: 0.5, max: 2 });
  assert.equal(byId.get('MTH 201W|noncredit').corequisite, 'MTH 1');
  assert.equal(byId.get('APEL 9701|apprenticeship').title, 'Electrician Apprenticeship I');
});

test('every course the AP chart waives exists as a Chabot credit course', () => {
  const waivers = load('./data/chabot_ap_waivers.json');
  for (const rule of waivers.rules) {
    for (const id of rule.waives) assert.ok(credit(id), `${rule.exam_id} waives ${id}, missing from catalog`);
  }
});
