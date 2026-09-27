/**
 * Browser-compatible prerequisite algorithms. No runtime dependencies.
 *
 * Course: { id: string, prereqs: string[], ...optionalMetadata }
 * Every course in the array is a selected course to finish.
 * prereqs is AND-only: resolve alternative requirements before calling.
 * [] means VERIFIED no prerequisites; omitted/null prerequisites are rejected.
 * Each unfinished course takes one semester. A prerequisite must finish in an
 * earlier semester. No unit caps, availability restrictions, or corequisites.
 */

function compile(courses) {
  if (!Array.isArray(courses)) {
    throw new TypeError('courses must be an array of prerequisite course records.');
  }
  const byId = new Map();
  for (const course of courses) {
    if (!course || typeof course.id !== 'string' || !course.id.trim()) {
      throw new TypeError('Every course must have a nonempty string id.');
    }
    if (byId.has(course.id)) throw new Error(`Duplicate course id: ${course.id}`);
    if (!Array.isArray(course.prereqs)) {
      throw new TypeError(`Course ${course.id} needs an explicit prereqs array; missing data is not an empty prerequisite list.`);
    }
    if (course.prereqs.some(id => typeof id !== 'string' || !id.trim())) {
      throw new TypeError(`Course ${course.id} has an invalid prerequisite id.`);
    }
    if (new Set(course.prereqs).size !== course.prereqs.length) {
      throw new Error(`Course ${course.id} repeats a prerequisite.`);
    }
    byId.set(course.id, { id: course.id, prereqs: [...course.prereqs] });
  }

  const dependents = new Map([...byId.keys()].map(id => [id, []]));
  const indegree = new Map();
  for (const course of byId.values()) {
    indegree.set(course.id, course.prereqs.length);
    for (const prerequisite of course.prereqs) {
      if (!byId.has(prerequisite)) {
        throw new Error(`Unknown prerequisite ${prerequisite} for ${course.id}.`);
      }
      dependents.get(prerequisite).push(course.id);
    }
  }

  // Kahn's algorithm. An index avoids the cost of repeatedly shifting an array.
  const order = [...byId.keys()].filter(id => indegree.get(id) === 0);
  for (let head = 0; head < order.length; head++) {
    for (const next of dependents.get(order[head])) {
      indegree.set(next, indegree.get(next) - 1);
      if (indegree.get(next) === 0) order.push(next);
    }
  }
  if (order.length !== byId.size) {
    // Remaining nodes can include descendants blocked by a cycle.
    const blockedCourseIds = [...byId.keys()].filter(id => indegree.get(id) > 0);
    const error = new Error(`Prerequisite cycle detected; ordering is blocked for: ${blockedCourseIds.join(', ')}`);
    error.code = 'CYCLE_DETECTED';
    error.blockedCourseIds = blockedCourseIds;
    throw error;
  }
  return { byId, order };
}

function completedSet(completed, byId) {
  if (!Array.isArray(completed) && !(completed instanceof Set)) {
    throw new TypeError('completed must be an array or Set of course ids.');
  }
  const done = new Set(completed);
  for (const id of done) {
    if (!byId.has(id)) throw new Error(`Unknown completed course: ${String(id)}`);
  }
  return done;
}

/** Return course ids in deterministic Kahn topological order. */
export function topoOrder(courses) {
  return compile(courses).order;
}

/**
 * Return one longest remaining prerequisite chain and a semester lower bound.
 * Completed courses are accepted as completed even if their ancestors are not
 * listed as completed. Completed courses have semester 0; new terms start at 1.
 * Ties use input/prerequisite order. This returns one critical path, not all.
 * @returns {{path: string[], semesters: number, semesterByCourse: Object}}
 */
export function criticalPath(courses, completed = []) {
  const { byId, order } = compile(courses);
  const done = completedSet(completed, byId);
  const semester = new Map();
  const predecessor = new Map();
  let end = null;
  let semesters = 0;

  for (const id of order) {
    if (done.has(id)) {
      semester.set(id, 0);
      continue;
    }
    let latestPrerequisite = 0;
    let previous = null;
    for (const prerequisite of byId.get(id).prereqs) {
      const term = semester.get(prerequisite);
      if (term > latestPrerequisite) {
        latestPrerequisite = term;
        previous = prerequisite;
      }
    }
    const term = latestPrerequisite + 1;
    semester.set(id, term);
    predecessor.set(id, previous);
    if (term > semesters) {
      semesters = term;
      end = id;
    }
  }

  const path = [];
  for (let id = end; id !== null; id = predecessor.get(id)) path.push(id);
  path.reverse();
  return { path, semesters, semesterByCourse: Object.fromEntries(semester) };
}

/** Return unfinished course ids whose prerequisites are all already completed. */
export function availableNow(courses, completed = []) {
  const { byId } = compile(courses);
  const done = completedSet(completed, byId);
  return [...byId.values()]
    .filter(course => !done.has(course.id) && course.prereqs.every(id => done.has(id)))
    .map(course => course.id);
}

/**
 * Validate a semester board. Accept either:
 *   [['A'], ['B', 'C']]
 * or:
 *   { completed: ['A'], semesters: [['B', 'C']] }
 *
 * Same-semester prerequisites are invalid. Partial plans are allowed; omitted
 * courses are returned in unscheduled. valid is NOT a degree-completion check.
 * Violation semesters are 1-based. Malformed input/invalid graphs throw errors;
 * unknown scheduled ids, repeats, and prerequisite violations are reported.
 */
export function validatePlan(courses, plan) {
  const { byId } = compile(courses);
  const config = Array.isArray(plan) ? { semesters: plan } : plan;
  if (!config || !Array.isArray(config.semesters)) {
    throw new TypeError('plan must be a semester array or { completed, semesters }.');
  }
  const done = completedSet(config.completed ?? [], byId);
  const initiallyDone = new Set(done);
  const scheduled = new Set();
  const firstSemester = new Map();
  const violations = [];

  for (let index = 0; index < config.semesters.length; index++) {
    const ids = config.semesters[index];
    const semester = index + 1;
    if (!Array.isArray(ids) || ids.some(id => typeof id !== 'string')) {
      throw new TypeError(`Semester ${semester} must be an array of course ids.`);
    }
    for (const id of ids) {
      if (!byId.has(id)) {
        violations.push({ code: 'UNKNOWN_COURSE', courseId: id, semester,
          message: `Unknown course ${id} in semester ${semester}.` });
        continue;
      }
      if (initiallyDone.has(id)) {
        violations.push({ code: 'ALREADY_COMPLETED', courseId: id, semester,
          message: `${id} is already completed.` });
        continue;
      }
      if (scheduled.has(id)) {
        violations.push({ code: 'DUPLICATE_COURSE', courseId: id, semester,
          firstSemester: firstSemester.get(id),
          message: `${id} is scheduled more than once.` });
        continue;
      }
      scheduled.add(id);
      firstSemester.set(id, semester);
      for (const prerequisite of byId.get(id).prereqs) {
        if (!done.has(prerequisite)) {
          violations.push({ code: 'UNMET_PREREQUISITE', courseId: id, semester,
            prerequisiteId: prerequisite,
            message: `${prerequisite} must be completed before semester ${semester} to take ${id}.` });
        }
      }
    }
    // Add this semester only after checking ALL its courses.
    // A timing-invalid earlier term still produces violations, keeping valid false.
    for (const id of ids) if (byId.has(id)) done.add(id);
  }
  const unscheduled = [...byId.keys()]
    .filter(id => !initiallyDone.has(id) && !scheduled.has(id));
  return { valid: violations.length === 0, violations, unscheduled };
}
