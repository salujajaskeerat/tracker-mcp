import json

import pytest

from tracker.analytics import Analytics, compile_linked_to, compile_where
from tracker.db import Database
from tracker.service import Tracker, TrackerError


@pytest.fixture
def tracker(tmp_path):
    return Tracker(Database(str(tmp_path / "analytics.db")), "workspace-a", "entry-agent")


def fails(code, function, *args, **kwargs):
    with pytest.raises(TrackerError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def titles(tracker, **kwargs):
    return [r["title"] for r in tracker.search_records(**kwargs)["records"]]


def cond(field, op, *value):
    return {"field": field if isinstance(field, list) else [field], "op": op,
            **({"value": value[0]} if value else {})}


def found(tracker, *conditions, **kwargs):
    return set(titles(tracker, where=list(conditions), limit=100, **kwargs))


OFFERS = {
    "Asha": {"offer": {"ctc": 95, "currency": "INR"}, "status": "Offer", "remote": True,
             "applied": "2026-01-15", "score": 4.5},
    "Bilal": {"offer": {"ctc": 120}, "status": "offer accepted", "remote": False,
              "applied": "2026-01-31T09:30:00+00:00", "score": 3},
    "Chen": {"offer": {"ctc": "95 LPA"}, "status": "Rejected", "remote": 1, "applied": "2026-02-10"},
    "Dara": {"offer": {"ctc": "95"}, "status": None, "applied": "2025-12-01", "score": "4.5"},
    "Esi": {"offer": {}, "remote": "true", "applied": "soon", "score": 2},
    "Femi": {"status": "Offer", "offer": {"ctc": 95.0}, "applied": 20260115},
}


@pytest.fixture
def offers(tracker):
    c = tracker.create_collection("candidates", "People in the hiring pipeline")
    return {name: tracker.create_record(c["id"], name, data) for name, data in OFFERS.items()}


def test_comparison_operators_are_typed(tracker, offers):
    ctc = ["offer", "ctc"]
    assert found(tracker, cond(ctc, "eq", 95)) == {"Asha", "Femi"}  # 95 == 95.0, never "95"
    assert found(tracker, cond(ctc, "eq", "95")) == {"Dara"}
    assert found(tracker, cond(ctc, "ne", 95)) == {"Bilal"}  # strings and missing are not "ne"
    assert found(tracker, cond(ctc, "gt", 95)) == {"Bilal"}
    assert found(tracker, cond(ctc, "gte", 95)) == {"Asha", "Bilal", "Femi"}
    assert found(tracker, cond(ctc, "lt", 120)) == {"Asha", "Femi"}
    assert found(tracker, cond(ctc, "lte", 120.0)) == {"Asha", "Bilal", "Femi"}
    assert found(tracker, cond(ctc, "gt", 0)) == {"Asha", "Bilal", "Femi"}  # "95 LPA" is not > 0
    assert found(tracker, cond(ctc, "gt", "")) == {"Chen", "Dara"}  # numbers are not > ""
    assert found(tracker, cond("score", "eq", 4.5)) == {"Asha"}
    assert found(tracker, cond(ctc, "gte", 95), cond("status", "eq", "Offer")) == {"Asha", "Femi"}


def test_booleans_are_not_numbers(tracker, offers):
    assert found(tracker, cond("remote", "eq", True)) == {"Asha"}  # not 1, not "true"
    assert found(tracker, cond("remote", "eq", False)) == {"Bilal"}
    assert found(tracker, cond("remote", "ne", True)) == {"Bilal"}
    assert found(tracker, cond("remote", "eq", 1)) == {"Chen"}
    assert found(tracker, cond("remote", "in", [True, "true"])) == {"Asha", "Esi"}
    for op in ("gt", "gte", "lt", "lte"):
        fails("INVALID_INPUT", tracker.search_records, where=[cond("remote", op, True)])
    fails("INVALID_INPUT", tracker.search_records, where=[cond("remote", "between", [False, True])])


def test_between_and_in(tracker, offers):
    ctc = ["offer", "ctc"]
    assert found(tracker, cond(ctc, "between", [95, 120])) == {"Asha", "Bilal", "Femi"}  # inclusive
    assert found(tracker, cond(ctc, "between", [96, 119])) == set()
    assert found(tracker, cond(ctc, "in", [120, "95", 7])) == {"Bilal", "Dara"}
    assert found(tracker, cond(ctc, "in", list(range(50)))) == set()
    for value in ([], list(range(51)), [1, None], [[1]], 5, "95"):
        fails("INVALID_INPUT", tracker.search_records, where=[cond(ctc, "in", value)])
    for value in ([1], [1, 2, 3], [2, 1], [1, "2"], ["a", None], 5):
        fails("INVALID_INPUT", tracker.search_records, where=[cond(ctc, "between", value)])


def test_iso_dates_compare_as_strings(tracker, offers):
    january = cond("applied", "between", ["2026-01-01", "2026-01-31T23:59:59+00:00"])
    assert found(tracker, january) == {"Asha", "Bilal"}  # the number 20260115 never matches
    assert found(tracker, cond("applied", "lt", "2026-01-01")) == {"Dara"}
    assert found(tracker, cond("applied", "gte", "2026-02-01"), cond("applied", "lt", "2027")) == {"Chen"}


def test_exists_missing_and_json_null(tracker, offers):
    assert found(tracker, cond("status", "exists")) == {"Asha", "Bilal", "Chen", "Dara", "Femi"}
    assert found(tracker, cond("status", "missing")) == {"Esi"}  # Dara's explicit null exists
    assert found(tracker, cond(["offer", "ctc"], "missing")) == {"Esi"}
    assert found(tracker, cond(["offer", "ctc", "deeper"], "missing")) == set(OFFERS)
    assert found(tracker, cond("status", "ne", "Offer")) == {"Bilal", "Chen"}  # null is not a string
    assert titles(tracker, filters={"status": None}) == ["Dara"]  # null equality stays with filters
    fails("INVALID_INPUT", tracker.search_records, where=[cond("status", "exists", True)])
    fails("INVALID_INPUT", tracker.search_records, where=[cond("status", "eq")])
    fails("INVALID_INPUT", tracker.search_records, where=[cond("status", "eq", None)])


def test_contains_is_case_insensitive_and_strings_only(tracker, offers):
    assert found(tracker, cond("status", "contains", "OFFER")) == {"Asha", "Bilal", "Femi"}
    assert found(tracker, cond("status", "contains", "accepted")) == {"Bilal"}
    assert found(tracker, cond(["offer", "ctc"], "contains", "95")) == {"Chen", "Dara"}  # not the number
    assert found(tracker, cond("applied", "contains", "2026")) == {"Asha", "Bilal", "Chen"}
    assert found(tracker, cond("offer", "contains", "ctc")) == set()  # objects are not strings
    c = tracker.create_collection("places", "Places")
    tracker.create_record(c["id"], "Street", {"name": "Hauptstraße"})
    assert found(tracker, cond("name", "contains", "STRASSE")) == {"Street"}
    for value in ("", 5, None, ["a"]):
        fails("INVALID_INPUT", tracker.search_records, where=[cond("status", "contains", value)])


def test_keys_with_dots_and_path_syntax_are_literal(tracker):
    c = tracker.create_collection("odd", "Odd keys")
    tracker.create_record(c["id"], "Dotted", {"a.b": {"c d": 1}, "x[0]": "bracket", "$": 2, "#-1": 3})
    tracker.create_record(c["id"], "Nested", {"a": {"b": {"c d": 1}}, "x": ["array"]})
    assert found(tracker, cond(["a.b", "c d"], "eq", 1)) == {"Dotted"}
    assert found(tracker, cond(["a", "b", "c d"], "eq", 1)) == {"Nested"}
    assert found(tracker, cond(["x[0]"], "exists")) == {"Dotted"}  # never an array index
    assert found(tracker, cond(["$"], "eq", 2), cond(["#-1"], "eq", 3)) == {"Dotted"}


@pytest.mark.parametrize("key", ['say "hi"', "back\\slash", "new\nline", "nul\x00", "", 5, None, "k" * 201])
def test_unaddressable_keys_are_rejected(tracker, key):
    fails("INVALID_INPUT", tracker.search_records, where=[cond([key], "exists")])
    fails("INVALID_INPUT", tracker.search_records, order_by={"field": [key], "type": "string"})
    fails("INVALID_INPUT", Analytics(tracker).aggregate, group_by={"field": [key]})
    fails("INVALID_INPUT", Analytics(tracker).aggregate, metrics=[{"op": "sum", "field": [key]}])


def test_injection_attempts_are_inert(tracker, offers):
    hostile = "x') OR 1=1 --"
    assert found(tracker, cond([hostile], "exists")) == set()
    assert found(tracker, cond([hostile], "missing")) == set(OFFERS)
    assert found(tracker, cond("status", "eq", "Offer' OR '1'='1")) == set()
    assert found(tracker, cond("status", "contains", "%' OR 1=1; DROP TABLE records; --")) == set()
    assert found(tracker, cond("status", "in", ["'); DELETE FROM records; --"])) == set()
    assert titles(tracker, order_by={"field": ["a'); DROP TABLE records; --"], "type": "string"}) == []
    anchor = offers["Asha"]["id"]
    assert titles(tracker, linked_to={"record_id": anchor, "relationship_types": ["x' OR 1=1 --"]}) == []
    result = Analytics(tracker).aggregate(group_by={"field": [hostile]},
                                          metrics=[{"op": "sum", "field": [hostile]}])
    assert result["groups"][0]["key"] is None and result["groups"][0]["count"] == len(OFFERS)
    assert len(titles(tracker, limit=100)) == len(OFFERS)


@pytest.mark.parametrize("where", [
    {}, "status", [[]], ["status"], [{"field": ["a"]}], [{"op": "exists"}], [{"field": "a", "op": "exists"}],
    [{"field": [], "op": "exists"}], [{"field": ["a"] * 9, "op": "exists"}],
    [{"field": ["a"], "op": "like", "value": "x"}], [{"field": ["a"], "op": ["eq"], "value": 1}],
    [{"field": ["a"], "op": "eq", "value": 1, "extra": True}], [{"field": ["a"], "op": "eq", "value": {"b": 1}}],
    [{"field": ["a"], "op": "eq", "value": [1]}], [{"field": ["a"], "op": "eq", "value": float("nan")}],
    [{"field": ["a"], "op": "eq", "value": 2**63}], [{"field": ["a"], "op": "eq", "value": "x" * 1001}],
    [{"field": ["a"], "op": "exists"}] * 21])
def test_invalid_where_shapes(tracker, where):
    fails("INVALID_INPUT", tracker.search_records, where=where)
    fails("INVALID_INPUT", Analytics(tracker).aggregate, where=where)
    fails("INVALID_INPUT", compile_where, where)


def test_compile_where_binds_every_caller_value():
    assert compile_where(None) == ([], []) and compile_where([]) == ([], [])
    fragments, args = compile_where([cond(["a.b", "c"], "eq", "v'"), cond(["d"], "in", [1, "x", True])])
    assert len(fragments) == 2 and "v'" not in "".join(fragments) and "a.b" not in "".join(fragments)
    assert args == ['$."a.b"."c"', '$."a.b"."c"', "v'", '$."d"', '$."d"', 1, '$."d"', '$."d"', "x",
                    '$."d"', '$."d"', 1]
    assert "".join(fragments).count("?") == len(args)


@pytest.fixture
def graph(tracker):
    people = tracker.create_collection("people", "People")
    companies = tracker.create_collection("companies", "Companies")
    made = {name: tracker.create_record(people["id"], name, {"level": level})
            for name, level in (("Asha", 5), ("Bilal", 3), ("Chen", 4), ("Dara", 2))}
    for name in ("Acme", "Globex"):
        made[name] = tracker.create_record(companies["id"], name, {"kind": "company"})
    for source, kind, target in (("Asha", "applied_to", "Acme"), ("Asha", "applied_to", "Globex"),
                                 ("Asha", "referred_by", "Acme"), ("Bilal", "applied_to", "Acme"),
                                 ("Acme", "interviewed", "Chen"), ("Acme", "applied_to", "Globex")):
        tracker.link_records(made[source]["id"], kind, made[target]["id"])
    return made, people, companies


def test_linked_to_directions_and_types(tracker, graph):
    made, people, _ = graph
    acme = made["Acme"]["id"]

    def linked(**kwargs):
        return set(titles(tracker, linked_to={"record_id": acme, **kwargs}))
    assert linked() == {"Asha", "Bilal", "Chen", "Globex"}
    assert linked(direction="both") == linked()
    assert linked(direction="outgoing") == {"Asha", "Bilal"}  # the match is the source
    assert linked(direction="incoming") == {"Chen", "Globex"}  # the match is the target
    assert linked(relationship_types=["referred_by"]) == {"Asha"}
    assert linked(relationship_types=["applied_to"], direction="incoming") == {"Globex"}
    assert linked(relationship_types=["applied_to", "interviewed"]) == {"Asha", "Bilal", "Chen", "Globex"}
    assert linked(relationship_types=["nothing"]) == set()
    assert set(titles(tracker, collection_id=people["id"], linked_to={"record_id": acme},
                      where=[cond("level", "gte", 4)])) == {"Asha", "Chen"}
    assert titles(tracker, query="ash", linked_to={"record_id": made["Globex"]["id"]}) == ["Asha"]


def test_linked_to_validation_and_scope(tracker, graph, tmp_path):
    made = graph[0]
    acme = made["Acme"]["id"]
    fails("NOT_FOUND", tracker.search_records, linked_to={"record_id": "no-such-record"})
    fails("NOT_FOUND", Analytics(tracker).aggregate, linked_to={"record_id": "no-such-record"})
    other = Tracker(tracker.db, "workspace-b", "entry-agent")
    fails("NOT_FOUND", other.search_records, linked_to={"record_id": acme})
    for bad in ("x", [], {}, {"record_id": 5}, {"record_id": acme, "direction": "sideways"},
                {"record_id": acme, "relationship_types": "applied_to"},
                {"record_id": acme, "relationship_types": ["t"] * 21},
                {"record_id": acme, "relationship_types": [""]}, {"record_id": acme, "depth": 2}):
        fails("INVALID_INPUT", tracker.search_records, linked_to=bad)
        fails("INVALID_INPUT", Analytics(tracker).aggregate, linked_to=bad)
    with tracker.db.connect() as con:
        assert compile_linked_to(tracker, con, None) == (None, [])
        sql, args = compile_linked_to(tracker, con, {"record_id": acme, "relationship_types": ["a"]})
        assert sql.startswith("EXISTS (") and args == [acme, acme, "a"] and sql.count("?") == 3
    archived = tracker.archive_record(acme, made["Acme"]["version"])  # an archived anchor still filters
    assert archived["archived_at"] and "Asha" in titles(tracker, linked_to={"record_id": acme})


def test_where_combines_with_text_filters_query_and_collection(tracker, offers):
    other = tracker.create_collection("alumni", "Former candidates")
    tracker.create_record(other["id"], "Asha (2019)", {"offer": {"ctc": 95}, "status": "Offer"})
    ctc = cond(["offer", "ctc"], "eq", 95)
    collection_id = offers["Asha"]["collection_id"]
    assert found(tracker, ctc) == {"Asha", "Femi", "Asha (2019)"}
    assert found(tracker, ctc, collection_id=collection_id) == {"Asha", "Femi"}
    assert found(tracker, ctc, query="ASHA") == {"Asha", "Asha (2019)"}
    assert found(tracker, ctc, filters={"offer": {"ctc": 95, "currency": "INR"}}) == {"Asha"}
    assert found(tracker, ctc, text="offer") == {"Asha", "Femi", "Asha (2019)"}
    assert found(tracker, ctc, text="inr") == {"Asha"}
    hit = tracker.search_records(text="inr", where=[ctc])["records"][0]
    assert "[INR]" in hit["match_snippet"]
    assert found(tracker, ctc, text="inr", query="asha", filters={"remote": True},
                 collection_id=collection_id) == {"Asha"}
    assert found(tracker, ctc, text="inr", collection_id=other["id"]) == set()


def all_pages(tracker, limit, **kwargs):
    seen, cursor = [], None
    while True:
        page = tracker.search_records(limit=limit, cursor=cursor, **kwargs)
        seen.extend(page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            return seen


@pytest.fixture
def ranked(tracker):
    c = tracker.create_collection("scores", "Scores")
    values = [3, 1.5, 3, 2**53 + 1, -7, 3.0, 0.1, 1e300, "3", True, None, [3], 2**53, 10]
    for index, value in enumerate(values):
        tracker.create_record(c["id"], f"n{index}", {"rank": value, "name": f"item {index % 4}", "pad": "p"})
    tracker.create_record(c["id"], "absent", {"name": "ünïcode"})
    tracker.create_record(c["id"], "long", {"name": "z" * 9000})
    return c


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("kind,field", [("number", "rank"), ("string", "name")])
def test_order_by_pagination_equals_unpaginated(tracker, ranked, direction, kind, field):
    order = {"field": [field], "type": kind, "direction": direction}
    full = tracker.search_records(limit=100, order_by=order)
    assert full["next_cursor"] is None
    pairs = [(r["sort_value"], r["id"]) for r in full["records"]]
    assert pairs == sorted(pairs, reverse=direction == "desc")
    assert len(pairs) == (10 if kind == "number" else 16)  # other types and missing are excluded
    assert all(type(value) in ((int, float) if kind == "number" else (str,)) for value, _ in pairs)
    for limit in (1, 2, 5):
        paged = all_pages(tracker, limit, order_by=order)
        assert [r["id"] for r in paged] == [r["id"] for r in full["records"]]


def test_order_by_defaults_filters_and_exact_cursor_values(tracker, ranked):
    order = {"field": ["rank"], "type": "number"}
    values = [r["sort_value"] for r in all_pages(tracker, 1, order_by=order)]
    # One record per page: 2**53 + 1 must not collapse into the float 2**53 inside the cursor.
    assert values == [-7, 0.1, 1.5, 3, 3, 3.0, 10, 2**53, 2**53 + 1, 1e300]
    assert [type(v) for v in values[-3:]] == [int, int, float]
    narrowed = all_pages(tracker, 2, order_by={**order, "direction": "desc"}, where=[cond("rank", "lte", 3)])
    assert [r["sort_value"] for r in narrowed] == [3, 3, 3, 1.5, 0.1, -7]
    assert [r["title"] for r in all_pages(tracker, 1, order_by={"field": ["missing"], "type": "string"})] == []


def test_order_by_validation_and_cursor_scope(tracker, ranked):
    order = {"field": ["rank"], "type": "number"}
    fails("INVALID_INPUT", tracker.search_records, text="item", order_by=order)
    for bad in ("rank", {"field": ["rank"]}, {"field": ["rank"], "type": "date"}, {"field": "rank", "type": "number"},
                {**order, "direction": "up"}, {**order, "nulls": "last"}, {"type": "number"}):
        fails("INVALID_INPUT", tracker.search_records, order_by=bad)
    cursor = tracker.search_records(limit=2, order_by=order)["next_cursor"]
    assert tracker.search_records(limit=2, order_by=order, cursor=cursor)["records"]
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=cursor)
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=cursor, order_by={**order, "direction": "desc"})
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=cursor, order_by=order,
          where=[cond("rank", "exists")])
    plain = tracker.search_records(limit=2)["next_cursor"]
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=plain, order_by=order)
    fails("INVALID_INPUT", tracker.search_records, limit=2, cursor=plain, where=[cond("rank", "exists")])


