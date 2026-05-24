# 0001 — Single SQLite warehouse

**Status:** Accepted
**Date:** 2026-05-22

## Context

The package needs persistent storage for ~150k rows across nine tables
(seasons → results), with single-writer access from a CLI invoked
interactively and from notebooks.

> **Note (2026-05-24):** Table count grew to 15 once ADR 0007 added the
> per-round tables. This ADR's reasoning about file-based storage,
> single-writer access, and operational simplicity is unchanged.
 Downstream consumers are notebooks,
dashboards, and an eventual ML pipeline — all of which run on the same
machine as the ingestion process for the foreseeable future.

The data volume (tens of MB) is well within SQLite's comfort zone. There is
no concurrent-writer requirement: only one `python -m wcl_data <cmd>` runs
at a time.

## Decision

Store all warehouse data in a **single SQLite file** at `data/wcl.sqlite`
(overridable via `WCL_DB_PATH`). Schema defined in
`src/wcl_data/db/schema.py` and applied idempotently via
`apply_schema()` on every connection open. No migrations framework yet — a
`schema_version` table exists for future use.

## Consequences

**Positive**

- Zero operational overhead: no server, no auth, no port, no Docker.
  Contributors can `git clone` and `init` and have a working warehouse in
  one minute.
- The whole warehouse is one portable file — easy to share, snapshot, back
  up (`cp data/wcl.sqlite data/snapshot.sqlite`), or ship with the repo
  in CI fixtures.
- Notebooks open it directly with `sqlite3.connect(...)` or
  `pandas.read_sql(...)`. No driver install.

**Negative**

- Schema changes require a migration story we haven't built yet. When the
  first real schema change comes, we'll add a numbered migrations
  directory and an `apply_migrations` step before `apply_schema`. Until
  then, `IF NOT EXISTS` does the job.
- SQLite serializes writes. Not a problem today (single-writer CLI) but
  rules out future "ingest two endpoints in parallel from separate
  processes" without rework.
- No row-level concurrency for readers during writes. Long-running queries
  in a notebook can briefly block an `ingest`. In practice, ingestion
  finishes per-row so this is unmeasurable.

## Alternatives considered

- **Postgres / MySQL** — overkill for this dataset size, and would require
  every contributor and every CI job to run a server. Reconsider if the
  warehouse grows past ~10 GB or we need concurrent writers.
- **DuckDB** — tempting for analytics performance, but its file format is
  newer and tooling support (notebooks, BI tools, GUI clients) is thinner.
  Worth revisiting when the ML layer materializes if analytical queries
  become the bottleneck.
- **Parquet files in a folder** — great for ML pipelines but bad for the
  incremental-update pattern (`UPDATE … WHERE id = ?` doesn't exist).
  Could be a downstream export target, not the warehouse.
