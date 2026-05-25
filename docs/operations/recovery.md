# Recovery

The package is designed so almost every failure recovers by **re-running
the same command**. This page documents the procedures and the design
guarantees behind them.

## Killed mid-run (Ctrl-C, OOM, terminal close)

**Procedure:** re-run the same command.

```bash
python -m wcl_data pull-new          # was killed → just run it again
```

**Why this works:** every successful row commits before the next request
goes out (per-row `commit()` in `Repository._maybe_commit`, see
[ADR 0002](../decisions/0002-streaming-writes.md)). When the process
dies, the in-flight HTTP request loses its data, but every row written
before it is durable. On the next run, `find_stale` returns the killed
row (NULL `last_fetched_at`) plus anything else still stale; the run
resumes from there.

You'll see the in-progress-when-killed row hydrated on the second pass.

**Don't** delete the database to "start clean" — that throws away
everything that was successful. Re-running is the right move.

## Run finished but `failed` count > 0

`refresh` / `pull-new` print a summary like:

```
entity               hydrated     failed
seasons                    38          0
season_leagues            450          0
events                   1401          0
competitions             5824          1
athletes                14922          0
```

A non-zero `failed` means a row's *parse* threw an exception (the network
fetch succeeded, but the response didn't match the parser's expectations).
The fetcher caught the exception, incremented the counter, and continued
— the rest of the batch is intact.

**Procedure:**

1. Find the failing row in `logs/wcl-data.log`:

   ```bash
   grep -A 5 "Failed to parse" logs/wcl-data.log | tail -30
   ```

2. The traceback identifies the path (`/events/1462`, etc.). Fetch that
   payload manually to inspect:

   ```python
   from wcl_data.config import load_settings
   from wcl_data.api.client import APIClient
   c = APIClient(load_settings())
   print(next(iter(c.stream("events", [1462]))).data)
   ```

3. If the failure is a known API quirk, patch the relevant fetcher (and
   add a fixture + test).
4. If it's a transient API blip, just re-run — the row stayed stale
   because `mark_fetched` never ran.

## Competition results look partially broken

Competitions hydration uses `delete + reinsert` for results, inside a
transaction. If a parse fails mid-loop, the transaction rolls back — the
competition keeps its prior results and its prior `last_fetched_at`,
unchanged. See [ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md).

So **partial result rows shouldn't exist** under normal failure. If they
do, the database may be in an unexpected state — back it up
([backup.md](backup.md)) and investigate before re-hydrating.

## Silent dropped rows (4xx storm)

The client treats 4xx as permanent and silently drops rows (see
[ADR 0003](../decisions/0003-selective-4xx-skip-retry.md)). A run that
finishes with 0 failures but a `status` showing hydration coverage well
below total → check the log for WARNING-level 4xx drops:

```bash
grep "HTTP 4" logs/wcl-data.log | tail
```

Patterns:

- **One ID returning 404 every run:** likely a permanently deleted World Climbing
  row. Known case: athlete `ifsc_id = 12334`. Document and ignore.
- **A burst of 401 / 403:** the session cookie expired. Run
  `python -m wcl_data auth` ([auth.md](auth.md)) and re-run.
- **A range of contiguous 404s during seasons probe:** the lookahead
  walked into unallocated IDs. Expected — no action needed.

## Schema looks wrong

The schema is rebuilt idempotently on every `open_db` via
`apply_schema()`. Missing tables or indexes can be restored by running:

```bash
python -m wcl_data init
```

If you've manually edited the schema, your edits may conflict with the
script's `CREATE TABLE IF NOT EXISTS` (silently skipped) or
`CREATE INDEX IF NOT EXISTS` (same). The package never *drops* anything
— so adding columns by hand will leave them in place but they won't be
populated.

For a real schema change, edit `src/wcl_data/db/schema.py`, increment
`CURRENT_VERSION`, and (eventually) add a migrations folder. As of now
there's no migration framework — see
[`../architecture/database-and-schema.md`](../architecture/database-and-schema.md).

## Total reset (nuclear option)

To start completely fresh:

**PowerShell (Windows):**

```powershell
Remove-Item data\wcl.sqlite
python -m wcl_data init
python -m wcl_data refresh
```

**bash:**

```bash
rm data/wcl.sqlite
python -m wcl_data init
python -m wcl_data refresh
```

This rebuilds the warehouse from scratch (~45-90 min — `pull-new` alone
won't backfill historical seasons or the per-round tables). Use only when
you suspect schema-level corruption or want to validate the cold-start
path. **Always back up first** (see [backup.md](backup.md)) if you're not
100% sure.

## What recovery does *not* cover

- **Corrupted SQLite file** (machine crash mid-write). SQLite's WAL
  recovery handles most of this automatically; for the rest, restore
  from a backup.
- **The World Climbing API changing structure.** No recovery procedure — the
  parser will start throwing parse failures (see "Run finished but
  failed count > 0" above). Patch the fetcher.
- **`.env` lost or corrupted.** Re-run `python -m wcl_data auth` to
  regenerate the credential lines. Other variables (`WCL_DB_PATH`,
  `WCL_MAX_WORKERS`, …) can be re-copied from `.env.example`.
