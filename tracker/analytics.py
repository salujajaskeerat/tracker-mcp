"""Read-only analytics: typed field conditions, link filters, sorting, and grouped aggregates.

Caller text never reaches SQL. Field paths, operands, and IDs are bound parameters; only fixed
fragments chosen from the tables below are interpolated.
"""
import math
import sqlite3

from .service import TrackerError, bounded, fts_query, invalid, object_json
from .service import text as clean_text

MAX_CONDITIONS = 20
MAX_PATH_KEYS = 8
MAX_KEY_LENGTH = 200
MAX_IN_VALUES = 50
MAX_OPERAND_LENGTH = 1000
MAX_RELATIONSHIP_TYPES = 20
MAX_METRICS = 10
INT64 = 2**63

COMPARISONS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
OPS = (*COMPARISONS, "between", "in", "contains", "exists", "missing")
# json_type() names accepted for each operand kind. A boolean is never a number.
TYPE_SQL = {"number": "IN ('integer', 'real')", "string": "= 'text'", "boolean": "IN ('true', 'false')"}
DIRECTIONS = ("outgoing", "incoming", "both")
METRIC_OPS = ("count", "sum", "avg", "min", "max")
SKIP_KEYS = {"number": "skipped_non_numeric", "string": "skipped_non_string"}


def json_path(field, name="field"):
    """Compile a list of object keys to a SQLite JSON path such as $."offer"."ctc".

    A list keeps keys containing dots unambiguous. SQLite reads a double-quoted label literally and
    has no escape for a quote inside it, so keys containing a double quote, a backslash, or a
    control character are rejected rather than escaped. Empty keys and array indexes are unsupported.
    The result is always passed to SQLite as a bound parameter.
    """
    if not isinstance(field, list) or not 1 <= len(field) <= MAX_PATH_KEYS:
        invalid(f"{name} must be a list of 1 to {MAX_PATH_KEYS} object keys, e.g. [\"offer\", \"ctc\"]")
    for key in field:
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_LENGTH:
            invalid(f"{name} keys must be nonempty strings of at most {MAX_KEY_LENGTH} characters")
        if '"' in key or "\\" in key or any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
            invalid(f"{name} keys containing a double quote, backslash, or control character "
                    "cannot be addressed and are rejected rather than escaped")
    return "$" + "".join(f'."{key}"' for key in field)


def operand(value, name):
    """Return (kind, bindable value) for a scalar operand; null, lists, and objects are rejected."""
    if type(value) is bool:
        return "boolean", int(value)  # json_extract yields 1/0; the json_type guard keeps it typed
    if type(value) is int:
        if not -INT64 <= value < INT64:
            invalid(f"{name} integers must fit in 64 bits")
        return "number", value
    if type(value) is float:
        if not math.isfinite(value):
            invalid(f"{name} must be a finite number")
        return "number", value
    if isinstance(value, str):
        if len(value) > MAX_OPERAND_LENGTH:
            invalid(f"{name} strings must be at most {MAX_OPERAND_LENGTH} characters")
        return "string", value
    invalid(f"{name} must be a string, number, or boolean; use filters for null, list, or object equality")


def keys_only(value, name, allowed, required=()):
    if not isinstance(value, dict):
        invalid(f"{name} must be an object")
    unknown = [k for k in value if k not in allowed]
    if unknown or any(k not in value for k in required):
        invalid(f"{name} accepts keys {sorted(allowed)}"
                + (f" and requires {sorted(required)}" if required else ""))
    return value


