import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import subprocess
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
import pytest

from tracker.db import Database
from tracker.seed import seed_demo
from tracker.service import Tracker, TrackerError


@pytest.fixture
def tracker(tmp_path):
    return Tracker(Database(str(tmp_path / "test.db")), "workspace-a", "entry-agent")


def make_record(tracker, data=None, title="Example"):
    collection = tracker.create_collection("generic", "Generic records")
    return tracker.create_record(collection["id"], title, {} if data is None else data)


def assert_code(code, function, *args, **kwargs):
    with pytest.raises(TrackerError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_hiring_graph_and_context(tracker):
    ids = seed_demo(tracker)
    person = tracker.get_record_context(ids["kirat"])
    assert {r["source_id"] for r in person["incoming"]} == {ids["kirat_backend"], ids["kirat_frontend"]}
    opening = tracker.get_record_context(ids["junior_backend"])
    assert {r["source_id"] for r in opening["incoming"]} == {ids["kirat_backend"], ids["abhishek_backend"]}
    app = tracker.get_record_context(ids["kirat_backend"])
    assert {r["target_id"] for r in app["outgoing"]} == {ids["kirat"], ids["junior_backend"]}
    assert {r["source_id"] for r in app["incoming"]} == {ids["coding_interview"], ids["design_interview"]}
    interview = tracker.get_record_context(ids["coding_interview"])
    assert {r["source_id"] for r in interview["incoming"]} == {ids["priya_feedback"], ids["rahul_feedback"]}
    assert all(r["linked_record"]["collection_name"] == "feedback" for r in interview["incoming"])
    feedback = tracker.get_record_context(ids["priya_feedback"])["record"]
    assert feedback["data"]["author"] == "Priya"
    assert feedback["created_by"] == "entry-agent"
    assert "incoming" not in interview["outgoing"][0]["linked_record"]  # one hop


def test_history_merge_versions_and_archive(tracker):
    original = make_record(tracker, {"status": "new", "keep": 1, "nested": {"a": 1}})
    updated = tracker.update_record(original["id"], {"status": None, "nested": {"b": 2}}, 1)
    assert updated["data"] == {"status": None, "keep": 1, "nested": {"b": 2}}
    assert updated["version"] == 2
    history = tracker.get_record_history(original["id"])["events"]
    assert history[0]["before"] == original
    assert history[0]["after"] == updated
    assert all(e["actor_id"] == "entry-agent" for e in history)
    assert_code("VERSION_CONFLICT", tracker.update_record, original["id"], {"keep": 4}, 1)
    assert_code("VERSION_CONFLICT", tracker.archive_record, original["id"], 1)
    assert len(tracker.get_record_history(original["id"])["events"]) == 2
    archived = tracker.archive_record(original["id"], 2)
    assert archived["version"] == 3 and archived["archived_at"]
    assert tracker.search_records()["records"] == []
    assert tracker.list_collections()["collections"][0]["active_record_count"] == 0
    assert tracker.get_record_history(original["id"])["events"][0]["operation"] == "archive"
    assert_code("ARCHIVED", tracker.update_record, original["id"], {}, 3)


def test_links_idempotence_and_auditable_correction(tracker):
    a, b, c = [make_record(tracker) for _ in range(3)]
    wrong = tracker.link_records(a["id"], "belongs_to", b["id"])
    assert tracker.link_records(a["id"], "belongs_to", b["id"]) == wrong
    assert len(tracker.get_record_history(a["id"])["events"]) == 2
    tracker.unlink_records(wrong["id"])
    right = tracker.link_records(a["id"], "belongs_to", c["id"])
    assert [r["id"] for r in tracker.get_record_context(a["id"])["outgoing"]] == [right["id"]]
    removed = next(e for e in tracker.get_record_history(a["id"])["events"] if e["operation"] == "unlink")
    assert removed["before"] == wrong and removed["after"] is None
    assert_code("NOT_FOUND", tracker.unlink_records, wrong["id"])


def test_workspace_isolation_and_database_constraints(tracker):
    other = Tracker(tracker.db, "workspace-b", "other-agent")
    local, foreign = make_record(tracker), make_record(other)
    assert_code("NOT_FOUND", tracker.link_records, local["id"], "ref", foreign["id"])
    assert_code("NOT_FOUND", tracker.create_record, foreign["collection_id"], "bad", {})
    assert_code("NOT_FOUND", tracker.get_record_context, foreign["id"])
    assert_code("NOT_FOUND", tracker.get_record_history, foreign["id"])
    assert_code("NOT_FOUND", tracker.update_record, foreign["id"], {}, 1)
    assert_code("NOT_FOUND", tracker.archive_record, foreign["id"], 1)
    assert {r["id"] for r in tracker.search_records()["records"]} == {local["id"]}
    with pytest.raises(sqlite3.IntegrityError), tracker.db.connect(write=True) as con:
        con.execute("INSERT INTO relationships VALUES ('bad', ?, ?, 'ref', ?, 'actor', 'now')",
                    (tracker.workspace, local["id"], foreign["id"]))
    with pytest.raises(sqlite3.IntegrityError), tracker.db.connect(write=True) as con:
        con.execute("UPDATE records SET collection_id=? WHERE id=?", (foreign["collection_id"], local["id"]))
    with pytest.raises(sqlite3.IntegrityError), tracker.db.connect(write=True) as con:
        con.execute("UPDATE records SET data_json='[]' WHERE id=?", (local["id"],))


def test_collection_normalization(tracker):
    first = tracker.create_collection("  People   Profiles ", "Original")
    assert tracker.create_collection("people profiles", "Ignored") == first
    assert tracker.list_collections()["collections"][0]["name"] == "people profiles"


def test_filter_semantics_and_literal_title_search(tracker):
    a = make_record(tracker, {"n": None, "active": True, "num": 1, "a.b": "literal", "obj": {"b": 2, "a": 1}}, "Straße 100%_done")
    make_record(tracker, {"active": 1, "num": 1.0}, "Other")
    def ids(**kwargs):
        return [r["id"] for r in tracker.search_records(**kwargs)["records"]]
    assert ids(query="STRASSE") == [a["id"]]
    assert ids(query="%_") == [a["id"]]
    assert ids(filters={"n": None, "active": True}) == [a["id"]]
    assert ids(filters={"a.b": "literal", "obj": {"a": 1, "b": 2}}) == [a["id"]]
    assert ids(filters={"num": 1}) == [a["id"]]
    assert ids(filters={"n": None, "active": False}) == []
    assert ids(query="' OR 1=1 --") == []


def test_pagination_scope_and_truncation(tracker):
    records = [make_record(tracker, {"long": "x" * 600}) for _ in range(5)]
    # Force timestamp ties to exercise the ID tiebreaker.
    with tracker.db.connect(write=True) as con:
        con.execute("UPDATE records SET created_at='2026-01-01T00:00:00+00:00'")
    cursor, found = None, []
    while True:
        page = tracker.search_records(limit=2, cursor=cursor)
        found.extend(r["id"] for r in page["records"])
        assert all(len(r["data_preview"]) <= 500 and r["data_preview_truncated"] for r in page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == sorted(r["id"] for r in records)
    cursor = tracker.search_records(limit=2)["next_cursor"]
    assert_code("INVALID_INPUT", tracker.search_records, query="changed", cursor=cursor)
    assert_code("INVALID_INPUT", tracker.search_records, cursor="garbage")
    for r in records[1:]:
        tracker.link_records(records[0]["id"], "ref", r["id"])
    context = tracker.get_record_context(records[0]["id"], 1, 1)
    assert context["outgoing_truncated"] and context["events_truncated"]
    assert len(context["outgoing"]) == len(context["events"]) == 1
    events, cursor = [], None
    while True:
        page = tracker.get_record_history(records[0]["id"], 2, cursor)
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(events) == len({e["id"] for e in events}) == 5
    assert events[-1]["operation"] == "create"


@pytest.mark.parametrize("data", [[], "text", {"nan": float("nan")}, {1: "bad key"}, {"big": "x" * 17000}])
def test_reject_invalid_data(tracker, data):
    collection = tracker.create_collection("test", "")
    assert_code("INVALID_INPUT", tracker.create_record, collection["id"], "test", data)
    assert tracker.search_records()["records"] == []


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_invalid_limits(tracker, limit):
    assert_code("INVALID_INPUT", tracker.search_records, limit=limit)


def test_atomic_rollback_on_event_failure(tracker, monkeypatch):
    original = make_record(tracker, {"value": "before"})
    def fail(*args):
        raise RuntimeError("Simulated audit write failure")
    monkeypatch.setattr(tracker, "_event", fail)
    with pytest.raises(RuntimeError):
        tracker.update_record(original["id"], {"value": "after"}, 1)
    assert tracker.get_record_context(original["id"])["record"] == original
    with pytest.raises(RuntimeError):
        tracker.create_record(original["collection_id"], "rollback", {})
    assert len(tracker.search_records()["records"]) == 1


def test_concurrent_updates_only_one_wins(tracker):
    original = make_record(tracker)
    def update(value):
        try:
            return tracker.update_record(original["id"], {"winner": value}, 1)["version"]
        except TrackerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first", "second"]))
    assert sorted(map(str, results)) == ["2", "VERSION_CONFLICT"]
    assert len(tracker.get_record_history(original["id"])["events"]) == 2


def test_seed_refuses_existing_file_and_data_persists(tmp_path):
    path = tmp_path / "demo.db"
    env = {**os.environ, "TRACKER_DB_PATH": str(path)}
    first = subprocess.run([sys.executable, "-m", "tracker.seed"], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    before = path.read_bytes()
    second = subprocess.run([sys.executable, "-m", "tracker.seed"], env=env, capture_output=True, text=True)
    assert second.returncode == 1 and "Refusing" in second.stderr
    assert path.read_bytes() == before
    reopened = Tracker(Database(str(path)))
    assert len(reopened.search_records()["records"]) == 11


def test_mcp_stdio_integration(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "tracker.server"],
            env={**os.environ, "TRACKER_DB_PATH": str(tmp_path / "stdio.db"),
                 "TRACKER_WORKSPACE_ID": "integration", "TRACKER_ACTOR_ID": "stdio-actor"})
        async with Client(params, read_timeout_seconds=10) as client:
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == {
                "list_collections", "create_collection", "search_records", "get_record_context",
                "create_record", "update_record", "link_records", "unlink_records", "get_record_history", "archive_record",
                "discover_collections", "resolve_record", "prepare_write", "commit_write", "get_collection_history"}
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result
                return result.structured_content
            blocked = await call("create_collection", {"name": "Expenses", "description": "Generic"})
            assert blocked["error"]["code"] == "REVIEW_REQUIRED"
            discovery = (await call("discover_collections", {"name": "expenses", "purpose": "Track spending"}))["result"]
            prepared = (await call("prepare_write", {"operation": "create_collection", "arguments": {
                "name": "expenses", "purpose": "Track spending", "record_meaning": "One expense transaction",
                "typical_fields": ["amount", "category"], "relationship_guidance": "May link to a project"},
                "review_ids": [discovery["discovery_id"]], "decision_reason": "No existing collection serves spending"}))["result"]
            collection = (await call("commit_write", {"action_id": prepared["action_id"]}))["result"]
            resolution = (await call("resolve_record", {"query": "Coffee", "collection_id": collection["id"]}))["result"]
            prepared = (await call("prepare_write", {"operation": "create_record", "arguments": {
                "collection_id": collection["id"], "title": "Coffee", "data": {"amount": 5}},
                "review_ids": [resolution["resolution_id"]], "decision_reason": "Record the requested purchase in Expenses"}))["result"]
            created = (await call("commit_write", {"action_id": prepared["action_id"]}))["result"]
            assert created["created_by"] == "stdio-actor"
            assert (await call("search_records", {"filters": {"amount": 5}}))["result"]["records"][0]["id"] == created["id"]
            resolution = (await call("resolve_record", {"query": created["id"]}))["result"]
            assert (await call("prepare_write", {"operation": "update_record", "arguments": {
                "record_id": created["id"], "changes": {}, "expected_version": 9},
                "review_ids": [resolution["resolution_id"]], "decision_reason": "Test stale version"}))["error"]["code"] == "VERSION_CONFLICT"
            # Legacy tools cannot be used to bypass resolution, regardless of valid IDs.
            legacy = {
                "create_record": {"collection_id": collection["id"], "title": "Extra", "data": {}},
                "update_record": {"record_id": created["id"], "changes": {}, "expected_version": 1},
                "archive_record": {"record_id": created["id"], "expected_version": 1},
                "link_records": {"source_id": created["id"], "relationship_type": "ref", "target_id": created["id"]},
                "unlink_records": {"relationship_id": "unknown"},
            }
            for name, arguments in legacy.items():
                assert (await call(name, arguments))["error"]["code"] == "REVIEW_REQUIRED"
            typo = (await call("resolve_record", {"query": "Cofee", "collection_id": collection["id"]}))["result"]
            assert typo["status"] == "needs_clarification"
            assert (await call("prepare_write", {"operation": "update_record", "arguments": {
                "record_id": created["id"], "changes": {"amount": 6}, "expected_version": 1},
                "review_ids": [typo["resolution_id"]], "decision_reason": "Unconfirmed typo"}))["error"]["code"] == "NEEDS_CLARIFICATION"
            assert (await call("search_records", {"limit": 101}))["error"]["code"] == "INVALID_INPUT"
            malformed = await client.call_tool("create_record", {"collection_id": collection["id"], "title": "bad", "data": []})
            assert malformed.is_error  # SDK schema validation before the handler
        # New process/conversation must retrieve persisted context.
        async with Client(params, read_timeout_seconds=10) as client:
            result = await client.call_tool("get_record_context", {"record_id": created["id"]})
            assert result.structured_content["result"]["record"] == created
            replay = await client.call_tool("commit_write", {"action_id": prepared["action_id"]})
            assert replay.structured_content["result"] == created
    asyncio.run(run())
