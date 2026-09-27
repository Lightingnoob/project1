"""Convert the text extraction of the Chabot College 2025-2026 catalog PDF into JSON.

Usage:
    python3 tools/parse_catalog.py CATALOG_TXT [OUT_DIR]

Writes two files to OUT_DIR (default: data/catalog):
    chabot_2025_2026_courses.json  one record per course (credit, noncredit, apprenticeship)
    chabot_2025_2026_pages.json    every catalog page as text, with page number and running header

Only the Python standard library is used. The parser relies on the catalog's layout:
a "<DEPARTMENT> (<CODE>) COURSES" heading starts each course list, and each course
starts with a line "<number> <title> <units> units" (or "<hours> hours" for noncredit).
"""
import json
import re
import sys
from pathlib import Path

CATALOG = 'Chabot College 2025-2026 Catalog'
AMOUNT = r'\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?'
PAGE_NUMBER = re.compile(r'^(?:(\d+) Chabot College 2025-2026|Chabot College 2025-2026 (\d+))\s*$')
DEPT_HEADING = re.compile(r'\(([A-Z]{2,5})\)\s*COURSES\s*$')
PROGRAM_HEADING = re.compile(r"^(?:[A-Z][A-Z &,/’'\-:]+ )?\(([A-Z]{2,5})\)\s*$")
# A program section starts with an all-caps name followed by one of these lines.
PROGRAM_START = re.compile(r'^(Degrees?|Certificates?( of (Achievement|Competency))?)\s*$')
SECTION_INTRO = re.compile(r'^[A-Z][A-Z ]+ (?:COURSES|COURSE LISTING)\s*$')
# "APPRENTICESHIP: AC TRANSIT(APAC)" directly followed by course entries, with no COURSES line.
APPRENTICE_HEADING = re.compile(r'^APPRENTICESHIP:.*\(([A-Z]{2,5})\)\s*$')  # "NONCREDIT COURSES", "MIRRORED COURSES"
ALL_CAPS = re.compile(r"^[A-Z][A-Z &,/’'\-:()]+$")
BARE_HEADING = re.compile(r'^([A-Z]{2,5}) COURSES\s*$')  # "STEM" then "STEM COURSES"
# Physical education departments number courses with letters: ARH1, SMLP, PDBBPrinciples (no space).
LETTER_DEPTS = {'ADPE', 'ATHL', 'DANC', 'KINE', 'PEAC', 'HEAG'}
LETTER_NUMBER = r'[A-Z]{2,4}\d{0,2}'
LETTER_ENTRY = re.compile(rf"^({LETTER_NUMBER})\s*([A-Z][a-z’'].*?)(?:\s+|(?<=[a-z+)]))({AMOUNT})\s+(units?|hours?)\s*$")
LETTER_START = re.compile(rf"^({LETTER_NUMBER})\s*([A-Z][a-z’'].*\S)\s*$")
NUMBER = r'C?\d{1,4}(?:[A-Z]{1,2}\d{0,2})?'  # 1, 1A, C1000, 201W, 5K1
# "C++4 units": the amount may follow punctuation with no space.
# Titles may start with "2-D"/"3-D"; the amount may be glued on ("In C++4 units", "Java4 units").
TITLE_START = r'(?:[A-Z]|\d-?D\b)'  # also 2D, 3D, 2-D
ENTRY = re.compile(rf'^({NUMBER})\s*({TITLE_START}.*?)(?:\s+|(?<=[a-z+)]))({AMOUNT})\s+(units?|hours?)\s*$')
ENTRY_START = re.compile(rf'^({NUMBER})\s*({TITLE_START}.*\S)\s*$')
HOURS = re.compile(r'\b(Lecture|Laboratory|Lab|Activity|Clinical|Field Work|Fieldwork|Work Experience|'
                   r'Laboratory/Activity|Practicum|Studio)\s*:\s*(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*hours?',
                   re.I)
# Labels that end the free-text description; the value runs until the next label.
LABELS = [
    ('prerequisite', r'Prerequisites?'),
    ('corequisite', r'Corequisites?'),
    ('strongly_recommended', r'Strongly Recommended'),
    ('recommended', r'Recommended'),
    ('advisory', r'Advisory'),
    ('enrollment_limitation', r'Enrollment Limitations?'),
]
LABEL_RE = re.compile(r'(?<![A-Za-z])(' + '|'.join(p for _, p in LABELS) + r')\s*:\s*')
RUNNING_HEADER = re.compile(r'^(?:(?:NON)?CREDIT COURSE LISTING|APPRENTICESHIP)(?:,|$)')
SECTION_OF = {'CREDIT COURSE LISTING': 'credit', 'NONCREDIT COURSE LISTING': 'noncredit',
              'APPRENTICESHIP': 'apprenticeship'}
