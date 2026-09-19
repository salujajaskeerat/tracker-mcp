"""Conservative discovery and write tickets. No model, semantic oracle or auth claims.

Tickets record what the agent reviewed. Human confirmation is an agent-reported
attestation, not something this local server can independently authenticate.
"""
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

from .db import canonical
from .service import (TrackerError, bounded, invalid, now, object_json, record,
                      summary, text, uid)

COLLECTION_SCAN_LIMIT = 200
RECORD_SCAN_LIMIT = 1000
CANDIDATE_LIMIT = 20


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def similarity(query, name):
    a, b = normalize(query), normalize(name)
    if a == b:
        return 1.0
    # Short names are especially dangerous: only exact matches at length 1–2.
    if min(len(a), len(b)) < 3:
        return 0.0
    if a in b or b in a:
        return 0.85
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def words(value):
    return set(re.findall(r"\w+", normalize(value))) - {
        "a", "an", "the", "of", "for", "to", "and", "one", "with", "record", "records"}


class Workflow:
    def __init__(self, tracker):
        self.t = tracker

    def _fingerprint(self, con):
        """Conservative workspace-wide snapshot; streams rows without loading all data.

        Includes graph, metadata and aliases, not tickets. Unrelated writes may
        invalidate a ticket; this is intentional for a small local demo.
        """
        digest = hashlib.sha256()
        for table in ("collections", "records", "relationships", "record_aliases", "collection_aliases"):
            digest.update(table.encode())
            for row in con.execute(f"SELECT * FROM {table} WHERE workspace_id=? ORDER BY rowid",
                                   (self.t.workspace,)):
                digest.update(canonical(dict(row)).encode())
        return digest.hexdigest()

    def _save(self, con, kind, payload):
        ticket_id = uid()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="microseconds")
        con.execute("INSERT INTO workflow_tickets VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                    (ticket_id, self.t.workspace, self.t.actor, kind, canonical(payload),
                     self._fingerprint(con), expires))
        return ticket_id

    def _load(self, con, ticket_id, kind, replay=False):
        row = con.execute("""SELECT * FROM workflow_tickets WHERE id=? AND workspace_id=?
            AND actor_id=? AND kind=?""", (ticket_id, self.t.workspace, self.t.actor, kind)).fetchone()
        if not row:
            raise TrackerError("NOT_FOUND", "Review ticket not found for this workspace and actor")
        if replay and row["result_json"] is not None:
            return json.loads(row["payload_json"]), json.loads(row["result_json"])
        if row["expires_at"] < now() or row["fingerprint"] != self._fingerprint(con):
            raise TrackerError("STALE_REVIEW", "Context changed or review expired; discover/resolve again")
        return json.loads(row["payload_json"]), None

    def discover_collections(self, name, purpose):
        name, purpose = text(name, "name", 100), text(purpose, "purpose", 1000)
        with self.t.db.connect(write=True) as con:
            rows = con.execute("SELECT * FROM collections WHERE workspace_id=? ORDER BY name, id LIMIT ?",
                               (self.t.workspace, COLLECTION_SCAN_LIMIT + 1)).fetchall()
            catalog = []
            for row in rows[:COLLECTION_SCAN_LIMIT]:
                item = dict(row)
                aliases = [r[0] for r in con.execute("""SELECT alias FROM collection_aliases
                    WHERE workspace_id=? AND collection_id=? ORDER BY alias""", (self.t.workspace, row["id"]))]
                samples = con.execute("""SELECT r.*, c.name AS collection_name FROM records r
                    JOIN collections c ON c.id=r.collection_id WHERE r.workspace_id=?
                    AND r.collection_id=? AND r.archived_at IS NULL ORDER BY r.created_at, r.id LIMIT 3""",
                    (self.t.workspace, row["id"])).fetchall()
                lexical = max([similarity(name, row["name"]), *[similarity(name, a) for a in aliases]])
                overlap = sorted(words(purpose) & words(row["description"]))[:10]
                reasons = []
                if lexical == 1:
                    reasons.append("exact normalized name or saved alias")
                elif lexical >= .65:
                    reasons.append("similar spelling or overlapping name")
                if overlap:
                    reasons.append("shared purpose words: " + ", ".join(overlap))
                item.update(aliases=aliases, examples=[summary(r) for r in samples],
                            reasons=reasons, similarity=round(lexical, 3))
                catalog.append(item)
            complete = len(rows) <= COLLECTION_SCAN_LIMIT
            candidates = [c for c in catalog if c["reasons"]]
            result = {"name": name, "purpose": purpose, "catalog": catalog,
                      "candidates": [{"id": c["id"], "name": c["name"], "reasons": c["reasons"]}
                                     for c in candidates], "complete": complete,
                      "status": "needs_review" if complete else "incomplete",
                      "guidance": "Review the entire catalog by purpose, including examples. Similarity is not semantic identity. Reuse a fitting collection; ask only if the distinction is unclear."}
            result["discovery_id"] = self._save(con, "discovery", result)
            return result

    def resolve_record(self, query, collection_id=None, context_record_ids=None):
        query = text(query, "query")
        context_record_ids = context_record_ids or []
        if not isinstance(context_record_ids, list) or len(context_record_ids) > 10:
            invalid("context_record_ids must contain at most 10 explicit linked record IDs")
        with self.t.db.connect(write=True) as con:
            if collection_id:
                self.t._collection(con, collection_id)
            for rid in context_record_ids:
                self.t._record(con, rid)
            sql = """SELECT r.*, c.name AS collection_name FROM records r
                     JOIN collections c ON c.id=r.collection_id WHERE r.workspace_id=?"""
            args = [self.t.workspace]
            if collection_id:
                sql += " AND r.collection_id=?"
                args.append(collection_id)
            rows = con.execute(sql + " ORDER BY r.id LIMIT ?", [*args, RECORD_SCAN_LIMIT + 1]).fetchall()
            candidates = []
            self_query = normalize(query) in {"me", "myself", "i"}
            for row in rows[:RECORD_SCAN_LIMIT]:
                aliases = [r[0] for r in con.execute("SELECT alias FROM record_aliases WHERE workspace_id=? AND record_id=?",
                                                   (self.t.workspace, row["id"]))]
                if self_query:
                    score = 1.0 if f"@self:{self.t.actor}" in aliases else 0.0
                    basis = "explicit self mapping for configured local actor"
                else:
                    visible_aliases = [a for a in aliases if not a.startswith("@self:")]
                    score = max([similarity(query, row["title"]), *[similarity(query, a) for a in visible_aliases]])
                    basis = "exact normalized title or confirmed alias" if score == 1 else "similar spelling; identity not established"
                    if query == row["id"]:
                        score, basis = 1.0, "explicit stable record ID"
                if score < .65:
                    continue
                linked_ids = {r[0] for r in con.execute("""SELECT target_id FROM relationships WHERE workspace_id=? AND source_id=?
                    UNION SELECT source_id FROM relationships WHERE workspace_id=? AND target_id=?""",
                    (self.t.workspace, row["id"], self.t.workspace, row["id"]))}
                if not set(context_record_ids) <= linked_ids:
                    continue
                context = self.t.get_record_context(row["id"], relationship_limit=5, event_limit=1)
                reasons = [basis]
                if context_record_ids:
                    reasons.append("has explicit links to every supplied context record")
                if row["archived_at"]:
                    reasons.append("archived: inspect before creating a replacement")
                candidates.append({**summary(row), "similarity": round(score, 3), "reasons": reasons,
                    "aliases": [a for a in aliases if not a.startswith("@self:")],
                    "incoming": context["incoming"], "outgoing": context["outgoing"],
                    "links_truncated": context["incoming_truncated"] or context["outgoing_truncated"]})
            candidates.sort(key=lambda c: (-c["similarity"], c["title"], c["id"]))
            complete = len(rows) <= RECORD_SCAN_LIMIT and len(candidates) <= CANDIDATE_LIMIT
            exact = [c for c in candidates if c["similarity"] == 1]
            status = "no_match"
            if not complete:
                status = "incomplete"
            elif candidates:
                status = "resolved" if len(exact) == 1 and not exact[0]["archived_at"] else "needs_clarification"
            result = {"query": query, "collection_id": collection_id, "context_record_ids": context_record_ids,
                      "candidates": candidates[:CANDIDATE_LIMIT], "complete": complete, "status": status,
                      "selected_record_id": exact[0]["id"] if status == "resolved" else None,
                      "guidance": "Names are not unique. Fuzzy/ambiguous candidates require a focused question or independent identity evidence. No match is not proof of absence. Never infer links from recency."}
            result["resolution_id"] = self._save(con, "resolution", result)
            return result

    def _selection(self, con, record_id, reviews, clarification):
        for review_id in reviews:
            result, _ = self._load(con, review_id, "resolution")
            if not result["complete"]:
                continue
            if record_id in {c["id"] for c in result["candidates"]}:
                if (result["status"] != "resolved" or result["selected_record_id"] != record_id) and not clarification:
                    raise TrackerError("NEEDS_CLARIFICATION", "Identify the intended candidate before writing",
                                       candidates=result["candidates"])
                return result
        raise TrackerError("REVIEW_REQUIRED", "Resolve every referenced record before preparing this write")

    def prepare_write(self, operation, arguments, review_ids, decision_reason, clarification=None):
        """Validate, execute in a rollback-only savepoint for preview, then persist a ticket."""
        object_json(arguments)
        reason = text(decision_reason, "decision_reason", 1000)
        if clarification is not None:
            clarification = text(clarification, "clarification", 1000)
        if not isinstance(review_ids, list) or not 1 <= len(review_ids) <= 12 or not all(isinstance(r, str) for r in review_ids):
            invalid("review_ids must contain 1–12 discovery/resolution IDs")
        with self.t.db.connect(write=True) as con:
            self._validate(con, operation, arguments, review_ids, clarification)
            # The real service validates versions, merged size and foreign keys here.
            # Rollback means preview never leaves records, aliases or events behind.
            con.execute("SAVEPOINT preview")
            try:
                result = self._perform(con, operation, arguments)
            finally:
                con.execute("ROLLBACK TO preview")
                con.execute("RELEASE preview")
            payload = {"operation": operation, "arguments": arguments, "review_ids": review_ids,
                       "decision_reason": reason, "agent_reported_clarification": clarification}
            action_id = self._save(con, "action", payload)
            return {"action_id": action_id, "operation": operation, "arguments": arguments,
                    "preview": result, "note": "Preview only. IDs/timestamps for new objects are provisional. Commit action_id without changing arguments. No extra user approval is needed when intent and identity are already clear."}

    def _validate(self, con, operation, args, reviews, clarification):
        fields = {
            "create_collection": ({"name", "purpose", "record_meaning", "typical_fields", "relationship_guidance"}, set()),
            "update_collection": ({"collection_id", "name", "purpose", "record_meaning", "typical_fields", "relationship_guidance"}, set()),
            "add_collection_alias": ({"collection_id", "alias"}, set()),
            "remove_collection_alias": ({"collection_id", "alias"}, set()),
            "create_record": ({"collection_id", "title", "data"}, set()),
            "update_record": ({"record_id", "changes", "expected_version"}, set()),
            "archive_record": ({"record_id", "expected_version"}, set()),
            "rename_record": ({"record_id", "title", "expected_version"}, set()),
            "add_record_alias": ({"record_id", "alias", "expected_version"}, set()),
            "remove_record_alias": ({"record_id", "alias", "expected_version"}, set()),
            "set_self": ({"record_id", "expected_version"}, set()),
            "link_records": ({"source_id", "relationship_type", "target_id"}, set()),
            "unlink_records": ({"relationship_id"}, set()),
        }
        if operation not in fields:
            invalid("Unsupported write operation")
        required, optional = fields[operation]
        if set(args) != required:
            invalid(f"{operation} requires exactly these arguments: {', '.join(sorted(required))}")
        for key in required - {"data", "changes", "expected_version", "typical_fields"}:
            text(args[key], key, 2000 if key in {"purpose", "record_meaning", "relationship_guidance"} else 300)
        if operation in {"create_collection", "update_collection", "add_collection_alias", "remove_collection_alias"}:
            discovery = None
            for review in reviews:
                # Collection operations consume discovery tickets only.
                candidate, _ = self._load(con, review, "discovery")
                if candidate["complete"]:
                    discovery = candidate
                    break
            if discovery is None:
                raise TrackerError("INCOMPLETE_REVIEW", "Catalog review is incomplete; no collection write is allowed")
            if operation in {"create_collection", "update_collection"}:
                if normalize(args["name"]) != normalize(discovery["name"]) or args["purpose"] != discovery["purpose"]:
                    raise TrackerError("REVIEW_REQUIRED", "Discover using the proposed name and purpose first")
                self._description(args)
            else:
                if normalize(args["alias"]) != normalize(discovery["name"]):
                    raise TrackerError("REVIEW_REQUIRED", "Discover the proposed alias first")
                if not clarification:
                    raise TrackerError("NEEDS_CLARIFICATION", "Aliases must be explicitly confirmed, not learned from typos")
            if "collection_id" in args:
                self.t._collection(con, args["collection_id"])
            # An exact normalized name always reuses the existing collection.
            exact = [c for c in discovery["catalog"] if normalize(c["name"]) == normalize(args.get("name", ""))]
            overlaps = [c for c in discovery["candidates"] if c["id"] != args.get("collection_id")]
            if overlaps and not (operation == "create_collection" and exact) and not clarification:
                raise TrackerError("NEEDS_CLARIFICATION", "Possible overlapping collection: reuse it or record the clarified distinction", candidates=overlaps)
            return
        if operation == "create_record":
            self.t._collection(con, args["collection_id"])
            matches = []
            for review in reviews:
                candidate, _ = self._load(con, review, "resolution")
                if candidate["collection_id"] == args["collection_id"] and normalize(candidate["query"]) == normalize(args["title"]) and not candidate["context_record_ids"]:
                    matches.append(candidate)
            if not matches or not any(r["complete"] for r in matches):
                raise TrackerError("REVIEW_REQUIRED", "Search the whole target collection for the proposed title before creation")
            if any(r["candidates"] for r in matches) and not clarification:
                raise TrackerError("NEEDS_CLARIFICATION", "Possible existing record (including archives); clarify whether this is a distinct record",
                                   candidates=matches[0]["candidates"])
            return
        ids = [args[k] for k in ("record_id", "source_id", "target_id") if k in args]
        if operation == "unlink_records":
            link = con.execute("SELECT * FROM relationships WHERE workspace_id=? AND id=?",
                               (self.t.workspace, args["relationship_id"])).fetchone()
            if not link:
                raise TrackerError("NOT_FOUND", "Relationship not found")
            ids = [link["source_id"], link["target_id"]]
        for rid in ids:
            self._selection(con, rid, reviews, clarification)
        if operation in {"add_record_alias", "remove_record_alias", "set_self"} and not clarification:
            raise TrackerError("NEEDS_CLARIFICATION", "Aliases and self mappings require an explicit user statement")

    def _description(self, args):
        fields = args["typical_fields"]
        if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
            invalid("typical_fields must list 1–20 examples; these are guidance, not required fields")
        fields = [text(f, "typical field", 100) for f in fields]
        return text(f"Purpose: {args['purpose']}\nOne record: {args['record_meaning']}\n"
                    f"Typical information (optional): {', '.join(fields)}\n"
                    f"Relationships: {args['relationship_guidance']}\nUnknown information may remain missing.",
                    "description", 2000)

    def _perform(self, con, operation, args):
        if operation == "create_collection":
            name = normalize(text(args["name"], "name", 100))
            # Also recognize pre-upgrade names that did not use Unicode normalization.
            for row in con.execute("SELECT * FROM collections WHERE workspace_id=?", (self.t.workspace,)):
                if normalize(row["name"]) == name:
                    return dict(row)
            return self.t.create_collection(name, self._description(args))
        if operation == "update_collection":
            before = dict(self.t._collection(con, args["collection_id"]))
            name = normalize(text(args["name"], "name", 100))
            for row in con.execute("SELECT id, name FROM collections WHERE workspace_id=?", (self.t.workspace,)):
                if row["id"] != before["id"] and normalize(row["name"]) == name:
                    invalid("Another collection already has that normalized name; reuse it instead")
            con.execute("UPDATE collections SET name=?, description=? WHERE workspace_id=? AND id=?",
                        (name, self._description(args), self.t.workspace, args["collection_id"]))
            after = dict(self.t._collection(con, args["collection_id"]))
            self.t._collection_event(con, after["id"], "update", before, after)
            return after
        if operation in {"add_collection_alias", "remove_collection_alias"}:
            alias = normalize(text(args["alias"], "alias", 100))
            if operation.startswith("add"):
                count = con.execute("SELECT count(*) FROM collection_aliases WHERE workspace_id=? AND collection_id=?",
                                    (self.t.workspace, args["collection_id"])).fetchone()[0]
                if count >= 20:
                    invalid("At most 20 aliases per collection")
                con.execute("INSERT OR IGNORE INTO collection_aliases VALUES (?, ?, ?)", (self.t.workspace, args["collection_id"], alias))
            else:
                con.execute("DELETE FROM collection_aliases WHERE workspace_id=? AND collection_id=? AND alias=?", (self.t.workspace, args["collection_id"], alias))
            self.t._collection_event(con, args["collection_id"], operation,
                                     {"alias": alias} if operation.startswith("remove") else None,
                                     {"alias": alias} if operation.startswith("add") else None)
            return {"collection_id": args["collection_id"], "alias": alias}
        if operation in {"rename_record", "add_record_alias", "remove_record_alias", "set_self"}:
            bounded(args["expected_version"], "expected_version", high=2**63-2)
            before = record(self.t._record(con, args["record_id"], active=True))
            if before["version"] != args["expected_version"]:
                raise TrackerError("VERSION_CONFLICT", "Record changed", current_version=before["version"])
            title = text(args["title"], "title") if operation == "rename_record" else before["title"]
            con.execute("UPDATE records SET title=?, version=version+1, updated_at=? WHERE workspace_id=? AND id=?",
                        (title, now(), self.t.workspace, before["id"]))
            alias = None
            if operation != "rename_record":
                alias = f"@self:{self.t.actor}" if operation == "set_self" else normalize(text(args["alias"], "alias"))
                if operation != "set_self" and alias.startswith("@self:"):
                    invalid("Use set_self for an actor-specific self mapping")
                if operation == "set_self":
                    existing = con.execute("SELECT record_id FROM record_aliases WHERE workspace_id=? AND alias=?",
                                           (self.t.workspace, alias)).fetchone()
                    if existing and existing[0] != before["id"]:
                        raise TrackerError("NEEDS_CLARIFICATION", "A different self mapping already exists; do not silently replace identity")
                if operation == "remove_record_alias":
                    con.execute("DELETE FROM record_aliases WHERE workspace_id=? AND record_id=? AND alias=?", (self.t.workspace, before["id"], alias))
                else:
                    count = con.execute("SELECT count(*) FROM record_aliases WHERE workspace_id=? AND record_id=?", (self.t.workspace, before["id"])).fetchone()[0]
                    if count >= 20:
                        invalid("At most 20 aliases per record")
                    con.execute("INSERT OR IGNORE INTO record_aliases VALUES (?, ?, ?)", (self.t.workspace, before["id"], alias))
            after = record(self.t._record(con, before["id"]))
            self.t._event(con, before["id"], operation, before, {"record": after, "alias": alias})
            return after
        method = {"create_record": self.t.create_record, "update_record": self.t.update_record,
                  "archive_record": self.t.archive_record, "link_records": self.t.link_records,
                  "unlink_records": self.t.unlink_records}[operation]
        return method(**args)

    def commit_write(self, action_id):
        with self.t.db.connect(write=True) as con:
            payload, replay = self._load(con, action_id, "action", replay=True)
            if replay is not None:
                return replay
            self._validate(con, payload["operation"], payload["arguments"], payload["review_ids"], payload["agent_reported_clarification"])
            result = self._perform(con, payload["operation"], payload["arguments"])
            # Retain review evidence so the decision is understandable after expiry.
            evidence = []
            for review in payload["review_ids"]:
                row = con.execute("SELECT kind, payload_json FROM workflow_tickets WHERE id=? AND workspace_id=? AND actor_id=?",
                                  (review, self.t.workspace, self.t.actor)).fetchone()
                reviewed = json.loads(row["payload_json"])
                evidence.append({"review_id": review, "kind": row["kind"], "query": reviewed.get("query", reviewed.get("name")),
                                 "purpose": reviewed.get("purpose"), "status": reviewed["status"],
                                 "candidate_ids": [c["id"] for c in reviewed["candidates"]]})
            audit = {**payload, "action_id": action_id, "evidence": evidence,
                     "attribution_note": "Reason and clarification are agent-reported, not authenticated user consent."}
            op, args = payload["operation"], payload["arguments"]
            if "collection" in op:
                cid = result.get("collection_id", result.get("id"))
                self.t._collection_event(con, cid, "decision", None, audit)
            else:
                rid = args.get("record_id", args.get("source_id"))
                if op == "create_record":
                    rid = result["id"]
                if op == "unlink_records":
                    rid = result["removed"]["source_id"]
                self.t._event(con, rid, "decision", None, audit)
                if op == "create_record":
                    self.t._collection_event(con, args["collection_id"], "reuse", None,
                                             {"record_id": rid, "decision_reason": payload["decision_reason"],
                                              "attribution_note": "Agent-reported collection choice"})
            con.execute("UPDATE workflow_tickets SET result_json=? WHERE id=?", (canonical(result), action_id))
            return result
