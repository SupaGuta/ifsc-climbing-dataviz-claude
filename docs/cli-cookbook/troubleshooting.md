# Troubleshooting

"I ran X and got Y, what now?" For deeper recovery procedures (killed runs,
schema reset) see [`../operations/recovery.md`](../operations/recovery.md).

## Symptom: every request fails with 401 / 403

**Cause:** the IFSC session cookie has expired. They typically last a few
months.

**Fix:**

```bash
python -m ifsc_data auth
```

This fetches a fresh CSRF token + session cookie and rewrites the two lines
in `.env`. Re-run your command:

```bash
python -m ifsc_data pull-new
```

If `auth` itself fails with `RuntimeError: Could not find <meta
name="csrf-token">`, the IFSC site layout has changed — open an issue.
Full details in [`../operations/auth.md`](../operations/auth.md).

## Symptom: run is much slower than expected

Default is `--workers 50`. The useful range is 50–100.

**Try higher concurrency:**

```bash
python -m ifsc_data pull-new --workers 75
python -m ifsc_data refresh --workers 100
```

Beyond ~100 the IFSC connection limits dominate; you won't see further
speedup and may start collecting 5xx retries.

**Check for retry storms:** open `logs/ifsc-data.log` and grep for `Retry
attempt`. Many retry attempts → upstream is degraded; back off `--workers`
or wait it out.

## Symptom: rows are quietly dropped (counts don't match expectations)

The client treats 4xx as **permanent** and silently drops the row (after a
WARNING). This is intentional — see
[ADR 0003](../decisions/0003-selective-4xx-skip-retry.md) — but it can be
surprising.

**Confirm:**

```bash
grep WARNING logs/ifsc-data.log | tail -20
```

You'll see lines like:

```
WARNING ifsc_data.api.client: Fetch failed for /athletes/12334: HTTP 404 Not Found
```

For the one known permanent 404 (athlete `ifsc_id = 12334`), this is
expected and recurring. For others, investigate — the IFSC ID may have
been deleted or merged on their side.

**To re-attempt a previously-dropped row:** there isn't a built-in retry
command. Either:

- Run `python -m ifsc_data hydrate <entity> --stale-days 0` to re-attempt
  everything in that entity (including the previously-dropped row if its
  skeleton still exists).
- Or delete the skeleton manually:

  ```bash
  sqlite3 data/ifsc.sqlite "DELETE FROM athletes WHERE ifsc_id = 12334;"
  ```

  Note: this also deletes the row's `results` rows via cascade. Only do
  this if the row is genuinely garbage.

## Symptom: a parse failure in the logs

Look for `log.exception("Failed to parse /...")` traceback in
`logs/ifsc-data.log`. The fetcher caught the exception, incremented the
`fail` counter, and continued — the rest of the batch was unaffected.

**Diagnose:**

1. Note the failing path (e.g. `/events/1462`).
2. Fetch the raw JSON yourself:

   ```bash
   python -c "from ifsc_data.config import load_settings; from ifsc_data.api.client import APIClient; s = load_settings(); c = APIClient(s); print(next(iter(c.stream('events', [1462]))).data)"
   ```

   On Windows the default console codepage is `cp1252` and `print()` will
   crash on payloads containing non-ASCII characters (athlete names with
   diacritics, etc.). Either set `PYTHONIOENCODING=utf-8` before invoking,
   or run `chcp 65001` in the same PowerShell session first.

3. Compare against what the parser expects. The fetcher modules are small
   and read top-to-bottom — usually the issue is a missing field or an
   unexpected type.

If the fix is a parser change, add a fixture to `tests/fixtures/` and a
regression test before patching.

## Symptom: `pull-new` finishes but row counts haven't changed

Two possibilities:

1. **Nothing was actually new on the IFSC side** since your last run. Check
   the timestamps in `data/exports/` or `logs/ifsc-data.log`.
2. **You ran `pull-new` instead of `refresh`** and were expecting stale-row
   updates. `pull-new` only re-hydrates the **container** entities and
   discovers new children — existing athlete profiles aren't touched. For a
   periodic full refresh, use `refresh` (default 30-day cadence).

## Symptom: `status` reports `hydrated` far less than `rows`

The run was killed before that table finished hydrating. Skeletons exist;
profiles don't. **Just re-run:**

```bash
python -m ifsc_data refresh           # or pull-new
```

Streaming writes mean previously-hydrated rows stay hydrated; the gap is
filled in on the next pass. Details in
[`../operations/recovery.md`](../operations/recovery.md).

## Symptom: `sqlite3.OperationalError: database is locked`

Another process has the DB open in write mode. Common causes:

- You ran `pull-new` in two terminals at once. SQLite serializes writers;
  the second one will wait then time out.
- A notebook has a long-running write transaction. Restart the notebook
  kernel or close the connection.

The CLI commands close the connection on exit, so this only happens when
two long-running operations overlap.