COURSE_ID = re.compile(r'\b([A-Z]{2,5}) (C?\d{1,4}[A-Z]{0,2})\b')


def split_pages(text):
    chunks = []
    for chunk in text.split('\n\n'):
        lines = chunk.split('\n')
        m = PAGE_NUMBER.match(lines[0]) if lines else None
        number = int(m.group(1) or m.group(2)) if m else None
        chunks.append((number, lines[1:] if m else lines))
    # A page's first line is a running header only if it is a listing header or repeats
    # across pages ("STUDENT SERVICES"); otherwise it is content, such as a course entry.
    firsts = {}
    for number, body in chunks:
        if number is not None and body:
            firsts[body[0].strip()] = firsts.get(body[0].strip(), 0) + 1
    pages = []
    for number, body in chunks:
        first = body[0].strip() if number is not None and body else None
        header = first if first and (RUNNING_HEADER.match(first) or first in SECTION_OF
                                     or (firsts[first] > 1 and ALL_CAPS.match(first))) else None
        pages.append({'page': number, 'running_header': header, 'text': '\n'.join(body).strip()})
    return pages


def section_type(header):
    for prefix, kind in SECTION_OF.items():
        if header and header.startswith(prefix):
            return kind
    return None


def number_or_range(text):
    parts = [float(x) for x in re.split(r'\s*-\s*', text)]
    as_num = lambda v: int(v) if v.is_integer() else v
    return {'min': as_num(parts[0]), 'max': as_num(parts[-1])}


def clean(text):
    text = text.replace('\u0002', '-')  # PDF soft hyphen, e.g. "chi-squared"
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r' ([,.;])', r'\1', text)
    return re.sub(r'\( ', '(', re.sub(r' \)', ')', text))


def finish(course):
    body = clean(' '.join(course.pop('_lines')))
    hours = {}
    for m in HOURS.finditer(body):
        key = m.group(1).lower().replace(' ', '_').replace('/', '_')
        key = 'laboratory' if key == 'lab' else key
        hours[key] = number_or_range(m.group(2))
    # Everything before the first hours line or label is the description.
    cut = [m.start() for m in HOURS.finditer(body)] + [m.start() for m in LABEL_RE.finditer(body)]
    first = min(cut) if cut else len(body)
    course['description'] = body[:first].strip()
    tail = body[first:]
    labels = list(LABEL_RE.finditer(tail))
    # Text after the hours with no label (e.g. a placement rule) is kept, not dropped.
    unlabeled = HOURS.sub('', tail[:labels[0].start()] if labels else tail).strip(' .,;')
    if unlabeled:
        course['other_requirements'] = unlabeled
    for m, nxt in zip(LABEL_RE.finditer(tail), list(LABEL_RE.finditer(tail))[1:] + [None]):
        key = next(k for k, p in LABELS if re.fullmatch(p, m.group(1), re.I))
        value = tail[m.end(): nxt.start() if nxt else len(tail)]
        value = HOURS.sub('', value).strip(' .,;')
        if value:
            course.setdefault(key, value)
            course[key] = course[key] if course[key] == value else f'{course[key]}; {value}'
    course['hours'] = hours
    see_also = re.search(r'\((?:see also|same as) ([^)]*)\)', course['description'], re.I)
    course['see_also'] = [f'{d} {n}' for d, n in COURSE_ID.findall(see_also.group(1))] if see_also else []
    if see_also and see_also.start() == 0:
        course['description'] = course['description'][see_also.end():].strip()
    formerly = re.search(r'\(?\bFormerly(?: called)?:? ([^).;]+)', course['description'], re.I)
    course['formerly'] = formerly.group(1).strip() if formerly else None
    for key in ('prerequisite', 'corequisite', 'strongly_recommended', 'recommended'):
        if key in course:
            ids = []
            for d, n in COURSE_ID.findall(course[key]):
                if f'{d} {n}' not in ids:
                    ids.append(f'{d} {n}')
            course[f'{key}_course_ids'] = ids
    return course


def entry(line, kind, dept=None):
    m = ENTRY.match(line) or (LETTER_ENTRY.match(line) if dept in LETTER_DEPTS else None)
    if not m:
        return None
    unit = m.group(4)
    # Noncredit courses are listed in hours, credit and apprenticeship courses in units.
    if unit.startswith('hour') != (kind == 'noncredit'):
        return None
    if HOURS.search(m.group(2)) or LABEL_RE.search(m.group(2)):
        return None
    return m


