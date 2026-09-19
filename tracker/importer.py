"""Guarded bulk creation: the server runs the duplicate review for every row itself.

A single create needs the agent to hold one resolution ticket per title. For many rows that
adds round trips but no safety, because the server can apply the same name scoring to the whole
batch and return only the rows that need a decision. Nothing is written until commit, and the
import goes stale if any name in the target collection changes in between.
"""
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from .db import canonical
from .service import TrackerError, invalid, object_json, text
from .workflow import CANDIDATE_THRESHOLD, RECORD_SCAN_LIMIT, normalize, score_normalized

MAX_ROWS = 100
MAX_LINKS = 200
MAX_IMPORT_BYTES = 1_000_000
ROW_CANDIDATE_LIMIT = 5
MAX_COMPARISONS = 1_000_000  # rows x existing names; about three seconds of scoring
# 2*min(a,b)/(a+b) >= threshold  <=>  shorter/longer >= threshold/(2-threshold)
LENGTH_BAND = CANDIDATE_THRESHOLD / (2 - CANDIDATE_THRESHOLD)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ImportRow(Input):
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    data: dict[str, JsonValue] = Field(default_factory=dict)


class Endpoint(Input):
    """Exactly one of: a row of this import by its key, or an existing resolved record."""
    row: str | None = None
    record_id: str | None = None


class ImportLink(Input):
    source: Endpoint
    relationship_type: str = Field(min_length=1, max_length=100)
    target: Endpoint


class Decision(Input):
    action: str
    record_id: str | None = None
    clarification: str | None = Field(default=None, min_length=1, max_length=1000)


def parse(value, model, name, low, high):
    if not isinstance(value, list) or not low <= len(value) <= high:
        invalid(f'{name} must contain {low}–{high} items')
    try:
        return TypeAdapter(list[model]).validate_python(value)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        invalid(f"Invalid {name} item {error['loc'][0]}: {error['msg']}")


