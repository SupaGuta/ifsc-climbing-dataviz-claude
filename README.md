# World Climbing Lab — Layer 0 (ingestion)

World Climbing Lab is a personal climbing-competition analytics project. This package (`wcl-data`) is the ingestion layer: a small Python package that pulls the public competition API at [ifsc.results.info](https://ifsc.results.info) into a single local SQLite warehouse. Downstream consumers (notebooks, future analytics, future ML) read from the warehouse — they never talk to the API directly.

> The international climbing federation rebranded from **IFSC** (International Federation of Sport Climbing) to **World Climbing** in 2026. The technical API endpoint and historical naming inside the code still use "IFSC" — both terms refer to the same federation and the same data.

> For contributor & architecture documentation (the **why** behind the design), see [`docs/`](docs/).

## What it does

- **Discovers** new entities (seasons, events, athletes, …) by walking the API tree.
- **Hydrates** each row with its full profile data (names, dates, rankings, locations).
- **Tracks staleness** per row via `last_fetched_at`, so subsequent runs only re-fetch rows that are NULL or older than the configured threshold. The default is 30 days.
- **Commits streaming** — every successful fetch is written to disk immediately. A `Ctrl-C` mid-run only loses the in-flight row, not the batch.
- **Retries intelligently** — 5xx and transport errors retried (default twice with 2s delay); 4xx treated as permanent so the discovery probe doesn't waste time on non-existent IDs.

## Setup

Requires **Python 3.12+** (the package uses PEP 695 generic syntax).

```bash
pip install -e ".[dev]"
cp .env.example .env
python -m wcl_data auth        # auto-populate WCL_CSRF_TOKEN + WCL_SESSION_COOKIE
```

The `auth` command fetches a fresh CSRF token + session cookie from ifsc.results.info and writes them to `.env`. Re-run it whenever the API starts returning 401/403. (You can also paste credentials manually from DevTools if you prefer.)

The other no-credentials commands are `init`, `status`, and `export` — they don't hit the API.

**Learn by doing:** a four-part Jupyter walkthrough lives in [`notebooks/`](notebooks/) and covers setup, the data model, the Python API, and querying/exporting. Install the optional extras with `pip install -e ".[notebook]"`, then open `notebooks/00_setup_and_first_crawl.ipynb`.

## Decision guide

| Want to … | Run |
|-----------|-----|
| Populate `.env` with fresh credentials | `auth` |
| First-time DB setup (full backfill) | `init` then `refresh` |
| Catch newly-published World Climbing content | `pull-new` |
| Refresh stale rows on the 30-day cadence | `refresh` |
| Force-refresh everything from scratch | `refresh --stale-days 0` |
| Touch one entity only | `hydrate <entity>` |
| See what's in the DB | `status` |
| Get CSV dumps for downstream tools | `export` |

## Global flag

| Flag | Description |
|------|-------------|
| `-v`, `--verbose` | Keep WARNING-level log lines on the console. Default behaviour hides them (they always go to `logs/wcl-data.log`). Place before the subcommand: `python -m wcl_data -v refresh`. |

## Commands

### `init`

Create the SQLite warehouse schema at `data/wcl.sqlite`. Idempotent — running it on an existing DB verifies the tables/indexes exist but never deletes data.

```bash
python -m wcl_data init
```

No arguments, no options. Use it once per machine.

---

### `auth`

Fetch a fresh CSRF token + session cookie from `https://ifsc.results.info` and write them into `.env`. No DevTools, no manual copy-paste.

```bash
python -m wcl_data auth [--dry-run] [--env-file PATH]
```

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dry-run` | flag | off | Print the fetched values (truncated to first 16 chars) without writing `.env`. Rerun without `--dry-run` to write the full values to `.env`. |
| `--env-file PATH` | path | `<repo>/.env` | Target file for the update. |

**Behaviour:** preserves every other line in `.env` (comments, other variables, ordering). Only the `WCL_CSRF_TOKEN` and `WCL_SESSION_COOKIE` lines are replaced in place. If either key is missing, it's appended.

**Examples:**

```bash
python -m wcl_data auth                 # fetch + write to .env
python -m wcl_data auth --dry-run       # just print, don't touch .env
python -m wcl_data auth --env-file /tmp/alt.env
```

**When to use:** first-time setup, or whenever `refresh` / `pull-new` / `hydrate` start failing with 401/403. The World Climbing session cookie typically lasts a few months.

---

### `refresh`

Discover new entities (probes for new seasons) then hydrate stale rows across the whole entity graph: seasons → season_leagues → events → competitions → athletes. The everyday command.

```bash
python -m wcl_data refresh [--limit N] [--stale-days N] [--workers N]
```

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--limit N` | int | unlimited | Cap the number of rows hydrated **per entity**. Useful for smoke tests. |
| `--stale-days N` | int | `WCL_STALE_DAYS` (env, default 30) | Re-fetch rows whose `last_fetched_at` is NULL or older than N days. `0` forces a re-fetch of everything. |
| `--workers N` | int | `WCL_MAX_WORKERS` (env, default 50) | Concurrent HTTP workers. Useful range: 50–100. |

**Examples:**

```bash
python -m wcl_data refresh                   # standard cadence: anything stale (>30 days)
python -m wcl_data refresh --limit 20        # smoke-test: 20 rows per entity
python -m wcl_data refresh --stale-days 0    # nuclear: force-refresh everything (~45-90 min)
python -m wcl_data refresh --workers 100     # push concurrency higher
```

**Limitation:** `refresh` won't re-discover children of recently-fetched parents (a new event added to a season hydrated yesterday won't appear for another 29 days). Use `pull-new` to catch those.

