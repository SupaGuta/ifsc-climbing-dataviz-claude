# Contributing

## Dev setup

```bash
git clone <repo>
cd ifsc-climbing-dataviz-claude
python -m venv .venv          # 3.12+ required (PEP 695 generic syntax: `class Fetched[K: Hashable]:`)
source .venv/bin/activate     # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"       # adds pytest + pytest-mock
cp .env.example .env
python -m ifsc_data auth      # fetch fresh CSRF + cookie into .env
python -m ifsc_data init      # create data/ifsc.sqlite
python -m ifsc_data pull-new  # populate it (~3–5 min)
```

For notebook work add `pip install -e ".[notebook]"` (jupyterlab + pandas).

## Running tests

```bash
pytest -q
```

The suite is fast (mocked transport for the HTTP client, in-memory SQLite for
the repository). One-line invocations:

```bash
pytest -q tests/test_db_repository.py             # just the repository
pytest -q tests/test_fetchers/ -k athletes        # one fetcher
pytest -q -x --pdb                                # stop on first failure, drop into pdb
```

Fixtures (captured JSON API responses) live in `tests/fixtures/`. Add a new
fixture whenever you add a fetcher — the test should not hit the live API.

## Code conventions

- **Python 3.11+ syntax.** PEP 695 generics (`def stream[K](...) -> Iterator[Fetched[K]]`)
  are used in `api/client.py`; keep them where they fit, don't backport for its own sake.
- **`from __future__ import annotations`** at the top of every module that
  uses forward references. The existing files all do this; copy the pattern.
- **`log = logging.getLogger(__name__)`** at module level, not inside
  functions.
- **No emojis in source or docs** unless explicitly requested.
- **Docstrings on modules** explaining purpose + key design choices. One-liners
  on public functions. Don't write multi-paragraph docstrings.
- **Comments explain *why*, not *what*.** Default to no comment. The
  existing modules are a good reference for the bar.
- **Type hints throughout.** `Optional[X]` is fine; we don't require `X |
  None` syntax. Pyright runs clean — see `git log` for the type-cleanup
  commit if you trip a new diagnostic.

## How to add a new fetcher

This is the most common extension. Use `src/ifsc_data/fetchers/athletes.py`
as the canonical example — it's the simplest of the five.

The full checklist:

1. **Schema** — add a `CREATE TABLE` + `CREATE INDEX idx_<table>_last_fetched`
   to `src/ifsc_data/db/schema.py`. Use `INTEGER PRIMARY KEY` for the local
   ID and `INTEGER UNIQUE NOT NULL` for `ifsc_id`. Include
   `last_fetched_at TEXT` if the entity should be hydratable.

2. **Repository** — add to `src/ifsc_data/db/repository.py`:
   - `upsert_<entity>_skeleton(self, ifsc_id, **parent_ids) -> int` — for
     cascade discovery from the parent fetcher.
   - `update_<entity>(self, row_id, **fields) -> None` — uses the
     allowed-field whitelist pattern (`allowed = {"foo", "bar"}`; ignore
     fields not in the set; abort if no fields).
   - Add the table name to `HYDRATABLE_TABLES` or `ALL_TABLES` at the top
     of the file so `_validate_table` accepts it.

3. **Fetcher** — create `src/ifsc_data/fetchers/<entity>.py`. The expected
   shape (mirrors `athletes.py`):

   ```python
   def hydrate(repo, client, *, stale_days, limit=None) -> tuple[int, int]:
       stale = repo.find_stale("<entity>", stale_days=stale_days)
       if limit is not None:
           stale = stale[:limit]
       if not stale:
           return 0, 0
       ifsc_to_id = {row["ifsc_id"]: row["id"] for row in stale}
       ok = fail = 0
       for fetched in client.stream("<endpoint>", ifsc_to_id.keys()):
           # parse + repo.update_<entity>(...) + repo.mark_fetched(...)
           # try/except Exception inside the loop, increment fail and continue
       return ok, fail
   ```

   Wrap multi-step DB work in `with repo.transaction():` only if you need
   atomicity (see [ADR 0005](decisions/0005-transactional-boundary-on-competitions.md)
   for when that applies).

4. **Parent cascade** — modify the parent fetcher to call
   `repo.upsert_<entity>_skeleton(...)` so the new table gets populated.

5. **Orchestrator** — in `src/ifsc_data/fetchers/refresh.py`:
   - Add the entity to the `ENTITIES` tuple.
   - Import the new module and add it to `refresh_all`, `pull_new`, and
     `hydrate_entity`.

6. **CLI** — `src/ifsc_data/cli.py`'s `hydrate` subcommand reads its
   `choices` from `ENTITIES`, so it picks up the new entity automatically.

7. **Tests** — add a `tests/test_fetchers/test_<entity>.py` with a
   captured-fixture JSON in `tests/fixtures/`. The existing tests are good
   templates.

8. **Export** (optional) — if the entity should appear in CSV dumps, add a
   view to `src/ifsc_data/exporter.py`. Pre-join with parent rows so the
   CSV is self-contained.

9. **Docs** — update the table in
   [architecture/database-and-schema.md](architecture/database-and-schema.md)
   and mention any non-obvious choices in a new ADR.

## Where logs go

- **Console:** INFO and above, colored (via colorama). WARNING is hidden
  unless `-v` / `--verbose` is passed before the subcommand.
- **File:** `logs/ifsc-data.log`, all levels including WARNING. Useful for
  post-mortem on a `pull-new` that quietly dropped rows due to 4xx.

## Asking for help

Open an issue or contact the maintainer. For design questions, link to the
ADR you'd be amending and propose what should change.
