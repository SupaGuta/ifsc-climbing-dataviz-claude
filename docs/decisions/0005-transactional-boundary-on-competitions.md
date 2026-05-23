# 0005 — Transactional boundary on competition hydration

**Status:** Accepted
**Date:** 2026-05-22

## Context

Hydrating a competition means fetching its full ranking and replacing the
current `results` rows for that competition. The IFSC API doesn't expose
result diffs; it returns the whole ranking each time. The implementation
therefore deletes existing results for the competition, then inserts the new
ones:

```python
repo.delete_results_for_competition(comp_id)
for entry in data.get("ranking") or []:
    repo.upsert_athlete_skeleton(...)
    repo.upsert_result(competition_id=comp_id, athlete_id=..., rank=...)
repo.mark_fetched("competitions", comp_id)
```

With the streaming-write model (see [ADR 0002](0002-streaming-writes.md)),
every line above commits immediately. If a parse error or process kill lands
between the `delete` and the loop completing, the competition ends up with
*fewer* results than it should — and `mark_fetched` may or may not have
run, so the next refresh may or may not catch it.

This is the only place in the ingestion path where a multi-step operation
must land atomically. Everywhere else, per-row commit is correct: if
hydrating event 42 fails, event 43 should still be saved.

## Decision

Wrap each per-competition work-unit in `with repo.transaction():`:

```python
for fetched in client.stream_paths(items):
    comp_id = int(fetched.key)
    try:
        with repo.transaction():
            repo.delete_results_for_competition(comp_id)
            for entry in data.get("ranking") or []:
                ...
                repo.upsert_result(...)
            repo.mark_fetched("competitions", comp_id)
        ok += 1
    except Exception as exc:
        log.exception("Failed to parse %s: %s", fetched.path, exc)
        fail += 1
```

The `transaction()` context (`src/ifsc_data/db/repository.py:transaction`)
suppresses per-call commits while active and commits once on clean exit /
rolls back on exception. Nested transactions are flattened: only the
outermost commits.

This block is the **only** transactional boundary in the package. All other
fetchers are per-row.

## Consequences

**Positive**

- A failed competition rolls back cleanly: results stay as they were,
  `last_fetched_at` stays as it was, the row is still stale, and the next
  run retries. No state where a competition has *some* of the new results
  and none of the old.
- The `delete` + `re-insert` pattern is safe to interrupt at any point.
- The transaction is per-competition, so a parse error in competition 47
  doesn't affect competition 46 (already committed) or competition 48
  (not started). Granularity matches the unit of work that needs
  atomicity.

**Negative**

- Adds one more concept (`transaction()`) to reason about than a pure
  per-row-commit model would. We considered using per-row commits here too
  for uniformity, but the delete-then-reinsert semantics needs atomicity —
  the speed gain from batching 20–80 inserts under one transaction is a
  side effect, not the motivation. Mitigation: the repository docstring
  explains the contract, and this ADR exists.
- `upsert_athlete_skeleton` inside the transaction means a newly-seen
  athlete's skeleton row is also rolled back on competition failure.
  Acceptable: if the competition failed, we don't need its athletes yet.

## Alternatives considered

- **Skip the delete; rely on `INSERT OR REPLACE` UNIQUE handling** — works
  for unchanged rankings, but if an athlete drops out (rare but possible
  after a DSQ correction), they'd remain in the results forever. The
  delete is the only clean way to handle removals. Rejected.
- **Soft delete (set a `deleted_at` column)** — useful for audit but
  adds a column to a table we want to keep narrow. Not worth it.
- **Transaction at the batch level (whole hydrate call)** — a single
  failed competition would roll back the whole batch. Loses the per-row
  durability we got from streaming writes. Rejected.