---

### `pull-new`

Catch all newly-published World Climbing content (new events, competitions, athletes appearing in recent results) without re-fetching ancient containers or re-hydrating the ~15k existing athlete profiles. Only **ongoing** containers (current-year seasons, events within 15 days of `date_end`, plus their descendants) are re-fetched; only newly-discovered athlete skeletons get hydrated.

```bash
python -m wcl_data pull-new [--limit N] [--workers N] [--grace-days N]
```

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--limit N` | int | unlimited | Cap rows touched per entity. |
| `--workers N` | int | `WCL_MAX_WORKERS` (default 50) | Concurrent worker count override. |
| `--grace-days N` | int | `WCL_GRACE_DAYS` (default 15) | Days past an event's `date_end` during which it's still re-fetched. `0` = strict (ended = frozen). |

**Examples:**

```bash
python -m wcl_data pull-new                # catch all new content (~30-60s on a steady-state warehouse)
python -m wcl_data pull-new --workers 75   # push concurrency
python -m wcl_data pull-new --limit 10     # smoke test
python -m wcl_data pull-new --grace-days 30   # more forgiving for late corrections
```

**Why this exists:** `refresh --stale-days 0` would also catch new content but takes ~45-90 min because it re-fetches every athlete profile *and* every historical container (including all per-round data introduced in ADR 0007). Athlete profile data almost never changes; ended seasons/events never gain new structural children. `pull-new` skips both kinds of waste. See [docs/decisions/0006-ongoing-only-pull-new.md](docs/decisions/0006-ongoing-only-pull-new.md) for the design rationale.

---

### `hydrate`

Hydrate one entity only. Same staleness logic as `refresh`, but scoped to a single table.

```bash
python -m wcl_data hydrate <entity> [--limit N] [--stale-days N] [--workers N]
```

**Positional argument:**

| Name | Choices |
|------|---------|
| `entity` | `seasons`, `season_leagues`, `events`, `competitions`, `athletes` |

**Options:** identical to `refresh` (`--limit`, `--stale-days`, `--workers`).

**Examples:**

```bash
python -m wcl_data hydrate athletes                       # refresh stale athlete profiles
python -m wcl_data hydrate competitions --stale-days 0    # re-fetch every competition's rankings
python -m wcl_data hydrate seasons --limit 5              # smoke test
```

**Discovery note:** `hydrate <entity>` doesn't *discover* new entities of that type — it only refreshes rows that already exist. Discovery happens by hydrating the parent (e.g. new athletes appear when you hydrate competitions). The lone exception is `hydrate seasons`, which also runs the seasons-probe discovery as a side effect of going through `refresh_orchestrator.hydrate_entity`.

---

### `status`

Print row counts and hydration coverage for every table. Doesn't hit the API.

```bash
python -m wcl_data status
```

**Output shape:**

```
DB: /path/to/data/wcl.sqlite
table                      rows   hydrated
seasons                      38         38
leagues                      15          -
season_leagues              450        450
…
```

`hydrated` shows how many rows have a non-NULL `last_fetched_at`. Tables without that column show `-`.

---

### `export`

Dump pre-joined SQL views to timestamped CSV files in `data/exports/`. Each CSV is self-contained — no need to follow foreign keys.

```bash
python -m wcl_data export [view] [--output-dir PATH]
```

**Positional argument:**

| Name | Choices | Default |
|------|---------|---------|
| `view` | `seasons`, `leagues`, `events`, `competitions`, `athletes`, `cup_rankings`, `results`, `round_results`, `ascents` | (omit to export the 8 non-bulky views — `ascents` is opt-in) |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--output-dir PATH` | path | `data/exports/` | Directory to write CSVs into. Created if missing. |

