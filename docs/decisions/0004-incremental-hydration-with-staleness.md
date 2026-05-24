# 0004 — Incremental hydration with `last_fetched_at` staleness

**Status:** Accepted
**Date:** 2026-05-22

## Context

The warehouse holds ~150k rows across nine tables. Most rows don't change
day-to-day: athlete birthdays, historical competition rankings, and
2017-season events are immutable. Re-fetching everything on every run is
wasteful (~30 minutes, ~150k API calls).

> **Note (2026-05-24):** Post-ADR 0007 the warehouse is now ~1.1M rows
> across 15 tables and a full re-fetch is closer to 45-90 minutes. The
> staleness model below scales without change — it operates per-row
> regardless of the row count.


But *some* rows change: the current season gets new events, new events get
new competitions, new competitions discover new athletes. We need a way to
say "refresh only what's stale or new" without hand-curating a refresh
list.

## Decision

Every hydratable table carries a `last_fetched_at TEXT` column (ISO-8601 UTC
with `Z` suffix, e.g. `"2026-05-22T18:50:30Z"`). The column is:

- **NULL** for skeleton rows created by discovery (parent didn't have the
  child's profile data, just its ID).
- **Set to `utcnow()`** by `repo.mark_fetched(table, row_id)` after a
  successful parse + write.

`repo.find_stale(table, *, stale_days)` returns rows matching
`last_fetched_at IS NULL OR last_fetched_at < cutoff`, where `cutoff = now -
stale_days`. The TEXT format is lexicographically sortable, so the SQL
comparison works without parsing.

This single column drives the three CLI modes:

| Mode             | `stale_days` for athletes | What runs                                |
|------------------|---------------------------|------------------------------------------|
| `refresh`        | 30 (configurable)         | Anything stale or NULL across all tables |
| `refresh --stale-days 0` | 0                 | Everything                               |
| `pull-new`       | 365_000                   | NULL only — i.e. just-discovered rows    |
| `hydrate <e>`    | 30 (configurable)         | One table only                           |

## Consequences

**Positive**

- Re-running `pull-new` daily costs minutes, not 30 of them, because
  unchanged athlete profiles are skipped.
- Killed runs resume cleanly: the in-flight row's `last_fetched_at` is
  still NULL, so the next run picks it up.
- A new contributor doesn't need to know which tables update frequently
  — the staleness model handles it uniformly.
- "Force re-fetch everything" is one flag (`--stale-days 0`) rather than
  a separate command path.

**Negative**

- The model can't express "this row is known not to change" without an
  extra column. Today, results never change (they're rewritten as part
  of the parent competition's hydration) and we model that by having
  `results` carry no `last_fetched_at` at all and depend on the parent's
  staleness instead. That's adequate but slightly opaque.
- Time-based staleness is a proxy for change. A daily-cadence contributor
  may re-fetch a row that didn't change. Acceptable cost for the
  simplicity.
- The `stale_days=365_000` trick that `pull_new` uses to mean "NULL only"
  is clever-not-clear. A dedicated `find_unhydrated(table)` method would
  read better. We accepted the clever path because the staleness API is
  uniform across all phases and the trick keeps `pull_new` from needing
  its own per-fetcher hook. The inline comment in `refresh.py` documents
  it.

## Alternatives considered

- **Change-data-capture via ETags or Last-Modified headers** — the World
  Climbing API doesn't reliably set these. Rejected.
- **Polling a `/changes` endpoint** — doesn't exist on the World Climbing API.
- **Full refresh every run, accept the 30 minutes** — what the predecessor
  did. The slow turnaround discouraged frequent ingestion, which meant
  the warehouse drifted out of date for weeks at a time. Rejected.
- **Per-table refresh cadence config** — overengineering. The single
  `--stale-days` knob plus `pull-new` covers every observed use case.