def compile_where(where):
    """Compile typed field conditions to (sql_fragments, args), to be ANDed on records alias r.

    where is None or up to 20 objects {"field": [keys...], "op": ..., "value": ...}. Operators:
    eq ne gt gte lt lte between in contains exists missing.

    Comparison is typed: a condition matches only when the stored JSON type matches the operand
    type. Numbers compare with numbers (integer or real), strings with strings, and booleans with
    booleans (eq, ne, in only); the number 95 never matches the string "95" and true never matches 1.
    ne means "the field exists with a compatible type and differs": records that lack the field or
    hold another type do not match ne. exists is true for an explicit JSON null; missing means the
    key is absent. contains is a case-insensitive substring test on string fields. between is
    inclusive. There is no date type: ISO 8601 strings of one format and offset order correctly as
    strings, so use string operands such as between ["2026-01-01", "2026-03-31"].
    """
    if where is None:
        return [], []
    if not isinstance(where, list) or len(where) > MAX_CONDITIONS:
        invalid(f"where must be a list of at most {MAX_CONDITIONS} conditions")
    fragments, args = [], []
    for condition in where:
        keys_only(condition, "each where condition", ("field", "op", "value"), ("field", "op"))
        path, op = json_path(condition["field"]), condition["op"]
        if not isinstance(op, str) or op not in OPS:
            invalid(f"op must be one of {', '.join(OPS)}")
        if op in ("exists", "missing"):
            if "value" in condition:
                invalid(f"{op} takes no value")
            fragments.append(f"(json_type(r.data_json, ?) IS {'NOT ' if op == 'exists' else ''}NULL)")
            args.append(path)
            continue
        if "value" not in condition:
            invalid(f"{op} requires a value")
        value = condition["value"]
        if op == "contains":
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_OPERAND_LENGTH:
                invalid(f"contains requires a nonempty string of at most {MAX_OPERAND_LENGTH} characters")
            # CASE guarantees casefold() only ever receives text; AND does not promise short-circuit.
            fragments.append("""(instr(casefold(CASE WHEN json_type(r.data_json, ?) = 'text'
                THEN json_extract(r.data_json, ?) ELSE '' END), ?) > 0)""")
            args.extend([path, path, value.casefold()])
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                invalid("between requires a [low, high] list")
            (kind, low), (high_kind, high) = operand(value[0], "between low"), operand(value[1], "between high")
            if kind != high_kind or kind == "boolean" or low > high:
                invalid("between requires two numbers or two strings with low <= high")
            fragments.append(f"""(json_type(r.data_json, ?) {TYPE_SQL[kind]}
                AND json_extract(r.data_json, ?) BETWEEN ? AND ?)""")
            args.extend([path, path, low, high])
        elif op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                invalid(f"in requires a list of 1 to {MAX_IN_VALUES} scalars")
            by_kind = {}
            for item in value:
                kind, bound = operand(item, "in values")
                by_kind.setdefault(kind, []).append(bound)
            parts = []
            for kind, values in by_kind.items():
                parts.append(f"""(json_type(r.data_json, ?) {TYPE_SQL[kind]}
                    AND json_extract(r.data_json, ?) IN ({', '.join('?' * len(values))}))""")
                args.extend([path, path, *values])
            fragments.append("(" + " OR ".join(parts) + ")")
        else:
            kind, bound = operand(value, "value")
            if kind == "boolean" and op not in ("eq", "ne"):
                invalid("booleans support only eq, ne, and in")
            fragments.append(f"""(json_type(r.data_json, ?) {TYPE_SQL[kind]}
                AND json_extract(r.data_json, ?) {COMPARISONS[op]} ?)""")
            args.extend([path, path, bound])
    return fragments, args


def relationship_types(value):
    types = [] if value is None else value
    if not isinstance(types, list) or len(types) > MAX_RELATIONSHIP_TYPES:
        invalid(f"relationship_types must list at most {MAX_RELATIONSHIP_TYPES} exact relationship types")
    return [clean_text(t, "relationship_type", 100) for t in types]


def link_direction(value):
    if value not in DIRECTIONS:
        invalid("direction must be outgoing, incoming or both")
    return value


def parse_linked_to(linked_to):
    """Validate shape only; returns None or (record_id, types, direction)."""
    if linked_to is None:
        return None
    keys_only(linked_to, "linked_to", ("record_id", "relationship_types", "direction"), ("record_id",))
    if not isinstance(linked_to["record_id"], str) or not 1 <= len(linked_to["record_id"]) <= 300:
        invalid("linked_to.record_id must be a record ID string")
    return (linked_to["record_id"], relationship_types(linked_to.get("relationship_types")),
            link_direction(linked_to.get("direction", "both")))


def compile_linked_to(tracker, con, linked_to):
    """Compile a link filter to (sql_fragment or None, args) for records alias r.

    Direction is from the matched record's point of view: "outgoing" means the matched record is
    the source and record_id the target. The anchor must exist in the workspace (NOT_FOUND
    otherwise); it may be archived. Any one matching relationship is enough.
    """
    parsed = parse_linked_to(linked_to)
    if parsed is None:
        return None, []
    record_id, types, direction = parsed
    tracker._record(con, record_id)
    sides = []
    if direction in ("outgoing", "both"):
        sides.append("(l.source_id=r.id AND l.target_id=?)")
    if direction in ("incoming", "both"):
        sides.append("(l.target_id=r.id AND l.source_id=?)")
    sql = f"""EXISTS (SELECT 1 FROM relationships l WHERE l.workspace_id=r.workspace_id
        AND ({' OR '.join(sides)})"""
    if types:
        sql += f" AND l.relationship_type IN ({', '.join('?' * len(types))})"
    return sql + ")", [*[record_id] * len(sides), *types]