def test_cursors_issued_before_these_parameters_stay_valid(tracker, ranked):
    import base64
    from tracker.db import canonical
    from tracker.service import cursor_scope
    first = tracker.search_records(collection_id=ranked["id"], query="n", filters={"pad": "p"}, limit=3)
    # The scope hash an older server computed: no text, where, linked_to or order_by parts.
    old_scope = cursor_scope("search", "workspace-a", ranked["id"], "n", {"pad": "p"})
    payload = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    assert payload[0] == old_scope
    last = first["records"][-1]
    with tracker.db.connect() as con:
        created = con.execute("SELECT created_at FROM records WHERE id=?", (last["id"],)).fetchone()[0]
    old_cursor = base64.urlsafe_b64encode(canonical([old_scope, created, last["id"]]).encode()).decode()
    assert old_cursor == first["next_cursor"]
    page = tracker.search_records(ranked["id"], "n", {"pad": "p"}, 3, old_cursor)  # positional callers
    assert [r["title"] for r in page["records"]] == ["n3", "n4", "n5"]
    assert titles(tracker, filters={"pad": "p"}, query="n", limit=100) == [f"n{i}" for i in range(14)]


def metric(group, index=0):
    return group["metrics"][index]


def test_aggregate_metrics_and_honest_skip_accounting(tracker, offers):
    ctc = ["offer", "ctc"]
    result = Analytics(tracker).aggregate(metrics=[
        {"op": "count"}, {"op": "sum", "field": ctc}, {"op": "avg", "field": ctc},
        {"op": "min", "field": ctc}, {"op": "max", "field": ctc}, {"op": "sum", "field": ["score"]},
        {"op": "min", "field": ["applied"], "type": "string"}, {"op": "max", "field": ctc, "type": "string"}])
    assert result["matched"] == 6 and result["groups_truncated"] is False and result["group_total"] == 1
    [group] = result["groups"]
    assert group["key"] is None and group["count"] == 6
    assert metric(group, 0) == {"op": "count", "value": 6}
    # Chen's "95 LPA" and Dara's "95" are reported as skipped, not silently dropped or coerced.
    assert metric(group, 1) == {"op": "sum", "field": ctc, "value": 310.0, "used": 3,
                                "skipped_missing": 1, "skipped_non_numeric": 2}
    assert metric(group, 2)["value"] == pytest.approx(310 / 3) and metric(group, 2)["used"] == 3
    assert (metric(group, 3)["value"], metric(group, 4)["value"]) == (95, 120)
    assert metric(group, 3)["type"] == "number" and metric(group, 3)["skipped_non_numeric"] == 2
    score = metric(group, 5)  # Dara's "4.5" is a string; Chen and Femi have no score
    assert (score["value"], score["used"], score["skipped_missing"], score["skipped_non_numeric"]) == (9.5, 3, 2, 1)
    applied = metric(group, 6)
    assert applied == {"op": "min", "field": ["applied"], "type": "string", "value": "2025-12-01", "used": 5,
                       "skipped_missing": 0, "skipped_non_string": 1}
    assert metric(group, 7)["value"] == "95 LPA" and metric(group, 7)["skipped_non_string"] == 3
    for m in group["metrics"][1:]:
        skipped = m.get("skipped_non_numeric", m.get("skipped_non_string"))
        assert m["used"] + m["skipped_missing"] + skipped == group["count"]
    json.dumps(result)


