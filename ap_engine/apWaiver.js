/**
 * Browser-compatible AP waiver eligibility. No runtime dependencies beyond graph.js.
 *
 * Two separate decisions are made for every AP score:
 *   1. Community college: does the score waive the local course? (default: 3+)
 *   2. Transfer campus: does the intended campus award course credit for the
 *      same score, using data/ap_campus_score_requirements.json?
 *
 * A course the community college waives is not automatically cleared for
 * transfer. UC/CSU awards are never used to mark a community college course
 * completed; only the community college's own waiver rule does that.
 *
 * Score entry:  { examId: 'ap:calculus_ab', score: 4, examDate?: 'YYYY-MM' }
 * CC waivers:   { default_min_score?: 3, rules: [{ exam_id, min_score?, waives: [courseId],
 *                 choose_one?: true,            // waives lists alternatives ("HIS 1 or HIS 2")
 *                 transfer_role?: 'major_prep'|'ge'|'elective',
 *                 cal_getc?: '2' | '3B or 4' | null,  // null = catalog lists no Cal-GETC area
 *                 conditions?: ['Portfolio review required ...'],
 *                 uc_equivalents?: { [campusId]: ['MATH 20A', ...] } }] }
 * Target:       { campusId: 'san_diego', contexts?: ['campus_chart', 'all_colleges'],
 *                 courseRoles?: { 'STAT C1000': 'ge' } }  // per-student override of transfer_role
 *
 * uc_equivalents names the campus course(s) the waived course articulates to
 * (for example from ASSIST). Without it, any specific campus course award counts
 * and the result is flagged for manual review.
 *
 * Articulation: optional ASSIST data, per campus and per community college course.
 *   { campuses: { san_diego: { 'MTH 1': ['MATH 20A'], 'CSCI 14': [] } } }
 * It takes precedence over uc_equivalents. An empty list means ASSIST shows
 * "no course articulated", which is reported as no_articulation.
 */
import { topoOrder } from './graph.js';

export const DEFAULT_CONTEXTS = Object.freeze(['campus_chart', 'all_colleges']);
const CC_DEFAULT_MIN_SCORE = 3;
const PERIOD_SPLIT = '2021-05';
const NO_CREDIT = new Set(['', 'none', 'none_listed', 'no_unit_credit']);
const REVIEW_TEXT = /placement|petition|consult|see department/i;
const TRANSFER_RANK = { course_credit: 3, units_or_ge_only: 2, no_award_at_score: 1 };
const ROLES = new Set(['major_prep', 'ge', 'elective']);

/** Classify a rule's course_credit text; the text itself is preserved, never parsed into a list. */
export function classifyCredit(text) {
  if (text === null || text === undefined || NO_CREDIT.has(String(text).trim().toLowerCase())) return 'none';
  if (REVIEW_TEXT.test(text)) return 'review';
  // Specific course awards carry a course number; "ECON Unassigned" or "elective_only" do not.
  return /\d/.test(text) ? 'course' : 'subject_or_elective';
}

function normalizeCode(text) {
  return String(text).toUpperCase().replace(/\s+/g, ' ').replace(/\b0+(\d)/g, '$1').trim();
}

