"""Extract Chabot College's Cal-GETC course list from the 2025-2026 catalog into GE course-list JSON.

Usage:
    python tools/extract_chabot_cal_getc.py            (from ap_engine/)

Reads   data/catalog/chabot_2025_2026_pages.json    the catalog text (tools/parse_catalog.py)
        data/catalog/chabot_2025_2026_courses.json  titles, units, "Formerly" codes and prerequisites
Writes  data/ge/chabot_cal_getc_2025_2026.json

Source: the "CAL-GETC Certificate of Achievement" program (catalog pp. 359-363), which lists every Chabot
course approved for each Cal-GETC area, effective Fall 2025 - Summer 2026. The output uses the same schema
as ASSIST's Cal-GETC course lists (porterville_cal_getc_2025_2026.json), so one loader reads both:
ge_pathway.requirements (areas, subareas, minimum courses, eligible_course_codes) + courses[].

One correction is recorded rather than copied: the catalog heads Ethnic Studies "Area 7" (IGETC's
number for it), while Cal-GETC numbers Ethnic Studies Area 6 (ASSIST's Cal-GETC lists). The area is
written as "6" with source_label "Area 7 - ETHNIC STUDIES".

Only the Python standard library is used.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent            # ap_engine/
PAGES = ROOT / 'data' / 'catalog' / 'chabot_2025_2026_pages.json'
COURSES = ROOT / 'data' / 'catalog' / 'chabot_2025_2026_courses.json'
OUT = ROOT / 'data' / 'ge' / 'chabot_cal_getc_2025_2026.json'

START = 'CAL-GETC\nCertificate of Achievement'
END = re.compile(r'^Total\s+\d+')
AREA = re.compile(r'^(?:Area\s+)?(\d)([A-C])?\s*-\s*(.+?)\s*$')
COURSE = re.compile(r'^([A-Z]{2,5}) (C?\d{1,4}[A-Z]{0,2})\b\s*(.*)$')
UNITS_AT_END = re.compile(r'^(.*?)\s*(\d+(?:\.\d+)?)\s*$')
SAME_AS = re.compile(r'\(same as ([^)]*)\)\s*', re.I)
UNITS_NOTE = re.compile(r'\((\d+)\s*semester\s*/\s*(\d+)\s*quarter units\)', re.I)
NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4}
CAL_GETC_ID = {'7': '6'}                                  # catalog "Area 7 - ETHNIC STUDIES" -> Cal-GETC Area 6
CODE = re.compile(r'^([A-Z]{2,5})\s*(C?\d{1,4}[A-Z]{0,2})\b')


SMALL_WORDS = {'and', 'or', 'of', 'the', 'in', 'to', 'for'}


def clean(text):
    """The PDF text has U+FFFD where the catalog printed curly quotes and apostrophes."""
    return text.replace('�', "'") if isinstance(text, str) else text


def titlecase(text):
    """'CRITICAL THINKING AND COMPOSITION' -> 'Critical Thinking and Composition'; mixed case is kept."""
    if not text.isupper():
        return text
    return ' '.join(w.lower() if i and w.lower() in SMALL_WORDS else w.capitalize()
                    for i, w in enumerate(text.split()))


def normalize_code(text):
    m = CODE.match(text.strip().upper())
    return f'{m[1]} {m[2]}' if m else None


def certificate_lines(pages):
    """Every line of the certificate's course list, with its catalog page."""
    out, started = [], False
    for page in pages:
        text = page['text']
        if not started:
            if START not in text:
                continue
            started, text = True, text[text.index(START):]
        for line in text.splitlines():
            line = line.strip()
            if not line or line == page['running_header']:
                continue
            if END.match(line):
                return out
            out.append((line, page['page']))
    raise SystemExit('The certificate list never reached its "Total" line.')


