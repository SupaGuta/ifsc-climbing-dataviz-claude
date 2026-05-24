# 0006 — Scope `pull-new` to ongoing containers only

**Status:** Accepted
**Date:** 2026-05-24

## Context

`pull-new` is the everyday "catch new content" command. Until this change,
it force-refreshed every container row on every run — all ~38 seasons,
~450 season_leagues, ~1,401 events, ~5,825 competitions, regardless of
when the season ended or the event finished. That's ~7,700 HTTP calls per
run, ~3-5 minutes wall-clock.

The vast majority of those re-fetches produced zero new information.
Re-fetching season 2005 to look for new leagues that will never appear,
or re-fetching a 2018 event's competition rankings that have been stable
for years, is pure overhead.

[ADR 0004](0004-incremental-hydration-with-staleness.md) introduced
`last_fetched_at` staleness as the model for "should we re-fetch this row?"
That model is correct for athletes (whose profile data does change over
time) but mismatched for structural containers. The World Climbing publishes events
ahead of time, not retroactively — a season is structurally frozen once
its calendar year is past.

## Decision

`pull-new` scopes the container re-fetch to **ongoing** rows only, where
"ongoing" is a deterministic predicate per table:

| Entity | Ongoing iff |
|---|---|
| `seasons` | `year IS NULL OR year >= current_year` |
| `season_leagues` | parent `seasons.year` matches above (JOIN) |
| `events` | `date_end IS NULL OR date_end >= today - 15 days` |
| `competitions` | parent `events.date_end` matches above (JOIN) |
| `athletes` | unchanged — `last_fetched_at IS NULL` only |

The NULL clauses are essential: skeletons created mid-run (e.g. a new
event discovered via re-fetching a season_league) have NULL year /
NULL date_end and need to flow through the next phase in the same
invocation.

The **15-day grace period** past `events.date_end` catches late result
corrections (DSQs posted in the days after an event ends) without
re-fetching ancient containers. Configurable via `WCL_GRACE_DAYS` env
var or `--grace-days N` flag.

Implementation: four new `Repository.find_ongoing_*` methods; each
container fetcher's `hydrate()` gets an optional `rows=` parameter so
`pull_new` can pass a custom work list instead of going through
`find_stale`. `refresh` and `hydrate <entity>` are unchanged.

## Consequences

**Positive**

- Wall-clock: ~3-5 min → ~30-60s on a steady-state warehouse. Verified
  at 33.5s on 2026-05-24 against a 15k-athlete warehouse.
- HTTP volume: ~7,700 calls → ~350 calls per run. Friendlier to the
  IFSC API.
- The expensive `find_ongoing_*` queries are O(rows-in-table) but the
  tables are small (≤~6k rows); milliseconds.
- The optimization is invisible to users — same `pull-new` command,
  same summary table, same row counts after. Just faster.
- Backward-compatible: each fetcher still accepts `stale_days=` for
  the `refresh` / `hydrate` paths.

**Negative**

- A retroactive World Climbing edit to an ended container (e.g. an event added to
  a 2020 season, or a competition added to a 2024 event whose date_end
  is more than 15 days past) won't be picked up by `pull-new`. This
  basically never happens in practice. When it does, `refresh
  --stale-days 0` is the escape hatch and still catches everything.
- Late result corrections (DSQs etc.) more than 15 days after the
  event ends won't flow in via `pull-new`. The 15-day grace covers the
  realistic window; longer corrections need `refresh`.
- The fetchers now have two work-list modes (`stale_days=` and `rows=`).
  Adds a small bit of API surface; mitigated by the docstring on each
  `hydrate()`.

## Alternatives considered

- **Strict frozen (no grace period)** — simplest, but misses
  same-week DSQ corrections. Rejected.
- **Longer grace (30 days)** — gives diminishing returns; 15 covers
  the realistic correction window.
- **Smarter "ended season" detection** (e.g. "ended iff all events
  date_end < today") — more accurate but requires a more complex JOIN
  and saves ~1 row in the seasons phase. Not worth the complexity.
- **Keep `pull_new` as it was; add a separate "smart-pull" command** —
  splitting the surface area adds discovery cost for users without
  benefit. The old `pull_new` had no defenders; `refresh --stale-days
  0` already covers the "force everything" need.