def test_aggregate_integer_sums_stay_exact_and_null_counts_as_missing(tracker):
    c = tracker.create_collection("ledger", "Ledger")
    for amount in (2**53 + 1, 2, None, True):
        tracker.create_record(c["id"], "entry", {"amount": amount})
    [group] = Analytics(tracker).aggregate(metrics=[{"op": "sum", "field": ["amount"]}])["groups"]
    assert metric(group) == {"op": "sum", "field": ["amount"], "value": 2**53 + 3, "used": 2,
                             "skipped_missing": 1, "skipped_non_numeric": 1}
    assert type(metric(group)["value"]) is int
    for amount in (2**62, 2**62):
        tracker.create_record(c["id"], "huge", {"amount": amount})
    fails("UNSUPPORTED", Analytics(tracker).aggregate, metrics=[{"op": "sum", "field": ["amount"]}])


def test_aggregate_default_metric_and_empty_result(tracker, offers):
    result = Analytics(tracker).aggregate(where=[cond("status", "eq", "Nobody")],
                                          metrics=[{"op": "count"}, {"op": "sum", "field": ["score"]}])
    assert result["matched"] == 0
    assert result["groups"] == [{"key": None, "count": 0, "metrics": [
        {"op": "count", "value": 0},
        {"op": "sum", "field": ["score"], "value": None, "used": 0, "skipped_missing": 0, "skipped_non_numeric": 0}]}]
    assert Analytics(tracker).aggregate()["groups"] == [
        {"key": None, "count": 6, "metrics": [{"op": "count", "value": 6}]}]
    empty = Analytics(tracker).aggregate(where=[cond("status", "eq", "Nobody")], group_by={"field": ["status"]})
    assert empty["groups"] == [] and empty["group_total"] == 0


