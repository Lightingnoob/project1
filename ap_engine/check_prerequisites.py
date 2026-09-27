"""Structural audit only; does not resolve eligibility, OR choices or corequisites."""
import json
from pathlib import Path
import networkx as nx

ROOT = Path(__file__).resolve().parent
registry = json.loads((ROOT / 'data/chabot_prerequisites.json').read_text())
records = registry['courses']
known = set(records) | {r['course_id'] for r in registry['external_references']}

def walk(expr):
    if expr is None:
        return
    assert expr['type'] in {'NONE', 'COURSE', 'AND', 'OR', 'CONDITION'}
    if expr['type'] == 'COURSE':
        assert expr['course_id'] in known, expr
        assert expr['timing'] in {'before', 'before_or_concurrent', 'same_term'}
        yield expr
    if expr['type'] in {'AND', 'OR'}:
        assert expr['items']
    for item in expr.get('items', []):
        yield from walk(item)

for course_id, record in records.items():
    assert course_id == record['course_id']
    assert record['source_ids']
    assert all(s in registry['sources'] for s in record['source_ids'])
    assert record['requirement'] is not None or record['status'] == 'needs_review'
    for field in ['requirement', 'proposed_requirement', 'published_course_prerequisite']:
        list(walk(record.get(field)))

coverage = {}
covered = set()
for name in ['computer_science', 'computer_engineering', 'biochemistry', 'physics']:
    data = json.loads((ROOT / 'data' / (name + '.json')).read_text())
    ids = {c['id'] for c in data['courses'] if c['institution_id'] == 'chabot'}
    embedded = data['prerequisite_data']['records']
    assert ids == set(embedded)
    assert all(embedded[i] == records[i] for i in ids)
    covered.update(ids)
    coverage[name] = len(ids)
assert covered == {i for i, r in records.items() if r['scope'] == 'major_course'}

def projection(include_proposals):
    graph = nx.DiGraph()
    graph.add_nodes_from(known)
    for course_id, record in records.items():
        expr = record['requirement']
        if expr is None and include_proposals:
            expr = record.get('proposed_requirement')
        for leaf in walk(expr):
            # The union of all OR branches is for cycle detection only.
            # Same-term and optional concurrency relationships are not strict edges.
            if leaf['timing'] == 'before':
                graph.add_edge(leaf['course_id'], course_id)
    cycles = list(nx.simple_cycles(graph))
    return {'nodes':graph.number_of_nodes(), 'edges':graph.number_of_edges(),
            'is_dag':nx.is_directed_acyclic_graph(graph), 'cycles':cycles}

result = {'networkx_version':nx.__version__, 'coverage':coverage,
          'unique_major_courses':len(covered),
          'user_clarified_rules':sorted(i for i,r in records.items() if r['status'] == 'user_clarified'),
          'unresolved_rules':sorted(i for i,r in records.items() if r['requirement'] is None),
          'documented_strict_reference_projection':projection(False),
          'strict_reference_projection_including_proposals':projection(True),
          'limits':['OR branches are combined only to inspect possible cycles; they are not all required.',
                    'Concurrent and same-term links excluded; not a full scheduling constraint check.',
                    'External unknown nodes have unresearched incoming prerequisites.',
                    'Acyclicity is not proof of curriculum accuracy or student eligibility.']}
assert result['documented_strict_reference_projection']['is_dag']
assert result['strict_reference_projection_including_proposals']['is_dag']
(ROOT / 'prerequisite_validation.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