/** True when credit text names the course code as a whole token ("MATH 1A" does not match "MATH 1AL"). */
export function mentionsCourse(text, code) {
  const needle = normalizeCode(code).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^A-Z0-9])${needle}(?![A-Z0-9])`).test(normalizeCode(text));
}

function periodOf(examDate) {
  return examDate < PERIOD_SPLIT ? `before_${PERIOD_SPLIT}` : `${PERIOD_SPLIT}_or_later`;
}

function indexScores(scores, examIds) {
  if (!Array.isArray(scores)) throw new TypeError('scores must be an array of { examId, score } entries.');
  const byExam = new Map();
  for (const entry of scores) {
    if (!entry || !examIds.has(entry.examId)) {
      throw new Error(`Unknown AP exam id: ${String(entry?.examId)}`);
    }
    if (!Number.isInteger(entry.score) || entry.score < 1 || entry.score > 5) {
      throw new TypeError(`Score for ${entry.examId} must be an integer from 1 to 5.`);
    }
    if (entry.examDate !== undefined && !/^\d{4}-(0[1-9]|1[0-2])$/.test(entry.examDate)) {
      throw new TypeError(`examDate for ${entry.examId} must be YYYY-MM.`);
    }
    if (byExam.has(entry.examId)) throw new Error(`Duplicate score for ${entry.examId}; submit one score per exam.`);
    byExam.set(entry.examId, entry);
  }
  return byExam;
}

function compileWaivers(ccWaivers, examIds) {
  if (!ccWaivers || !Array.isArray(ccWaivers.rules)) {
    throw new TypeError('ccWaivers must be { rules: [...] } from the community college AP chart.');
  }
  const defaultMin = ccWaivers.default_min_score ?? CC_DEFAULT_MIN_SCORE;
  return ccWaivers.rules.map(rule => {
    if (!examIds.has(rule.exam_id)) throw new Error(`Unknown AP exam id in ccWaivers: ${rule.exam_id}`);
    if (!Array.isArray(rule.waives) || rule.waives.some(id => typeof id !== 'string' || !id.trim())) {
      throw new TypeError(`ccWaivers rule for ${rule.exam_id} needs a waives array of course ids.`);
    }
    if (rule.transfer_role !== undefined && !ROLES.has(rule.transfer_role)) {
      throw new Error(`Unknown transfer_role ${rule.transfer_role} for ${rule.exam_id}.`);
    }
    if (rule.choose_one && rule.waives.length < 2) {
      throw new Error(`choose_one rule for ${rule.exam_id} needs at least two alternative courses.`);
    }
    if (rule.cal_getc !== undefined && rule.cal_getc !== null && typeof rule.cal_getc !== 'string') {
      throw new TypeError(`cal_getc for ${rule.exam_id} must be a string or null.`);
    }
    if (rule.conditions !== undefined && !Array.isArray(rule.conditions)) {
      throw new TypeError(`conditions for ${rule.exam_id} must be an array of strings.`);
    }
    return { ...rule, min_score: rule.min_score ?? defaultMin };
  });
}

function summarizeRule(rule) {
  return { id: rule.id, context: rule.context, scores: [...rule.scores],
    courseCredit: rule.course_credit, geOrOther: rule.ge_or_other_requirements,
    units: rule.units, conditions: rule.conditions ?? [] };
}

/**
 * Evaluate one exam at one campus for one period (null = period-independent rules only).
 * targetCourses, when given, must appear in the award text to count as course credit.
 */
function evaluatePeriod(rules, entry, byExam, period, targetCourses) {
  const inPeriod = rules.filter(r => !r.exam_period || period === null || r.exam_period === period);
  const grants = rule => {
    if (classifyCredit(rule.course_credit) !== 'course') return false;
    return !targetCourses || targetCourses.some(code => mentionsCourse(rule.course_credit, code));
  };
  const matched = inPeriod.filter(rule => {
    if (!rule.scores.includes(entry.score)) return false;
    if (rule.combination !== 'all_exam_ids_required') return true;
    return rule.exam_ids.every(id => byExam.has(id) && rule.scores.includes(byExam.get(id).score));
  });
  // Combination rules need other exams too, so they never set the single-exam threshold.
  const thresholds = inPeriod.filter(r => r.combination !== 'all_exam_ids_required' && grants(r))
    .map(r => Math.min(...r.scores));
  const status = matched.some(grants) ? 'course_credit'
    : matched.length ? 'units_or_ge_only' : 'no_award_at_score';
  return { status, matched, requiredScore: thresholds.length ? Math.min(...thresholds) : null };
}

function evaluateTransfer(entry, byExam, campus, contexts, targetCourses) {
  const examRules = campus.rules.filter(r => r.exam_ids.includes(entry.examId));
  const rules = examRules.filter(r => contexts.includes(r.context));
  const otherContexts = [...new Set(examRules.map(r => r.context).filter(c => !contexts.includes(c)))];
  const coverage = campus.exam_coverage?.find(c => c.exam_id === entry.examId)?.campus_rule_status;
  const reasons = [];

  if (!rules.length) {
    const status = otherContexts.length ? 'context_required' : 'not_listed';
    reasons.push(status === 'context_required'
      ? `Rules exist only for: ${otherContexts.join('; ')}. Pass the student's college as a context.`
      : 'Not listed in this campus chart; not_listed is not a denial of credit.');
    return { status, requiredScore: null, matchedRules: [], otherContexts, manualReview: true, reasons };
  }

  // UCLA splits some exams at May 2021. Without an exam date, evaluate every
  // period and keep the least favorable outcome.
  const periods = [...new Set(rules.map(r => r.exam_period).filter(Boolean))];
  let result;
  if (!periods.length) {
    result = evaluatePeriod(rules, entry, byExam, null, targetCourses);
  } else if (entry.examDate) {
    result = evaluatePeriod(rules, entry, byExam, periodOf(entry.examDate), targetCourses);
  } else {
    const outcomes = periods.map(p => evaluatePeriod(rules, entry, byExam, p, targetCourses));
    result = outcomes.reduce((worst, o) => TRANSFER_RANK[o.status] < TRANSFER_RANK[worst.status] ? o : worst);
    const required = outcomes.map(o => o.requiredScore);
    result = { ...result, requiredScore: required.includes(null) ? null : Math.max(...required),
      matched: [...new Set(outcomes.flatMap(o => o.matched))] };
    reasons.push('Award depends on exam date (before/after May 2021); provide examDate.');
  }

  for (const rule of result.matched) {
    if (rule.manual_review) reasons.push(`${rule.id} is marked for manual review.`);
    if (classifyCredit(rule.course_credit) === 'review') reasons.push(`${rule.id}: ${rule.course_credit}.`);
    for (const condition of rule.conditions ?? []) reasons.push(`${rule.id}: ${condition}`);
  }
  if (coverage && coverage !== 'recorded') reasons.push(`Campus coverage status: ${coverage}.`);
  if (result.status === 'course_credit' && !targetCourses) {
    reasons.push('No articulated campus course given; confirm the award matches the major requirement.');
  }
  return { status: result.status, requiredScore: result.requiredScore,
    matchedRules: result.matched.map(summarizeRule), otherContexts,
    manualReview: reasons.length > 0, reasons };
}

