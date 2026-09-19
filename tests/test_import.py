import pytest

from tracker.db import Database
from tracker.importer import Importer
from tracker.service import Tracker, TrackerError
from tracker.workflow import Workflow


@pytest.fixture
def setup(tmp_path):
    t = Tracker(Database(str(tmp_path / 'import.db')), 'home', 'assistant')
    people = t.create_collection('people', 'One person')
    return t, Workflow(t), Importer(Workflow(t)), people


def fails(code, fn, *args, **kwargs):
    with pytest.raises(TrackerError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def rows(*titles):
    return [{'key': f'r{i}', 'title': title, 'data': {'n': i}} for i, title in enumerate(titles)]


def test_clean_import_creates_records_links_and_audit(setup):
    t, w, imp, people = setup
    company = t.create_record(t.create_collection('companies', 'Employers')['id'], 'Acme', {})
    review = w.resolve_record('Acme')
    prepared = imp.prepare(people['id'], rows('Rohan Mehta', 'Priya Raman'),
        links=[{'source': {'row': 'r0'}, 'relationship_type': 'works_at', 'target': {'record_id': company['id']}},
               {'source': {'row': 'r1'}, 'relationship_type': 'knows', 'target': {'row': 'r0'}}],
        review_ids=[review['resolution_id']], decision_reason='Onboarding list from the user')
    assert prepared['needs_decision'] == [] and prepared['preview'] == {'records': 2, 'links': 2}
    assert t.search_records(collection_id=people['id'])['records'] == []  # nothing written yet
    result = imp.commit(prepared['action_id'])
    assert [c['title'] for c in result['created']] == ['Rohan Mehta', 'Priya Raman']
    rohan = t.get_record_context(result['created'][0]['id'])
    assert rohan['record']['data'] == {'n': 0} and rohan['record']['created_by'] == 'assistant'
    assert {r['relationship_type'] for r in rohan['outgoing']} == {'works_at'}
    assert {r['relationship_type'] for r in rohan['incoming']} == {'knows'}
    decision = next(e for e in rohan['events'] if e['operation'] == 'decision')['after']
    assert decision['action_id'] == prepared['action_id'] and decision['row_key'] == 'r0'
    event = t.get_collection_history(people['id'])['events'][0]
    assert event['operation'] == 'import' and event['after']['created'] == 2
    assert t.search_records(text='rohan')['records'][0]['id'] == rohan['record']['id']  # indexed by triggers
    assert imp.commit(prepared['action_id']) == result  # replay does not write twice
    assert len(t.search_records(collection_id=people['id'])['records']) == 2


def test_possible_duplicates_need_a_decision_per_row(setup):
    t, w, imp, people = setup
    existing = t.create_record(people['id'], 'Rohan Mehta', {})
    archived = t.create_record(people['id'], 'Sara Thomas', {})
    t.archive_record(archived['id'], 1)
    prepared = imp.prepare(people['id'], rows('Rohan Mehta', 'Rohan Mehtaa', 'Sara Thomas', 'Brand New'),
                           decision_reason='CSV import')
    report = {r['key']: r for r in prepared['rows']}
    assert prepared['needs_decision'] == ['r0', 'r1', 'r2']
    assert report['r0']['candidates'][0] == {'id': existing['id'], 'title': 'Rohan Mehta', 'archived_at': None, 'similarity': 1.0}
    assert report['r1']['candidates'][0]['id'] == existing['id'] and report['r1']['similar_rows_in_this_import'] == ['r0']
    assert report['r2']['candidates'][0]['archived_at'] and report['r3']['status'] == 'clean'
    error = fails('NEEDS_CLARIFICATION', imp.commit, prepared['action_id'], {'r0': {'action': 'skip'}})
    assert error.details['missing'] == ['r1', 'r2']
    decisions = {'r0': {'action': 'use_existing', 'record_id': existing['id']}, 'r1': {'action': 'skip'},
                 'r2': {'action': 'create_anyway'}}
    fails('NEEDS_CLARIFICATION', imp.commit, prepared['action_id'], decisions)  # no reason given
    fails('INVALID_INPUT', imp.commit, prepared['action_id'], {**decisions, 'r0': {'action': 'use_existing', 'record_id': archived['id']}})
    fails('INVALID_INPUT', imp.commit, prepared['action_id'], {**decisions, 'r1': {'action': 'merge'}})
    fails('NEEDS_CLARIFICATION', imp.commit, prepared['action_id'], {**decisions, 'r3': {'action': 'skip'}})  # clean row
    assert len(t.search_records(collection_id=people['id'])['records']) == 1  # failed commits wrote nothing
    decisions['r2'] = {'action': 'create_anyway', 'clarification': 'User confirmed a different Sara applied'}
    result = imp.commit(prepared['action_id'], decisions)
    assert [c['title'] for c in result['created']] == ['Sara Thomas', 'Brand New']
    assert result['reused'] == [{'key': 'r0', 'record_id': existing['id']}] and result['skipped'] == ['r1']


def test_rows_that_only_resemble_each_other(setup):
    t, w, imp, people = setup
    prepared = imp.prepare(people['id'], rows('Anita Desai', 'anita  DESAI', 'Vikram'), decision_reason='Pasted list')
    assert prepared['needs_decision'] == ['r0', 'r1']
    assert all(r['status'] == 'batch_duplicate' for r in prepared['rows'][:2])
    both = {'r0': {'action': 'create_anyway'}, 'r1': {'action': 'create_anyway'}}
    fails('NEEDS_CLARIFICATION', imp.commit, prepared['action_id'], both)  # two namesakes need a reason
    fails('INVALID_INPUT', imp.commit, prepared['action_id'], {**both, 'r1': {'action': 'use_existing', 'record_id': 'x'}})
    result = imp.commit(prepared['action_id'], {'r0': {'action': 'create_anyway'}, 'r1': {'action': 'skip'}})
    assert [c['key'] for c in result['created']] == ['r0', 'r2']


def test_links_follow_decisions_and_existing_records_need_reviews(setup):
    t, w, imp, people = setup
    existing = t.create_record(people['id'], 'Rohan Mehta', {})
    company = t.create_record(t.create_collection('companies', 'Employers')['id'], 'Acme', {})
    link = lambda key: {'source': {'row': key}, 'relationship_type': 'works_at', 'target': {'record_id': company['id']}}
    fails('REVIEW_REQUIRED', imp.prepare, people['id'], rows('New Person'), links=[link('r0')], decision_reason='x')
    review = w.resolve_record('Acme')
    prepared = imp.prepare(people['id'], rows('Rohan Mehta', 'Rohan M'), links=[link('r0'), link('r1')],
                           review_ids=[review['resolution_id']], decision_reason='x')
    result = imp.commit(prepared['action_id'], {'r0': {'action': 'use_existing', 'record_id': existing['id']},
                                                'r1': {'action': 'skip'}})
    assert [l['source_id'] for l in result['links']] == [existing['id']]  # link attached to the existing record
    assert result['links_dropped'] == [{'link_index': 1, 'reason': 'an endpoint row was skipped'}]
    assert [r['source_id'] for r in t.get_record_context(company['id'])['incoming']] == [existing['id']]


def test_import_is_atomic_and_goes_stale_when_names_change(setup):
    t, w, imp, people = setup
    other = t.create_collection('expenses', 'Money')
    prepared = imp.prepare(people['id'], rows('Asha', 'Meera'), decision_reason='x')
    t.create_record(other['id'], 'Coffee', {})  # unrelated collection: still valid
    t.create_record(people['id'], 'Asha', {})   # a namesake appeared after the review
    fails('STALE_REVIEW', imp.commit, prepared['action_id'])
    archived = t.create_record(other['id'], 'Old vendor', {})
    t.archive_record(archived['id'], 1)
    review = w.resolve_record(archived['id'])
    prepared = None
    error = fails('ARCHIVED', imp.prepare, people['id'], rows('Devika'), review_ids=[review['resolution_id']],
        clarification='User named this vendor', decision_reason='x',
        links=[{'source': {'row': 'r0'}, 'relationship_type': 'paid', 'target': {'record_id': archived['id']}}])
    assert error and [r['title'] for r in t.search_records(collection_id=people['id'])['records']] == ['Asha']
    foreign = Importer(Workflow(Tracker(t.db, 'elsewhere', 'assistant')))
    fails('NOT_FOUND', foreign.prepare, people['id'], rows('X'), decision_reason='x')


@pytest.mark.parametrize('kwargs', [
    dict(rows=[]), dict(rows=rows(*['P'] * 101)), dict(rows=rows('A') + rows('B')),  # duplicate keys
    dict(rows=[{'key': 'a', 'title': 'A', 'data': {}, 'extra': 1}]), dict(rows=[{'key': 'a', 'title': '  ', 'data': {}}]),
    dict(rows=[{'key': 'a', 'title': 'A', 'data': {'big': 'x' * 17000}}]),
    dict(rows=rows('A'), links=[{'source': {'row': 'zz'}, 'relationship_type': 't', 'target': {'row': 'r0'}}]),
    dict(rows=rows('A'), links=[{'source': {'row': 'r0', 'record_id': 'x'}, 'relationship_type': 't', 'target': {'row': 'r0'}}]),
    dict(rows=rows('A'), links=[{'source': {}, 'relationship_type': 't', 'target': {'row': 'r0'}}]),
    dict(rows=rows('A'), decision_reason=' '), dict(rows=rows('A'), review_ids=['x'] * 13)])
def test_import_bounds(setup, kwargs):
    t, w, imp, people = setup
    fails('INVALID_INPUT', imp.prepare, people['id'], **{'decision_reason': 'x', **kwargs})
    assert t.search_records()['records'] == []


def test_duplicate_review_is_bounded_and_matches_the_plain_scorer(setup, monkeypatch):
    import random
    import tracker.importer as module
    from tracker.workflow import normalize, score_normalized
    t, w, imp, people = setup
    rng = random.Random(5)
    name = lambda: ''.join(rng.choices('abcde ', k=rng.randint(2, 9))).strip() or 'x'
    existing = [name() for _ in range(300)]
    for title in existing:
        t.create_record(people['id'], title, {})
    batch = [{'key': str(i), 'title': name(), 'data': {}} for i in range(40)]
    report = imp.prepare(people['id'], batch, decision_reason='x')['rows']
    for row in report:  # the shortcuts must flag exactly what scoring every pair would flag
        expected = {normalize(e) for e in existing if score_normalized(normalize(row['title']), normalize(e))}
        assert bool(row['candidates']) == bool(expected)
        assert {normalize(c['title']) for c in row['candidates']} <= expected
    monkeypatch.setattr(module, 'MAX_COMPARISONS', 300 * 10)
    error = fails('INVALID_INPUT', imp.prepare, people['id'], batch, decision_reason='x')
    assert 'at most 10 rows' in str(error)
    assert imp.prepare(people['id'], batch[:10], decision_reason='x')['action_id']