def test_aggregate_filters_match_search(tracker, offers):
    analytics = Analytics(tracker)
    ctc = cond(["offer", "ctc"], "gte", 95)
    assert analytics.aggregate(where=[ctc])["matched"] == 3
    assert analytics.aggregate(where=[ctc], filters={"status": "Offer"})["matched"] == 2
    assert analytics.aggregate(where=[ctc], text="inr")["matched"] == 1
    assert analytics.aggregate(text="offer accepted")["matched"] == 1
    assert analytics.aggregate(collection_id=offers["Asha"]["collection_id"])["matched"] == 6
    empty = tracker.create_collection("empty", "Nothing here")
    assert analytics.aggregate(collection_id=empty["id"], text="offer")["matched"] == 0
    fails("NOT_FOUND", analytics.aggregate, collection_id="no-such-collection")
    fails("INVALID_INPUT", analytics.aggregate, text="!!!")
    fails("INVALID_INPUT", analytics.aggregate, filters=["status"])
    tracker.db.fts = False
    fails("UNSUPPORTED", analytics.aggregate, text="offer")
    assert analytics.aggregate(where=[ctc])["matched"] == 3


def test_group_by_field_keeps_key_types_apart(tracker):
    c = tracker.create_collection("mixed", "Mixed")
    for value in (1, 1, 1.0, "1", "1", True, False, None, [1], {"a": 1}, 2.5):
        tracker.create_record(c["id"], "v", {"v": value, "n": 10})
    tracker.create_record(c["id"], "absent", {"n": 10})
    result = Analytics(tracker).aggregate(group_by={"field": ["v"]}, metrics=[{"op": "sum", "field": ["n"]}])
    groups = {(type(g["key"]).__name__, g["key"]): g["count"] for g in result["groups"]}
    assert groups == {("NoneType", None): 4, ("int", 1): 3, ("str", "1"): 2, ("bool", True): 1,
                      ("bool", False): 1, ("float", 2.5): 1}
    assert [g["count"] for g in result["groups"]] == [4, 3, 2, 1, 1, 1]  # count desc, then key
    assert [g["key"] for g in result["groups"][3:]] == [False, True, 2.5]
    assert all(metric(g)["value"] == 10 * g["count"] for g in result["groups"])
    assert result["matched"] == 12 and result["group_total"] == 6
    nested = Analytics(tracker).aggregate(group_by={"field": ["v", "a"]})
    assert [(g["key"], g["count"]) for g in nested["groups"]] == [(None, 11), (1, 1)]