class Importer:
    def __init__(self, workflow):
        self.w = workflow
        self.t = workflow.t

    def _review(self, con, collection_id, rows):
        """Score every row title against every title and alias in the collection, and against
        the other rows. Same rules and threshold as resolve_record; archives are included."""
        scanned = con.execute("SELECT count(*) FROM records WHERE workspace_id=? AND collection_id=?",
                              (self.t.workspace, collection_id)).fetchone()[0]
        if scanned > RECORD_SCAN_LIMIT:
            raise TrackerError('INCOMPLETE_REVIEW', 'Collection is too large to review for duplicates; import is not allowed')
        names = [(normalize(row['name']), row['id'], row['title'], row['archived_at']) for row in con.execute(
            """SELECT r.id, r.title, r.archived_at, r.title AS name FROM records r
               WHERE r.workspace_id=? AND r.collection_id=?
               UNION ALL SELECT r.id, r.title, r.archived_at, a.alias FROM record_aliases a
               JOIN records r ON r.id=a.record_id AND r.workspace_id=a.workspace_id
               WHERE a.workspace_id=? AND r.collection_id=? AND a.alias NOT LIKE '@self:%'""",
            (self.t.workspace, collection_id, self.t.workspace, collection_id))]
        if len(rows) * len(names) > MAX_COMPARISONS:
            # No index can skip comparisons without risking a missed typo, so bound the work.
            invalid(f'This collection has {len(names)} names to check; import at most '
                    f'{max(1, MAX_COMPARISONS // len(names))} rows per call')
        titles = [normalize(row.title) for row in rows]
        report = []
        for index, row in enumerate(rows):
            best, title = {}, titles[index]
            # One matcher per row caches the row's character counts, so the shared-character
            # upper bound (symmetric, hence safe to compute this way round) rejects most names
            # cheaply. Survivors get the exact score in resolve_record's orientation.
            bound = SequenceMatcher(None, autojunk=False)
            bound.set_seq2(title)
            low, high = len(title) * LENGTH_BAND, len(title) / LENGTH_BAND
            for name, rid, shown, archived_at in names:
                if name != title and name not in title and title not in name:
                    if not low <= len(name) <= high:
                        continue
                    bound.set_seq1(name)
                    if bound.quick_ratio() < CANDIDATE_THRESHOLD:
                        continue
                score = score_normalized(title, name)
                if score and score > best.get(rid, {'similarity': 0})['similarity']:
                    best[rid] = {'id': rid, 'title': shown, 'archived_at': archived_at, 'similarity': round(score, 3)}
            candidates = sorted(best.values(), key=lambda c: (-c['similarity'], c['title'], c['id']))
            twins = [other.key for position, other in enumerate(rows)
                     if position != index and score_normalized(titles[index], titles[position])]
            status = 'possible_duplicate' if candidates else 'batch_duplicate' if twins else 'clean'
            report.append({'key': row.key, 'title': row.title, 'status': status,
                           'candidates': candidates[:ROW_CANDIDATE_LIMIT],
                           'candidates_truncated': len(candidates) > ROW_CANDIDATE_LIMIT,
                           'similar_rows_in_this_import': twins})
        return report

    def _check(self, con, payload):
        """Shape, references and identity evidence. Runs at prepare and again at commit."""
        collection_id = payload['collection_id']
        self.t._collection(con, collection_id)
        rows = parse(payload['rows'], ImportRow, 'rows', 1, MAX_ROWS)
        links = parse(payload['links'], ImportLink, 'links', 0, MAX_LINKS)
        if len(canonical(payload['rows']).encode()) > MAX_IMPORT_BYTES:
            invalid(f'rows exceed {MAX_IMPORT_BYTES} bytes in total; split the import')
        keys = [row.key for row in rows]
        if len(set(keys)) != len(keys):
            invalid('row keys must be unique within the import')
        for row in rows:
            text(row.title, 'title')
            object_json(row.data)
        for index, link in enumerate(links):
            text(link.relationship_type, 'relationship_type', 100)
            for end in (link.source, link.target):
                if (end.row is None) == (end.record_id is None):
                    invalid(f'link {index}: each endpoint needs exactly one of row or record_id')
                if end.row is not None and end.row not in keys:
                    invalid(f'link {index}: unknown row key {end.row!r}')
                if end.record_id is not None:
                    # Existing records need the same identity evidence as any other write.
                    self.w._selection(con, end.record_id, payload['review_ids'], payload['clarification'])
        return rows, links

    def _perform(self, con, payload, rows, links, decisions, action_id=None):
        ids, created, reused, skipped = {}, [], [], []
        for row in rows:
            decision = decisions.get(row.key)
            if decision and decision.action == 'skip':
                skipped.append(row.key)
            elif decision and decision.action == 'use_existing':
                ids[row.key] = decision.record_id
                reused.append({'key': row.key, 'record_id': decision.record_id})
            else:
                record = self.t.create_record(payload['collection_id'], row.title, row.data)
                ids[row.key] = record['id']
                created.append({'key': row.key, 'id': record['id'], 'title': record['title'], 'version': record['version']})
                if action_id is not None:
                    self.t._event(con, record['id'], 'decision', None, {
                        'operation': 'import_record', 'action_id': action_id, 'row_key': row.key,
                        'decision_reason': payload['decision_reason'],
                        'agent_reported_clarification': decision.clarification if decision else None,
                        'attribution_note': 'Reason and clarification are agent-reported, not authenticated user consent.'})
        made, dropped = [], []
        for index, link in enumerate(links):
            ends = [end.record_id if end.record_id is not None else ids.get(end.row) for end in (link.source, link.target)]
            if None in ends:
                dropped.append({'link_index': index, 'reason': 'an endpoint row was skipped'})
                continue
            result = self.t.link_records(ends[0], link.relationship_type, ends[1])
            made.append({'link_index': index, 'id': result['id'], 'source_id': ends[0],
                         'relationship_type': result['relationship_type'], 'target_id': ends[1]})
        summary = {'created': created, 'reused': reused, 'skipped': skipped, 'links': made, 'links_dropped': dropped}
        if action_id is not None:
            self.t._collection_event(con, payload['collection_id'], 'import', None, {
                'action_id': action_id, 'decision_reason': payload['decision_reason'],
                'created': len(created), 'reused': len(reused), 'skipped': len(skipped), 'links': len(made),
                'decisions': {key: decision.action for key, decision in decisions.items()},
                'attribution_note': 'Agent-reported import decisions'})
        return summary

    def prepare(self, collection_id, rows, links=None, review_ids=None, decision_reason=None, clarification=None):
        # The MCP layer passes typed models; store plain JSON so the ticket can replay exactly.
        payload = {'collection_id': text(collection_id, 'collection_id'),
                   'rows': [row.model_dump() for row in parse(rows, ImportRow, 'rows', 1, MAX_ROWS)],
                   'links': [link.model_dump() for link in parse([] if links is None else links,
                                                                 ImportLink, 'links', 0, MAX_LINKS)],
                   'review_ids': [] if review_ids is None else review_ids,
                   'decision_reason': text(decision_reason, 'decision_reason', 1000),
                   'clarification': None if clarification is None else text(clarification, 'clarification', 1000)}
        if not isinstance(payload['review_ids'], list) or len(payload['review_ids']) > 12 \
                or not all(isinstance(r, str) for r in payload['review_ids']):
            invalid('review_ids must contain at most 12 resolution IDs')
        with self.t.db.connect(write=True) as con:
            parsed_rows, parsed_links = self._check(con, payload)
            report = self._review(con, collection_id, parsed_rows)
            # Preview as if every row were created: validates sizes, link rules and foreign keys.
            con.execute('SAVEPOINT import_preview')
            try:
                preview = self._perform(con, payload, parsed_rows, parsed_links, {})
            finally:
                con.execute('ROLLBACK TO import_preview')
                con.execute('RELEASE import_preview')
            payload['needs_decision'] = {r['key']: {'candidates': [c['id'] for c in r['candidates']],
                                                    'twins': r['similar_rows_in_this_import']}
                                         for r in report if r['status'] != 'clean'}
            # Stale as soon as any name in the collection changes: a new record could be a duplicate.
            action_id = self.w._save(con, 'import_action', payload, [f'names:{collection_id}'])
            return {'action_id': action_id, 'rows': report,
                    'needs_decision': sorted(payload['needs_decision']),
                    'preview': {'records': len(preview['created']), 'links': len(preview['links'])},
                    'note': 'Nothing was written. Rows listed in needs_decision each require a decision at '
                            'commit_import: skip, use_existing (one of that row\'s candidates) or create_anyway '
                            'with an actual clarification. Similarity is spelling only, not proof of identity.'}

    def commit(self, action_id, decisions=None):
        decisions = {} if decisions is None else decisions
        if not isinstance(decisions, dict):
            invalid('decisions must be an object keyed by row key')
        try:
            parsed = TypeAdapter(dict[str, Decision]).validate_python(decisions)
        except ValidationError as exc:
            invalid('Invalid decision: ' + str(exc.errors(include_input=False)[0]['msg']))
        with self.t.db.connect(write=True) as con:
            payload, replay = self.w._load(con, action_id, 'import_action', replay=True)
            if replay is not None:
                return replay
            needed = payload['needs_decision']
            if set(parsed) != set(needed):
                raise TrackerError('NEEDS_CLARIFICATION', 'Decide every flagged row, and only flagged rows',
                                   missing=sorted(set(needed) - set(parsed)), unexpected=sorted(set(parsed) - set(needed)))
            for key, decision in parsed.items():
                if decision.action == 'use_existing':
                    if decision.record_id not in needed[key]['candidates']:
                        invalid(f'row {key!r}: use_existing must name one of the candidates returned for that row')
                elif decision.action == 'create_anyway':
                    # Rows that only resemble each other need no explanation once the others are
                    # skipped or mapped to existing records: one of them has to be the original.
                    only_twins = not needed[key]['candidates'] and all(
                        parsed[twin].action != 'create_anyway' for twin in needed[key]['twins'])
                    if not only_twins and (not decision.clarification or not decision.clarification.strip()):
                        raise TrackerError('NEEDS_CLARIFICATION', f'row {key!r}: create_anyway needs the actual reason this is a distinct record')
                elif decision.action != 'skip':
                    invalid(f'row {key!r}: action must be skip, use_existing or create_anyway')
            rows, links = self._check(con, payload)
            result = self._perform(con, payload, rows, links, parsed, action_id=action_id)
            con.execute('UPDATE workflow_tickets SET result_json=? WHERE id=?', (canonical(result), action_id))
            return result