def parse_courses(pages):
    courses = []
    dept = None
    kind = None
    current = None
    for page in pages:
        page_kind = section_type(page['running_header'])
        lines = page['text'].split('\n')
        if page['page'] is not None and lines and lines[0].strip() == page['running_header']:
            lines = lines[1:]
        if dept and page_kind is None and page['running_header'] is not None:
            # A page that is no longer a course listing ends the department's list.
            dept = None
            if current:
                courses.append(finish(current))
                current = None
        prev = ''
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if PAGE_NUMBER.match(line) or RUNNING_HEADER.match(line):
                i += 1  # a page header that landed mid-chunk
                continue
            heading = DEPT_HEADING.search(prev + ' ' + line) if 'COURSES' in line else None
            bare = BARE_HEADING.match(line)
            apprentice = APPRENTICE_HEADING.match(line)
            if not heading and apprentice and i + 1 < len(lines) and ENTRY.match(lines[i + 1].strip()):
                heading = apprentice
            if not heading and bare and prev.strip() == bare.group(1):
                heading = bare
                if current and current['_lines'] and current['_lines'][-1] == prev:
                    current['_lines'].pop()
            if heading:
                if current:
                    courses.append(finish(current))
                    current = None
                dept, kind = heading.group(1), page_kind or kind or 'credit'
                prev = line
                i += 1
                continue
            ends = (PROGRAM_HEADING.match(line) and not ENTRY.match(line)) or SECTION_INTRO.match(line) \
                or (PROGRAM_START.match(line) and ALL_CAPS.match(prev))
            if dept and ends:
                if current:
                    # Drop an all-caps program name already appended to the last course.
                    if current['_lines'] and current['_lines'][-1] == prev and ALL_CAPS.match(prev):
                        current['_lines'].pop()
                    courses.append(finish(current))
                    current = None
                dept = None
            if dept:
                m = entry(line, kind, dept)
                joined = 0
                starts = ENTRY_START.match(line) or (dept in LETTER_DEPTS and LETTER_START.match(line))
                if not m and starts and len(line) < 75:
                    for extra in (1, 2):
                        rest = [lines[i + k].strip() for k in range(1, extra + 1) if i + k < len(lines)]
                        # A title wraps onto short lines that are not themselves a course or a label line.
                        if len(rest) < extra or any(ENTRY_START.match(r) or ':' in r or len(r) > 75
                                                    or (dept in LETTER_DEPTS and LETTER_ENTRY.match(r)) for r in rest):
                            break
                        m = entry(' '.join([line] + rest), kind, dept)
                        if m:
                            joined = extra
                            break
                if m:
                    if current:
                        courses.append(finish(current))
                    number, title, amount, unit = m.groups()
                    current = {'id': f'{dept} {number}', 'department': dept, 'number': number,
                               'title': clean(title), 'type': kind,
                               'units': number_or_range(amount) if unit.startswith('unit') else None,
                               'noncredit_hours': number_or_range(amount) if unit.startswith('hour') else None,
                               'page': page['page'], '_lines': []}
                    i += 1 + joined
                    prev = line
                    continue
                if current and line:
                    current['_lines'].append(line)
            prev = line
            i += 1
    if current:
        courses.append(finish(current))
    return courses


def main():
    src = Path(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else 'data/catalog')
    text = src.read_text(encoding='utf-8').replace('\r\n', '\n').replace('\r', '\n')
    pages = split_pages(text)
    courses = parse_courses(pages)
    out.mkdir(parents=True, exist_ok=True)
    meta = {'catalog': CATALOG, 'source': src.name,
            'generated_by': 'tools/parse_catalog.py'}
    (out / 'chabot_2025_2026_pages.json').write_text(json.dumps(
        {**meta, 'page_count': len(pages), 'pages': pages}, indent=1, ensure_ascii=False) + '\n')
    (out / 'chabot_2025_2026_courses.json').write_text(json.dumps(
        {**meta, 'course_count': len(courses),
         'notes': ['Parsed from PDF text; titles, units, hours and requisites follow the catalog wording.',
                   'prerequisite_course_ids lists every course code mentioned, whether AND or OR; read the '
                   'prerequisite text for the actual rule.',
                   'Same course number can appear twice when a department has both credit and noncredit '
                   'listings; use id + type as the key.'],
         'courses': courses}, indent=1, ensure_ascii=False) + '\n')
    print(f'{len(pages)} pages, {len(courses)} courses -> {out}')


if __name__ == '__main__':
    main()