def test_group_by_month_and_year_buckets(tracker, offers):
    analytics = Analytics(tracker)
    months = analytics.aggregate(group_by={"field": ["applied"], "bucket": "month"},
                                 metrics=[{"op": "count"}, {"op": "sum", "field": ["offer", "ctc"]}])
    assert [(g["key"], g["count"]) for g in months["groups"]] == [
        ("2026-01", 2), (None, 2), ("2025-12", 1), ("2026-02", 1)]  # "soon" and 20260115 are unbucketable
    assert metric(months["groups"][0], 1)["value"] == 215
    years = analytics.aggregate(group_by={"field": ["applied"], "bucket": "year"})
    assert [(g["key"], g["count"]) for g in years["groups"]] == [("2026", 3), (None, 2), ("2025", 1)]
    c = tracker.create_collection("dates", "Date shapes")
    for value in ("2026-13-01", "2026-1-05", "20260105", "2026", "2026-03", "2026-03x", "12026-03-01"):
        tracker.create_record(c["id"], value, {"d": value})
    months = analytics.aggregate(collection_id=c["id"], group_by={"field": ["d"], "bucket": "month"})
    assert [(g["key"], g["count"]) for g in months["groups"]] == [(None, 6), ("2026-03", 1)]
    years = analytics.aggregate(collection_id=c["id"], group_by={"field": ["d"], "bucket": "year"})
    assert [(g["key"], g["count"]) for g in years["groups"]] == [("2026", 5), (None, 2)]


