"""SQLite schema and short-lived transaction connections."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(workspace_id, name), UNIQUE(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, collection_id TEXT NOT NULL,
    title TEXT NOT NULL, data_json TEXT NOT NULL
        CHECK(json_valid(data_json) AND json_type(data_json) = 'object'),
    version INTEGER NOT NULL CHECK(version >= 1), created_by TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
    UNIQUE(workspace_id, id),
    FOREIGN KEY(workspace_id, collection_id) REFERENCES collections(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, source_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL, target_id TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(workspace_id, source_id, relationship_type, target_id),
    FOREIGN KEY(workspace_id, source_id) REFERENCES records(workspace_id, id),
    FOREIGN KEY(workspace_id, target_id) REFERENCES records(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, record_id TEXT NOT NULL,
    operation TEXT NOT NULL, before_json TEXT, after_json TEXT,
    actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id, record_id) REFERENCES records(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS collection_events (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, collection_id TEXT NOT NULL,
    operation TEXT NOT NULL, before_json TEXT, after_json TEXT,
    actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id, collection_id) REFERENCES collections(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS record_aliases (
    workspace_id TEXT NOT NULL, record_id TEXT NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY(workspace_id, record_id, alias),
    FOREIGN KEY(workspace_id, record_id) REFERENCES records(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS collection_aliases (
    workspace_id TEXT NOT NULL, collection_id TEXT NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY(workspace_id, collection_id, alias),
    FOREIGN KEY(workspace_id, collection_id) REFERENCES collections(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS workflow_tickets (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
    expires_at TEXT NOT NULL, result_json TEXT
);
CREATE INDEX IF NOT EXISTS collection_history
    ON collection_events(workspace_id, collection_id, created_at, id);
CREATE INDEX IF NOT EXISTS record_search ON records(workspace_id, created_at, id);
CREATE INDEX IF NOT EXISTS record_collection ON records(workspace_id, collection_id);
CREATE INDEX IF NOT EXISTS relationship_target ON relationships(workspace_id, target_id);
CREATE INDEX IF NOT EXISTS event_history ON events(workspace_id, record_id, created_at, id);
"""

# Full-text index over titles and every JSON value (keys are not indexed). Triggers keep it
# in step with records inside the same transaction, so rollbacks and previews stay exact.
# record_search_keys supplies a stable integer rowid: records.rowid may change on VACUUM.
FTS_BODY = """coalesce((SELECT group_concat(atom, ' | ') FROM json_tree({row}.data_json)
        WHERE type IN ('text', 'integer', 'real')), '')"""
FTS_INSERT = """INSERT INTO record_fts(rowid, title, body) VALUES (
        (SELECT doc_id FROM record_search_keys WHERE record_id=new.id), new.title, """ \
    + FTS_BODY.format(row="new") + ");"
FTS_DELETE = "DELETE FROM record_fts WHERE rowid=(SELECT doc_id FROM record_search_keys WHERE record_id=old.id);"
FTS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS record_search_keys (
    doc_id INTEGER PRIMARY KEY, record_id TEXT NOT NULL UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5(
    title, body, tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS record_fts_insert AFTER INSERT ON records BEGIN
    INSERT INTO record_search_keys(record_id) VALUES (new.id);
    {FTS_INSERT}
END;
CREATE TRIGGER IF NOT EXISTS record_fts_update AFTER UPDATE OF title, data_json ON records BEGIN
    {FTS_DELETE}
    {FTS_INSERT}
END;
CREATE TRIGGER IF NOT EXISTS record_fts_delete AFTER DELETE ON records BEGIN
    {FTS_DELETE}
    DELETE FROM record_search_keys WHERE record_id=old.id;
END;
"""
# Idempotent backfill for databases created before the index existed.
FTS_BACKFILL = f"""
INSERT INTO record_search_keys(record_id) SELECT id FROM records
    WHERE id NOT IN (SELECT record_id FROM record_search_keys) ORDER BY created_at, id;
INSERT INTO record_fts(rowid, title, body)
    SELECT k.doc_id, r.title, {FTS_BODY.format(row="r")}
    FROM records r JOIN record_search_keys k ON k.record_id=r.id
    WHERE k.doc_id NOT IN (SELECT rowid FROM record_fts);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def matches(data_json, filters_json):
    """Literal top-level keys; canonical JSON equality, including nested values."""
    data, filters = json.loads(data_json), json.loads(filters_json)
    return all(k in data and canonical(data[k]) == canonical(v) for k, v in filters.items())


class Database:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        self._connection = ContextVar("tracker_connection", default=None)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            try:
                con.executescript(FTS_SCHEMA + "BEGIN IMMEDIATE;" + FTS_BACKFILL + "COMMIT;")
                self.fts = True
            except sqlite3.OperationalError as exc:
                if "no such module" not in str(exc):
                    raise
                # SQLite built without FTS5: everything except text search keeps working.
                logging.warning("SQLite FTS5 unavailable; full-text search is disabled")
                self.fts = False

    @contextmanager
    def connect(self, write=False):
        # A workflow may call several service operations in one atomic transaction.
        # ContextVar isolates connections across threads/tasks; no global connection.
        existing = self._connection.get()
        if existing is not None:
            con, writable = existing
            if write and not writable:
                raise RuntimeError("Cannot promote a read transaction to a write transaction")
            yield con
            return
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.create_function("casefold", 1, str.casefold, deterministic=True)
        con.create_function("matches", 2, matches, deterministic=True)
        token = self._connection.set((con, write))
        try:
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            self._connection.reset(token)
            con.close()