def parse_order_by(order_by):
    """Validate order_by; returns None or (json path, "number"|"string", descending)."""
    if order_by is None:
        return None
    keys_only(order_by, "order_by", ("field", "type", "direction"), ("field", "type"))
    if order_by["type"] not in ("number", "string"):
        invalid('order_by.type must be "number" or "string"')
    direction = order_by.get("direction", "asc")
    if direction not in ("asc", "desc"):
        invalid('order_by.direction must be "asc" or "desc"')
    return json_path(order_by["field"], "order_by.field"), order_by["type"], direction == "desc"


def sort_position(position, kind):
    """Restore a sorted search's cursor value to the exact type it was issued with."""
    if position and kind == "number":
        try:
            try:
                position[0] = int(position[0])
            except ValueError:
                position[0] = float(position[0])
            if isinstance(position[0], int) and not -INT64 <= position[0] < INT64:
                raise ValueError
        except ValueError:
            invalid("Invalid cursor or cursor used with a different search/workspace")
    return position


def parse_group_by(group_by):
    if group_by is None:
        return ("none",)
    if not isinstance(group_by, dict) or not group_by:
        invalid('group_by must be {"field": [...]}, {"collection": true} or {"linked": {...}}')
    if "field" in group_by:
        keys_only(group_by, "group_by", ("field", "bucket"))
        if group_by.get("bucket") not in (None, "month", "year"):
            invalid('group_by.bucket must be "month" or "year"')
        return "field", json_path(group_by["field"], "group_by.field"), group_by.get("bucket")
    if "collection" in group_by:
        keys_only(group_by, "group_by", ("collection",))
        if group_by["collection"] is not True:
            invalid("group_by.collection must be true")
        return ("collection",)
    keys_only(group_by, "group_by", ("linked",), ("linked",))
    linked = keys_only(group_by["linked"], "group_by.linked", ("relationship_types", "direction"))
    return ("linked", relationship_types(linked.get("relationship_types")),
            link_direction(linked.get("direction", "both")))


def parse_metrics(metrics):
    if metrics is None:
        metrics = [{"op": "count"}]
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= MAX_METRICS:
        invalid(f"metrics must be a list of 1 to {MAX_METRICS} metric objects")
    parsed = []
    for metric in metrics:
        keys_only(metric, "each metric", ("op", "field", "type"), ("op",))
        op = metric["op"]
        if not isinstance(op, str) or op not in METRIC_OPS:
            invalid(f"metric op must be one of {', '.join(METRIC_OPS)}")
        if op == "count":
            if len(metric) > 1:
                invalid("count takes no field or type; it counts the records in each group")
            parsed.append({"op": op})
            continue
        if "field" not in metric:
            invalid(f"{op} requires a field")
        kind = metric.get("type", "number")
        if kind not in ("number", "string") or (kind == "string" and op in ("sum", "avg")):
            invalid('metric type must be "number", or "string" for min and max only')
        path = json_path(metric["field"], "metric field")
        parsed.append({"op": op, "field": list(metric["field"]), "type": kind, "path": path})
    return parsed


# Group key columns k1, k2 (GROUP BY) and k3 (extra label) for each group_by form.
FIELD_KEYS = """CASE json_type(m.data_json, ?) WHEN 'integer' THEN 'number' WHEN 'real' THEN 'number'
        WHEN 'text' THEN 'string' WHEN 'true' THEN 'boolean' WHEN 'false' THEN 'boolean' END AS k1,
    CASE WHEN json_type(m.data_json, ?) IN ('integer', 'real', 'text', 'true', 'false')
        THEN json_extract(m.data_json, ?) END AS k2, NULL AS k3"""
BUCKETS = {"month": (7, """(v GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' OR v GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*')
                AND substr(v, 6, 2) BETWEEN '01' AND '12'"""),
           "year": (4, "(v GLOB '[0-9][0-9][0-9][0-9]' OR v GLOB '[0-9][0-9][0-9][0-9]-*')")}