**Filename pattern:** `<view>_<UTC>.csv`, e.g. `results_2026-05-22T185030Z.csv`. Re-running never overwrites a prior export.

**Examples:**

```bash
python -m wcl_data export                         # all eight default views to data/exports/
python -m wcl_data export results                 # only the big denormalized view
python -m wcl_data export ascents                 # opt-in: ~900k rows × 31 columns (~200 MB)
python -m wcl_data export athletes --output-dir /tmp/csv
```

**Views:**

| View | Default? | What's in it |
|------|:--------:|--------------|
| `seasons` | ✓ | `season_ifsc_id`, `year`, `last_fetched_at` |
| `leagues` | ✓ | `league_id`, `name` |
| `events` | ✓ | event details with `season_year`, `league_name`, `city`, `country`, `date_start`, `date_end`, `is_paraclimbing` |
| `competitions` | ✓ | one row per (event × discipline × category) with `discipline`, `category`, `gender` resolved to names |
| `athletes` | ✓ | one row per athlete with `firstname`, `lastname`, `gender` (as `"male"`/`"female"`), `height`, `arm_span`, `birthday`, `city`, `country`, `country_iso3`, federation fields, `paraclimbing_sport_class` + status + review date, speed-PB fields |
| `cup_rankings` | ✓ | one row per (athlete × cup × discipline) with `cup_name`, `season`, `discipline`, `rank` — season-end overall standings |
| `results` | ✓ | one row per (athlete × competition) with `event_name`, `season_year`, `league_name`, `event_city`, `event_country`, `event_date`, `discipline`, `category`, `gender`, athlete name/country, `rank` — everything pre-joined |
| `round_results` | ✓ | per-round breakdown: one row per (athlete × round) with `round_name`, `round_kind`, `round_format`, `round_rank`, `round_score`, `starting_group` plus the same event/discipline context as `results` |
| `ascents` | opt-in | **the big one** — one row per (athlete × route × stage) with all discipline-specific columns (`top`/`plus`/`time_ms`/`zone`/`points` …). ~900k rows, ~200 MB CSV; pass explicitly. |

## Environment variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `WCL_CSRF_TOKEN` | (required) | `X-Csrf-Token` header from ifsc.results.info DevTools. |
| `WCL_SESSION_COOKIE` | (required) | `Cookie` header value. |
| `WCL_REFERER` | `https://ifsc.results.info` | Sent as `Referer` header. |
| `WCL_MAX_WORKERS` | `50` | Default concurrent worker count. Overridable per-command with `--workers`. |
| `WCL_CONNECT_TIMEOUT` | `5` | TCP/TLS connect timeout in seconds. Fast-fails on a stalled handshake without holding up the worker pool. |
| `WCL_READ_TIMEOUT` | `120` | Per-request read timeout in seconds. Larger because a single big-event payload (~5 MB) can take seconds to stream. |
| `WCL_DB_PATH` | `data/wcl.sqlite` | Where the warehouse lives. Relative paths resolve against the repo root. |
| `WCL_STALE_DAYS` | `30` | Default staleness threshold for `refresh` / `hydrate`. Overridable with `--stale-days`. |
| `WCL_GRACE_DAYS` | `15` | Days past an event's `date_end` during which `pull-new` still treats it as ongoing. Catches late result corrections. Overridable with `--grace-days`. |

## Data model

Single SQLite file at `data/wcl.sqlite`, schema defined in `src/wcl_data/db/schema.py`:

**Base tables (10):**

