# Python API

Programmatic use of `ifsc_data` from a script, notebook, or REPL. Useful
when the CLI's task model doesn't fit — e.g. ingesting only a specific list
of athletes, embedding a hydration step in a larger pipeline, or driving
custom exports.

For interactive walkthroughs see [`notebooks/`](../../notebooks/),
particularly:

- [`02_the_python_api.ipynb`](../../notebooks/02_the_python_api.ipynb) — the
  ground-up tour
- [`03_querying_and_exporting.ipynb`](../../notebooks/03_querying_and_exporting.ipynb)
  — read-mostly use of the warehouse

This folder is a **reference**, not a tutorial. Each page documents one
class or module's public surface with copy-pasteable snippets.

## When to use the Python API vs the CLI

| Use case | Reach for |
|---|---|
| First-time setup, daily refresh, exports to disk | CLI |
| Smoke test before code change | CLI (`--limit`) |
| Hydrate a hand-picked list of athletes | Python API (custom `client.stream` call) |
| Pipe results straight into pandas without round-tripping CSV | Python API |
| Embed ingestion in a notebook cell | Python API |
| Build a custom export view | Python API (new entry in `exporter.VIEWS`) |
| Add a new fetcher | Python API + see [`../contributing.md`](../contributing.md) |

## Reference pages

- [repository.md](repository.md) — `Repository` class: typed CRUD,
  staleness lookup, `transaction()` context
- [client.md](client.md) — `APIClient` class: streaming, retry, concurrency
- [fetchers-and-orchestrator.md](fetchers-and-orchestrator.md) — calling
  `refresh_all` / `pull_new` / `hydrate_entity` programmatically, plus
  individual fetcher entry points

## Setup boilerplate

Most snippets in the reference pages start from this:

```python
from ifsc_data.config import load_settings
from ifsc_data.db.schema import open_db
from ifsc_data.db.repository import Repository
from ifsc_data.api.client import APIClient

settings = load_settings()                  # reads .env
conn = open_db(settings.db_path)            # applies schema if missing
repo = Repository(conn)
client = APIClient(settings)
```

`load_settings()` raises `RuntimeError` if `IFSC_CSRF_TOKEN` /
`IFSC_SESSION_COOKIE` are missing. For read-only / no-API work (queries,
exports), pass `require_credentials=False`:

```python
settings = load_settings(require_credentials=False)
```

For the *why* behind the package's shape see
[`../architecture/`](../architecture/) and the
[ADRs](../decisions/).
