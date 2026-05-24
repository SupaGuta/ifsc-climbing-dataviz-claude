# Database & schema

A single SQLite file at `data/wcl.sqlite` (or wherever `WCL_DB_PATH` points).
No migrations framework, no ORM, no external DB server. The schema is defined
in `src/wcl_data/db/schema.py` as one `CREATE TABLE IF NOT EXISTS` script
that runs on every connection open via `apply_schema()`. The rationale for
the single-file SQLite choice is in
[ADR 0001](../decisions/0001-single-sqlite-warehouse.md).

## Tables

The schema mirrors the API entity tree. Fifteen tables total, plus a
`schema_version` bookkeeping table.

| Table             | Rows fed by                             | Hydratable | Notes                                                       |
|-------------------|------------------------------------------|:----------:|-------------------------------------------------------------|
| `seasons`         | `seasons.discover` + `seasons.hydrate`   |     ✓      | `year` is set during hydration                              |
| `leagues`         | `seasons.hydrate`                        |            | Reference data, name-unique                                 |
| `season_leagues`  | `seasons.hydrate` (skeleton) + `season_leagues.hydrate` | ✓ | Join row between season × league                            |
| `disciplines`     | `season_leagues.hydrate`                 |            | Reference data, name-unique (lowercase)                     |
| `categories`      | `season_leagues.hydrate`                 |            | Reference data; `gender` is 0=men, 1=women, NULL=other      |
| `events`          | `seasons.hydrate` / `season_leagues.hydrate` (skeleton) + `events.hydrate` | ✓ | City + country from `parsers.event_location` + API field + `CITY_TO_COUNTRY` dict + sibling backfill. Carries `country_iso3` sibling (ADR 0008). |
| `competitions`    | `events.hydrate` (skeleton) + `competitions.hydrate` | ✓ | UNIQUE on `(event_id, ifsc_id)`, not on `ifsc_id` alone     |
| `athletes`        | `competitions.hydrate` (skeleton) + `athletes.hydrate` | ✓ | Carries `country_iso3` sibling (ADR 0008) and federation / sport-class / speed-PB fields (ADR 0009). Paraclimbing proxy is `paraclimbing_sport_class IS NOT NULL` (heuristic); authoritative flag is on `events`. |
| `cup_rankings`    | `athletes.hydrate`                        |            | Derived: wiped + reinserted per athlete. Season-end overall standing per (cup × discipline). |
| `results`         | `competitions.hydrate`                   |            | Derived: wiped + reinserted per competition. Final overall rank only. |
| `category_rounds` | `competitions.hydrate`                   |     ✓      | Phases of a competition (qualif / semi / final). `last_fetched_at` reserved for future startlist work. |
| `round_stages`    | `competitions.hydrate`                   |            | Sub-stages of a round (speed-final heats, combined sub-disciplines). |
| `routes`          | `competitions.hydrate`                   |     ✓      | One per (round × route). `last_fetched_at` reserved for future startlist work. |
| `round_results`   | `competitions.hydrate`                   |            | Derived: per-round rank + score per athlete                 |
| `stage_results`   | `competitions.hydrate`                   |            | Derived: per-stage detail (combined sub-stages, speed heats)|
| `ascents`         | `competitions.hydrate`                   |            | Derived: per-route performance. Excluded from `export_all` by default. |

**Hydratable** tables carry a `last_fetched_at` TEXT column. Every hydratable
table also has `CREATE INDEX idx_<table>_last_fetched` so `find_stale`
filtering stays cheap as the warehouse grows.

## Identity model

Every entity has two IDs:

- **`id` INTEGER PRIMARY KEY** — the local row PK. SQLite auto-assigns these.
  Used everywhere internally for foreign keys.
- **`ifsc_id` INTEGER UNIQUE** — the IFSC API's ID, used to build URLs like
  `/seasons/{ifsc_id}`. Always populated; that's how rows are first inserted.

The split exists because the upstream `ifsc_id` is not always sufficient as a PK:
`competitions.ifsc_id` is *not* globally unique (the API uses the same comp
ID across multiple events), which is why the competitions table uses
`UNIQUE (event_id, ifsc_id)` instead.

Foreign keys reference `id`, never `ifsc_id`. `PRAGMA foreign_keys = ON` is
set when opening the connection — but enforcement isn't strict because
skeleton rows are sometimes created before their parents are fully hydrated.

## Staleness via `last_fetched_at`

The whole incremental-update story (see
[ADR 0004](../decisions/0004-incremental-hydration-with-staleness.md)) is
built on this one column.

- **Format:** `"%Y-%m-%dT%H:%M:%SZ"` (ISO-8601 with explicit `Z`).
  Lexicographically sortable, so `last_fetched_at < cutoff` is a correct TEXT
  comparison — no parsing in SQL.
