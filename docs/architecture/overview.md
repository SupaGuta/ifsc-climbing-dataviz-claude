# Architecture overview

`wcl_data` is **Layer 0** of a larger project: it ingests the World Climbing public API
into a single local SQLite warehouse. Downstream consumers (dashboards,
notebooks, ML pipelines) read from the warehouse — they never talk to the API
directly. This isolates one of the messier parts of the project (a public,
session-authenticated API with weak schema guarantees) behind a stable local
contract: a SQLite file with documented tables.

## Layers, top to bottom

```
┌────────────────────────────────────────────────────────────────────────┐
│  cli.py            argparse entry point                                │
│                      │                                                 │
│                      ▼                                                 │
│  fetchers/refresh.py    orchestrator: pull_new / refresh_all / hydrate │
│                      │                                                 │
│        ┌─────────────┴────────────┬───────────────┬───────────────┐    │
│        ▼                          ▼               ▼               ▼    │
│  fetchers/seasons.py    fetchers/events.py    competitions.py   etc.   │
│                      │                                                 │
│        ┌─────────────┴──────────────┐                                  │
│        ▼                            ▼                                  │
│  api/client.py                db/repository.py                         │
│  (streaming HTTP +            (typed CRUD +                            │
│   selective retry)             transaction() context)                  │
│                                     │                                  │
│                                     ▼                                  │
│                          db/schema.py  →  data/wcl.sqlite             │
└────────────────────────────────────────────────────────────────────────┘
```

- **CLI** (`src/wcl_data/cli.py`) is a thin argparse wrapper that wires up
  `Settings`, opens the DB, and delegates to the orchestrator.
- **Orchestrator** (`src/wcl_data/fetchers/refresh.py`) decides *what* to do
  (`refresh_all`, `pull_new`, `hydrate_entity`) and walks the entity graph in a
  fixed topological order.
- **Per-entity fetchers** (`src/wcl_data/fetchers/{seasons,season_leagues,events,competitions,athletes}.py`)
  own the parse logic for one API endpoint each. They call the HTTP client to
  stream rows in and the repository to write rows out.
- **HTTP client** (`src/wcl_data/api/client.py`) does concurrent, streaming
  fetches with selective retry. See [api-client.md](api-client.md).
- **Repository** (`src/wcl_data/db/repository.py`) is the only thing that
  writes SQL. Every method commits per-row unless wrapped in
  `with repo.transaction():`. See [database-and-schema.md](database-and-schema.md).
- **Schema** (`src/wcl_data/db/schema.py`) is the single source of truth for
  table layout. Idempotent — `apply_schema()` runs on every DB open.

## The entity graph

The API is a tree rooted at *seasons*. Each season lists its leagues and
events; each event lists its competitions; each competition lists its
athletes (via the results array) and their ranks. The package's tables
mirror that **discovery / parent-fetch order**:

```
seasons ──┬── season_leagues ──┐
          │                    ├── events ── competitions ──┬── results
          └────────────────────┘                            ├── athletes ── cup_rankings
                                                            └── category_rounds
                                                                  ├── round_stages
                                                                  ├── routes
                                                                  ├── round_results
                                                                  ├── stage_results
                                                                  └── ascents
```

> **This is a discovery tree, not the FK graph.** A few load-bearing
> schema relationships are not visible above:
> - `athletes` has **no FK to `competitions`** — they connect only through
>   the `results` / `round_results` / `stage_results` / `ascents` join
>   tables. Athletes are *discovered* during competition hydration, not
>   children of competitions in the schema sense.
> - `round_results`, `stage_results`, and `ascents` each carry a direct
>   `competition_id` FK (in addition to their per-round parent). The
>   per-competition wipe path uses `competition_id` directly.
> - `ascents` has 4 NOT NULL FKs (`competitions`, `round_stages`,
>   `routes`, `athletes`) — it's a 4-way join row, not a chain.
>
> See [database-and-schema.md](database-and-schema.md) for the full FK
> reference.

