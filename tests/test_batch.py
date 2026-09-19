import asyncio
import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tracker.batch import Batch
from tracker.db import Database
from tracker.service import Tracker, TrackerError
from tracker.workflow import Workflow


@pytest.fixture
def setup(tmp_path):
    t = Tracker(Database(str(tmp_path / 'batch.db')), 'test', 'agent')
    c = t.create_collection('people', 'One person')
    records = [t.create_record(c['id'], name, {}) for name in ['Kirat', 'Abhishek']]
    return t, Batch(Workflow(t)), records


def actions(b, records):
    reviews = b.resolve([{'query': r['id']} for r in records])['results']
    return [dict(operation='update_record', arguments=dict(record_id=r['id'],
                changes={'city': 'Delhi'}, expected_version=r['version']),
                review_ids=[review['result']['resolution_id']], decision_reason='User requested city update')
            for r, review in zip(records, reviews)]


def failure(code, fn, *args):
    with pytest.raises(TrackerError) as error:
        fn(*args)
    assert error.value.code == code
    return error.value


def test_reads_context_pagination_and_history(setup):
    t, b, records = setup
    t.link_records(records[0]['id'], 'knows', records[1]['id'])
    request = dict(operation='search_records', limit=1, include_context=True)
    page = b.read([request, dict(operation='list_collections')])['results'][0]['result']
    assert page['next_cursor']
    context = page['contexts'][0]
    assert context == t.get_record_context(page['records'][0]['id'], event_limit=0)
    assert context['events_included'] is False and context['events_truncated'] is None
    next_page = b.read([{**request, 'cursor': page['next_cursor']}])['results'][0]['result']
    assert next_page['records'][0]['id'] != page['records'][0]['id']
    full = b.read([dict(operation='get_record_context', record_id=records[0]['id'], event_limit=100)])
    assert full['results'][0]['result'] == t.get_record_context(records[0]['id'], event_limit=100)
    e = failure('NOT_FOUND', b.read, [dict(operation='list_collections'), dict(operation='get_record_context', record_id='missing')])
    assert e.details['item_index'] == 1


@pytest.mark.parametrize('requests', [[], [{'operation': 'list_collections'}]*11,
    [{'operation': 'search_records', 'limit': 60}]*2, [{'operation': 'search_records', 'typo': True}]])
def test_read_bounds(setup, requests):
    failure('INVALID_INPUT', setup[1].read, requests)


def test_preview_commit_audit_and_restart_replay(setup):
    t, b, records = setup
    prepared = b.prepare(actions(b, records))
    assert all(t.get_record_context(r['id'])['record'] == r for r in records)
    result = b.commit(prepared['action_id'])
    assert result['atomic'] and len(result['results']) == 2
    for i, r in enumerate(records):
        events = t.get_record_history(r['id'])['events']
        audit = next(e['after'] for e in events if e['operation'] == 'decision')
        assert audit['batch_item_index'] == i and audit['action_id'] == prepared['action_id']
        assert audit['evidence'][0]['candidate_ids'] == [r['id']]
        assert t.get_record_context(r['id'])['record']['version'] == 2
    reopened = Batch(Workflow(Tracker(Database(t.db.path), 'test', 'agent')))
    assert reopened.commit(prepared['action_id']) == result
    assert all(len(t.get_record_history(r['id'])['events']) == 3 for r in records)


def test_later_failure_rolls_back_preview_and_commit(setup, monkeypatch):
    t, b, records = setup
    items = actions(b, records)
    items[1]['arguments']['expected_version'] = 99
    assert failure('VERSION_CONFLICT', b.prepare, items).details['item_index'] == 1
    assert all(t.get_record_context(r['id'])['record'] == r for r in records)
    items[1]['arguments']['expected_version'] = 1
    prepared = b.prepare(items)
    original = b.w._audit_decision
    def broken(con, payload, result, action_id):
        if payload['batch_item_index'] == 1:
            raise RuntimeError('Audit failure on second item')
        original(con, payload, result, action_id)
    monkeypatch.setattr(b.w, '_audit_decision', broken)
    with pytest.raises(RuntimeError):
        b.commit(prepared['action_id'])
    assert all(t.get_record_context(r['id'])['record'] == r for r in records)
    assert all(len(t.get_record_history(r['id'])['events']) == 1 for r in records)
    monkeypatch.setattr(b.w, '_audit_decision', original)
    assert b.commit(prepared['action_id'])['atomic']


