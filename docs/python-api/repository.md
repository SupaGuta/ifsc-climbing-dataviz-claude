# `Repository`

Typed CRUD wrappers around the SQLite warehouse. Lives in
[`src/wcl_data/db/repository.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/db/repository.py).
Every method commits before returning unless wrapped in `with
repo.transaction():`. For the *why* see
[`../architecture/database-and-schema.md`](../architecture/database-and-schema.md)
and [ADR 0002](../decisions/0002-streaming-writes.md).

## Constructing

```python
from wcl_data.config import load_settings
from wcl_data.db.schema import open_db
from wcl_data.db.repository import Repository

settings = load_settings(require_credentials=False)
conn = open_db(settings.db_path)            # creates schema if missing
repo = Repository(conn)
```

`open_db(path)` is the recommended entry point — it sets
`PRAGMA foreign_keys = ON`, applies `apply_schema`, and uses
`sqlite3.Row` as the row factory. Don't construct a `sqlite3.Connection`
yourself unless you need to.

## Counting

```python
repo.count("athletes")                      # total rows
repo.count_hydrated("athletes")             # rows with last_fetched_at NOT NULL
```

`count_hydrated` only accepts hydratable tables (`HYDRATABLE_TABLES`):
`seasons`, `season_leagues`, `events`, `competitions`, `athletes`. Anything
else raises `ValueError`.

## Finding stale rows

```python
stale = repo.find_stale("athletes", stale_days=30)
# → list[sqlite3.Row] with `id` and `ifsc_id` columns
for row in stale:
    print(row["id"], row["ifsc_id"])
```

`find_stale` returns rows where `last_fetched_at IS NULL OR last_fetched_at
< (now - stale_days)`. With `stale_days=0` everything matches; with a huge
`stale_days` (e.g. `365_000`) only NULL matches. This is how `pull_new`
implements "hydrate only newly-discovered athletes."

For custom queries that need the same cutoff format:

```python
cutoff = repo.stale_cutoff(30)              # 'YYYY-MM-DDTHH:MM:SSZ'
```

## Finding ongoing rows

For `pull-new`'s ongoing-only scope (see
[ADR 0006](../decisions/0006-ongoing-only-pull-new.md)):

```python
repo.find_ongoing_seasons()                  # year >= current_year OR NULL
repo.find_ongoing_season_leagues()           # parent season is ongoing
repo.find_ongoing_events(grace_days=15)      # date_end >= today - grace_days OR NULL
repo.find_ongoing_competitions(grace_days=15) # parent event is ongoing
```

All four return `list[sqlite3.Row]`. The first three return rows shaped
`(id, ifsc_id)`. `find_ongoing_competitions` returns rows shaped
`(comp_id, comp_ifsc, event_ifsc)` — the JOIN gives you the event's
`ifsc_id` so you can build the
`/events/{event_ifsc}/result/{comp_ifsc}` path without a second query.

`grace_days` defaults to 15 (the production default for `pull-new`) but
is parameterizable for testing or strict-mode runs (`grace_days=0`).

These are read-only — they don't write anything. The intended pattern is
to pass the result into the matching fetcher's `hydrate(rows=...)`
parameter, e.g.:

```python
from wcl_data.fetchers import events as events_fetcher
events_fetcher.hydrate(repo, client, rows=repo.find_ongoing_events())
```

## Marking a row as freshly hydrated

```python
repo.mark_fetched("athletes", row_id)
```

Stamps `last_fetched_at = utcnow()`. Call this *after* writing the row's
fields, so a parse failure mid-write leaves the timestamp NULL and the row
still eligible for retry.

## Upserts

Every entity has an `upsert_*` method. Returns the local `id`:

```python
season_row_id = repo.upsert_season(ifsc_id=42, year=2024)
league_row_id = repo.upsert_league("World Cup")
sl_row_id = repo.upsert_season_league(ifsc_id=99, season_id=season_row_id, league_id=league_row_id)
discipline_id = repo.upsert_discipline("lead")
category_id = repo.upsert_category("Men", gender=0)
event_row_id = repo.upsert_event_skeleton(ifsc_id=123, season_id=season_row_id, league_id=league_row_id)
comp_id = repo.upsert_competition(event_id=event_row_id, ifsc_id=7,
                                  discipline_id=discipline_id, category_id=category_id)
athlete_id = repo.upsert_athlete_skeleton(ifsc_id=555)
```

Upserts use `ON CONFLICT DO UPDATE` with `COALESCE(excluded.value, table.value)`
so re-running with NULL fields doesn't blow away existing data. See
[`../architecture/database-and-schema.md`](../architecture/database-and-schema.md)
for the pattern.

## Field updates

```python
repo.update_event(event_row_id,
                  name="IFSC World Cup - Chamonix (FRA) 2024",
                  city="Chamonix", country="FRA",
                  date_start="2024-07-12", date_end="2024-07-14",
                  is_paraclimbing=0)

repo.update_athlete(athlete_id,
                    firstname="Janja", lastname="Garnbret",
                    gender=1, country="SLO", birthday="1999-03-12")
```

`update_*` methods use a whitelist of allowed field names; unknown keys are
silently ignored. Calling with no recognized fields is a no-op.

## Results

```python
repo.upsert_result(competition_id=comp_id, athlete_id=athlete_id, rank=1)
repo.delete_results_for_competition(comp_id)
```

`upsert_result` uses `INSERT OR REPLACE`. The `delete + reinsert` pattern is
how `competitions.hydrate` handles ranking changes — see
[ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md).

## Transactions

Default behavior: every method commits before returning. To group a
multi-step operation atomically:

```python
with repo.transaction():
    repo.delete_results_for_competition(comp_id)
    for entry in ranking:
        athlete_id = repo.upsert_athlete_skeleton(entry["athlete_id"])
        repo.upsert_result(competition_id=comp_id, athlete_id=athlete_id, rank=entry["rank"])
    repo.mark_fetched("competitions", comp_id)
```

The block commits on clean exit, rolls back on any exception. Nested
`transaction()` blocks are flattened — only the outermost commits.

This is the **only** place in the package that uses a transaction. Use it
when a multi-step write must land or roll back as a unit; default to
per-call commits everywhere else.

## Backfill helpers

```python
repo.backfill_event_country_for_row(event_id, "FRA")            # single row
affected = repo.backfill_event_country_from_siblings()           # cross-batch
```

The cross-batch backfill fills NULL country on events whose city appears
on a sibling row with a known country. One SQL pass; uses `MAX()` to pick
deterministically when multiple sibling countries exist. Returns the
number of rows affected.

## Constants

```python
from wcl_data.db.repository import HYDRATABLE_TABLES, ALL_TABLES, TS_FMT, utcnow

HYDRATABLE_TABLES       # ("seasons", "season_leagues", "events", "competitions", "athletes")
ALL_TABLES              # HYDRATABLE_TABLES + ("leagues", "disciplines", "categories", "results")
TS_FMT                  # "%Y-%m-%dT%H:%M:%SZ"
utcnow()                # current UTC stamp in TS_FMT
```

Use these instead of hardcoding table names or format strings. Table-name
arguments to generic methods (`count`, `count_hydrated`, `mark_fetched`,
`find_stale`) are validated against these tuples and raise `ValueError`
for anything else.
