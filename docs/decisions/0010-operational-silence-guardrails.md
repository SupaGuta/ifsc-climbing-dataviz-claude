# 0010 — Operational silence guardrails: auth abort, 429 retry, exit codes

**Status:** Accepted
**Date:** 2026-05-25

## Context

[ADR 0003](0003-selective-4xx-skip-retry.md) shipped a deliberately
narrow retry policy — transport + 5xx only — and explicitly noted two
gaps in its *Consequences*:

1. *"A genuine 403/401 (auth lapsed) is treated as permanent and silently
   dropped. The user gets WARNING-level log lines but no row updates."*
2. *"A 429 (rate limit) would also be dropped. The IFSC API has never
   returned 429 in production. If that changes, add a 429-aware
   predicate and document in a new ADR."*

Both stayed theoretical at v0.1 — but as the warehouse approaches its
first ML-downstream consumer, "X hydrated, Y failed" with Y silently
populated by every-row-401 becomes a load-bearing reliability concern.
A run that exits 0 with the wrong row counts is worse than one that
crashes.

In parallel the CLI's error UX has aged poorly:
`sqlite3.OperationalError: database is locked` dumps a 30-line
traceback; a missing `WCL_CSRF_TOKEN` does the same. Wrapper scripts
and CI jobs can't branch on the underlying cause without parsing
stderr text.

## Decision

Three coordinated changes, all defaults-on:

### 1. Consecutive 401/403 trips `AuthFailureAbort`

`api/client.py` tracks consecutive 401/403 responses across the worker
pool (a single shared counter, reset by any 200). On the fifth
consecutive auth failure the next `_fetch_one` raises
`AuthFailureAbort`, which propagates out of `stream_paths` to the CLI
boundary. The run terminates with a one-line stderr message pointing
at `python -m wcl_data auth` and exits 5.

### 2. 429 is transient with `Retry-After` honored

`_default_retry_on` now returns `True` for 429 in addition to 5xx and
transport. The fixed `time.sleep(retry_delay)` between batches becomes
exponential backoff with jitter: `retry_delay * 2**attempt +
random.uniform(0, min(0.5, retry_delay))`. Per-response `Retry-After`
seconds-int headers are parsed onto `FetchError.retry_after` and the
batch-level inter-attempt sleep is clamped up by the max value seen,
so a server-supplied cooldown is respected even when the local
backoff is smaller.

### 3. Exit-code taxonomy + CLI wrappers

`wcl_data.cli.main` wraps the dispatch in two targeted try/except
blocks: `RuntimeError` from `load_settings` (missing creds) → exit 4
with friendly stderr; `sqlite3.OperationalError` → exit 3 with a
"database is locked" hint when appropriate; `AuthFailureAbort` → exit
5. The full taxonomy (0/1/2/3/4/5) is documented at
[`../cli-cookbook/exit-codes.md`](../cli-cookbook/exit-codes.md).

Defaults: `_AUTH_FAILURE_THRESHOLD = 5`, `_MAX_RESPONSE_BYTES = 50 MB`,
both class-level constants on `APIClient` so subclasses can tune.

## Consequences

**Positive**

- A run that hits rotated credentials mid-batch dies in under a second
  instead of completing with a silent under-fetch. The original
  silent-fail mode ADR 0003 flagged is closed.
- Rate-limited traffic is now first-class — when the upstream starts
  emitting 429s (e.g. a Cloudflare layer in front of `ifsc.results.info`
  during a high-traffic event), the client behaves correctly without
  patching.
- Wrapper scripts / CI jobs can branch on exit code alone
  (`if [ "$code" -eq 4 ]; then …`), no stderr parsing.
- Response-body guards (`Content-Type` startswith
  `application/json`, `Content-Length` ≤ 50 MB) catch the case where
  the upstream returns an HTML interstitial with status 200 — without
  these the parser would have raised `JSONDecodeError` inside a
  `log.exception` hot loop and the user would have seen a confusing
  failure mode several layers from the cause.

**Negative**