/**
 * Check ASSIST articulation data before use. Throws on a bad shape or unknown campus.
 * Returns the courses that no AP rule waives: harmless (an agreement lists every
 * major course), but useful for spotting a typo such as 'MATH 1' for 'MTH 1'.
 */
export function validateArticulation(articulation, ccWaivers, campusData) {
  if (!articulation || typeof articulation.campuses !== 'object' || Array.isArray(articulation.campuses)) {
    throw new TypeError('articulation must be { campuses: { [campusId]: { [courseId]: [campus course, ...] } } }.');
  }
  const campusIds = new Set(campusData.campuses.map(c => c.id));
  const waived = new Set((ccWaivers?.rules ?? []).flatMap(r => r.waives ?? []));
  const unmatchedCourses = new Set();
  for (const [campusId, courses] of Object.entries(articulation.campuses)) {
    if (!campusIds.has(campusId)) throw new Error(`Unknown campus in articulation: ${campusId}`);
    if (!courses || typeof courses !== 'object' || Array.isArray(courses)) {
      throw new TypeError(`articulation.campuses.${campusId} must map course ids to lists of campus courses.`);
    }
    for (const [courseId, codes] of Object.entries(courses)) {
      if (!Array.isArray(codes) || codes.some(code => typeof code !== 'string' || !code.trim())) {
        throw new TypeError(`Articulation for ${courseId} at ${campusId} must be a list of course codes ([] for none).`);
      }
      if (!waived.has(courseId)) unmatchedCourses.add(courseId);
    }
  }
  return { unmatchedCourses: [...unmatchedCourses] };
}