| Table          | Purpose                                                  | Hydratable |
|----------------|----------------------------------------------------------|:----------:|
| `seasons`      | Year + ifsc_id                                           |     ✓      |
| `leagues`      | League names (World Cup, etc.)                           |            |
| `season_leagues` | (season × league) join, drives event discovery         |     ✓      |
| `disciplines`  | Lead / speed / boulder / combined / boulder&lead         |            |
| `categories`   | Men / Women / Youth A Male / paraclimbing classes / …    |            |
| `events`       | Competition events with city, country, dates             |     ✓      |
| `competitions` | (event × discipline × category) triples                  |     ✓      |
| `athletes`     | Athlete profiles (name, country, height, birthday, …)    |     ✓      |
| `results`      | (competition × athlete × rank) — derived from competitions hydration | |
| `cup_rankings` | (athlete × cup × discipline × rank) — derived from athletes hydration (ADR 0009) | |

**Per-round tables (6, added in ADR 0007 — populated as a side effect of `competitions` hydration):**

| Table             | Purpose                                                            | Hydratable |
|-------------------|--------------------------------------------------------------------|:----------:|
| `category_rounds` | One row per round (qualif / semi / final) inside a competition.    |            |
| `round_stages`    | Sub-divisions of a round: combined sub-stages, speed-final heats; `seq=0` default stage for simple rounds. | |
| `routes`          | One row per (round, route). Speed-final lanes reuse routes — keyed by IFSC route id. |            |
| `round_results`   | One row per (round, athlete) with `rank` / `score` / `starting_group`. | |
| `stage_results`   | One row per (stage, athlete). Mirrors `round_results` for simple rounds; distinct for combined / speed-final. | |
| `ascents`         | Wide table: one row per (stage × athlete × route) with discipline-specific columns (`top`, `plus`, `time_ms`, `zone`, `points`, …). | |

"Hydratable" tables carry a `last_fetched_at` column. Run `hydrate <entity>` or `refresh` to refresh stale rows.

## Project layout

```
src/wcl_data/
├── api/client.py            # Streaming HTTP client (concurrent + selective retry)
├── db/
│   ├── schema.py            # DDL + apply_schema()
│   └── repository.py        # Typed CRUD per entity + transaction() context manager
├── fetchers/
│   ├── seasons.py           # /seasons/{id}        → seasons + leagues + season_leagues
│   ├── season_leagues.py    # /season_leagues/{id} → disciplines, categories, event skeletons
│   ├── events.py            # /events/{id}         → event details + competition skeletons
│   ├── competitions.py      # /events/{ev}/result/{c} → results + athlete skeletons
│   ├── athletes.py          # /athletes/{id}       → athlete profiles
│   └── refresh.py           # Orchestrator (refresh_all, pull_new)
├── parsers/event_location.py    # Heuristic event-name → (city, ISO3 country)
├── exporter.py              # Denormalized CSV exports
├── cli.py                   # argparse entry point
├── config.py                # Settings loaded from .env
└── logging_setup.py         # Coloured console + WARNING-filtered logging
tests/
├── fixtures/                # Captured JSON API samples for offline tests
└── test_*.py                # pytest
```

## Tests

```bash
pytest -q
```

140 tests covering: the event-location parser (incl. ISO3 validation, IFSC/IOC variant acceptance, the "Event - Country" no-paren fallback, the city dictionary fallback for historical UIAA rows, and the `to_iso3` IFSC→ISO3 normalization), the streaming API client (mocked transport, retry policy, give-up semantics, retry non-duplication), the repository (upsert idempotency, transaction commit/rollback, stale-detection boundary, table-name validation, country backfill), the per-fetcher parse logic (athletes — including federation, speed-PB, paraclimbing sport-class, and cup-rankings expansion; events; competitions including per-round transactional rollback and the four discipline shapes — lead / speed / boulder / combined), the `pull-new` ongoing-only filter, and the CSV exporter (all views, join correctness, `country_iso3` columns, filename format, edge cases).

## Notes / known limits

- The athletes-level paraclimbing flag is `paraclimbing_sport_class IS NOT NULL` (heuristic — a paraclimber without a sport-class assignment reads as NULL). For authoritative per-competition status, cross-check against `events.is_paraclimbing` via the results join. The v3-era `athletes.is_paraclimbing` bool was dropped in v4 (ADR 0009).
- The session cookie in `.env` is not refreshed automatically; refresh manually when the API starts 401-ing.
