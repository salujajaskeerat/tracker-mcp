import sqlite3

import pytest

from tracker.batch import Batch
from tracker.db import SCHEMA, Database
from tracker.seed import seed_demo
from tracker.service import Tracker, TrackerError
from tracker.workflow import Workflow


@pytest.fixture
def tracker(tmp_path):
    return Tracker(Database(str(tmp_path / "search.db")), "workspace-a", "entry-agent")


def fails(code, function, *args, **kwargs):
    with pytest.raises(TrackerError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def titles(tracker, **kwargs):
    return [r["title"] for r in tracker.search_records(**kwargs)["records"]]


def test_text_search_reads_values_at_any_depth_but_not_keys(tracker):
    c = tracker.create_collection("applications", "Job applications")
    tracker.create_record(c["id"], "Acme application", {
        "notes": "Met the recruiter at PyCon", "salary": 185000,
        "contact": {"name": "Zoë Müller", "tags": ["referral", "remote"]}})
    tracker.create_record(c["id"], "Globex application", {"notes": "Cold outreach"})
    assert titles(tracker, text="pycon") == ["Acme application"]
    assert titles(tracker, text="zoe muller") == ["Acme application"]  # accents and case fold
    assert titles(tracker, text="referral") == ["Acme application"]
    assert titles(tracker, text="185000") == ["Acme application"]
    assert titles(tracker, text="recruit") == ["Acme application"]  # prefix
    assert titles(tracker, text="pycon outreach") == []  # every word must match one record
    assert titles(tracker, text="notes") == []  # key names are not content
    assert set(titles(tracker, text="application")) == {"Acme application", "Globex application"}
    hit = tracker.search_records(text="pycon")["records"][0]
    assert "[PyCon]" in hit["match_snippet"]


@pytest.mark.parametrize("text", ['AND', 'a OR b', '"unbalanced', 'title:x', '-x', 'x*', 'NEAR(a b)', "it's (odd)"])
def test_text_search_treats_fts_operators_as_plain_words(tracker, text):
    c = tracker.create_collection("notes", "Notes")
    tracker.create_record(c["id"], "Plain", {"body": "nothing relevant"})
    assert tracker.search_records(text=text)["records"] == []


@pytest.mark.parametrize("text", ["", "   ", "!!!", "x" * 301, 5])
def test_text_search_rejects_unusable_input(tracker, text):
    fails("INVALID_INPUT", tracker.search_records, text=text)


def test_index_follows_updates_archives_and_workspaces(tracker):
    c = tracker.create_collection("people", "People")
    person = tracker.create_record(c["id"], "Kirat", {"city": "Pune"})
    assert titles(tracker, text="pune") == ["Kirat"]
    tracker.update_record(person["id"], {"city": "Delhi"}, 1)
    assert titles(tracker, text="pune") == [] and titles(tracker, text="delhi") == ["Kirat"]
    other = Tracker(tracker.db, "workspace-b", "entry-agent")
    assert other.search_records(text="delhi")["records"] == []
    tracker.archive_record(person["id"], 2)
    assert titles(tracker, text="delhi") == []
    # Archived text stays indexed; only the active-record filter hides it.
    with tracker.db.connect() as con:
        assert con.execute("SELECT count(*) FROM record_fts WHERE record_fts MATCH 'delhi'").fetchone()[0] == 1


def test_text_combines_with_collection_query_and_filters(tracker):
    a = tracker.create_collection("applications", "Applications")
    b = tracker.create_collection("interviews", "Interviews")
    tracker.create_record(a["id"], "Acme backend", {"status": "open", "notes": "kubernetes heavy"})
    tracker.create_record(a["id"], "Globex backend", {"status": "closed", "notes": "kubernetes too"})
    tracker.create_record(b["id"], "Acme onsite", {"notes": "asked about kubernetes"})
    assert len(titles(tracker, text="kubernetes")) == 3
    assert set(titles(tracker, text="kubernetes", collection_id=a["id"])) == {"Acme backend", "Globex backend"}
    assert titles(tracker, text="kubernetes", filters={"status": "open"}) == ["Acme backend"]
    assert set(titles(tracker, text="kubernetes", query="acme")) == {"Acme backend", "Acme onsite"}


def test_text_search_ranks_title_matches_first_and_paginates(tracker):
    c = tracker.create_collection("notes", "Notes")
    for index in range(6):
        tracker.create_record(c["id"], f"Note {index}", {"body": "mentions rust once among many other words"})
    tracker.create_record(c["id"], "Rust", {"body": "unrelated"})
    full = tracker.search_records(text="rust", limit=100)["records"]
    assert full[0]["title"] == "Rust" and len(full) == 7
    paged, cursor = [], None
    while True:
        page = tracker.search_records(text="rust", limit=2, cursor=cursor)
        paged += page["records"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [r["id"] for r in paged] == [r["id"] for r in full]
    cursor = tracker.search_records(text="rust", limit=2)["next_cursor"]
    fails("INVALID_INPUT", tracker.search_records, text="other", limit=2, cursor=cursor)
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=cursor)


def test_existing_database_is_backfilled_once_and_survives_vacuum(tmp_path):
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO collections VALUES ('c', 'home', 'people', 'Legacy', 'then')")
    con.execute("""INSERT INTO records VALUES ('p','home','c','Kirat','{"team":"platform"}',1,'actor','then','then',NULL)""")
    con.commit()
    con.close()
    t = Tracker(Database(path), "home")
    assert titles(t, text="platform") == ["Kirat"]
    t.create_record("c", "Asha", {"team": "payments"})
    con = sqlite3.connect(path)
    con.execute("VACUUM")
    con.close()
    reopened = Tracker(Database(path), "home")
    assert titles(reopened, text="platform") == ["Kirat"]
    assert titles(reopened, text="payments") == ["Asha"]
    with reopened.db.connect() as con:
        assert con.execute("SELECT count(*) FROM record_fts").fetchone()[0] == 2


def test_previews_leave_no_index_entries(tracker):
    w = Workflow(tracker)
    c = tracker.create_collection("people", "People")
    review = w.resolve_record("Ghost", c["id"])
    action = w.prepare_write("create_record", {"collection_id": c["id"], "title": "Ghost",
                             "data": {"note": "ectoplasm"}}, [review["resolution_id"]], "New person")
    assert titles(tracker, text="ectoplasm") == []
    w.commit_write(action["action_id"])
    assert titles(tracker, text="ectoplasm") == ["Ghost"]


def test_without_fts5_only_text_search_is_unavailable(tracker):
    record = tracker.create_record(tracker.create_collection("people", "People")["id"], "Kirat", {})
    tracker.db.fts = False
    fails("UNSUPPORTED", tracker.search_records, text="kirat")
    assert [r["id"] for r in tracker.search_records(query="kir")["records"]] == [record["id"]]


def test_batch_read_text_search_matches_single_call(tracker):
    seed_demo(tracker)
    single = tracker.search_records(text="backend", limit=3)
    batch = Batch(Workflow(tracker)).read([dict(operation="search_records", text="backend", limit=3,
                                                include_context=True)])["results"][0]["result"]
    assert batch["records"] == single["records"] and batch["next_cursor"] == single["next_cursor"]
    assert [c["record"]["id"] for c in batch["contexts"]] == [r["id"] for r in single["records"]]


def test_traverse_walks_the_hiring_chain_in_one_call(tracker):
    ids = seed_demo(tracker)
    walk = tracker.traverse(ids["kirat"], max_depth=3)
    by_id = {n["id"]: n for n in walk["nodes"]}
    assert by_id[ids["kirat_backend"]]["depth"] == 1
    assert by_id[ids["junior_backend"]]["depth"] == 2
    assert by_id[ids["coding_interview"]]["depth"] == 2
    feedback = by_id[ids["priya_feedback"]]
    assert feedback["depth"] == 3
    assert [s["record_id"] for s in feedback["path"]] == [ids["kirat_backend"], ids["coding_interview"], ids["priya_feedback"]]
    assert all(s["direction"] == "incoming" and s["title"] for s in feedback["path"])
    assert ids["kirat"] not in by_id and walk["start"]["id"] == ids["kirat"]
    assert not walk["truncated"] and walk["reached_count"] == len(walk["nodes"])
    walked = {e["id"] for e in walk["edges"]}
    assert {s["relationship_id"] for n in walk["nodes"] for s in n["path"]} <= walked
    # Depth is a hard bound.
    assert ids["priya_feedback"] not in {n["id"] for n in tracker.traverse(ids["kirat"], max_depth=2)["nodes"]}


def test_traverse_direction_type_and_collection_filters(tracker):
    ids = seed_demo(tracker)
    app = tracker.get_record_context(ids["kirat_backend"])
    assert tracker.traverse(ids["kirat"], direction="outgoing")["nodes"] == []
    outgoing = {n["id"] for n in tracker.traverse(ids["kirat_backend"], max_depth=1, direction="outgoing")["nodes"]}
    assert outgoing == {ids["kirat"], ids["junior_backend"]}
    opening_type = next(r["relationship_type"] for r in app["outgoing"] if r["target_id"] == ids["junior_backend"])
    only = tracker.traverse(ids["kirat_backend"], max_depth=1, relationship_types=[opening_type])
    assert {n["id"] for n in only["nodes"]} == {ids["junior_backend"]}
    # The collection filter hides intermediate applications without blocking the route.
    opening_collection = tracker.get_record_context(ids["junior_backend"])["record"]["collection_id"]
    openings = tracker.traverse(ids["kirat"], max_depth=2, collection_id=opening_collection)
    assert {n["id"] for n in openings["nodes"]} == {ids["junior_backend"], ids["senior_frontend"]}
    assert openings["reached_count"] > len(openings["nodes"])
    assert all(n["path"][0]["record_id"] in {ids["kirat_backend"], ids["kirat_frontend"]} for n in openings["nodes"])


def test_traverse_handles_cycles_limits_and_scope(tracker):
    c = tracker.create_collection("nodes", "Nodes")
    a, b, d = (tracker.create_record(c["id"], name, {}) for name in "ABD")
    tracker.link_records(a["id"], "next", b["id"])
    tracker.link_records(b["id"], "next", d["id"])
    tracker.link_records(d["id"], "next", a["id"])
    cycle = tracker.traverse(a["id"], max_depth=4, direction="outgoing")
    assert [(n["title"], n["depth"]) for n in cycle["nodes"]] == [("B", 1), ("D", 2)]
    assert len(cycle["edges"]) == 3 and not cycle["truncated"]
    hub = tracker.create_record(c["id"], "Hub", {})
    for index in range(5):
        tracker.link_records(tracker.create_record(c["id"], f"Leaf {index}", {})["id"], "member", hub["id"])
    capped = tracker.traverse(hub["id"], max_depth=1, max_nodes=3)
    assert capped["truncated"] and len(capped["nodes"]) == 3
    tracker.archive_record(b["id"], 1)
    assert tracker.traverse(a["id"], max_depth=1, direction="outgoing")["nodes"][0]["archived_at"]
    fails("NOT_FOUND", Tracker(tracker.db, "workspace-b").traverse, a["id"])
    fails("NOT_FOUND", tracker.traverse, a["id"], collection_id="missing")
    for bad in (dict(max_depth=0), dict(max_depth=5), dict(max_nodes=201), dict(direction="sideways"),
                dict(relationship_types=["x"] * 21), dict(relationship_types=[""])):
        fails("INVALID_INPUT", tracker.traverse, a["id"], **bad)