class Analytics:
    def __init__(self, tracker):
        self.t = tracker

    def aggregate(self, collection_id=None, where=None, filters=None, text=None, linked_to=None,
                  group_by=None, metrics=None, limit=50):
        """Count and summarise active records in one SQL pass, reporting what each total excludes.

        Filters (collection_id, where, filters, text, linked_to) mean the same as in search_records
        and combine with AND. There is no row cap: every matching active record is aggregated.

        metrics: 1-10 of {"op": "count"} or {"op": "sum"|"avg"|"min"|"max", "field": [keys...]}.
        sum and avg use JSON numbers only. min and max use numbers by default, or strings (for
        example ISO 8601 dates) with "type": "string"; one type per metric, never mixed. Every
        non-count metric reports used, skipped_missing (key absent or JSON null) and
        skipped_non_numeric (skipped_non_string for string metrics): present but the wrong type,
        such as the string "95 LPA". value is null when used is 0.

        group_by: None; {"field": [keys...]} with optional "bucket": "month"|"year" for ISO 8601
        date strings; {"collection": true}; or {"linked": {"relationship_types", "direction"}}.
        Field keys stay typed (1, "1" and true are separate groups; 1 and 1.0 are one number).
        Missing, null, list, object, and unbucketable values share the null group. Groups are
        ordered by record count descending, then key; limit bounds the groups returned.
        """
        bounded(limit, "limit")
        conditions, condition_args = compile_where(where)
        encoded = object_json({} if filters is None else filters)
        match = None if text is None else fts_query(text)
        if match is not None and not self.t.db.fts:
            raise TrackerError("UNSUPPORTED", "This SQLite build lacks FTS5; use where and filters instead")
        parse_linked_to(linked_to)  # shape errors before any database work
        grouping, specs = parse_group_by(group_by), parse_metrics(metrics)

        source, scope, args = "records r", ["r.workspace_id=?", "r.archived_at IS NULL"], [self.t.workspace]
        if match is not None:
            source = "record_fts JOIN record_search_keys k ON k.doc_id=record_fts.rowid JOIN records r ON r.id=k.record_id"
            scope.append("record_fts MATCH ?")
            args.append(match)
        if collection_id is not None:
            scope.append("r.collection_id=?")
            args.append(collection_id)
        if encoded != "{}":
            scope.append("matches(r.data_json, ?)")
            args.append(encoded)
        scope.extend(conditions)
        args.extend(condition_args)

        if grouping[0] == "none":
            keyed, key_args = "SELECT m.data_json, NULL AS k1, NULL AS k2, NULL AS k3 FROM m", []
        elif grouping[0] == "collection":
            keyed = """SELECT m.data_json, c.name AS k1, c.id AS k2, NULL AS k3 FROM m
                JOIN collections c ON c.id=m.collection_id"""
            key_args = []
        elif grouping[0] == "field" and grouping[2] is None:
            keyed, key_args = f"SELECT m.data_json, {FIELD_KEYS} FROM m", [grouping[1]] * 3
        elif grouping[0] == "field":
            length, test = BUCKETS[grouping[2]]
            # The inner CASE hands GLOB/substr text only; other types become NULL and fail the test.
            keyed = f"""SELECT data_json, CASE WHEN {test} THEN 'string' END AS k1,
                CASE WHEN {test} THEN substr(v, 1, {length}) END AS k2, NULL AS k3
                FROM (SELECT m.data_json, CASE WHEN json_type(m.data_json, ?) = 'text'
                    THEN json_extract(m.data_json, ?) END AS v FROM m)"""
            key_args = [grouping[1]] * 2
        else:
            _, types, direction = grouping
            typed = f" AND l.relationship_type IN ({', '.join('?' * len(types))})" if types else ""
            sides = []
            if direction in ("outgoing", "both"):
                sides.append("SELECT DISTINCT l.source_id AS rid, l.target_id AS oid FROM relationships l "
                             "WHERE l.workspace_id=?" + typed)
            if direction in ("incoming", "both"):
                sides.append("SELECT DISTINCT l.target_id AS rid, l.source_id AS oid FROM relationships l "
                             "WHERE l.workspace_id=?" + typed)
            # DISTINCT and UNION de-duplicate, so a record counts once per linked record however many
            # relationship types or directions connect the pair.
            keyed = f"""SELECT m.data_json, x.title AS k1, x.id AS k2, xc.name AS k3 FROM m
                LEFT JOIN ({' UNION '.join(sides)}) e ON e.rid=m.id
                LEFT JOIN records x ON x.id=e.oid LEFT JOIN collections xc ON xc.id=x.collection_id"""
            key_args = [x for _ in sides for x in (self.t.workspace, *types)]

        columns, metric_args = "", []
        for index, spec in enumerate(specs):
            if spec["op"] == "count":
                continue
            path, typed = spec["path"], f"json_type(data_json, ?) {TYPE_SQL[spec['type']]}"
            columns += f""", sum({typed}) AS used{index},
                sum(coalesce(json_type(data_json, ?), 'null') = 'null') AS missing{index}"""
            metric_args.extend([path, path])
            if spec["op"] == "sum":
                # Integers are summed apart from reals so an all-integer total stays exact.
                columns += f""", sum(CASE WHEN json_type(data_json, ?) = 'integer' THEN json_extract(data_json, ?) END) AS a{index},
                    sum(CASE WHEN json_type(data_json, ?) = 'real' THEN json_extract(data_json, ?) END) AS b{index}"""
                metric_args.extend([path] * 4)
            else:
                columns += f""", {spec['op']}(CASE WHEN {typed} THEN json_extract(data_json, ?) END) AS a{index}"""
                metric_args.extend([path] * 2)

        with self.t.db.connect() as con:
            if collection_id is not None:
                self.t._collection(con, collection_id)
            link_sql, link_args = compile_linked_to(self.t, con, linked_to)
            if link_sql:
                scope.append(link_sql)
                args.extend(link_args)
            base = f"FROM {source} WHERE " + " AND ".join(scope)
            matched = con.execute("SELECT count(*) " + base, args).fetchone()[0]
            try:
                rows = con.execute(f"""WITH m AS (SELECT r.id, r.collection_id, r.data_json {base}),
                    g AS ({keyed})
                    SELECT k1, k2, k3, count(*) AS n, count(*) OVER () AS group_total {columns}
                    FROM g GROUP BY k1, k2 ORDER BY n DESC, k1 IS NULL, k1, k2 LIMIT ?""",
                    [*args, *key_args, *metric_args, limit + 1]).fetchall()
            except sqlite3.OperationalError as exc:
                if "integer overflow" not in str(exc):
                    raise
                raise TrackerError("UNSUPPORTED", "An integer sum exceeds 64 bits; narrow the filters") from None

        if grouping[0] == "none" and not rows:
            # An ungrouped aggregate always reports its one group, even when nothing matched.
            rows = [{"k1": None, "k2": None, "k3": None, "n": 0, "group_total": 1,
                     **{f"{name}{i}": None for i in range(len(specs)) for name in ("used", "missing", "a", "b")}}]
        groups = []
        for row in rows[:limit]:
            if grouping[0] == "none" or row["k2"] is None:
                key = None
            elif grouping[0] == "collection":
                key = {"collection_id": row["k2"], "collection_name": row["k1"]}
            elif grouping[0] == "linked":
                key = {"record_id": row["k2"], "title": row["k1"], "collection_name": row["k3"]}
            else:
                key = bool(row["k2"]) if row["k1"] == "boolean" else row["k2"]
            results = []
            for index, spec in enumerate(specs):
                if spec["op"] == "count":
                    results.append({"op": "count", "value": row["n"]})
                    continue
                used, missing = row[f"used{index}"] or 0, row[f"missing{index}"] or 0
                value = row[f"a{index}"]
                if spec["op"] == "sum":
                    parts = [x for x in (value, row[f"b{index}"]) if x is not None]
                    value = sum(parts) if parts else None
                result = {"op": spec["op"], "field": spec["field"], "value": value, "used": used,
                          "skipped_missing": missing, SKIP_KEYS[spec["type"]]: row["n"] - used - missing}
                if spec["op"] in ("min", "max"):
                    result["type"] = spec["type"]
                results.append(result)
            groups.append({"key": key, "count": row["n"], "metrics": results})
        note = ("Active records only. used/skipped_* show how many records each metric includes and "
                "excludes; a value covers only its used records. The null key groups records whose "
                "group value is missing or unsupported.")
        if grouping[0] == "linked":
            note += (" A record linked to several records is counted in each of their groups, so "
                     "group counts and sums can add up to more than matched.")
        return {"matched": matched, "grouped_by": grouping[0], "groups": groups,
                "group_total": rows[0]["group_total"] if rows else 0,
                "groups_truncated": len(rows) > limit, "note": note}
