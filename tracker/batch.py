"""Bounded batch calls over existing operations; no arbitrary dispatch or SQL."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from .db import canonical
from .service import TrackerError, invalid, object_json

MAX_RESPONSE_BYTES = 2_000_000


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SearchRead(Input):
    operation: Literal['search_records']
    collection_id: str | None = None
    query: str | None = None
    filters: dict[str, JsonValue] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None
    text: str | None = None
    include_context: bool = False
    relationship_limit: int = Field(default=20, ge=1, le=100)
    event_limit: int = Field(default=0, ge=0, le=100)


class ContextRead(Input):
    operation: Literal['get_record_context']
    record_id: str
    relationship_limit: int = Field(default=20, ge=1, le=100)
    event_limit: int = Field(default=0, ge=0, le=100)


class CollectionsRead(Input):
    operation: Literal['list_collections']


ReadRequest = Annotated[SearchRead | ContextRead | CollectionsRead, Field(discriminator='operation')]


class ResolveRequest(Input):
    query: str = Field(min_length=1, max_length=300)
    collection_id: str | None = None
    context_record_ids: list[str] = Field(default_factory=list, max_length=10)


class WriteRequest(Input):
    operation: Literal['update_record', 'archive_record', 'rename_record',
                       'add_record_alias', 'remove_record_alias', 'set_self',
                       'link_records', 'unlink_records']
    arguments: dict[str, JsonValue]
    review_ids: list[str] = Field(min_length=1, max_length=12)
    decision_reason: str = Field(min_length=1, max_length=1000)
    clarification: str | None = Field(default=None, min_length=1, max_length=1000)


def parse_items(items, item_type):
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        invalid('A batch must contain 1–10 items')
    try:
        return TypeAdapter(list[item_type]).validate_python(items)
    except ValidationError as exc:
        invalid('Invalid batch item: ' + str(exc.errors(include_input=False)[0]['msg']))


def bounded_response(result):
    if len(canonical(result).encode()) > MAX_RESPONSE_BYTES:
        raise TrackerError('RESPONSE_TOO_LARGE', 'Batch response exceeds 2 MB; reduce page, context, or batch sizes. Nothing was committed.')
    return result


def item_error(exc, index):
    return TrackerError(exc.code, str(exc), **{**exc.details, 'item_index': index})


class Batch:
    def __init__(self, workflow):
        self.w = workflow
        self.t = workflow.t

    def read(self, requests):
        requests = parse_items(requests, ReadRequest)
        if sum(r.limit if isinstance(r, SearchRead) else 1 for r in requests) > 100:
            invalid('Batch search/context record budget is 100; reduce page limits')
        results = []
        with self.t.db.connect():
            for index, request in enumerate(requests):
                try:
                    if isinstance(request, SearchRead):
                        result = self.t.search_records(request.collection_id, request.query,
                                                       request.filters, request.limit, request.cursor,
                                                       request.text)
                        if request.include_context:
                            result['contexts'] = [self.t.get_record_context(
                                r['id'], request.relationship_limit, request.event_limit)
                                for r in result['records']]
                    elif isinstance(request, ContextRead):
                        result = self.t.get_record_context(request.record_id, request.relationship_limit, request.event_limit)
                    else:
                        result = self.t.list_collections()
                    results.append({'item_index': index, 'operation': request.operation, 'result': result})
                except TrackerError as exc:
                    raise item_error(exc, index) from exc
            return bounded_response({'results': results, 'snapshot': 'One consistent read transaction'})

    def resolve(self, requests):
        requests = parse_items(requests, ResolveRequest)
        with self.t.db.connect(write=True) as con:
            fingerprint = self.w._fingerprint(con)
            results = []
            for index, request in enumerate(requests):
                try:
                    result = self.w.resolve_record(**request.model_dump(), _snapshot=fingerprint)
                    results.append({'item_index': index, 'result': result})
                except TrackerError as exc:
                    raise item_error(exc, index) from exc
            return bounded_response({'results': results})

    def _validate(self, con, actions, fingerprint):
        # ALL reviews are checked against the same pre-write snapshot, before any
        # mutation. Never ignore stale checks merely because earlier items wrote.
        for index, action in enumerate(actions):
            try:
                object_json(action.arguments)
                if not action.decision_reason.strip() or (action.clarification is not None and not action.clarification.strip()):
                    invalid('Decision reason and any clarification must be nonblank')
                self.w._validate(con, action.operation, action.arguments, action.review_ids,
                                 action.clarification, fingerprint=fingerprint)
            except TrackerError as exc:
                raise item_error(exc, index) from exc

    def _perform(self, con, actions, action_id=None):
        results = []
        for index, action in enumerate(actions):
            try:
                result = self.w._perform(con, action.operation, action.arguments)
                if action_id is not None:
                    payload = action.model_dump(exclude={'clarification'})
                    payload['agent_reported_clarification'] = action.clarification
                    payload['batch_item_index'] = index
                    self.w._audit_decision(con, payload, result, action_id)
                results.append({'item_index': index, 'operation': action.operation, 'result': result})
            except TrackerError as exc:
                raise item_error(exc, index) from exc
        return bounded_response({'results': results, 'atomic': True})

    def prepare(self, actions):
        actions = parse_items(actions, WriteRequest)
        with self.t.db.connect(write=True) as con:
            fingerprint = self.w._fingerprint(con)
            self._validate(con, actions, fingerprint)
            con.execute('SAVEPOINT batch_preview')
            try:
                preview = self._perform(con, actions)
            finally:
                con.execute('ROLLBACK TO batch_preview')
                con.execute('RELEASE batch_preview')
            payload = {'actions': [a.model_dump() for a in actions]}
            action_id = self.w._save(con, 'batch_action', payload, fingerprint=fingerprint)
            return {'action_id': action_id, 'preview': preview,
                    'note': 'No mutations committed. Use commit_batch_write. Items run in order; all succeed or all roll back.'}

    def commit(self, action_id):
        with self.t.db.connect(write=True) as con:
            fingerprint = self.w._fingerprint(con)
            payload, replay = self.w._load(con, action_id, 'batch_action', replay=True, fingerprint=fingerprint)
            if replay is not None:
                return replay
            actions = parse_items(payload['actions'], WriteRequest)
            self._validate(con, actions, fingerprint)
            result = self._perform(con, actions, action_id=action_id)
            con.execute('UPDATE workflow_tickets SET result_json=? WHERE id=?', (canonical(result), action_id))
            return result
