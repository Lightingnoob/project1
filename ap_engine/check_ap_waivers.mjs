#!/usr/bin/env node
/**
 * Command-line AP waiver check for a Chabot student.
 *
 *   node check_ap_waivers.mjs --campus davis calculus_ab=3 psychology=4
 *   node check_ap_waivers.mjs --campus los_angeles --context "The College" english_language_and_composition=3
 *   node check_ap_waivers.mjs --campus san_diego --role "STAT C1000=ge" statistics=3
 *
 * Options:
 *   --campus ID         transfer campus id (davis, san_diego, los_angeles, berkeley, csueb, ...)
 *   --context NAME      college/school within the campus (repeatable), e.g. "The College"
 *   --role COURSE=ROLE  override a course's role: major_prep | ge | elective (repeatable)
 *   --date EXAM=YYYY-MM exam date, for campuses whose rules changed over time (repeatable)
 *   --articulation FILE ASSIST data (default: data/chabot_assist_articulation.json)
 *   --json              print the full result as JSON
 */
import { readFileSync } from 'node:fs';
import { evaluateApWaivers } from './apWaiver.js';

const load = path => JSON.parse(readFileSync(new URL(path, import.meta.url)));
const loadFile = path => JSON.parse(readFileSync(path));

export function parseArgs(argv) {
  const opts = { contexts: [], courseRoles: {}, dates: {}, scores: [], json: false,
    articulation: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`${arg} needs a value.`);
      return argv[++i];
    };
    const pair = text => {
      const at = text.lastIndexOf('=');
      if (at < 1) throw new Error(`Expected NAME=VALUE, got "${text}".`);
      return [text.slice(0, at).trim(), text.slice(at + 1).trim()];
    };
    if (arg === '--campus') opts.campusId = next();
    else if (arg === '--context') opts.contexts.push(next());
    else if (arg === '--role') { const [c, r] = pair(next()); opts.courseRoles[c] = r; }
    else if (arg === '--date') { const [e, d] = pair(next()); opts.dates[e.replace(/^ap:/, '')] = d; }
    else if (arg === '--articulation') opts.articulation = next();
    else if (arg === '--json') opts.json = true;
    else if (arg.startsWith('--')) throw new Error(`Unknown option ${arg}.`);
    else {
      const [exam, score] = pair(arg);
      if (!/^\d$/.test(score)) throw new Error(`Score for ${exam} must be 1-5.`);
      opts.scores.push({ examId: `ap:${exam.replace(/^ap:/, '')}`, score: Number(score) });
    }
  }
  if (!opts.campusId) throw new Error('--campus is required.');
  for (const s of opts.scores) {
    const date = opts.dates[s.examId.slice(3)];
    if (date) s.examDate = date;
  }
  return opts;
}

const LABEL = {
  waive: 'WAIVE',
  take_course_for_transfer: 'TAKE THE COURSE',
  verify_before_waiving: 'ASK A COUNSELOR',
  not_eligible: 'NOT ELIGIBLE (<3)',
};

export function formatResult(result) {
  const lines = [`Transfer campus: ${result.campusId} (${result.contexts.join(', ')})`, ''];
  for (const exam of result.exams) {
    const cc = exam.communityCollege;
    const area = cc.calGetc.length ? `  Cal-GETC ${cc.calGetc.join('; ')}` : '';
    lines.push(`${exam.examId.slice(3)} = ${exam.score}${area}`);
    const rows = result.courses.filter(c => c.examId === exam.examId);
    if (!rows.length) lines.push(cc.eligible ? '  (GE/unit credit only; no Chabot course waived)' : '  (score below 3)');
    for (const c of rows) {
      const need = c.recommendation === 'take_course_for_transfer' && c.requiredTransferScore
        ? ` - campus needs ${c.requiredTransferScore}` : '';
      const uc = c.targetCourses ? ` [UC: ${c.targetCourses.join(', ') || 'none articulated'}]` : '';
      lines.push(`  ${c.courseId.padEnd(11)} ${LABEL[c.recommendation]}${need}${uc}`);
      for (const r of c.reasons) lines.push(`      - ${r}`);
    }
    for (const note of cc.conditions) if (!rows.length) lines.push(`      - ${note}`);
  }
  return lines.join('\n');
}

export function main(argv) {
  const opts = parseArgs(argv);
  const articulation = opts.articulation ? loadFile(opts.articulation)
    : load('./data/chabot_assist_articulation.json');
  const result = evaluateApWaivers({
    scores: opts.scores,
    ccWaivers: load('./data/chabot_ap_waivers.json'),
    campusData: load('./data/ap_campus_score_requirements.json'),
    target: { campusId: opts.campusId, contexts: opts.contexts.length ? opts.contexts : undefined,
      courseRoles: opts.courseRoles },
    articulation,
  });
  return opts.json ? JSON.stringify(result, null, 2) : formatResult(result);
}

if (import.meta.url === new URL(process.argv[1], 'file://').href) {
  try {
    console.log(main(process.argv.slice(2)));
  } catch (err) {
    console.error(`Error: ${err.message}`);
    process.exit(1);
  }
}
