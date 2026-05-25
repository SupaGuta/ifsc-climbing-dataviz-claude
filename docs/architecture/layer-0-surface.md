# Layer 0 surface

`wcl_data` is the **Layer 0** of the World Climbing Lab project: it ingests
the public competition API at [ifsc.results.info](https://ifsc.results.info)
into a single local SQLite warehouse at `data/wcl.sqlite`. Higher layers
(`wcl_analytics`, `wcl_ml`, notebooks) consume the warehouse and never call
the API directly.

This page is the **contract** Layer 0 exposes to those consumers. If you're
building anything on top of the warehouse, this is what you can rely on and
what you should not assume.

## What Layer 0 owns

- HTTP fetch + retry against `ifsc.results.info`, session-cookie auth.
- Parsing of the API payloads into typed SQLite rows.
- One narrow heuristic: event city/country recovery from free-text event
  names (see [parsing-and-heuristics.md](parsing-and-heuristics.md)).
- Idempotent schema migration on every DB open.
- The `last_fetched_at`-based staleness model that drives `refresh` /
  `pull-new` / `hydrate`.

## What Layer 0 does **not** own

These are deliberate non-goals. If you need any of them, build them
downstream — do not extend Layer 0.

- **Derived aggregates.** Season-end podium counts, athlete career arcs,
  rolling form, head-to-head records. The API's
  `discipline_podiums` / `all_results` / `world_championships_discipline_podiums`
  blocks were explicitly rejected (see [ADR 0009](../decisions/0009-athletes-payload-expansion.md)).
- **Startlists and starting orders.** `/api/v1/routes/{id}/startlist` and
  per-stage starting orders are not ingested. A `last_fetched_at` hook
  existed on `category_rounds` / `routes` in schema v4 but was dropped in
  v5 (see 2026-05-25 note on [ADR 0007](../decisions/0007-per-round-ingestion.md)).
- **`/cups/{id}` endpoint.** Cup standings are captured per athlete via
  `cup_rankings`, not via a top-down cup table.
- **Judge, route-setter, official data.** Not consumed.
- **Live ranking deltas.** Only the final ranking per competition is
  stored.
- **External enrichments.** Wikidata, Wikipedia, biometric scraping. A
  one-shot exploration ruled it not worth industrializing — `arm_span`
  coverage from Wikidata caps around 50 athletes for top-200 effort.
- **Pre-API archive results.** The API itself only goes back to the late
  1990s; anything earlier is out of scope.
- **A web UI, BI tool, or analysis surface.** Layer 0 is a CLI + a Python
  API + CSV exports. Downstream consumers own visualization.

## Stable surface (the contract)

The 16 tables below are the stable surface as of **schema v5**. Per-column
detail lives in [`../data-dictionary/`](../data-dictionary/README.md).

| Table             | Stable since | Hydratable | Notes |
|-------------------|:------------:|:----------:|-------|
| `seasons`         | v1           | ✓ | |
| `leagues`         | v1           |   | Reference |
| `season_leagues`  | v1           | ✓ | |
| `disciplines`     | v1           |   | Reference |
| `categories`      | v1           |   | Reference |
| `events`          | v1 (+v4 `country_iso3`) | ✓ | |
| `competitions`    | v1           | ✓ | |
| `athletes`        | v1 (+v4 federation/sport-class/speed-PB; v4 dropped `is_paraclimbing`) | ✓ | |
| `results`         | v1           |   | Derived: wiped+reinserted per competition |
| `cup_rankings`    | v4           |   | Derived: wiped+reinserted per athlete |
| `category_rounds` | v3 (lost `last_fetched_at` in v5) |   | Structural: UPSERTed per competition |
| `round_stages`    | v3           |   | |
| `routes`          | v3 (lost `last_fetched_at` in v5) |   | Structural: UPSERTed per competition |
| `round_results`   | v3           |   | Derived |
| `stage_results`   | v3           |   | Derived |
| `ascents`         | v3           |   | Derived |

**Hydratable** = carries `last_fetched_at`; reachable by `find_stale`,
`mark_fetched`, and the `hydrate <entity>` CLI verb.

## Guarantees

The following hold across schema versions; if they ever change, a new ADR
will document the migration.

- **Identity.** Every entity has both a local `id INTEGER PRIMARY KEY` and
  an `ifsc_id INTEGER`. FKs reference `id`, never `ifsc_id`.
- **Uniqueness.** `competitions` uses `UNIQUE (event_id, ifsc_id)` because
  competition IFSC ids are not globally unique. Every other hydratable
  table has `ifsc_id UNIQUE`. `athletes.ifsc_id` is unique *at any point
  in time*, but the IFSC reassigns deleted ids — see the `ifsc_id`
  reassignment gotcha on [athletes.md](../data-dictionary/athletes.md).
- **NULL semantics.** NULL means *we don't know*, never zero or false. A
  NULL on a populated field is almost always an upstream gap, not a parser
  bug. `last_fetched_at IS NULL` specifically means "skeleton — never
  hydrated."
- **Timestamps.** All `last_fetched_at` values are ISO-8601 UTC with a
  literal `Z` suffix (`YYYY-MM-DDTHH:MM:SSZ`). Lexicographic comparison is
  safe and correct.
- **Date strings.** `birthday`, `date_start`, `date_end`,
  `sport_class_review_date`, `speed_pb_date` are `YYYY-MM-DD` text. No
  time component.
- **Gender.** INTEGER `0` = male, `1` = female, NULL = unknown. Same
  encoding on `athletes.gender` and `categories.gender`. CSV exports
  translate back to string labels.
- **Country.** Two columns wherever a country appears: `country` (raw
  federation code, mix of ISO 3166-1 alpha-3 and IFSC/IOC variants) and
  `country_iso3` (canonical ISO 3166-1 alpha-3 only, derived via the
  `IFSC_TO_ISO3` map). NULL iff `country` is NULL. See
  [ADR 0008](../decisions/0008-country-iso3-sibling-column.md).
- **Score columns** are TEXT, not REAL. The API renders them as
  polymorphic strings (`"TOP"`, `"49+"`, `"7.75"`, `"4.82"`); parsing them
  is downstream-consumer responsibility (see [ADR 0007](../decisions/0007-per-round-ingestion.md)).
- **Foreign keys** are declared and `PRAGMA foreign_keys = ON` is set, but
  enforcement is not strict (skeleton rows may exist before parents are
  fully hydrated).

## Schema version

Stored in the `schema_version` table; readable via `python -m wcl_data
status` or `Repository.schema_version()`. Migrations are applied
idempotently on every `open_db`. Column additions use
`_add_missing_column`; column removals use `_drop_column_if_exists`. There
is no separate migrations directory — `apply_schema` is the single source
of truth.

A schema-breaking change goes through:

1. A new ADR documenting the change and rationale.
2. A bump of `CURRENT_VERSION` in `src/wcl_data/db/schema.py`.
3. The migration call in `apply_schema()`.
4. A test asserting the migration applies cleanly on a v(N-1) DB.

## How to consume

- **CLI:** `python -m wcl_data export` writes denormalized CSVs to
  `data/exports/`. See the [README](../README.md) for view reference.
- **Python:** `wcl_data.config.load_settings()` → `wcl_data.db.schema.open_db(path)`
  → `wcl_data.db.repository.Repository(conn)`. See
  [`../python-api/`](../python-api/README.md).
- **Raw SQL:** `sqlite3 data/wcl.sqlite` works fine; the per-table docs
  list all columns + types.

The warehouse file is **single-writer**: do not modify it from any
process other than `wcl_data`. Concurrent read-only access is safe.

## Where to go next

- [`overview.md`](overview.md) — system architecture and lifecycle of one
  ingest run.
- [`database-and-schema.md`](database-and-schema.md) — the design behind
  the schema, transactional boundaries, upsert pattern.
- [`../data-dictionary/`](../data-dictionary/README.md) — per-column
  reference with coverage stats.
- [`../decisions/`](../decisions/README.md) — every non-obvious choice has
  an ADR.