def test_group_by_collection(tracker, graph):
    _, people, companies = graph
    result = Analytics(tracker).aggregate(group_by={"collection": True},
                                          metrics=[{"op": "count"}, {"op": "avg", "field": ["level"]}])
    assert [g["key"] for g in result["groups"]] == [
        {"collection_id": people["id"], "collection_name": "people"},
        {"collection_id": companies["id"], "collection_name": "companies"}]
    assert [g["count"] for g in result["groups"]] == [4, 2]
    assert metric(result["groups"][0], 1)["value"] == 3.5
    assert metric(result["groups"][1], 1) == {"op": "avg", "field": ["level"], "value": None, "used": 0,
                                              "skipped_missing": 2, "skipped_non_numeric": 0}


def test_group_by_linked_double_counts_and_has_a_null_group(tracker, graph):
    made, people, _ = graph
    analytics = Analytics(tracker)
    # Asha reaches Acme through two relationship types but counts once in Acme's group.
    result = analytics.aggregate(collection_id=people["id"], metrics=[{"op": "sum", "field": ["level"]}],
                                 group_by={"linked": {"direction": "outgoing"}})
    summary = [(g["key"] and g["key"]["title"], g["count"], metric(g)["value"]) for g in result["groups"]]
    assert summary == [("Acme", 2, 8), (None, 2, 6), ("Globex", 1, 5)]
    assert result["groups"][0]["key"] == {"record_id": made["Acme"]["id"], "title": "Acme",
                                          "collection_name": "companies"}
    assert result["matched"] == 4 and sum(g["count"] for g in result["groups"]) == 5  # Asha twice
    assert "more than matched" in result["note"]
    assert "more than matched" not in analytics.aggregate()["note"]

    def grouped(**linked):
        found = analytics.aggregate(collection_id=people["id"], group_by={"linked": linked})["groups"]
        return {g["key"] and g["key"]["title"]: g["count"] for g in found}
    assert grouped(direction="incoming") == {"Acme": 1, None: 3}  # Acme interviewed Chen
    assert grouped() == {"Acme": 3, "Globex": 1, None: 1}
    assert grouped(direction="both", relationship_types=["referred_by"]) == {"Acme": 1, None: 3}
    assert grouped(relationship_types=["applied_to", "interviewed"]) == {"Acme": 3, "Globex": 1, None: 1}