def parse(lines):
    """-> areas: [{id, source_label, name, rule, subareas: [{id, name, note, courses: [(code, title, units, same_as, page)]}]}]"""
    areas, area, sub, course = [], None, None, None

    def close_course():
        nonlocal course
        if course:
            raise SystemExit(f'Course entry without units: {course}')

    for line, page in lines:
        line = clean(line)
        if course:                                                  # a course entry continues until its units
            course['text'] += ' ' + line
            m = UNITS_AT_END.match(course['text'])
            if m and not line.endswith('-'):
                finish(sub, course, m)
                course = None
            continue
        m = AREA.match(line)
        if m and not COURSE.match(line):
            number, letter, name = m.groups()
            if letter:                                              # subarea header, e.g. "1A - ENGLISH COMPOSTION"
                sub = {'id': number + letter, 'name': name, 'note': '', 'courses': []}
                area['subareas'].append(sub)
            else:                                                   # area header, e.g. "Area 2 - Mathematical ..."
                area = {'id': CAL_GETC_ID.get(number, number), 'source_label': line, 'name': name,
                        'rule': '', 'subareas': []}
                areas.append(area)
                sub = None
            continue
        m = COURSE.match(line)
        if m:
            if sub is None:                                         # area without subareas (2, 4, Ethnic Studies)
                sub = {'id': area['id'], 'name': area['name'], 'note': '', 'courses': []}
                area['subareas'].append(sub)
            course = {'code': f'{m[1]} {m[2]}', 'text': m[3], 'page': page}
            u = UNITS_AT_END.match(course['text'])
            if u and course['text'] and not SAME_AS.fullmatch(course['text'].strip() + ' '):
                finish(sub, course, u)
                course = None
            continue
        # Descriptive text: a rule under an area, a note under a subarea (or before its first course)
        if area is None:                                            # program description before "Area 1"
            continue
        if sub is not None and not sub['courses']:
            sub['note'] = (sub['note'] + ' ' + line).strip()
        elif sub is not None:
            area.setdefault('notes', []).append(line)
        else:
            area['rule'] = (area['rule'] + ' ' + line).strip()
    close_course()
    return areas


def finish(sub, course, m):
    text, units = m[1], float(m[2])
    same_as = []
    for group in SAME_AS.findall(text):
        same_as += [c for c in (normalize_code(x) for x in group.split(',')) if c]
    title = ' '.join(SAME_AS.sub('', text).split())
    if course['code'] not in [c[0] for c in sub['courses']]:          # the catalog repeats ES 52 / ES 53 once
        sub['courses'].append((course['code'], title, units, same_as, course['page']))


def minimum_courses(rule):
    m = re.match(r'^\s*(one|two|three|four)\s+course', rule, re.I)
    if not m:
        raise SystemExit(f'No course count in rule: {rule!r}')
    return NUMBER_WORDS[m[1].lower()]


def semester_units(text):
    m = UNITS_NOTE.search(text.replace('/ ', '/'))
    return int(m[1]) if m else None


