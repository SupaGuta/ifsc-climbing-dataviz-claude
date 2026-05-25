# Ingestion pipeline

`wcl_data` exposes three "modes" of ingestion at the CLI: `refresh`,
`pull-new`, and `hydrate`. All three are thin compositions of the same two
primitives — **discover** and **hydrate** — applied across the entity graph
defined in `src/wcl_data/db/repository.py`:

```python
HYDRATABLE_TABLES = ("seasons", "season_leagues", "events", "competitions", "athletes")
```

`refresh.ENTITIES` aliases `HYDRATABLE_TABLES`; both names point to the
same tuple object. The canonical edit site is `HYDRATABLE_TABLES` —
adding a new entity requires also extending `_FETCHER_MODULES` in
`refresh.py` (an `assert` at module-load enforces the pairing).

This document explains those two primitives and how the three CLI modes combine
them.

## Discover vs hydrate

**Discover** inserts skeleton rows (PK + `ifsc_id` only, `last_fetched_at`
NULL) so the next phase has something to hydrate. There are two flavors:

1. **Probe-based discovery** — used only for `seasons`, which has no parent
   endpoint. `seasons.discover` reads `MAX(ifsc_id)` from the DB and fetches
   the next `lookahead` IDs (default 5) past it. On an empty DB it bootstraps
   by probing IDs 0–49 (`range(0, INITIAL_PROBE_RANGE)` with
   `INITIAL_PROBE_RANGE = 50`). 4xx responses are silently dropped (see
   [ADR 0003](../decisions/0003-selective-4xx-skip-retry.md)) so non-existent
   IDs don't burn retry budget.
2. **Cascade discovery** — every other entity is discovered as a side effect
   of hydrating its parent. Hydrating a season inserts its `season_leagues`
   and `events` skeletons; hydrating an event inserts its `competitions`
   skeletons; hydrating a competition inserts its `athletes` skeletons. The
   skeleton's NULL `last_fetched_at` makes it eligible for the next phase.

**Hydrate** fills in a row's profile fields and stamps `last_fetched_at`. Each
fetcher's `hydrate(repo, client, *, stale_days, limit)` follows the same
shape:

```python
stale = repo.find_stale(table, stale_days=stale_days)
for fetched in client.stream(endpoint, [r["ifsc_id"] for r in stale]):
    parse(fetched.data) → repo.update_<entity>(...) → repo.mark_fetched(...)
```

`find_stale` returns rows where `last_fetched_at IS NULL OR last_fetched_at <
cutoff`. NULL → never hydrated. `cutoff` → older than `stale_days` ago. With
`stale_days=0`, the cutoff is "now," so every row matches. With
`stale_days=365_000` only NULL rows match. These three settings are the basis
of the three CLI modes.

## The three CLI modes

### `refresh` — standard cadence

```bash
python -m wcl_data refresh                  # uses WCL_STALE_DAYS, default 30
```

Calls `refresh_all(repo, client, stale_days=N)`. Walks all five entities in
order, hydrating anything stale (>N days) or never-hydrated. Athlete profiles
get re-fetched too — that's the slow part.

**Use it for:** periodic maintenance on the 30-day cadence, or one-shot
`--stale-days 0` to nuke everything (~45-90 min — the per-round tables
from ADR 0007 are the bulk of the work).

**Limitation:** `refresh` won't re-discover children of recently-fetched
parents. If you ran `refresh` yesterday and a new event was added to a season
hydrated yesterday, that event won't appear for another 29 days. That's the
gap `pull-new` fills.

### `pull-new` — catch new content cheaply

```bash
python -m wcl_data pull-new                 # ~30-60s on a steady-state warehouse
```

Calls `pull_new(repo, client)`. Runs `seasons.discover` first to probe for
any new seasons past the highest known `ifsc_id`, then re-fetches **only
ongoing containers** (current-year seasons → events within 15 days of
`date_end`, plus their descendants), then hydrates athletes with
`stale_days=365_000` — effectively "NULL only" — so only brand-new athlete
skeletons get filled in.

