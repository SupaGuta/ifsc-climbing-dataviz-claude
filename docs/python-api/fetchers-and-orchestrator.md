# Fetchers and orchestrator

The orchestrator in
[`src/wcl_data/fetchers/refresh.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/fetchers/refresh.py)
is what the CLI's `refresh`, `pull-new`, and `hydrate` subcommands call
under the hood. You can call it directly from Python when you need finer
control. The individual fetcher modules also expose their `hydrate()`
function for narrowly-scoped work.

For the design (discover-vs-hydrate, three CLI modes) see
[`../architecture/ingestion-pipeline.md`](../architecture/ingestion-pipeline.md).

## Boilerplate

Every snippet below assumes:

```python
from wcl_data.config import load_settings
from wcl_data.db.schema import open_db
from wcl_data.db.repository import Repository
from wcl_data.api.client import APIClient
from wcl_data.fetchers import refresh

settings = load_settings()
conn = open_db(settings.db_path)
repo = Repository(conn)
client = APIClient(settings)
```

## Orchestrator entry points

### `refresh_all`

```python
summary = refresh.refresh_all(repo, client, stale_days=30, limit=None)
# {'seasons': (ok, fail), 'season_leagues': (ok, fail), ...}
```

Walks the full graph (seasons → season_leagues → events → competitions →
athletes) in order, hydrating anything stale or NULL. Returns per-entity
(ok, fail) counts.

This is what the CLI's `refresh` runs. `stale_days=0` re-fetches
everything (~45-90 min, including all per-round data from ADR 0007);
`stale_days=30` is the standard cadence.

### `pull_new`

```python
summary = refresh.pull_new(repo, client, limit=None, grace_days=15)
```

Re-fetches **ongoing** containers only — current-year seasons, events
within `grace_days` of `date_end`, plus their descendants — then hydrates
**only newly-discovered athletes** by passing `stale_days=365_000` (only
NULL `last_fetched_at` matches). The everyday "catch new content cheaply"
entry point. See [ADR 0006](../decisions/0006-ongoing-only-pull-new.md).

`grace_days` defaults to 15. The CLI surface (`--grace-days`) and env var
(`WCL_GRACE_DAYS`) plumb through to this argument.

### `hydrate_entity`

```python
ok, fail = refresh.hydrate_entity(repo, client, "athletes", stale_days=30)
ok, fail = refresh.hydrate_entity(repo, client, "events", stale_days=0, limit=100)
```

Runs one phase only. `entity` must be one of `refresh.ENTITIES` (an alias
for `wcl_data.db.repository.HYDRATABLE_TABLES`):

```python
refresh.ENTITIES        # ("seasons", "season_leagues", "events", "competitions", "athletes")
```

`hydrate_entity("seasons", ...)` also runs `seasons.discover` first
(seasons have no parent endpoint). Entities with a discovery probe are
tracked in `refresh._DISCOVERY_ENTITIES`; the orchestrator dispatches
through `refresh._FETCHER_MODULES[entity].discover(...)` so a future
entity that gains a `discover()` callable is wired in by adding its name
to both `_FETCHER_MODULES` and `_DISCOVERY_ENTITIES`.

## Per-fetcher entry points

Each fetcher module exposes a `hydrate(repo, client, *, stale_days=None,
rows=None, limit=None)` with identical signature (the `rows=`/`stale_days=`
resolution is centralized in `wcl_data.fetchers._common.resolve_rows`);
`seasons` additionally exposes `discover`. Pass either `stale_days=`
(default — fetcher calls the canonical stale-rows source for the table) or
`rows=` (caller supplies the work list, which is shape-checked against the
table's expected column set). `pull_new` uses `rows=`; `refresh` /
`hydrate_entity` use `stale_days=`.

```python
from wcl_data.fetchers import seasons, season_leagues, events, competitions, athletes

# Probe for new seasons past MAX(ifsc_id)
seasons.discover(repo, client, lookahead=10)

# Hydrate just one entity, equivalent to refresh.hydrate_entity
ok, fail = athletes.hydrate(repo, client, stale_days=30, limit=None)

# Custom scope (e.g. only ongoing events) via rows=
ok, fail = events.hydrate(repo, client, rows=repo.find_ongoing_events(grace_days=30))
```

Call signature is uniform — pick the level that matches your task.

## Hydrating a hand-picked list

The fetchers' `hydrate` functions read their work list from
`repo.find_stale(...)`. To hydrate a specific arbitrary list of athletes
(e.g. for a research-specific subset), you bypass the fetcher and use the
client + repo directly:

```python
from wcl_data.api.client import APIClient

target_ifsc_ids = [1234, 5678, 9012]
ifsc_to_id = {ifsc: repo.upsert_athlete_skeleton(ifsc) for ifsc in target_ifsc_ids}

for fetched in client.stream("athletes", ifsc_to_id.keys()):
    ath_ifsc = int(fetched.key)
    ath_row_id = ifsc_to_id[ath_ifsc]
    data = fetched.data
    gender_str = (data.get("gender") or "").lower()
    gender = 0 if gender_str == "male" else (1 if gender_str == "female" else None)
    repo.update_athlete(ath_row_id,
                        firstname=data.get("firstname"),
                        lastname=data.get("lastname"),
                        gender=gender,
                        country=data.get("country"))
    repo.mark_fetched("athletes", ath_row_id)
```

This is essentially what `athletes.hydrate` does — copy its loop body
from [`src/wcl_data/fetchers/athletes.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/fetchers/athletes.py)
when you need a starting template.

## Summary printing

The CLI's `_print_summary` from `src/wcl_data/cli.py` is a one-liner
worth lifting if you want the same formatted output:

```python
def print_summary(summary: dict[str, tuple[int, int]]) -> None:
    print(f"{'entity':<20} {'hydrated':>10} {'failed':>10}")
    for entity, (ok, fail) in summary.items():
        print(f"{entity:<20} {ok:>10} {fail:>10}")
```

## When to write a new fetcher

If the IFSC API exposes a new endpoint (e.g. `/judges/{id}`), don't
ad-hoc it — follow the pattern. The full checklist is in
[`../contributing.md`](../contributing.md). Use
[`src/wcl_data/fetchers/athletes.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/fetchers/athletes.py)
as the canonical template: it's the simplest of the five.
