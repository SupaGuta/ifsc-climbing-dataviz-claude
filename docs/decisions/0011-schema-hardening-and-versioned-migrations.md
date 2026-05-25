# 0011 — Schema hardening + versioned forward migrations

**Status:** Accepted
**Date:** 2026-05-25

## Context

The v0.1 warehouse encoded its schema invariants in Python (the upsert
helpers' parameter lists) rather than in the DDL. Examples:

- `events.season_id` is set by every production path that creates an
  event, but the column was nullable — a future regression that created
  an event without a season would have landed silently.
- `ascents.top` is a 0/1 flag in the ingest code, but the column was a
  free INTEGER — a malformed upstream payload with `top=2` would have
  passed through.
- `events.date_start` is an ISO date string from the API, but no GLOB
  check pinned the format — a future upstream that started serving
  `"06/15/2024"` would have corrupted downstream date arithmetic.
- `cup_rankings`'s inline `UNIQUE (athlete_id, cup_ifsc_id, d_cat_id)`
  treated each NULL `d_cat_id` as distinct under SQLite default UNIQUE
  semantics, so an athlete with two un-classified cup rows accumulated
  duplicates. Worse, the upsert used `INSERT OR REPLACE`, which churned
  the autoincrement `id` on every re-hydration — invalidating any
  out-of-band consumer that had cached `cup_rankings.id`.
- `apply_schema` re-ran every ALTER on every open, gated only by a
  "column exists?" check, with no recorded `schema_version` history. A
  crash mid-migration left the DB in an ambiguous state; there was no
  way to know *which* migrations had already run.

This was acceptable for a single-developer scratch warehouse, but the
imminent ML downstream means schema invariants need to hold against
unexpected upstream changes — and the operator needs to be able to look
at a DB and know what version it's at.

## Decision

Five coordinated schema changes, plus a migration framework rewrite.
The combined target version is `CURRENT_VERSION = 6`.

### 1. Version-gated forward migrations + idempotent safety net

`apply_schema` now reads `schema_version`, runs each
`_migrate_vN_to_vN+1` step guarded by `current < N+1`, and records the
new version in its own commit after each step succeeds. A crash between
steps leaves the warehouse at a coherent prior version (still openable,
just under-migrated), rather than a half-migrated state ambiguously
recorded as v6.

A fresh DB skips the migration walk entirely: `apply_schema` detects
"no data tables yet, version=0" and applies the final DDL + v6-only
artifacts in one go. Pre-versioning DBs (created before
`schema_version` existed) fall through to the migration walk.

`_migrate_pre_v1_to_v5` lumps everything below v5 into a single
idempotent step — we never captured snapshots for v1–v4 individually,
so reconstructing them would be archaeology, not safety.

Independent of the version-gated walk, `apply_schema` runs the
column-add chain (`_ensure_columns_present`) **unconditionally** as its
last step. The same chain is the column-add leg of the v0→v5 migration
and a defensive pre-step of the v5→v6 rebuild. If a prior migration
was interrupted between an ALTER and the version-row commit, or if a
column was manually dropped via the sqlite shell, the next open
self-heals instead of crashing the v5→v6 rebuild three layers down with
`OperationalError: no such column`.

### 1a. v5→v6 pre-validation

The rebuild's `INSERT INTO X_new SELECT … FROM X` step is the dangerous
point: any pre-existing row that violates one of the new constraints
aborts the migration mid-flight, locking the operator out of the
warehouse until they hand-patch the offending data. Two pre-steps run
before the rebuild starts:

- `_v5_to_v6_assert_not_null_preconditions` queries each
  about-to-be-NOT-NULL column and raises `RuntimeError` *with the row
  count and the offending column name* if any NULL is found. The
  operator sees a one-line message instead of an `IntegrityError: NOT
  NULL constraint failed` deep in the rebuild script — they can run a
  `SELECT id, ifsc_id FROM <table> WHERE <column> IS NULL` to find the
  rows.

- `_v5_to_v6_clean_check_violations` NULLs out values that the new
  CHECK constraints would reject (gender ∉ {0,1,NULL}, malformed dates,
  `cup_rankings.d_cat_id ≤ 0`, etc.) and logs an INFO line per affected
  (table, column, count). Discarding legacy bad data is the same net
  result as a failed rebuild, just reached without locking the
  warehouse closed.

### 2. NOT NULL on always-populated FK columns

`season_leagues.season_id`, `season_leagues.league_id`, and
`events.season_id` are `NOT NULL` in v6. These are set by every
production fetcher path; the constraint catches regressions where a new
code path forgets the parent reference. `events.league_id` stays
nullable: events discovered via `/seasons/{id}.events[]` arrive without
a league association, which only `/season_leagues/{id}` later fills in.

### 3. CHECK on boolean-flavored INTEGERs

Columns that the ingest code only ever writes 0/1/NULL into get a
`CHECK (col IS NULL OR col IN (0, 1))`: `ascents.top`, `plus`, `dnf`,
`dns`, `restarted`; `stage_results.winner`; `events.is_paraclimbing`;
`categories.gender`; `athletes.gender`.

### 4. CHECK on date-like columns

Columns whose value comes from an upstream date field get a GLOB check
that the prefix matches `YYYY-MM-DD…`: `events.date_start` /
`date_end`, `athletes.birthday`, `category_rounds.status_as_of`. The
pattern is `[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*` (explicit
digit classes), not `????-??-??*` — SQLite GLOB's `?` matches any
single character including ASCII letters, so the wildcard form
silently accepted `'abcd-ef-gh'`. Digit classes catch both directions
(legitimate dates pass, garbage rejected).

### 5. `cup_rankings` UNIQUE on `COALESCE(d_cat_id, -1)` + stable-id upsert

The inline `UNIQUE (athlete_id, cup_ifsc_id, d_cat_id)` is replaced by
an expression unique index `idx_cup_rankings_uniq ON cup_rankings
(athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1))`. NULL d_cat values
now collide under the COALESCE-to-`-1` projection. The upsert switches
from `INSERT OR REPLACE` (which deleted + reinserted) to `INSERT … ON
CONFLICT (athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1)) DO UPDATE`
(which mutates the row in place, preserving `id` when used in
isolation).

A `CHECK (d_cat_id IS NULL OR d_cat_id > 0)` constraint on
`cup_rankings.d_cat_id` prevents a literal `-1` from colliding with
the NULL bucket of the expression UNIQUE — `COALESCE(-1, -1) =
COALESCE(NULL, -1)`, so without the CHECK an upstream that ever
shipped `d_cat_id = -1` would silently overwrite a NULL-d_cat row on
the same (athlete, cup).

The v5→v6 migration deduplicates pre-existing NULL-d_cat duplicates
before installing the new index — `DELETE … WHERE id NOT IN (SELECT
MAX(id) … GROUP BY athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1))`.
The "keep MAX(id)" rule preserves the most recent upsert.

**Caveat on id stability.** The "stable id" claim of `ON CONFLICT DO
UPDATE` only holds when the conflict target row actually exists in the
DB when the upsert runs. The only production caller
(`athletes.hydrate`) currently calls `delete_cup_rankings_for_athlete`
before its upsert loop, so the conflict target is gone and SQLite
assigns fresh rowids. The delete-then-rewrite pattern is how the
fetcher keeps cup_rankings in sync with the latest payload (rankings
that disappeared from the payload need to leave the DB). If a future
downstream needs id stability across re-hydration, the fix is in
`athletes.hydrate` — track seen `(cup_ifsc_id, d_cat_id)` keys and
`DELETE … WHERE NOT IN (…)` instead of the blanket wipe. The
`upsert_cup_ranking` docstring documents this rather than claiming
something the production code doesn't deliver.

### Other v6 schema work

- `CREATE INDEX idx_events_date_end ON events(date_end)` — the
  ongoing-events query in `Repository.find_ongoing_events` filters on
  `date_end >= cutoff`, previously a table scan.
- `_configure_connection(conn)` centralizes the `PRAGMA foreign_keys =
  ON` + `row_factory = sqlite3.Row` setup used by both `open_db` and
  the tests' `memory_db` fixture — they can no longer drift.
- `open_db` asserts `sqlite3.sqlite_version_info >= (3, 35)` because
  `ALTER TABLE … DROP COLUMN` (used by the v4→v5 leg) landed there.
- `seasons.hydrate` and `season_leagues.hydrate` both wrap each
  per-iteration body in `with repo.transaction():`, matching the
  pattern `competitions.hydrate` established under ADR 0005. A parse
  failure mid-iteration rolls back any partial writes (e.g. a new
  `leagues` row inserted before the events loop crashed) instead of
  leaving the warehouse half-populated for the failing row.
- `season_leagues.hydrate` and the relevant repository upsert
  signatures (`upsert_season_league`, `upsert_event_skeleton`) now
  treat NULL FKs as a hard skip-with-WARN path: SQLite UPSERT only
  intercepts UNIQUE/PK conflicts, so a NULL on a newly-NOT-NULL column
  trips the INSERT side before `ON CONFLICT … DO UPDATE` can
  COALESCE-preserve the prior value. The fetcher checks resolvability
  before calling and logs a "could not resolve" WARN, then proceeds
  with `mark_fetched` so the row leaves the stale pool. The
  repository upsert signatures dropped their `Optional[int] = None`
  contract — calling with `None` was a no-op in v5 (COALESCE preserved
  existing) but now raises `IntegrityError`.

## Consequences

**Positive**

- Schema invariants the Python code already maintained are now
  enforced by SQLite — a future regression on the upsert side trips
  `IntegrityError` immediately rather than producing a quietly-wrong
  warehouse.
- `cup_rankings.id` is stable across re-hydration, so analytics
  notebooks (and the planned ML downstream) can persist
  cup-ranking-keyed joins without invalidating them on every refresh.
- The version-gated framework gives a clear answer to "what version is
  this DB at?" via `SELECT MAX(version) FROM schema_version`.
  Mid-migration crashes leave the DB at the last fully-committed
  version, not a half-state.
- Operators can grep stderr for `CHECK constraint failed: <col> IS NULL
  OR <col> GLOB '????-??-??*'` and immediately know the upstream
  changed a date format — instead of debugging downstream date math
  three layers later.

**Negative**

- The v5→v6 migration rebuilds eight tables (CREATE _new + INSERT
  SELECT + DROP + RENAME). On a full backfill warehouse (~600 MB) this
  adds a one-time ~30–60s cost at first open under the new code. The
  migration is transactional with `PRAGMA foreign_key_check` before
  COMMIT, so a power loss mid-rebuild rolls back cleanly.
- The v6 expression UNIQUE on `cup_rankings` requires SQLite to
  evaluate `COALESCE(d_cat_id, -1)` on every insert and on every
  conflict-target match. The cost is microseconds per row at the
  ingest cadence we run, and the trade-off — collapsed duplicates —
  outweighs it. Heavier consumers that do range scans on the unique
  index would see the expression re-evaluated; in practice
  `cup_rankings` is read by athlete + cup_ifsc filters that hit the
  separate `idx_cup_rankings_athlete` / `idx_cup_rankings_cup` indexes
  first.
- Tests that previously created event skeletons ad-hoc (no
  `season_id`) had to be updated to seed a season first. The change
  was mechanical but touched ~10 test functions across three files.
- The `_DDL_V6_ONLY` block exists because the shared `DDL` constant
  can't safely include the expression UNIQUE: running it on a real v5
  DB with NULL-d_cat duplicates fails before the v5→v6 migration's
  dedupe step can fire. Carrying two DDL fragments is a smell; once
  v6 is universal (no DBs older than v5 in the wild) we can fold them
  back together.

## Alternatives considered

- **`CHECK (col IN ('Y', 'N'))` instead of integer 0/1** — would let
  the schema document the meaning directly. Rejected: the ingest code
  already writes integers, and converting at the boundary doubles the
  surface area for type bugs. Documentation belongs in the data
  dictionary, not the storage type.
- **Migrate via `defer_foreign_keys=1` instead of toggling
  `foreign_keys=OFF`** — the v5→v6 table rebuilds need FK enforcement
  disabled during the DROP TABLE / RENAME dance, since references in
  child tables would otherwise CASCADE. `defer_foreign_keys` runs
  within a transaction but doesn't disable CASCADE on DROP. Rejected.
- **Sequential v0→v1, v1→v2, … v5→v6 migration functions** — clean
  mental model, but we don't have snapshots of the v1–v4 DDL; they'd
  have to be reverse-engineered from git history. The cost of getting
  them subtly wrong outweighed the cleanliness benefit for a
  single-operator codebase. Lumped them into one
  `_migrate_pre_v1_to_v5` instead.
- **Drop the `INSERT OR IGNORE INTO schema_version` row from previous
  apply_schema** — the v5 code wrote `INSERT OR IGNORE … VALUES (5)`
  on every open, accumulating no row history. We kept it to preserve
  existing DBs' version state at upgrade time. New migrations use
  plain `INSERT` so the history accumulates one row per migration
  step.
