"""Scenario tests for the agent-facing safety workflow, not claims of LLM accuracy."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from tracker.db import Database
from tracker.seed import seed_demo
from tracker.service import Tracker, TrackerError
from tracker.workflow import Workflow


@pytest.fixture
def setup(tmp_path):
    tracker = Tracker(Database(str(tmp_path / "workflow.db")), "home", "assistant")
    return tracker, Workflow(tracker)


def fails(code, fn, *args, **kwargs):
    with pytest.raises(TrackerError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code


def collection_args(name="expenses", purpose="Track money spent"):
    return dict(name=name, purpose=purpose, record_meaning="One expense transaction",
                typical_fields=["amount", "category", "date"],
                relationship_guidance="May link to a project or person; unknown details stay missing")


def prepare_collection(w, args, clarification=None):
    discovery = w.discover_collections(args["name"], args["purpose"])
    return w.prepare_write("create_collection", args, [discovery["discovery_id"]],
                           "Reviewed existing purposes; this organizes the requested information.", clarification)


def create_collection(w, **kwargs):
    return w.commit_write(prepare_collection(w, collection_args(**kwargs))["action_id"])


def make_person(t, title="Kirat"):
    c = t.create_collection("people", "One person; identity and contact information")
    return t.create_record(c["id"], title, {})


def selection(w, query, collection_id=None, context_record_ids=None):
    return w.resolve_record(query, collection_id, context_record_ids)["resolution_id"]


def test_collection_clarity_creation_history_and_replay(setup):
    t, w = setup
    action = prepare_collection(w, collection_args())
    assert t.list_collections()["collections"] == []  # preview is a rollback
    c = w.commit_write(action["action_id"])
    assert "One record: One expense transaction" in c["description"]
    assert "Typical information (optional): amount, category, date" in c["description"]
    assert w.commit_write(action["action_id"]) == c
    reopened = Workflow(Tracker(Database(t.db.path), "home", "assistant"))
    assert reopened.commit_write(action["action_id"]) == c
    events = t.get_collection_history(c["id"])["events"]
    assert [e["operation"] for e in events] == ["decision", "create"]
    assert events[0]["after"]["decision_reason"].startswith("Reviewed")
    assert events[0]["actor_id"] == "assistant"
    assert "agent-reported" in events[0]["after"]["attribution_note"]


def test_collection_typo_overlap_and_exact_reuse(setup):
    t, w = setup
    first = create_collection(w)
    discovery = w.discover_collections("expneses", "Track money spent")
    assert discovery["candidates"][0]["id"] == first["id"]
    fails("NEEDS_CLARIFICATION", w.prepare_write, "create_collection", collection_args("expneses"),
          [discovery["discovery_id"]], "A guessed spelling must not create another collection")
    exact = prepare_collection(w, collection_args(" EXPENSES "))
    assert w.commit_write(exact["action_id"])["id"] == first["id"]
    assert len(t.list_collections()["collections"]) == 1
    travel = collection_args("travel expenses", "Track money spent on travel")
    fails("NEEDS_CLARIFICATION", prepare_collection, w, travel)
    action = prepare_collection(w, travel, "User explicitly keeps business travel separate from personal spending.")
    distinct = w.commit_write(action["action_id"])
    assert distinct["id"] != first["id"]


def test_purpose_evidence_even_with_different_names(setup):
    t, w = setup
    people = t.create_collection("people", "One person including candidates and customers, with contact information")
    person = t.create_record(people["id"], "Kirat", {"email": "example@example.test"})
    d = w.discover_collections("applicants", "Track candidates with contact information")
    assert d["candidates"][0]["id"] == people["id"]
    assert d["catalog"][0]["examples"][0]["id"] == person["id"]
    assert any("purpose words" in r for r in d["catalog"][0]["reasons"])
    # Even no lexical overlap returns the complete catalog for agent interpretation.
    d = w.discover_collections("humans", "Recruitment directory")
    assert d["catalog"][0]["id"] == people["id"]
    assert d["complete"]


def test_collection_rename_description_and_alias_audit(setup):
    t, w = setup
    c = create_collection(w)
    args = collection_args("spending", "Track personal purchases")
    args["collection_id"] = c["id"]
    d = w.discover_collections(args["name"], args["purpose"])
    action = w.prepare_write("update_collection", args, [d["discovery_id"]], "User requested a clearer name and description")
    changed = w.commit_write(action["action_id"])
    assert changed["name"] == "spending"
    d = w.discover_collections("expenses", "Track personal purchases")
    action = w.prepare_write("add_collection_alias", {"collection_id": c["id"], "alias": "expenses"},
                             [d["discovery_id"]], "Remember the former name", "User asked to keep expenses as an alternative name")
    w.commit_write(action["action_id"])
    d = w.discover_collections("expenses", "Track personal purchases")
    assert "exact normalized name or saved alias" in d["catalog"][0]["reasons"]
    events = t.get_collection_history(c["id"])["events"]
    update = next(e for e in events if e["operation"] == "update")
    assert update["before"]["name"] == "expenses" and update["after"]["name"] == "spending"
    page = t.get_collection_history(c["id"], 1)
    assert page["next_cursor"]
    assert t.get_collection_history(c["id"], 1, page["next_cursor"])["events"][0]["operation"] == "add_collection_alias"


def test_typo_never_silently_writes_and_preserves_identity(setup):
    t, w = setup
    p = make_person(t)
    resolution = w.resolve_record("Kriat", p["collection_id"])
    assert resolution["status"] == "needs_clarification"
    assert resolution["candidates"][0]["id"] == p["id"]
    args = {"record_id": p["id"], "changes": {"city": "Delhi"}, "expected_version": 1}
    fails("NEEDS_CLARIFICATION", w.prepare_write, "update_record", args,
          [resolution["resolution_id"]], "Change the city")
    assert t.get_record_context(p["id"])["record"] == p
    prepared = w.prepare_write("update_record", args, [resolution["resolution_id"]],
                               "Use the clarified identity", "User said: Yes, I mean Kirat")
    assert t.get_record_context(p["id"])["record"] == p
    after = w.commit_write(prepared["action_id"])
    assert after["id"] == p["id"] and after["data"]["city"] == "Delhi"
    assert w.resolve_record("Kriat")["candidates"][0]["aliases"] == []  # no accidental learning


def test_duplicate_names_require_context_or_question(setup):
    t, w = setup
    first, second = make_person(t, "Rahul"), make_person(t, "Rahul")
    r = w.resolve_record("Rahul")
    assert r["status"] == "needs_clarification" and len(r["candidates"]) == 2
    args = {"record_id": first["id"], "changes": {"note": "called"}, "expected_version": 1}
    fails("NEEDS_CLARIFICATION", w.prepare_write, "update_record", args, [r["resolution_id"]], "Record call")
    project = t.create_record(first["collection_id"], "Apollo project", {})
    t.link_records(first["id"], "works_on", project["id"])
    resolved = w.resolve_record("Rahul", context_record_ids=[project["id"]])
    assert resolved["status"] == "resolved" and resolved["candidates"][0]["id"] == first["id"]
    assert resolved["candidates"][0]["outgoing"][0]["linked_record"]["title"] == "Apollo project"


def test_whole_hiring_chain_feedback_and_wrong_link_correction(setup):
    t, w = setup
    ids = seed_demo(t)
    person = w.resolve_record("Kriat", ids["collections"]["people"])
    assert person["status"] == "needs_clarification"
    apps = t.get_record_context(ids["kirat"])["incoming"]
    assert len(apps) == 2
    # Explicit opening AND person resolve which application, not recent mention.
    app = w.resolve_record("Kirat Backend", ids["collections"]["applications"],
                           [ids["kirat"], ids["junior_backend"]])
    assert app["status"] == "resolved"
    interview = w.resolve_record("Kirat backend coding interview", ids["collections"]["interviews"],
                                 [app["candidates"][0]["id"]])
    assert interview["status"] == "resolved"
    title = "Priya follow-up after edge-case discussion"
    r = w.resolve_record(title, ids["collections"]["feedback"])
    action = w.prepare_write("create_record", {"collection_id": ids["collections"]["feedback"], "title": title,
        "data": {"author": "Priya", "assessment": "Handled edge cases clearly."}},
        [r["resolution_id"]], "User requested an additional assessment, distinct from prior feedback")
    feedback = w.commit_write(action["action_id"])
    assert feedback["created_by"] == "assistant" and feedback["data"]["author"] == "Priya"
    # Simulate interruption/retry: same action returns same feedback instead of a duplicate.
    assert w.commit_write(action["action_id"])["id"] == feedback["id"]
    reviews = [selection(w, feedback["id"]), selection(w, ids["design_interview"])]
    action = w.prepare_write("link_records", {"source_id": feedback["id"], "relationship_type": "interview",
                                            "target_id": ids["design_interview"]}, reviews, "Simulated incorrect interview selection")
    wrong = w.commit_write(action["action_id"])
    reviews = [selection(w, feedback["id"]), selection(w, ids["design_interview"])]
    action = w.prepare_write("unlink_records", {"relationship_id": wrong["id"]}, reviews, "User corrected the interview reference")
    w.commit_write(action["action_id"])
    reviews = [selection(w, feedback["id"]), selection(w, ids["coding_interview"])]
    action = w.prepare_write("link_records", {"source_id": feedback["id"], "relationship_type": "interview",
                                            "target_id": ids["coding_interview"]}, reviews, "Attach to the explicitly identified coding interview")
    right = w.commit_write(action["action_id"])
    assert t.get_record_context(feedback["id"])["outgoing"][0]["id"] == right["id"]
    assert any(e["operation"] == "unlink" and e["before"]["id"] == wrong["id"] for e in t.get_record_history(feedback["id"])["events"])


def test_archived_duplicate_is_visible_and_creation_needs_distinction(setup):
    t, w = setup
    p = make_person(t)
    t.archive_record(p["id"], 1)
    r = w.resolve_record("Kriat", p["collection_id"])
    assert r["candidates"][0]["archived_at"]
    args = {"collection_id": p["collection_id"], "title": "Kriat", "data": {}}
    fails("NEEDS_CLARIFICATION", w.prepare_write, "create_record", args, [r["resolution_id"]], "No active match")
    action = w.prepare_write("create_record", args, [r["resolution_id"]], "A distinct person with a similar name",
                             "User explicitly said this is a different person, not archived Kirat")
    assert w.commit_write(action["action_id"])["id"] != p["id"]


def test_alias_rename_self_and_actor_scope(setup):
    t, w = setup
    p = make_person(t, "Jaskeerat")
    assert w.resolve_record("me")["status"] == "no_match"
    args = {"record_id": p["id"], "expected_version": 1, "alias": "Jas"}
    r = selection(w, p["id"])
    fails("NEEDS_CLARIFICATION", w.prepare_write, "add_record_alias", args, [r], "Nickname")
    a = w.prepare_write("add_record_alias", args, [r], "Remember a deliberate nickname", "User said: I also go by Jas")
    w.commit_write(a["action_id"])
    assert w.resolve_record("Jas")["status"] == "resolved"
    a = w.prepare_write("rename_record", {"record_id": p["id"], "title": "Jaskeerat Saluja", "expected_version": 2},
                        [selection(w, "Jas")], "User supplied full spelling")
    renamed = w.commit_write(a["action_id"])
    assert renamed["id"] == p["id"]
    a = w.prepare_write("set_self", {"record_id": p["id"], "expected_version": 3}, [selection(w, p["id"])],
                        "Resolve future first-person requests", "User explicitly identified this record as themselves")
    w.commit_write(a["action_id"])
    assert w.resolve_record("me")["candidates"][0]["id"] == p["id"]
    other_actor = Workflow(Tracker(t.db, t.workspace, "different-actor"))
    assert other_actor.resolve_record("me")["status"] == "no_match"
    a = w.prepare_write("remove_record_alias", {"record_id": p["id"], "alias": "Jas", "expected_version": 4},
                        [selection(w, p["id"])], "Remove incorrect nickname", "User explicitly removed this nickname")
    w.commit_write(a["action_id"])
    assert w.resolve_record(p["id"])["candidates"][0]["aliases"] == []


def test_stale_graph_expiry_and_cross_workspace_rejected(setup):
    t, w = setup
    a, b = make_person(t), make_person(t, "Abhishek")
    action = w.prepare_write("update_record", {"record_id": a["id"], "changes": {"note": "x"}, "expected_version": 1},
                             [selection(w, a["id"])], "Intended update")
    # A graph change invalidates review even when the record data version is unchanged.
    t.link_records(a["id"], "knows", b["id"])
    fails("STALE_REVIEW", w.commit_write, action["action_id"])
    assert t.get_record_context(a["id"])["record"]["version"] == 1
    action = w.prepare_write("archive_record", {"record_id": a["id"], "expected_version": 1}, [selection(w, a["id"])], "Archive request")
    other = Workflow(Tracker(t.db, "foreign", t.actor))
    fails("NOT_FOUND", other.commit_write, action["action_id"])
    other = Workflow(Tracker(t.db, t.workspace, "foreign-actor"))
    fails("NOT_FOUND", other.commit_write, action["action_id"])
    with t.db.connect(write=True) as con:
        con.execute("UPDATE workflow_tickets SET expires_at='2000' WHERE id=?", (action["action_id"],))
    fails("STALE_REVIEW", w.commit_write, action["action_id"])


def test_review_cannot_be_reused_for_unseen_record_or_filtered_duplicate_check(setup):
    t, w = setup
    a, b = make_person(t), make_person(t, "Abhishek")
    review = selection(w, a["id"])
    fails("REVIEW_REQUIRED", w.prepare_write, "archive_record", {"record_id": b["id"], "expected_version": 1}, [review], "Wrong ID")
    review = selection(w, "Kirat", a["collection_id"], [b["id"]])
    fails("REVIEW_REQUIRED", w.prepare_write, "create_record", {"collection_id": a["collection_id"], "title": "Kirat", "data": {}},
          [review], "A narrowed empty search must not authorize duplicate creation")


def test_incomplete_review_never_authorizes_writes(setup, monkeypatch):
    import tracker.workflow as module
    t, w = setup
    a, b = make_person(t), make_person(t, "Other")
    monkeypatch.setattr(module, "RECORD_SCAN_LIMIT", 1)
    r = w.resolve_record("Kirat", a["collection_id"])
    assert r["status"] == "incomplete"
    fails("REVIEW_REQUIRED", w.prepare_write, "create_record", {"collection_id": a["collection_id"], "title": "Kirat", "data": {}},
          [r["resolution_id"]], "Incomplete search", "Even clarification cannot waive an incomplete scan")
    monkeypatch.setattr(module, "COLLECTION_SCAN_LIMIT", 0)
    d = w.discover_collections("expenses", "Track money spent")
    assert not d["complete"]
    fails("INCOMPLETE_REVIEW", w.prepare_write, "create_collection", collection_args(), [d["discovery_id"]], "Incomplete catalog")


def test_simultaneous_creations_and_atomic_audit_failure(setup, monkeypatch):
    t, w = setup
    a = prepare_collection(w, collection_args())
    b = prepare_collection(w, collection_args())
    def commit(action):
        try:
            return w.commit_write(action["action_id"])["name"]
        except TrackerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(commit, [a, b])) == ["STALE_REVIEW", "expenses"]
    assert len(t.list_collections()["collections"]) == 1
    p = make_person(t)
    a = w.prepare_write("archive_record", {"record_id": p["id"], "expected_version": 1}, [selection(w, p["id"])], "Archive request")
    original = t._event
    def fail_decision(con, rid, op, before, after):
        if op == "decision":
            raise RuntimeError("Decision audit failure")
        return original(con, rid, op, before, after)
    monkeypatch.setattr(t, "_event", fail_decision)
    with pytest.raises(RuntimeError):
        w.commit_write(a["action_id"])
    assert t.get_record_context(p["id"])["record"]["archived_at"] is None
    assert len(t.get_record_history(p["id"])["events"]) == 1
    monkeypatch.setattr(t, "_event", original)
    assert w.commit_write(a["action_id"])["archived_at"]


def test_additive_migration_preserves_existing_records(tmp_path):
    import sqlite3
    from tracker.db import SCHEMA
    path = str(tmp_path / "legacy.db")
    # Legacy tables are the prefix before the first newly added table.
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS collection_events")[0])
    con.execute("INSERT INTO collections VALUES ('c', 'home', 'people', 'Legacy description', 'then')")
    con.execute("INSERT INTO records VALUES ('p','home','c','Kirat','{}',1,'actor','then','then',NULL)")
    con.commit()
    con.close()
    t = Tracker(Database(path), "home")
    assert t.get_record_context("p")["record"]["title"] == "Kirat"
    assert t.list_collections()["collections"][0]["description"] == "Legacy description"
    assert t.get_collection_history("c")["events"] == []  # no invented historical attribution
    assert Workflow(t).resolve_record("Kriat")["candidates"][0]["id"] == "p"


def test_exact_candidate_does_not_authorize_a_different_fuzzy_candidate(setup):
    t, w = setup
    exact, similar = make_person(t, "Kirat"), make_person(t, "Kiran")
    r = w.resolve_record("Kirat")
    assert r["status"] == "resolved" and r["selected_record_id"] == exact["id"]
    assert {c["id"] for c in r["candidates"]} == {exact["id"], similar["id"]}
    fails("NEEDS_CLARIFICATION", w.prepare_write, "archive_record",
          {"record_id": similar["id"], "expected_version": 1}, [r["resolution_id"]], "Wrong suggestion chosen")


def test_unicode_normalized_collection_reuses_legacy_spelling(setup):
    t, w = setup
    old = t.create_collection("Ｅｘｐｅｎｓｅｓ", "Legacy spending records")
    action = prepare_collection(w, collection_args())
    assert w.commit_write(action["action_id"])["id"] == old["id"]
    assert len(t.list_collections()["collections"]) == 1


def test_collection_rename_cannot_create_normalized_duplicate(setup):
    t, w = setup
    existing = t.create_collection("Ｅｘｐｅｎｓｅｓ", "Legacy spending")
    other = t.create_collection("projects", "One project")
    args = collection_args()
    args["collection_id"] = other["id"]
    d = w.discover_collections(args["name"], args["purpose"])
    fails("INVALID_INPUT", w.prepare_write, "update_collection", args, [d["discovery_id"]],
          "Attempt to rename", "A clarification cannot override exact normalized uniqueness")
    assert {c["name"] for c in t.list_collections()["collections"]} == {existing["name"], "projects"}


def test_resolution_covers_large_collections_without_missing_typos_or_aliases(setup):
    from tracker.service import now, uid
    t, w = setup
    target = make_person(t, "Jaskeerat Saluja")
    nicknamed = make_person(t, "Zed")
    review = w.resolve_record("Zed", target["collection_id"])
    w.commit_write(w.prepare_write("add_record_alias", {"record_id": nicknamed["id"], "alias": "Captain",
        "expected_version": 1}, [review["resolution_id"]], "User said so", "User calls Zed Captain")["action_id"])
    with t.db.connect(write=True) as con:  # well past the former 1000-record scan ceiling
        for index in range(1500):
            con.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                        (uid(), t.workspace, target["collection_id"], f"Filler person {index:04}", "{}", 1, t.actor, now(), now()))
    for query, status in (("Jaskeerat Saluja", "resolved"), ("jaskeerat sluja", "needs_clarification"),
                          (target["id"], "resolved"), ("Absent Name", "no_match")):
        r = w.resolve_record(query, target["collection_id"])
        assert (r["status"], r["complete"]) == (status, True)
        assert [c["id"] for c in r["candidates"]] == ([] if status == "no_match" else [target["id"]])
    assert w.resolve_record("captain")["selected_record_id"] == nicknamed["id"]
    crowded = w.resolve_record("Filler person", target["collection_id"])  # too many to review
    assert crowded["status"] == "incomplete" and len(crowded["candidates"]) == 20
    fresh = w.resolve_record("Entirely New", target["collection_id"])
    created = w.commit_write(w.prepare_write("create_record", {"collection_id": target["collection_id"],
        "title": "Entirely New", "data": {}}, [fresh["resolution_id"]], "No existing person matches")["action_id"])
    assert created["title"] == "Entirely New"


def test_candidate_score_shortcuts_never_change_a_qualifying_score():
    import random
    from tracker.workflow import CANDIDATE_THRESHOLD, candidate_score, similarity
    rng = random.Random(11)
    for _ in range(20000):
        a, b = ("".join(rng.choices("abcde fg", k=rng.randint(1, 9))) for _ in range(2))
        expected = similarity(a, b)
        assert candidate_score(a, b) == (expected if expected >= CANDIDATE_THRESHOLD else 0)


def prepared_update(t, w, record, collection_scope=False):
    review = w.resolve_record(record["title"], record["collection_id"] if collection_scope else None)
    current = t.get_record_context(record["id"])["record"]["version"]
    return w.prepare_write("update_record", {"record_id": record["id"], "changes": {"note": "x"},
                           "expected_version": current}, [review["resolution_id"]], "Requested update")


def test_unrelated_writes_do_not_invalidate_reviews(setup):
    t, w = setup
    kirat = make_person(t)
    other = t.create_collection("expenses", "Money spent")
    action = prepared_update(t, w, kirat, collection_scope=True)
    coffee = t.create_record(other["id"], "Coffee", {"amount": 5})      # another collection
    t.update_record(coffee["id"], {"amount": 6}, 1)                     # plain data, elsewhere
    lunch = t.create_record(other["id"], "Lunch", {})
    t.link_records(coffee["id"], "same_day", lunch["id"])               # links between other records
    t.create_collection("projects", "Work")                             # catalog change
    assert w.commit_write(action["action_id"])["data"] == {"note": "x"}
    # A plain data update on the reviewed record itself is caught by its version, not by staleness.
    action = prepared_update(t, w, kirat, collection_scope=True)
    t.update_record(kirat["id"], {"city": "Pune"}, 2)
    fails("VERSION_CONFLICT", w.commit_write, action["action_id"])


@pytest.mark.parametrize("change", ["create", "rename", "alias", "archive", "link"])
def test_changes_that_could_alter_identity_invalidate_reviews(setup, change):
    t, w = setup
    kirat, other = make_person(t), make_person(t, "Abhishek")
    action = prepared_update(t, w, kirat, collection_scope=True)
    if change == "create":
        t.create_record(kirat["collection_id"], "Kirat", {})  # a namesake now exists
    elif change == "link":
        t.link_records(other["id"], "knows", kirat["id"])
    else:
        review = w.resolve_record("Abhishek", other["collection_id"])
        operation, extra = {"rename": ("rename_record", {"title": "Kirat"}),
                            "alias": ("add_record_alias", {"alias": "Kirat"}),
                            "archive": ("archive_record", {})}[change]
        w.commit_write(w.prepare_write(operation, {"record_id": other["id"], "expected_version": 1, **extra},
                       [review["resolution_id"]], "Requested", "User said so")["action_id"])
    fails("STALE_REVIEW", w.commit_write, action["action_id"])


def test_workspace_wide_and_context_reviews_track_their_own_scope(setup):
    t, w = setup
    kirat = make_person(t)
    expenses = t.create_collection("expenses", "Money spent")
    wide = prepared_update(t, w, kirat)  # resolved across the whole workspace
    t.create_record(expenses["id"], "Coffee", {})
    fails("STALE_REVIEW", w.commit_write, wide["action_id"])  # any new name could have matched
    opening = t.create_record(expenses["id"], "Opening", {})
    t.link_records(kirat["id"], "applied", opening["id"])
    review = w.resolve_record("Kirat", kirat["collection_id"], [opening["id"]])
    action = w.prepare_write("update_record", {"record_id": kirat["id"], "changes": {}, "expected_version": 1},
                             [review["resolution_id"]], "Requested")
    namesake = t.create_record(expenses["id"], "Kirat", {})
    t.link_records(namesake["id"], "applied", opening["id"])  # changes who is linked to the context record
    fails("STALE_REVIEW", w.commit_write, action["action_id"])


def test_discovery_is_stale_only_after_catalog_changes(setup):
    t, w = setup
    person = make_person(t)
    action = prepare_collection(w, collection_args())
    t.create_record(person["collection_id"], "Someone", {})  # records do not change the catalog
    t.link_records(person["id"], "knows", person["id"])
    assert w.commit_write(action["action_id"])["name"] == "expenses"
    action = prepare_collection(w, collection_args(name="projects", purpose="Client engagements"))
    t.create_collection("clients", "Customers")
    fails("STALE_REVIEW", w.commit_write, action["action_id"])


def test_previews_do_not_bump_revisions_and_legacy_tickets_are_stale(setup):
    t, w = setup
    kirat = make_person(t)
    first = prepared_update(t, w, kirat, collection_scope=True)
    review = w.resolve_record("Kirat", kirat["collection_id"])
    w.prepare_write("rename_record", {"record_id": kirat["id"], "title": "Kirat S", "expected_version": 1},
                    [review["resolution_id"]], "Preview only")  # rolled back: must not look like a rename
    assert w.commit_write(first["action_id"])["version"] == 2
    second = prepared_update(t, w, kirat, collection_scope=True)
    with t.db.connect(write=True) as con:  # a ticket saved by the old whole-workspace hash
        con.execute("UPDATE workflow_tickets SET fingerprint=? WHERE kind='resolution'", ("ab" * 32,))
    fails("STALE_REVIEW", w.commit_write, second["action_id"])


def test_two_actors_in_different_collections_do_not_block_each_other(setup):
    t, w = setup
    kirat = make_person(t)
    expenses = t.create_collection("expenses", "Money spent")
    coffee = t.create_record(expenses["id"], "Coffee", {})
    other_tracker = Tracker(t.db, t.workspace, "second-agent")
    other = Workflow(other_tracker)
    mine = prepared_update(t, w, kirat, collection_scope=True)
    theirs = prepared_update(other_tracker, other, coffee, collection_scope=True)
    assert other.commit_write(theirs["action_id"])["version"] == 2
    assert w.commit_write(mine["action_id"])["version"] == 2