The "ongoing" predicate is deterministic — see
[ADR 0006](../decisions/0006-ongoing-only-pull-new.md) for the full table
and rationale. In short: ended seasons never gain new leagues/events and
ended events never gain new competitions, so re-fetching them is pure
overhead. The 15-day grace period (configurable via `WCL_GRACE_DAYS` /
`--grace-days`) catches late result corrections without re-fetching
ancient data.

**Why this exists:** `refresh --stale-days 0` would also catch new content
but takes ~45-90 min because it re-fetches every athlete profile AND every
historical container (including all per-round data from ADR 0007). Athlete profile fields (`height`, `birthday`, …)
almost never change, and historical containers literally cannot change
structurally, so `pull-new` skips both kinds of waste.

The athletes trick: the `stale_days=365_000` argument in `refresh.py` —
`find_stale`'s SQL is `last_fetched_at IS NULL OR last_fetched_at <
cutoff`, and with a cutoff 1000 years in the past, the cutoff branch
matches nothing, leaving only the NULL branch. That NULL set is *exactly*
the athletes just discovered during the competitions phase.

The ongoing-containers trick: `pull_new` calls
`repo.find_ongoing_seasons` / `find_ongoing_season_leagues` /
`find_ongoing_events` / `find_ongoing_competitions` and passes the
resulting row lists into each fetcher's `hydrate(rows=...)` parameter —
bypassing the staleness model entirely for these phases.

### `hydrate <entity>` — surgical

```bash
python -m wcl_data hydrate athletes
python -m wcl_data hydrate events --stale-days 0
```

Calls `hydrate_entity(repo, client, entity, stale_days=N)`. Runs just one
phase. Same staleness semantics as `refresh` (default 30 days, `--stale-days
0` to force).

**Note:** `hydrate <entity>` only refreshes rows that already exist for that
entity. New discovery happens by hydrating the *parent*. The one exception is
`hydrate seasons`, which also runs `seasons.discover` first (because seasons
have no parent).

## Order matters

The `HYDRATABLE_TABLES` order isn't just an enumeration — it's the
topological dependency order. `season_leagues` need seasons to exist;
`events` are discovered via both seasons and season_leagues; `competitions`
need events; results+athletes need competitions. Running the phases out of
order would either skip new children or leave dangling FKs.

The orchestrator hard-codes the order in `refresh.py` (`refresh_all` and
`pull_new` enumerate the phases explicitly). Per-entity fetchers never
call each other directly — they only read from / write to the DB.

## Where to add a new entity

If the World Climbing API ever exposes a new endpoint (e.g. `/judges/{id}`), the pattern
is:

1. Add a table + index to `src/wcl_data/db/schema.py`.
2. Add an `upsert_<entity>_skeleton` and `update_<entity>` to
   `src/wcl_data/db/repository.py`.
3. Add `src/wcl_data/fetchers/<entity>.py` with the same `hydrate(repo,
   client, *, stale_days, limit)` signature as the existing ones.
4. Add the entity name to `HYDRATABLE_TABLES` in `repository.py` (the
   canonical tuple; `refresh.ENTITIES` aliases it), add the new fetcher
   module to `_FETCHER_MODULES` in `refresh.py` (a module-load `assert`
   enforces the pairing), and wire it into `refresh_all` and `pull_new`.
   `hydrate_entity` dispatches via `_FETCHER_MODULES` automatically.
5. If the entity has a parent, modify the parent's fetcher to insert
   skeletons (cascade discovery). If not, add a `discover()` probe like
   `seasons.discover`.
6. Add fixtures under `tests/fixtures/` and a test under
   `tests/test_fetchers/`.

See [contributing.md](../contributing.md) for the full add-a-fetcher walkthrough
using `athletes.py` as the canonical example.