def main():
    pages = json.loads(PAGES.read_text(encoding='utf-8'))['pages']
    catalog = json.loads(COURSES.read_text(encoding='utf-8'))['courses']
    by_code = {}
    for c in sorted(catalog, key=lambda c: c['type'] != 'credit'):     # credit records first
        by_code.setdefault(c['id'], c)
    areas = parse(certificate_lines(pages))

    requirements, courses, problems = [], {}, []
    for a in areas:
        total = minimum_courses(a['rule'])
        labs = [s for s in a['subareas'] if re.search(r'\bLAB\b', s['name'], re.I)]
        counted = [s for s in a['subareas'] if s not in labs]
        # "one each from 1A, 1B and 1C", "at least one from 3A and one from 3B", "one from 5A and one from 5B"
        per_subarea = total // len(counted) if len(counted) > 1 else total
        if len(counted) > 1 and per_subarea * len(counted) != total:
            raise SystemExit(f'Area {a["id"]}: {total} courses do not split over {len(counted)} subareas')
        requirement = {'id': a['id'], 'source_label': a['source_label'], 'name': titlecase(a['name']),
                       'minimum_courses': total, 'minimum_semester_units': semester_units(a['rule']),
                       'rule': a['rule'], 'subareas': []}
        if labs:
            requirement['laboratory_required'] = True
            requirement['laboratory_subarea'] = labs[0]['id']
        disciplines = re.search(r'\b(one|two|three)\s+(?:different\s+)?(?:academic\s+)?disciplines', a['rule'], re.I)
        if disciplines:
            requirement['minimum_disciplines'] = NUMBER_WORDS[disciplines[1].lower()]
        if a.get('notes'):
            requirement['notes'] = [' '.join(' '.join(a['notes']).split())]
        for s in a['subareas']:
            name = re.sub(r'\s*\(.*$', '', s['name']).strip()
            name = titlecase(name)
            entry = {'id': s['id'], 'name': name.replace('Compostion', 'Composition')}
            if s in labs:
                entry['note'] = ' '.join(s['name'].split('(', 1)[1:]).rstrip(')') + ' ' + s['note']
                entry['note'] = ' '.join(entry['note'].replace(')', '').split())
            else:
                entry['minimum_courses'] = per_subarea
                units = semester_units(s['name'] + ' ' + s['note'])
                if units:
                    entry['minimum_semester_units'] = units
            entry['eligible_course_codes'] = [c[0] for c in s['courses']]
            requirement['subareas'].append(entry)
            for code, title, units, same_as, page in s['courses']:
                record = by_code.get(code)
                row = courses.setdefault(code, {
                    'id': 'chabot:' + code.replace(' ', ''), 'code': code, 'subject': code.split()[0],
                    'number': code.split()[1], 'title': clean(record['title']) if record else title,
                    'semester_units': units, 'cal_getc_areas': [],
                    'same_as': same_as, 'former_codes': [], 'prerequisite': None, 'prerequisite_course_ids': [],
                    'catalog_type': record['type'] if record else None, 'source_pages': []})
                row['cal_getc_areas'].append(s['id'])
                if page not in row['source_pages']:
                    row['source_pages'].append(page)
                if record:
                    formerly = normalize_code(record.get('formerly') or '')
                    if formerly and formerly != code:
                        row['former_codes'] = [formerly]
                    row['prerequisite'] = clean(record.get('prerequisite'))
                    row['prerequisite_course_ids'] = record.get('prerequisite_course_ids') or []
                    catalog_units = (record.get('units') or {}).get('max')
                    if catalog_units and float(catalog_units) != units:
                        problems.append(f'{code}: certificate lists {units} units, course listing {catalog_units}')
                else:
                    problems.append(f'{code}: not in the catalog course listing; title and units from the certificate')
        requirements.append(requirement)

    doc = {
        'schema_version': '1.0.0',
        'dataset_id': 'chabot_cal_getc_2025_2026',
        'source': {
            'publisher': 'Chabot College',
            'document_title': 'Chabot College 2025-2026 Catalog - CAL-GETC Certificate of Achievement',
            'catalog_pages': sorted({p for c in courses.values() for p in c['source_pages']}),
            'academic_year': '2025-2026',
            'effective_terms': 'Fall 2025, Spring 2026 and Summer 2026 (the certificate says the list is '
                               'subject to change each academic year; ASSIST is the official list)',
            'generated_by': 'tools/extract_chabot_cal_getc.py',
        },
        'institution': {'id': 'chabot', 'name': 'Chabot College', 'institution_type': 'California Community College'},
        'ge_pathway': {
            'id': 'cal_getc',
            'name': 'California General Education Transfer Curriculum',
            'display_name': 'Cal-GETC',
            'minimum_total_semester_units': 34,
            'transfer_scope_statement': 'The certificate says Cal-GETC is one option to complete lower-division '
                                        'general education for the University of California or the California '
                                        'State University.',
            'requirements': requirements,
        },
        'courses': sorted(courses.values(), key=lambda c: c['code']),
        'notes': [
            'Chabot College course list for Cal-GETC, transcribed from the catalog certificate; not IGETC and not '
            'the UC 7-course pattern.',
            'The catalog labels Ethnic Studies "Area 7"; Cal-GETC numbers it Area 6, which is used here.',
            'A course listed in 5C and in 5A or 5B may satisfy both (catalog: "courses listed in Area 5C that are '
            'also listed in Area 5A or 5B may be used to satisfy both areas").',
            'cal_getc_areas lists every area the certificate shows a course under.',
        ] + problems,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Wrote {OUT.relative_to(ROOT)}: {len(courses)} courses in {len(requirements)} areas')
    for p in problems:
        print('  note:', p)


if __name__ == '__main__':
    main()