Five tables are **hydratable** (carry `last_fetched_at`): `seasons`,
`season_leagues`, `events`, `competitions`, `athletes`. The rest of the
16-table warehouse falls into two non-hydratable groups:

- **Reference tables** (`leagues`, `disciplines`, `categories`) — tiny,
  rewritten on every parent hydration; no staleness needed.
- **Derived data** wiped + reinserted as a side effect of a parent's
  hydration: `results` (per competition — see
  [ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md));
  the per-round family added in
  [ADR 0007](../decisions/0007-per-round-ingestion.md) (`category_rounds`,
  `round_stages`, `routes`, `round_results`, `stage_results`, `ascents`);
  and `cup_rankings` (per athlete — see
  [ADR 0009](../decisions/0009-athletes-payload-expansion.md)).

See [database-and-schema.md](database-and-schema.md) for the full
table-by-table reference.

Hydration order is **fixed** in `src/wcl_data/db/repository.py`:

```python
HYDRATABLE_TABLES = ("seasons", "season_leagues", "events", "competitions", "athletes")
```

`refresh.ENTITIES` is an alias for this tuple — both names point to the
same object. The canonical edit site is `HYDRATABLE_TABLES`.

Each phase can *create* skeleton rows for the next phase (e.g. hydrating a
season inserts `season_league` and `event` skeletons with NULL
`last_fetched_at`). The downstream phase picks them up because NULL counts as
stale. This is why a fresh DB walks the whole tree top-down on the first run.

## Lifecycle of one `pull-new` invocation

`pull-new` is the everyday command. Here's what happens when you run it:

1. **`cli.main`** parses args, calls `config.load_settings()`, opens the DB
   (which applies the schema), and builds an `APIClient` + `Repository`.
2. **`refresh.pull_new(repo, client)`** runs `seasons.discover` first (to
   probe for any new seasons past the highest known `ifsc_id`), then runs
   each hydration phase in order with `stale_days=0` (re-fetch all
   containers) for seasons → competitions, and `stale_days=365_000` for
   athletes (effectively: NULL only, i.e. only the freshly-discovered ones).
3. For each entity, the phase function (e.g. `events.hydrate`):
   - Calls `repo.find_stale(table, stale_days=...)` to get the work list.
   - Iterates `client.stream(endpoint, ifsc_ids)`, which returns `Fetched[K]`
     tuples as each HTTP request completes — concurrently, up to
     `--workers` (default 50) at a time.
   - For each result, parses the JSON, writes via the repository, and calls
     `repo.mark_fetched(table, row_id)`.
   - On any parse exception: logs `log.exception(...)` and moves on. Network
     failures are handled one layer down by the client's retry loop.
4. Per-row commits mean a `Ctrl-C` mid-run loses *only* the in-flight row.
   Re-running picks up where it stopped because the killed row's
   `last_fetched_at` is still NULL.

A full `pull-new` against a current warehouse touches a few hundred rows
across seasons → events → competitions, hydrates only the brand-new athletes
discovered along the way, and finishes in ~30-60 seconds on a steady-state
warehouse (a few minutes if a backlog of new athletes has accumulated).

## Where to go next

- [ingestion-pipeline.md](ingestion-pipeline.md) — `refresh` vs `pull-new` vs `hydrate`, and why the staleness model produces those three modes
- [api-client.md](api-client.md) — streaming, retry, concurrency
- [database-and-schema.md](database-and-schema.md) — table-by-table reference and the transactional boundary
- [layer-0-surface.md](layer-0-surface.md) — the contract Layer 0 exposes to downstream consumers (stable surface, guarantees, what's deliberately out of scope)
- [parsing-and-heuristics.md](parsing-and-heuristics.md) — where the package guesses, and where it gives up rather than guess
- [../decisions/](../decisions/README.md) — the *why* behind the design choices above
