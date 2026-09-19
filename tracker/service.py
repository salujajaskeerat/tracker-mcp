"""Domain-neutral operations; all identity comes from configured local context."""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from .db import Database, canonical

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
MAX_DATA_BYTES = 16_384


class TrackerError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def invalid(message):
    raise TrackerError("INVALID_INPUT", message)


def text(value, name, maximum=300):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        invalid(f"{name} must be nonblank and at most {maximum} characters")
    return value.strip()


def object_json(value):
    try:
        value = JSON_OBJECT.validate_python(value, strict=True)
        encoded = canonical(value)
        if len(encoded.encode()) > MAX_DATA_BYTES:
            invalid(f"JSON object exceeds {MAX_DATA_BYTES} bytes")
        return encoded
    except (ValidationError, ValueError, TypeError, RecursionError):
        invalid("Expected a finite JSON object with string keys")


def bounded(value, name, low=1, high=100):
    if type(value) is not int or not low <= value <= high:
        invalid(f"{name} must be an integer between {low} and {high}")
    return value


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid():
    return str(uuid4())


def record(row):
    result = dict(row)
    result["data"] = json.loads(result.pop("data_json"))
    return result


def event(row):
    result = dict(row)
    for key in ("before", "after"):
        raw = result.pop(key + "_json")
        result[key] = json.loads(raw) if raw is not None else None
    return result


def summary(row):
    result = {k: row[k] for k in ("id", "title", "collection_id", "collection_name", "version", "archived_at")}
    raw = row["data_json"]
    result.update(data_preview=raw[:500], data_preview_truncated=len(raw) > 500)
    return result


def cursor_scope(*parts):
    return hashlib.sha256(canonical(parts).encode()).hexdigest()


def decode_cursor(cursor, scope):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(payload, list) or len(payload) != 3 or payload[0] != scope
                or not all(isinstance(x, str) for x in payload)):
            raise ValueError
        return payload[1:]
    except (ValueError, TypeError):
        invalid("Invalid cursor or cursor used with a different search/workspace")


def next_cursor(rows, limit, scope):
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return base64.urlsafe_b64encode(canonical([scope, last["created_at"], last["id"]]).encode()).decode()