function recommend(ccEligible, transfer, role, calGetc) {
  if (!ccEligible) return { recommendation: 'not_eligible', basis: 'community_college_minimum' };
  if (transfer.status === 'course_credit') return { recommendation: 'waive', basis: 'transfer_course_credit' };
  // A GE role only helps when the exam actually carries a Cal-GETC area.
  if (role === 'ge' && calGetc !== null) return { recommendation: 'waive', basis: 'cal_getc_certification' };
  if (role === 'elective') return { recommendation: 'waive', basis: 'elective_units' };
  if (role === 'major_prep' && (transfer.status === 'units_or_ge_only' || transfer.status === 'no_award_at_score')) {
    return { recommendation: 'take_course_for_transfer', basis: 'transfer_requires_higher_score_or_course' };
  }
  return { recommendation: 'verify_before_waiving', basis: 'transfer_outcome_unresolved' };
}

/**
 * Decide, per AP score and per community college course, whether the course can
 * be waived locally and whether that waiver holds up at the intended transfer campus.
 * @returns {{campusId: string, contexts: string[], exams: Object[], courses: Object[]}}
 */
export function evaluateApWaivers({ scores, ccWaivers, campusData, target, articulation }) {
  if (!campusData || !Array.isArray(campusData.campuses) || !Array.isArray(campusData.exams)) {
    throw new TypeError('campusData must be the ap_campus_score_requirements dataset.');
  }
  const campus = campusData.campuses.find(c => c.id === target?.campusId);
  if (!campus) {
    throw new Error(`Unknown transfer campus: ${String(target?.campusId)}. Known: ${campusData.campuses.map(c => c.id).join(', ')}`);
  }
  const contexts = target.contexts ?? [...DEFAULT_CONTEXTS];
  const knownContexts = new Set([...DEFAULT_CONTEXTS, ...campus.rules.map(r => r.context)]);
  for (const context of contexts) {
    if (!knownContexts.has(context)) throw new Error(`Unknown context for ${campus.id}: ${context}`);
  }
  const courseRoles = target.courseRoles ?? {};
  for (const [courseId, role] of Object.entries(courseRoles)) {
    if (!ROLES.has(role)) throw new Error(`Unknown role ${role} for ${courseId} in target.courseRoles.`);
  }
  if (articulation !== undefined) validateArticulation(articulation, ccWaivers, campusData);
  const campusArticulation = articulation?.campuses?.[campus.id] ?? {};
  const examIds = new Set(campusData.exams.map(e => e.id));
  const byExam = indexScores(scores, examIds);
  const waiverRules = compileWaivers(ccWaivers, examIds);

  const exams = [];
  const courses = [];
  const seen = new Set();
  for (const entry of byExam.values()) {
    const rules = waiverRules.filter(r => r.exam_id === entry.examId);
    const eligibleRules = rules.filter(r => entry.score >= r.min_score);
    const transfer = evaluateTransfer(entry, byExam, campus, contexts, null);
    exams.push({ examId: entry.examId, score: entry.score,
      communityCollege: { eligible: eligibleRules.length > 0,
        minScore: rules.length ? Math.min(...rules.map(r => r.min_score)) : null,
        waives: [...new Set(eligibleRules.flatMap(r => r.waives))],
        calGetc: [...new Set(eligibleRules.map(r => r.cal_getc).filter(Boolean))],
        conditions: [...new Set(rules.flatMap(r => r.conditions ?? []))] },
      transfer });

    // Below-minimum rules are reported too, so a student sees what a higher score would waive.
    for (const rule of [...eligibleRules, ...rules.filter(r => !eligibleRules.includes(r))]) {
      const ccEligible = eligibleRules.includes(rule);
      for (const courseId of rule.waives) {
        const targetCourses = campusArticulation[courseId] ?? rule.uc_equivalents?.[campus.id] ?? null;
        const courseTransfer = !targetCourses ? transfer
          : targetCourses.length ? evaluateTransfer(entry, byExam, campus, contexts, targetCourses)
          : { status: 'no_articulation', requiredScore: null, manualReview: true,
              reasons: [`ASSIST lists no ${campus.id} course for ${courseId}; confirm whether the major needs it.`] };
        const key = `${entry.examId}|${courseId}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const role = courseRoles[courseId] ?? rule.transfer_role;
        const { recommendation, basis } = recommend(ccEligible, courseTransfer, role, rule.cal_getc);
        const reasons = [...courseTransfer.reasons];
        let review = courseTransfer.manualReview;
        if (basis === 'cal_getc_certification') {
          if (rule.cal_getc) {
            reasons.push(`Counts toward Cal-GETC Area ${rule.cal_getc} (community college AP chart).`);
            // "3B or 4": the certifying college picks the area, so confirm which one applies.
            if (/\bor\b/.test(rule.cal_getc)) review = true;
          } else {
            reasons.push('Cal-GETC area for this exam is not encoded here; confirm the area (no AP meets Area 1B).');
            review = true;
          }
        }
        if (role === 'ge' && rule.cal_getc === null && ccEligible) {
          reasons.push('The community college chart lists no Cal-GETC area for this exam, so it cannot clear a GE course.');
        }
        if (rule.choose_one) {
          reasons.push(`Waives one of ${rule.waives.join(', ')}; pick one with a counselor (credit is awarded once).`);
          review = true;
        }
        for (const condition of rule.conditions ?? []) {
          reasons.push(condition);
          review = true;
        }
        courses.push({ courseId, examId: entry.examId, score: entry.score,
          communityCollegeMinScore: rule.min_score, communityCollegeEligible: ccEligible,
          transferRole: role ?? null, targetCourses,
          choiceGroup: rule.choose_one ? entry.examId : null,
          alternatives: rule.choose_one ? [...rule.waives] : null,
          calGetc: rule.cal_getc ?? null,
          recommendation, basis,
          requiredTransferScore: courseTransfer.requiredScore,
          transferStatus: courseTransfer.status,
          manualReview: review,
          reasons });
      }
    }
  }
  return { campusId: campus.id, contexts, exams, courses };
}

/**
 * Merge AP waivers into a completed list for graph.js (availableNow, criticalPath, validatePlan).
 * mode 'community_college': every locally waived course counts as completed.
 * mode 'transfer': only courses recommended 'waive' for the intended campus count.
 * choices: { [examId]: courseId } picks the course for "one of" rules (e.g. HIS 1 or HIS 2).
 * An eligible "one of" rule without a choice throws rather than guessing.
 */
export function completedWithWaivers(courses, completed, evaluation, { mode = 'community_college', choices = {} } = {}) {
  if (mode !== 'community_college' && mode !== 'transfer') throw new Error(`Unknown mode: ${mode}`);
  if (!evaluation || !Array.isArray(evaluation.courses)) throw new TypeError('evaluation must come from evaluateApWaivers.');
  const ids = new Set(topoOrder(courses));
  const done = new Set(completed);
  for (const decision of evaluation.courses) {
    const counts = mode === 'transfer' ? decision.recommendation === 'waive' : decision.communityCollegeEligible;
    if (!counts) continue;
    if (decision.choiceGroup) {
      const chosen = choices[decision.choiceGroup];
      if (chosen === undefined) {
        throw new Error(`${decision.choiceGroup} waives one of ${decision.alternatives.join(', ')}; pass choices[${JSON.stringify(decision.choiceGroup)}].`);
      }
      if (!decision.alternatives.includes(chosen)) {
        throw new Error(`Choice ${chosen} for ${decision.choiceGroup} is not one of ${decision.alternatives.join(', ')}.`);
      }
      if (chosen !== decision.courseId) continue;
    }
    if (!ids.has(decision.courseId)) throw new Error(`Waived course ${decision.courseId} is not in the course list.`);
    done.add(decision.courseId);
  }
  return [...done];
}
