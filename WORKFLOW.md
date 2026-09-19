# How the Workflow class works

`Workflow` is not a tool. It is the rule-keeper that sits between the tools and the data.

## Where it sits

```
  Agent (Claude)
      │  calls a tool by name
      ▼
  server.py        thin wrappers, no logic: each tool is one line that calls a method below
      │
      ├── reads ───────────────────────────────▶ service.py / analytics.py ──▶ SQLite
      │   (search, context, traverse, aggregate, history)       no tickets involved
      │
      └── anything that writes
              │
              ▼
          workflow.py   ◀── batch.py and importer.py reuse its ticket functions
              │  checks tickets, then calls
              ▼
          service.py ──▶ SQLite
```

Only 4 tools are `Workflow` methods directly. The rest of `Workflow` is private.

| The agent sees this tool | It runs this method | Public? |
| --- | --- | --- |
| `discover_collections` | `Workflow.discover_collections` | yes |
| `resolve_record` | `Workflow.resolve_record` | yes |
| `prepare_write` | `Workflow.prepare_write` | yes |
| `commit_write` | `Workflow.commit_write` | yes |
| (never) | `_save`, `_load`, `_revisions`, `_selection`, `_validate`, `_perform`, `_audit_decision` | no, internal |

## One table holds all three IDs

All three IDs are rows in `workflow_tickets` ([db.py:54](tracker/db.py#L54)). The `kind` column tells them apart.

```
workflow_tickets
┌──────────┬──────────────┬───────────────────────┬──────────────────┬────────────┬─────────────┐
│ id       │ kind         │ payload_json          │ fingerprint      │ expires_at │ result_json │
├──────────┼──────────────┼───────────────────────┼──────────────────┼────────────┼─────────────┤
│ d-111    │ discovery    │ catalog the agent saw │ {"catalog": 7}   │ +15 min    │             │
│ r-222    │ resolution   │ candidates it saw     │ {"names:c1": 12, │ +15 min    │             │
│          │              │                       │  "links:p9": 3}  │            │             │
│ a-333    │ action       │ exact write + r-222   │ {}               │ +15 min    │ (filled on  │
│          │              │                       │                  │            │   commit)   │
└──────────┴──────────────┴───────────────────────┴──────────────────┴────────────┴─────────────┘
   the agent only ever holds the id column; everything else stays on the server
```

| ID | Means | Made by | Used by | Goes stale when |
| --- | --- | --- | --- | --- |
| `discovery_id` | "I reviewed the existing record types" | `discover_collections` | `prepare_write` for collection changes | any collection or collection alias changes |
| `resolution_id` | "I looked this name up and saw these candidates" | `resolve_record` | `prepare_write` for record changes | a record in that scope is created, renamed, aliased or archived, or a candidate's links change |
| `action_id` | "This exact write was previewed" | `prepare_write` | `commit_write` | any ticket it cites goes stale |

All expire after 15 minutes. All belong to one workspace and one agent identity.

## One write, step by step

User says: *"rohan moved to onsite"*

```
 ① resolve_record("rohan")
       Workflow: score every name → 2 candidates → status needs_clarification
       _save(kind=resolution)                                   ──▶ returns r-222
                       │
                       │  agent asks the user, gets "Rohan Mehta"
                       ▼
 ② prepare_write(update_record, {record_id, changes, expected_version},
                 review_ids=[r-222], reason, clarification="user said Rohan Mehta")
       _validate  → _load(r-222): mine? not expired? counters unchanged?
                  → _selection: is this record among r-222's candidates?
                                ambiguous, so is there a clarification?
       _perform   → runs the real update inside a SAVEPOINT, then rolls it back
       _save(kind=action)                                       ──▶ returns a-333 + preview
                       │
                       ▼
 ③ commit_write(a-333)
       _load(a-333, replay=True) → already committed? return the saved result, stop
       _validate  → same checks again, now
       _perform   → the real update
       _audit_decision → reason + evidence into the record's history
       result_json = result                                     ──▶ returns the updated record
```

Steps ② and ③ each run inside one `BEGIN IMMEDIATE` transaction, so the check and the write cannot be separated by another writer.

## What each private method does

| Method | One line | Read it at |
| --- | --- | --- |
| `_save` | Creates any ticket, with a snapshot of only the counters it depends on | [workflow.py:79](tracker/workflow.py#L79) |
| `_load` | Reads a ticket. Rejects if not yours, expired, or counters changed. Returns the saved result on replay | [workflow.py:92](tracker/workflow.py#L92) |
| `_revisions` | Reads the current counters from the `revisions` table | [workflow.py:70](tracker/workflow.py#L70) |
| `_selection` | Decides whether a resolution ticket authorizes one specific record | [workflow.py:227](tracker/workflow.py#L227) |
| `_validate` | Per-operation rules: required arguments, which ticket kind, when clarification is mandatory | [workflow.py:263](tracker/workflow.py#L263) |
| `_perform` | Runs the write by calling `service.py`. Used for both preview and commit | [workflow.py:348](tracker/workflow.py#L348) |
| `_audit_decision` | Stores reason, clarification and reviewed candidates in history | [workflow.py:425](tracker/workflow.py#L425) |

## The counters behind "goes stale"

Triggers in [db.py](tracker/db.py) bump these inside the writing transaction. A ticket compares its snapshot to the current values.

| Counter | Bumped when |
| --- | --- |
| `names:<collection>` and `names:*` | record created, renamed, aliased, archived |
| `links:<record>` | a link added or removed at that record |
| `catalog` | a collection or collection alias changes |

Plain data updates bump nothing. `expected_version` guards those.

## Who reuses Workflow

| Module | Tools it serves | What it borrows |
| --- | --- | --- |
| [batch.py](tracker/batch.py) | `batch_resolve_records`, `prepare_batch_write`, `commit_batch_write` | `resolve_record`, `_validate`, `_perform`, `_audit_decision`, `_save`, `_load` |
| [importer.py](tracker/importer.py) | `prepare_import`, `commit_import` | `_selection`, `_save`, `_load`, and the name scorer |

Their `action_id`s are the same kind of row with `kind` set to `batch_action` or `import_action`.

## What the errors mean

| Error | Raised in | Meaning |
| --- | --- | --- |
| `REVIEW_REQUIRED` | `_selection`, `_validate` | No ticket covers this record, or the wrong kind of ticket |
| `NEEDS_CLARIFICATION` | `_selection`, `_validate` | The ticket was ambiguous and no clarification was given |
| `STALE_REVIEW` | `_load` | Expired, or a counter it depends on changed |
| `NOT_FOUND` | `_load` | The ticket belongs to another workspace or agent |
| `VERSION_CONFLICT` | `service.py`, via `_perform` | Someone edited the record since the agent read it |
| `INCOMPLETE_REVIEW` | `_validate` | The lookup could not cover everything, so it authorizes nothing |