def test_group_limit_order_and_truncation(tracker):
    c = tracker.create_collection("tags", "Tags")
    for tag, copies in (("b", 2), ("a", 2), ("c", 3), ("d", 1), ("e", 1)):
        for _ in range(copies):
            tracker.create_record(c["id"], tag, {"tag": tag})
    analytics = Analytics(tracker)
    page = analytics.aggregate(group_by={"field": ["tag"]}, limit=3)
    assert [(g["key"], g["count"]) for g in page["groups"]] == [("c", 3), ("a", 2), ("b", 2)]
    assert page["groups_truncated"] is True and page["group_total"] == 5 and page["matched"] == 9
    full = analytics.aggregate(group_by={"field": ["tag"]}, limit=5)
    assert full["groups_truncated"] is False and [g["key"] for g in full["groups"]] == ["c", "a", "b", "d", "e"]
    for limit in (0, 101, "5", True):
        fails("INVALID_INPUT", analytics.aggregate, limit=limit)


def test_aggregate_respects_workspace_and_archives(tracker, offers):
    other = Tracker(tracker.db, "workspace-b", "entry-agent")
    c = other.create_collection("candidates", "Other workspace")
    other.create_record(c["id"], "Zed", {"offer": {"ctc": 1000}, "status": "Offer"})
    tracker.archive_record(offers["Bilal"]["id"], offers["Bilal"]["version"])
    metrics = [{"op": "sum", "field": ["offer", "ctc"]}]
    [group] = Analytics(tracker).aggregate(metrics=metrics)["groups"]
    assert (group["count"], metric(group)["value"]) == (5, 190.0)
    [theirs] = Analytics(other).aggregate(metrics=metrics)["groups"]
    assert (theirs["count"], metric(theirs)["value"]) == (1, 1000)
    fails("NOT_FOUND", Analytics(other).aggregate, collection_id=offers["Asha"]["collection_id"])
    assert found(tracker, cond(["offer", "ctc"], "gt", 100)) == set()  # archived Bilal
    assert found(other, cond(["offer", "ctc"], "gt", 100)) == {"Zed"}
    by_collection = Analytics(other).aggregate(group_by={"collection": True})["groups"]
    assert [g["key"]["collection_id"] for g in by_collection] == [c["id"]]


