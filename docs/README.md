# Documentation

Docs for `ifsc_data`. The root [`README.md`](https://github.com/SupaGuta/world-climbing-lab/blob/main/README.md) is the *user*
guide (install, CLI reference, `.env` variables). What lives here is
everything else — the **why** behind the design, the per-table data
reference, recipes for common tasks, the Python API, and operational
procedures.

This tree is append-only. New layers get new folders; existing files are
edited in place as code changes.

## Map

```
docs/
├── README.md                         ← you are here
├── contributing.md                   ← dev setup, tests, conventions, add-a-fetcher walkthrough
├── architecture/                     ← current snapshot of the system (the why)
│   ├── overview.md                   ← system map + lifecycle of one ingest run
│   ├── ingestion-pipeline.md         ← discover → hydrate cycle, refresh vs pull-new vs hydrate
│   ├── api-client.md                 ← streaming client, retry policy, concurrency
│   ├── database-and-schema.md        ← tables, staleness, transactional boundary
│   └── parsing-and-heuristics.md     ← event-name parsing, paraclimbing heuristic, lossy data
├── decisions/                        ← ADR-style record of non-obvious choices
│   ├── README.md                     ← what an ADR is + template
│   ├── 0001-single-sqlite-warehouse.md
│   ├── 0002-streaming-writes.md
│   ├── 0003-selective-4xx-skip-retry.md
│   ├── 0004-incremental-hydration-with-staleness.md
│   ├── 0005-transactional-boundary-on-competitions.md
│   ├── 0006-ongoing-only-pull-new.md
│   └── 0007-per-round-ingestion.md
├── data-dictionary/                  ← per-table column reference (units, ranges, coverage)
│   ├── README.md                     ← conventions, units, recompute-coverage snippet
│   ├── seasons.md
│   ├── season-leagues.md
│   ├── events.md
│   ├── competitions.md
│   ├── athletes.md
│   ├── results.md
│   ├── category-rounds.md            ← phases (qualif/semi/final)
│   ├── round-stages.md               ← speed-final heats + combined sub-stages
│   ├── routes.md                     ← lead routes, speed lanes, boulders
│   ├── round-results.md              ← per-round rank + score
│   ├── stage-results.md              ← per-stage detail
│   ├── ascents.md                    ← per-route performance detail
│   └── reference-tables.md           ← leagues, disciplines, categories
├── cli-cookbook/                     ← task-oriented recipes
│   ├── README.md
│   ├── setup.md                      ← first-time install through first populate
│   ├── daily-use.md                  ← pull-new, refresh, hydrate, smoke tests, concurrency
│   ├── exports.md                    ← export + filter recipes (pandas + sqlite3 CLI)
│   ├── custom-queries.md             ← SQL one-liners for ad-hoc questions
│   └── troubleshooting.md            ← "I ran X and got Y, what now"
├── python-api/                       ← programmatic use (reference, not tutorial)
│   ├── README.md
│   ├── repository.md                 ← Repository class
│   ├── client.md                     ← APIClient class
│   └── fetchers-and-orchestrator.md  ← refresh_all / pull_new / hydrate_entity + per-fetcher entry points
└── operations/                       ← keep-it-healthy procedures
    ├── README.md
    ├── auth.md                       ← IFSC credential rotation, what 401/403 mean
    ├── logs.md                       ← log locations, levels, manual cleanup
    ├── recovery.md                   ← killed runs, partial ingest, schema reset
    └── backup.md                     ← SQLite snapshots, .env hygiene, CSV exports
```

## Where to start by goal

- **"I want to use this thing."** Start with
  [cli-cookbook/setup.md](cli-cookbook/setup.md), then
  [cli-cookbook/daily-use.md](cli-cookbook/daily-use.md). When you need
  to query the data, jump to
  [cli-cookbook/exports.md](cli-cookbook/exports.md) or
  [cli-cookbook/custom-queries.md](cli-cookbook/custom-queries.md).
- **"What's in the database?"** [data-dictionary/](data-dictionary/README.md) —
  one file per table, with coverage stats and gotchas.
- **"I want to script against it."** [python-api/README.md](python-api/README.md)
  → the three reference pages.
- **"Something's broken."**
  [cli-cookbook/troubleshooting.md](cli-cookbook/troubleshooting.md) for
  "I ran X and got Y," [operations/recovery.md](operations/recovery.md)
  for procedure-level recovery.
- **"How do I keep credentials / logs / backups healthy?"**
  [operations/](operations/README.md) — one page per topic.
- **"I want to extend the code."** [contributing.md](contributing.md)
  → [architecture/overview.md](architecture/overview.md) → the relevant
  architecture page → the relevant ADR.
- **"Why is it shaped this way?"**
  [architecture/](architecture/overview.md) for the design,
  [decisions/](decisions/README.md) for the trade-offs each design choice locked
  in.

## Where to start by role

- **New contributor:** [contributing.md](contributing.md) →
  [architecture/overview.md](architecture/overview.md)
- **Trying to extend ingestion:**
  [architecture/ingestion-pipeline.md](architecture/ingestion-pipeline.md)
  → [contributing.md](contributing.md) (add-a-fetcher section)
- **Touching the DB layer:**
  [architecture/database-and-schema.md](architecture/database-and-schema.md)
  + ADRs [0002](decisions/0002-streaming-writes.md),
  [0005](decisions/0005-transactional-boundary-on-competitions.md)
- **Touching the HTTP layer:**
  [architecture/api-client.md](architecture/api-client.md) + ADR
  [0003](decisions/0003-selective-4xx-skip-retry.md)
- **Downstream consumer (notebooks, ML pipeline):**
  [data-dictionary/](data-dictionary/README.md) →
  [python-api/README.md](python-api/README.md)

## Other resources

- [`README.md`](https://github.com/SupaGuta/world-climbing-lab/blob/main/README.md) — user guide / CLI reference
- [`notebooks/`](https://github.com/SupaGuta/world-climbing-lab/tree/main/notebooks) — four-part interactive walkthrough (setup → data model → Python API → querying/exporting)
- Module docstrings — every `.py` opens with one