- The 5-consecutive-401 threshold is a heuristic. A pathological
  pattern (every 6th request 200, every other 401) could keep the run
  alive while still leaving most rows un-hydrated. In practice 401s
  are all-or-nothing (the cookie either exists or it doesn't), so
  this hasn't been observed. If it surfaces, the threshold drops to
  2 and we revisit.
- Exponential backoff at high concurrency means a global 5xx wave
  produces a longer total run time than the old fixed delay — but
  succeeds where the fixed-delay version would have given up.
- Adding `Content-Length` and `Content-Type` guards to `_fetch_one`
  means a legitimate edge-case payload type (e.g. an API ever serving
  `application/vnd.api+json`) would be rejected. Acceptable for Layer 0
  — `ifsc.results.info` ships only `application/json` today; if that
  changes the guard relaxes to a prefix-set match.

## Alternatives considered

- **Auto-refresh credentials on 401 instead of aborting** — `APIClient`
  now exposes `refresh_credentials()` as an opt-in hook (fetches fresh
  CSRF + cookie, mutates session headers + Settings in-memory). It is
  *not* wired into the default retry path because uncontrolled refresh
  during a run can mask legitimate auth-config bugs (e.g. a stale
  `WCL_REFERER`). Callers who want self-healing behavior can subclass
  or plug into a custom `retry_on`. Rejected as default.
- **Hard fail on first 401 (threshold = 1)** — too brittle: a single
  stale connection in the urllib3 pool would kill the run. Rejected.
- **Per-batch retry budget instead of consecutive counter** —
  considered, but the consecutive-counter pattern is simpler under
  concurrency (one shared int, lock-protected) and captures the actual
  failure mode (sustained, not bursty).

## Post-review refinements (2026-05-25)

A code-review pass on the initial implementation surfaced fifteen findings;
all were addressed in the same merge.

- **Counter reset ordering.** The auth-failure counter now resets only after
  every body guard (Content-Type, Content-Length, oversize stream-cap,
  JSON-decode) passes — resetting on bare `status==200` defeated the abort
  for the exact case the comment said it caught (HTML interstitial on
  expired auth).
- **JSON decode failures are FetchErrors.** `resp.json()` was outside
  `_fetch_one`'s try/except, so a truncated body would escape past
  `stream_paths`'s `except FetchError` and silently terminate the fetcher
  loop. The new code reads the body via `iter_content`, parses with
  `json.loads`, and re-raises any `JSONDecodeError` as a transient FetchError.
- **HTML-200 and oversize-200 are transient.** Both raise FetchError with
  `status_code=None` so `_default_retry_on` retries them (transport-error
  path) rather than silently dropping the row.
- **Streaming body cap.** Chunked-transfer-encoded responses (Cloudflare's
  default) omit Content-Length, so the header-only oversize check was
  effectively no-op for the most common upstream layout. The new code
  streams via `iter_content(chunk_size=64KB)` with a running byte counter.
- **Manual pool lifecycle for fast abort.** `with ThreadPoolExecutor` calls
  `shutdown(wait=True)` on __exit__, which would have made AuthFailureAbort
  wait up to `read_timeout` for in-flight workers. The pool is now managed
  manually with `shutdown(wait=False, cancel_futures=True)` on abort.
- **Exponential-backoff fix.** The first retry now sleeps `retry_delay`
  (not `2*retry_delay`) — the original `(2 ** attempt)` with attempt=1
  on first retry doubled every operator expectation.
- **`Retry-After` capped at 300s.** A misbehaving server could otherwise
  send `Retry-After: inf` and pin the main thread in `time.sleep`.
- **`Retry-After` honored on 200 paths.** Parsing was inside the
  `if status != 200:` branch only — Cloudflare 200+Retry-After interstitial
  pages lost the cooldown signal.
- **`AuthFailureAbort` log uses `exc.threshold`.** Reading
  `self._consecutive_auth_failures` from the main thread races with
  workers (200 resets, 401 increments) and would produce a misleading
  count in the user-facing ERROR line.
- **Float timeouts.** `WCL_CONNECT_TIMEOUT=0.5` (and other sub-second
  values) used to raise an uncaught ValueError; `Settings.connect_timeout`
  and `read_timeout` are now `float`, parsed with `float(...)`.
- **WAL fallback warning.** `open_db` now reads back the result of
  `PRAGMA journal_mode = WAL` and logs a WARNING if SQLite refused
  (CIFS / SMB / some FUSE filesystems).
- **`_cmd_auth` errors → exit 4.** The recovery command for expired
  credentials had its own RuntimeError / HTTPError / OSError paths
  unhandled, producing tracebacks + exit 1. They now route through the
  same `EXIT_AUTH` translation as `load_settings` failures.
- **Non-locked `OperationalError` propagates.** "database disk image is
  malformed" (and other non-lock OperationalErrors) used to be silently
  labelled "another wcl-data process may be running" and exit 3. The
  handler now only translates the actual `locked` case; everything else
  surfaces its traceback.
- **`events.hydrate` backfill in `try/finally`.** Mid-batch
  `AuthFailureAbort` used to skip the post-loop city/country backfill,
  leaving processed events with `city` set but `country=NULL`. The
  backfill now runs even when the for-loop is unwound by an exception.
- **Partial summary on abort.** `refresh_all` / `pull_new` build the
  summary dict incrementally and attach it to `AuthFailureAbort` as
  `partial_summary`; the CLI prints it so the operator sees which
  entities completed before the abort.