def test_staleness_expiry_and_scope(setup):
    t, b, records = setup
    prepared = b.prepare(actions(b, records))
    for workspace, actor in [('other', 'agent'), ('test', 'other')]:
        failure('NOT_FOUND', Batch(Workflow(Tracker(t.db, workspace, actor))).commit, prepared['action_id'])
    t.link_records(records[0]['id'], 'knows', records[1]['id'])
    failure('STALE_REVIEW', b.commit, prepared['action_id'])
    prepared = b.prepare(actions(b, records))
    with t.db.connect(write=True) as con:
        con.execute("UPDATE workflow_tickets SET expires_at='2000' WHERE id=?", (prepared['action_id'],))
    failure('STALE_REVIEW', b.commit, prepared['action_id'])


def test_typo_requires_per_item_clarification(setup):
    t, b, records = setup
    items = actions(b, records)
    typo = b.resolve([{'query': 'Kriat'}])['results'][0]['result']
    items[0]['review_ids'] = [typo['resolution_id']]
    items[1]['clarification'] = 'User identified Abhishek'
    failure('NEEDS_CLARIFICATION', b.prepare, items)
    items[0]['clarification'] = 'User confirmed they mean Kirat'
    assert b.commit(b.prepare(items)['action_id'])['atomic']


def test_sequential_versions_and_link_reviews(setup):
    t, b, records = setup
    items = actions(b, records)
    second = {**items[0], 'arguments': {**items[0]['arguments'], 'expected_version': 2, 'changes': {'country': 'India'}}}
    result = b.commit(b.prepare([items[0], second])['action_id'])
    assert result['results'][1]['result']['version'] == 3
    items = actions(b, [t.get_record_context(r['id'])['record'] for r in records])
    link = dict(operation='link_records', arguments=dict(source_id=records[0]['id'], target_id=records[1]['id'], relationship_type='knows'),
                review_ids=items[0]['review_ids'], decision_reason='User described relationship')
    failure('REVIEW_REQUIRED', b.prepare, [link])
    link['review_ids'] += items[1]['review_ids']
    assert b.commit(b.prepare([items[0], link])['action_id'])['atomic']


def test_shared_fingerprint_and_response_rollback(setup, monkeypatch):
    import tracker.batch as module
    t, b, records = setup
    count = 0
    original = b.w._fingerprint
    def counted(con):
        nonlocal count
        count += 1
        return original(con)
    monkeypatch.setattr(b.w, '_fingerprint', counted)
    items = actions(b, records)
    assert count == 1
    prepared = b.prepare(items)
    assert count == 2
    monkeypatch.setattr(module, 'MAX_RESPONSE_BYTES', 1)
    failure('RESPONSE_TOO_LARGE', b.commit, prepared['action_id'])
    assert all(t.get_record_context(r['id'])['record'] == r for r in records)
    with t.db.connect() as con:
        before = con.execute('SELECT count(*) FROM workflow_tickets').fetchone()[0]
    failure('RESPONSE_TOO_LARGE', b.resolve, [{'query': records[0]['id']}])
    with t.db.connect() as con:
        assert con.execute('SELECT count(*) FROM workflow_tickets').fetchone()[0] == before


def test_batch_cannot_bypass_collection_discovery(setup):
    failure('INVALID_INPUT', setup[1].prepare, [dict(operation='create_collection', arguments={}, review_ids=['x'], decision_reason='test')])


def test_batch_stdio(setup):
    t, b, records = setup
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'tracker.server'],
            env={**os.environ, 'TRACKER_DB_PATH': t.db.path, 'TRACKER_WORKSPACE_ID': 'test', 'TRACKER_ACTOR_ID': 'agent'})
        async with Client(params, read_timeout_seconds=10) as client:
            async def call(name, **args):
                response = await client.call_tool(name, args)
                assert not response.is_error, response
                assert response.structured_content['ok'], response
                return response.structured_content['result']
            read = await call('batch_read', requests=[dict(operation='search_records', include_context=True)])
            assert len(read['results'][0]['result']['contexts']) == 2
            resolved = await call('batch_resolve_records', requests=[{'query': r['id']} for r in records])
            items = [dict(operation='update_record', arguments=dict(record_id=r['id'], expected_version=1, changes={'done': True}),
                          review_ids=[review['result']['resolution_id']], decision_reason='Requested update')
                     for r, review in zip(records, resolved['results'])]
            prepared = await call('prepare_batch_write', actions=items)
            result = await call('commit_batch_write', action_id=prepared['action_id'])
        async with Client(params, read_timeout_seconds=10) as client:
            replay = await client.call_tool('commit_batch_write', {'action_id': prepared['action_id']})
            assert replay.structured_content['result'] == result
    asyncio.run(run())