@pytest.mark.parametrize("kwargs", [
    {"group_by": "status"}, {"group_by": {}}, {"group_by": {"field": "status"}},
    {"group_by": {"field": ["status"], "bucket": "week"}}, {"group_by": {"field": ["a"], "collection": True}},
    {"group_by": {"collection": False}}, {"group_by": {"collection": 1}}, {"group_by": {"linked": True}},
    {"group_by": {"linked": {"direction": "up"}}}, {"group_by": {"linked": {"record_id": "x"}}},
    {"group_by": {"linked": {"relationship_types": ["t"] * 21}}}, {"group_by": {"by": ["status"]}},
    {"metrics": []}, {"metrics": {"op": "count"}}, {"metrics": [{"op": "count"}] * 11}, {"metrics": ["count"]},
    {"metrics": [{"op": "median", "field": ["a"]}]}, {"metrics": [{"op": "sum"}]}, {"metrics": [{"field": ["a"]}]},
    {"metrics": [{"op": "count", "field": ["a"]}]}, {"metrics": [{"op": "sum", "field": "a"}]},
    {"metrics": [{"op": "sum", "field": ["a"], "type": "string"}]},
    {"metrics": [{"op": "min", "field": ["a"], "type": "date"}]},
    {"metrics": [{"op": "sum", "field": ["a"], "as": "total"}]}])
def test_invalid_aggregate_shapes(tracker, kwargs):
    fails("INVALID_INPUT", Analytics(tracker).aggregate, **kwargs)
