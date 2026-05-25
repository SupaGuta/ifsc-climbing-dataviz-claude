# Daily use

Recipes for the three ingestion commands: `pull-new`, `refresh`,
`hydrate <entity>`. For the *why* behind these three modes existing, see
[`../architecture/ingestion-pipeline.md`](../architecture/ingestion-pipeline.md).

## Catch new World Climbing content (the everyday command)

```bash
python -m wcl_data pull-new
```

Re-fetches **ongoing** container entities only (current-year seasons,
events within 15 days of `date_end`, plus their descendants) to surface
newly-published content, then hydrates only **brand-new** athlete
skeletons. Takes ~30–60 seconds on a steady-state warehouse.

Use this **daily or weekly**. It's the default cadence.

**Grace period override** (catches late result corrections within N days
of an event's end):

```bash
python -m wcl_data pull-new --grace-days 30   # more forgiving
python -m wcl_data pull-new --grace-days 0    # strict: ended = frozen
```

Default is 15 days, configurable via `WCL_GRACE_DAYS` in `.env`. See
[ADR 0006](../decisions/0006-ongoing-only-pull-new.md) for the rationale.
If you need to catch a retroactive edit to an ended container, use
`refresh --stale-days 0` instead.

## Refresh stale rows on the 30-day cadence (covers all containers)

```bash
python -m wcl_data refresh
```

Discover + hydrate anything stale (default: NULL or older than 30 days)
across the full graph, **including athlete profiles**. Wall-clock varies
with staleness: ~30s if nothing's overdue, up to ~30 minutes when many
athletes are due for the 30-day re-hydration. For the nuclear option
(`--stale-days 0`), see below.

Override the threshold per run:

```bash
python -m wcl_data refresh --stale-days 7      # weekly cadence
python -m wcl_data refresh --stale-days 0      # force everything (~45-90 min)
```

## Force-refresh everything from scratch

```bash
python -m wcl_data refresh --stale-days 0
```

The nuclear option: every hydratable row is treated as stale, including
~14,900 athlete profiles. **~45-90 minutes.** Use after a parser change to
re-extract every event's city/country, after a schema bump (e.g. ADR 0007's
per-round tables), or once a year for general freshness.

**Note on the per-round backfill:** after the per-round tables landed in
schema v2 (ADR 0007), the new tables (`category_rounds`, `round_stages`,
`routes`, `round_results`, `stage_results`, `ascents`) start empty even
for previously-hydrated competitions. The `competitions.last_fetched_at`
doesn't get cleared by the schema upgrade, so running `pull-new` alone
won't backfill them — only ongoing competitions would be touched. A
one-time `refresh --stale-days 0` re-hydrates everything and fills the new
tables. Plan ~45-90 minutes for that first run; subsequent `pull-new` and
`refresh` calls keep the new tables in sync automatically.

## Touch one entity only

```bash
python -m wcl_data hydrate athletes
python -m wcl_data hydrate events --stale-days 0
python -m wcl_data hydrate competitions
```

Same staleness semantics as `refresh`, scoped to one table. Useful after
fixing a fetcher and wanting to re-parse only that entity's payloads
without re-walking the whole graph.

**Note:** `hydrate <entity>` only refreshes rows that *already exist*. New
discovery happens by hydrating the parent. The one exception is
`hydrate seasons`, which also runs the seasons-probe.

Choices: `seasons`, `season_leagues`, `events`, `competitions`, `athletes`.

## Smoke test with `--limit`

```bash
python -m wcl_data pull-new --limit 10
python -m wcl_data refresh --limit 20
python -m wcl_data hydrate events --limit 5
```

Caps rows touched **per entity**. The first 10–20 rows usually catch any
broken parser. Use this when validating a code change before the full run.

## Tune concurrency with `--workers`

Defaults to 50 (or `WCL_MAX_WORKERS` from `.env`). Useful range is 50–100.

```bash
python -m wcl_data pull-new --workers 75
python -m wcl_data refresh --workers 100
```

Beyond ~100 you start running into World Climbing's connection limits without
measurable speedup. Below 30 you're leaving throughput on the table.

The flag sizes both the `ThreadPoolExecutor` and the urllib3 connection
pool — see [`../architecture/api-client.md`](../architecture/api-client.md)
for why both numbers matter.

## See what's in the DB

```bash
python -m wcl_data status
```

Doesn't touch the API. Prints row counts and (for hydratable tables)
hydration coverage:

```
table                      rows   hydrated
seasons                      38         38
...
```

If `hydrated` is significantly less than `rows`, the row is a known-but-
unfilled skeleton — run `refresh` or `hydrate <entity>` to backfill.

## Keeping WARNINGs visible

By default, WARNING log lines (4xx drops, parse failures) are hidden from
console and only written to `logs/wcl-data.log`. Add `-v` before the
subcommand to keep them on-screen:

```bash
python -m wcl_data -v pull-new
python -m wcl_data -v refresh --stale-days 0
```

Useful when debugging a fetcher change or a credentials issue. For the
log structure see [`../operations/logs.md`](../operations/logs.md).

## When something goes wrong

- Failures starting around the same time as a credential rotation →
  [`../operations/auth.md`](../operations/auth.md).
- A `pull-new` was killed mid-run →
  [`../operations/recovery.md`](../operations/recovery.md). Short answer:
  just re-run it.
- A specific row keeps failing → see
  [troubleshooting.md](troubleshooting.md).