- **NULL semantics:** a row exists but has never been hydrated. Skeleton rows
  inserted during discovery have `last_fetched_at = NULL`, so the next
  hydration phase picks them up.
- **Cutoff:** `Repository.stale_cutoff(stale_days)` returns the same TEXT
  format. `find_stale` uses `last_fetched_at IS NULL OR last_fetched_at <
  cutoff` — the NULL branch catches skeletons; the cutoff branch catches
  stale rows.
- **`mark_fetched`** updates `last_fetched_at = utcnow()` and runs *after* the
  row's fields are updated, so a parse failure leaves the timestamp NULL and
  the row eligible for retry on the next run.

`stale_days=0` → cutoff is now → every row matches.
`stale_days=365_000` → cutoff is ~1000 years ago → only NULL matches. This is
the trick `pull_new` uses to hydrate only newly-discovered athletes; see
[ingestion-pipeline.md](ingestion-pipeline.md).

## Commit cadence and the `transaction()` context

Every `Repository` method commits before returning, unless the call is wrapped
in `with repo.transaction():`. This is `_maybe_commit`:

```python
def _maybe_commit(self) -> None:
    if not self._in_transaction:
        self.conn.commit()
```

Per-row commit is what makes `Ctrl-C` safe: the in-flight HTTP request loses
its data, but every row written before it is durable. See
[ADR 0002](../decisions/0002-streaming-writes.md).

**The exception is `competitions.hydrate`.** It wraps each competition's
work-unit in a transaction — covering both the `results` write and the
per-round structural / athlete data (`category_rounds`, `round_stages`,
`routes`, `round_results`, `stage_results`, `ascents`):

```python
with repo.transaction():
    repo.delete_round_data_for_competition(comp_id)
    repo.delete_results_for_competition(comp_id)
    # Phase A: top-level category_rounds + routes + default/combined stages
    # Phase B: per-athlete dispatch on (ascents | combined_stages | speed_elimination_stages)
    repo.mark_fetched("competitions", comp_id)
```

The `delete + reinsert` pattern means a partial failure mid-loop would leave
the competition with empty per-round tables and a NULL `last_fetched_at`. The
transaction rolls back all of these on exception, so either everything lands
or nothing does. See
[ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md) for
the original rationale and
[ADR 0007](../decisions/0007-per-round-ingestion.md) for the per-round
extension.

Nested transactions are flattened: only the outermost commits. This matters
because some repo methods could in principle call others — keeping the
nesting safe avoids future foot-guns.

## Upsert pattern

Every entity has an `upsert_*` method using SQLite's
`ON CONFLICT (...) DO UPDATE SET ... RETURNING id`:

```python
"INSERT INTO seasons (ifsc_id, year) VALUES (?, ?) "
"ON CONFLICT(ifsc_id) DO UPDATE SET "
"  year = COALESCE(excluded.year, seasons.year) "
"RETURNING id"
```

Two key choices:

- **`COALESCE(excluded.value, table.value)`** — when re-hydrating, NULL values
  in the new fetch don't blow away existing fields. The API occasionally
  omits a field on one response that was present on another; COALESCE means
  we keep whatever we know.
- **`RETURNING id`** — `INSERT OR IGNORE` would return zero rows on conflict
  and force a separate `SELECT id`. `ON CONFLICT DO UPDATE ... RETURNING` is
  one round trip.

Skeleton inserts (e.g. `upsert_athlete_skeleton`) are a degenerate form:
`ON CONFLICT DO UPDATE SET ifsc_id = excluded.ifsc_id` (a no-op) just to make
`RETURNING id` work.

## Generic queries and `_validate_table`

Four repo methods take a table name argument: `count`, `count_hydrated`,
`mark_fetched`, `find_stale`. The table name is interpolated into the SQL via
f-string, which is normally a SQL-injection risk. The guard is
`_validate_table(table, allowed)` which checks against the
`HYDRATABLE_TABLES` / `ALL_TABLES` tuples at the top of
`src/wcl_data/db/repository.py` and raises `ValueError` for anything else.
The CLI never passes user input directly into these; the only callers are the
fetcher modules with hardcoded strings.

## What's deliberately *not* here

- **No migrations framework.** `schema_version` exists for future use but
  there's only one version. When the schema needs to change, we'll add a
  numbered migrations directory and an `apply_migrations` step before
  `apply_schema`. Until then, `IF NOT EXISTS` is enough.
- **No ORM.** The repository is hand-written CRUD because (a) the dataset is
  small and the queries are simple, and (b) the project's medium-term plan
  is downstream ML work that will read raw SQL anyway.
- **No connection pool.** SQLite uses a single connection per process; the
  HTTP layer fans out across threads but they all write through the same
  `Repository` instance. SQLite serializes writes anyway, so a pool would
  buy nothing.