class Tracker:
    def __init__(self, db: Database, workspace_id="local", actor_id="local-agent"):
        self.db = db
        self.workspace = text(workspace_id, "workspace_id")
        self.actor = text(actor_id, "actor_id")

    @classmethod
    def from_env(cls):
        return cls(Database(os.getenv("TRACKER_DB_PATH", "tracker.db")),
                   os.getenv("TRACKER_WORKSPACE_ID", "local"),
                   os.getenv("TRACKER_ACTOR_ID", "local-agent"))

    def _record(self, con, record_id, active=False):
        row = con.execute("SELECT * FROM records WHERE workspace_id=? AND id=?",
                          (self.workspace, record_id)).fetchone()
        if row is None:
            raise TrackerError("NOT_FOUND", "Record not found in this workspace")
        if active and row["archived_at"]:
            raise TrackerError("ARCHIVED", "Archived records cannot be changed or newly linked")
        return row

    def _collection(self, con, collection_id):
        row = con.execute("SELECT * FROM collections WHERE workspace_id=? AND id=?",
                          (self.workspace, collection_id)).fetchone()
        if row is None:
            raise TrackerError("NOT_FOUND", "Collection not found in this workspace")
        return row

    def _event(self, con, record_id, operation, before, after):
        con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid(), self.workspace, record_id, operation,
                     canonical(before) if before is not None else None,
                     canonical(after) if after is not None else None, self.actor, now()))

    def _collection_event(self, con, collection_id, operation, before, after):
        con.execute("INSERT INTO collection_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid(), self.workspace, collection_id, operation,
                     canonical(before) if before is not None else None,
                     canonical(after) if after is not None else None, self.actor, now()))

    def get_collection_history(self, collection_id, limit=20, cursor=None):
        bounded(limit, "limit")
        scope = cursor_scope("collection_history", self.workspace, collection_id)
        position = decode_cursor(cursor, scope)
        with self.db.connect() as con:
            self._collection(con, collection_id)
            sql = "SELECT * FROM collection_events WHERE workspace_id=? AND collection_id=?"
            args = [self.workspace, collection_id]
            if position:
                sql += " AND (created_at, id) < (?, ?)"
                args.extend(position)
            rows = con.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?",
                               [*args, limit + 1]).fetchall()
            return {"events": [event(r) for r in rows[:limit]],
                    "next_cursor": next_cursor(rows, limit, scope)}

    def list_collections(self):
        with self.db.connect() as con:
            rows = con.execute("""SELECT c.*, count(r.id) AS active_record_count
                FROM collections c LEFT JOIN records r ON r.collection_id=c.id
                AND r.workspace_id=c.workspace_id AND r.archived_at IS NULL
                WHERE c.workspace_id=? GROUP BY c.id ORDER BY c.name, c.id LIMIT 1001""",
                (self.workspace,)).fetchall()
            return {"collections": [dict(r) for r in rows[:1000]], "truncated": len(rows) > 1000}

    def create_collection(self, name, description):
        name = " ".join(text(name, "name", 100).casefold().split())
        if not isinstance(description, str) or len(description) > 2000:
            invalid("description must be a string of at most 2000 characters")
        with self.db.connect(write=True) as con:
            inserted = con.execute("""INSERT INTO collections VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, name) DO NOTHING""",
                (uid(), self.workspace, name, description, now()))
            result = dict(con.execute("SELECT * FROM collections WHERE workspace_id=? AND name=?",
                                      (self.workspace, name)).fetchone())
            if inserted.rowcount:
                self._collection_event(con, result["id"], "create", None, result)
            return result

    def create_record(self, collection_id, title, data):
        title, encoded = text(title, "title"), object_json(data)
        with self.db.connect(write=True) as con:
            self._collection(con, collection_id)
            rid, timestamp = uid(), now()
            con.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (rid, self.workspace, collection_id, title, encoded, 1,
                         self.actor, timestamp, timestamp, None))
            result = record(self._record(con, rid))
            self._event(con, rid, "create", None, result)
            return result

    def search_records(self, collection_id=None, query=None, filters=None, limit=20, cursor=None):
        bounded(limit, "limit")
        if query is not None and (not isinstance(query, str) or len(query) > 300):
            invalid("query must be a string of at most 300 characters")
        encoded = object_json({} if filters is None else filters)
        scope = cursor_scope("search", self.workspace, collection_id, query, json.loads(encoded))
        position = decode_cursor(cursor, scope)
        where, args = ["r.workspace_id=?", "r.archived_at IS NULL"], [self.workspace]
        if collection_id is not None:
            where.append("r.collection_id=?")
            args.append(collection_id)
        if query is not None:
            where.append("instr(casefold(r.title), ?) > 0")
            args.append(query.casefold())
        where.append("matches(r.data_json, ?)")
        args.append(encoded)
        if position:
            where.append("(r.created_at, r.id) > (?, ?)")
            args.extend(position)
        with self.db.connect() as con:
            if collection_id is not None:
                self._collection(con, collection_id)
            rows = con.execute("""SELECT r.*, c.name AS collection_name FROM records r
                JOIN collections c ON c.id=r.collection_id WHERE """ + " AND ".join(where)
                + " ORDER BY r.created_at, r.id LIMIT ?", [*args, limit + 1]).fetchall()
            return {"records": [summary(r) for r in rows[:limit]],
                    "next_cursor": next_cursor(rows, limit, scope),
                    "note": "No matches is not proof an entity does not exist; check spelling, scope, and archives."}

    def _change(self, record_id, expected_version, changes=None, archive=False):
        bounded(expected_version, "expected_version", high=2**63-2)
        if not archive:
            object_json(changes)
        with self.db.connect(write=True) as con:
            before = record(self._record(con, record_id))
            if before["version"] != expected_version:
                raise TrackerError("VERSION_CONFLICT", "Retrieve context and reconsider the change",
                                   current_version=before["version"])
            if before["archived_at"]:
                raise TrackerError("ARCHIVED", "Record is already archived")
            data = before["data"] if archive else {**before["data"], **changes}
            timestamp = now()
            changed = con.execute("""UPDATE records SET data_json=?, version=version+1,
                updated_at=?, archived_at=? WHERE workspace_id=? AND id=? AND version=?""",
                (object_json(data), timestamp, timestamp if archive else None,
                 self.workspace, record_id, expected_version))
            if changed.rowcount != 1:
                raise TrackerError("VERSION_CONFLICT", "Record changed")
            after = record(self._record(con, record_id))
            self._event(con, record_id, "archive" if archive else "update", before, after)
            return after

    def update_record(self, record_id, changes, expected_version):
        return self._change(record_id, expected_version, changes)

    def archive_record(self, record_id, expected_version):
        return self._change(record_id, expected_version, archive=True)

    def link_records(self, source_id, relationship_type, target_id):
        relationship_type = text(relationship_type, "relationship_type", 100)
        with self.db.connect(write=True) as con:
            self._record(con, source_id, active=True)
            self._record(con, target_id, active=True)
            existing = con.execute("""SELECT * FROM relationships WHERE workspace_id=?
                AND source_id=? AND relationship_type=? AND target_id=?""",
                (self.workspace, source_id, relationship_type, target_id)).fetchone()
            if existing:
                return dict(existing)
            result = dict(id=uid(), workspace_id=self.workspace, source_id=source_id,
                          relationship_type=relationship_type, target_id=target_id,
                          created_by=self.actor, created_at=now())
            con.execute("INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(result.values()))
            self._event(con, source_id, "link", None, result)
            return result

    def unlink_records(self, relationship_id):
        with self.db.connect(write=True) as con:
            row = con.execute("SELECT * FROM relationships WHERE workspace_id=? AND id=?",
                              (self.workspace, relationship_id)).fetchone()
            if row is None:
                raise TrackerError("NOT_FOUND", "Relationship not found in this workspace")
            result = dict(row)
            con.execute("DELETE FROM relationships WHERE workspace_id=? AND id=?",
                        (self.workspace, relationship_id))
            self._event(con, row["source_id"], "unlink", result, None)
            return {"removed": result}

    def _history(self, con, record_id, limit, cursor=None):
        scope = cursor_scope("history", self.workspace, record_id)
        position = decode_cursor(cursor, scope)
        sql = "SELECT * FROM events WHERE workspace_id=? AND record_id=?"
        args = [self.workspace, record_id]
        if position:
            sql += " AND (created_at, id) < (?, ?)"
            args.extend(position)
        rows = con.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?",
                           [*args, limit + 1]).fetchall()
        return {"events": [event(r) for r in rows[:limit]],
                "next_cursor": next_cursor(rows, limit, scope)}

    def get_record_history(self, record_id, limit=20, cursor=None):
        bounded(limit, "limit")
        with self.db.connect() as con:
            self._record(con, record_id)
            return self._history(con, record_id, limit, cursor)

    def get_record_context(self, record_id, relationship_limit=20, event_limit=10):
        bounded(relationship_limit, "relationship_limit")
        bounded(event_limit, "event_limit", low=0)
        with self.db.connect() as con:
            result = {"record": record(self._record(con, record_id))}
            # Separate directional limits prevent one direction from hiding the other.
            for direction, column, other in (("outgoing", "source_id", "target_id"),
                                              ("incoming", "target_id", "source_id")):
                rows = con.execute(f"""SELECT rel.*, r.title AS linked_title, r.collection_id AS linked_collection_id,
                    r.version AS linked_version, r.archived_at AS linked_archived_at,
                    r.data_json AS linked_data_json, c.name AS linked_collection_name
                    FROM relationships rel JOIN records r ON r.id=rel.{other} AND r.workspace_id=rel.workspace_id
                    JOIN collections c ON c.id=r.collection_id AND c.workspace_id=r.workspace_id
                    WHERE rel.workspace_id=? AND rel.{column}=? ORDER BY rel.created_at, rel.id LIMIT ?""",
                    (self.workspace, record_id, relationship_limit + 1)).fetchall()
                links = []
                for row in rows[:relationship_limit]:
                    linked = {key: row["linked_" + key] for key in
                              ("title", "collection_id", "version", "archived_at", "data_json", "collection_name")}
                    linked["id"] = row[other]
                    relationship = {key: row[key] for key in
                                    ("id", "workspace_id", "source_id", "relationship_type", "target_id", "created_by", "created_at")}
                    links.append({**relationship, "linked_record": summary(linked)})
                result[direction] = links
                result[direction + "_truncated"] = len(rows) > relationship_limit
            if event_limit == 0:
                result.update(events=[], events_included=False, events_truncated=None, events_next_cursor=None)
            else:
                history = self._history(con, record_id, event_limit)
                result.update(events=history["events"], events_truncated=history["next_cursor"] is not None,
                              events_next_cursor=history["next_cursor"])
            return result
