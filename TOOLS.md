# Tools at a glance

17 working tools in 4 groups, plus 6 disabled legacy names.

```
  USER SAYS SOMETHING
          │
          ▼
  ┌───────────────┐   "what / where / who / how much?"    ┌──────────────┐
  │   1. FIND     │ ────────────────────────────────────▶ │   ANSWER     │
  │  (read only)  │                                       └──────────────┘
  └───────┬───────┘
          │ "add / change / link / remove"
          ▼
  ┌───────────────┐      ┌───────────────┐      ┌───────────────┐
  │  2. IDENTIFY  │ ───▶ │  3. PREPARE   │ ───▶ │   4. COMMIT   │
  │ which record? │      │   dry run     │      │  real write   │
  │ which type?   │      │  + preview    │      │   + audit     │
  └───────────────┘      └───────────────┘      └───────────────┘
     gives a ticket        needs the ticket       needs the action_id
```

Reading is free. Writing always goes 2 → 3 → 4.

## 1. Find (read only, no ticket)

| Tool | Use it when the user asks | What it adds |
| --- | --- | --- |
| `list_collections` | "what am I tracking?" | The list of record types and how many of each |
| `search_records` | "where did I write about postgres?" "offers above 40" | Finds records by any word inside them, by field conditions, by link, sorted |
| `get_record_context` | "tell me about Rohan" | One record in full, its direct links, recent changes |
| `traverse` | "everything about the ledger job" | Follows links several steps in one call: person → application → interview → feedback |
| `aggregate` | "what has hiring cost us?" "spend per recruiter" | Totals, averages and counts done by the server, and it reports what it had to skip |
| `get_record_history` | "what changed on this and why?" | Every change to a record with before, after and the reason |
| `get_collection_history` | "when did we start tracking this?" | Every change to a record type |
| `batch_read` | several of the above at once | Up to 10 reads in one call, from one consistent snapshot |

## 2. Identify (gives the ticket a write needs)

| Tool | Use it when | What it adds |
| --- | --- | --- |
| `resolve_record` | Before changing, linking or creating any record | Finds who a name means. Handles typos, aliases, "me", namesakes and archives. Says `resolved`, `needs_clarification`, `no_match` or `incomplete` |
| `batch_resolve_records` | Several names in one message | Same, for up to 10 names in one call |
| `discover_collections` | Before creating or changing a record type | Shows every existing type with purpose and samples, so a twin like "candidates" vs "applications" is not created |

## 3 and 4. Prepare, then commit

| Pair | Use it for | What it adds |
| --- | --- | --- |
| `prepare_write` → `commit_write` | One change: create, update, rename, archive, link, unlink, alias, "this is me", new record type | A dry run with a preview. Refused if the name was ambiguous and nobody clarified |
| `prepare_batch_write` → `commit_batch_write` | Several changes to existing records from one message | Up to 10 changes that all succeed or all roll back |
| `prepare_import` → `commit_import` | A pasted list or CSV of new records | Up to 100 records in two calls. The server checks every row for duplicates and asks only about the doubtful ones |

Every commit is atomic, audited with the reason, and safe to retry with the same `action_id`.

## Which one? Quick picker

| The user says | Tools, in order |
| --- | --- |
| "who is handling the Stripe role?" | `search_records` → `get_record_context` |
| "show me everything connected to Anita" | `resolve_record` → `traverse` |
| "how much budget is left?" | `aggregate` |
| "rohan moved to onsite" | `resolve_record` → `prepare_write` → `commit_write` |
| "three updates from today…" | `batch_resolve_records` → `prepare_batch_write` → `commit_batch_write` |
| "add these 30 candidates" | `prepare_import` → `commit_import` |
| "start tracking expenses" | `discover_collections` → `prepare_write` → `commit_write` |
| "meera referred me to razorpay, not stripe" | `resolve_record` ×2 → `get_record_context` → `prepare_batch_write` (unlink + link) → `commit_batch_write` |

## What stops a bad write

| Situation | Result |
| --- | --- |
| Typo, or two records with the same name | `NEEDS_CLARIFICATION` until the agent gives real evidence |
| No lookup done first | `REVIEW_REQUIRED` |
| Something relevant changed since the lookup | `STALE_REVIEW`, look up again |
| Someone else edited the record | `VERSION_CONFLICT` |
| Too many matches to review | `incomplete`, no write allowed |

## Disabled legacy names

`create_collection`, `create_record`, `update_record`, `archive_record`, `link_records`, `unlink_records`
always return `REVIEW_REQUIRED`. They exist only so old clients get a clear message.
