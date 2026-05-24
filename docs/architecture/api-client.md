# API client

`src/wcl_data/api/client.py` — a thin streaming HTTP client over `requests`.
Its only job is to fetch a batch of paths concurrently and yield each result
as soon as it arrives, so the caller can commit to disk immediately rather
than waiting for the whole batch.

Everything else (URL construction, parsing, retry decisions specific to a
fetcher) lives in the per-fetcher modules. The client itself knows nothing
about seasons, athletes, or the SQLite schema.

## Streaming, not batching

The public entry points are generators:

```python
def stream(endpoint, ifsc_ids, *, max_retries=2, retry_delay=2.0, retry_on=None)
    -> Iterator[Fetched[int]]
def stream_paths[K: Hashable](items: Iterable[tuple[K, str]], *, ...) -> Iterator[Fetched[K]]
```

A `Fetched[K]` is a frozen dataclass with `(key, path, data)`: `key` is
whatever the caller passed in (typically an `ifsc_id`, but for competitions
it's the row PK because the path needs both event and competition ifsc_ids).
`data` is the parsed JSON dict.

Internally, `stream_paths` submits all the work to a `ThreadPoolExecutor`,
then iterates `as_completed(futures)`, yielding each `Fetched` the instant
its future resolves:

```python
with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
    future_to_item = {pool.submit(self._fetch_one, path): (key, path) for key, path in pending}
    for future in as_completed(future_to_item):
        ...
        yield Fetched(key=key, path=path, data=data)
```

The yield is what makes streaming work: the calling fetcher receives one
result at a time and commits to SQLite between iterations. A `Ctrl-C` loses
only the in-flight row, never the batch. See
[ADR 0002](../decisions/0002-streaming-writes.md) for the design rationale.

## Concurrency

`max_workers` (default 50, overridable per command with `--workers` or
globally with `WCL_MAX_WORKERS`) controls both:

1. **Thread pool size** — how many simultaneous `_fetch_one` calls.
2. **urllib3 connection pool size** — the `HTTPAdapter` is mounted with
   `pool_connections=max_workers, pool_maxsize=max_workers, pool_block=False`.

The pool sizing matters: with default urllib3 settings (10 connections) and 50
threads, you'd get a `Connection pool is full, discarding connection`
warning storm and effectively serialize most requests. The adapter is
configured at session construction in `APIClient.__init__`.

`pool_block=False` means threads that race past the pool cap open transient
connections rather than blocking. Useful headroom but rarely triggers in
practice.

The session is shared across all threads. `requests.Session` is documented as
thread-safe for GET; we never do anything else.

## Retry policy

Retries are **selective** — see [ADR 0003](../decisions/0003-selective-4xx-skip-retry.md)
for the full motivation. The short version:

| Failure                          | Treated as | Retry? |
|----------------------------------|------------|--------|
| Transport error (DNS, conn, …)   | Transient  | yes    |
| HTTP 5xx                         | Transient  | yes    |
| HTTP 4xx (404 in particular)     | Permanent  | **no** |

This is encoded in `_default_retry_on`:

```python
def _default_retry_on(exc: FetchError) -> bool:
    if exc.status_code is None:   # transport
        return True
    return exc.status_code >= 500
```

`stream_paths` accumulates the items that failed-and-should-retry, sleeps
`retry_delay` (default 2s), and re-submits them up to `max_retries` (default
2) more times. Each individual failure (including 4xx drops on the first
attempt) is logged at WARNING; the final post-`max_retries` give-up is
logged at ERROR. After the final attempt, surviving failures are dropped —
the caller never sees them.

**Why 4xx is permanent:** seasons discovery probes IDs that may not exist
(`/seasons/47` when the highest is 42). The World Climbing API returns 404 for those.
Retrying each 404 twice with a 2-second sleep would burn ~6 seconds per
non-existent ID on every `pull-new`. Treating 4xx as permanent makes the
probe nearly free.

**Caller override:** `retry_on` is a `Callable[[FetchError], bool]`. If a
fetcher needs a different policy (e.g. retry on 403 once because the cookie
is being refreshed) it can pass its own predicate. Nothing currently does.

## Errors

`FetchError(msg, *, status_code=None)` — raised by `_fetch_one` on any
non-200 or any `requests.RequestException`. Carries the HTTP status code (or
`None` for transport errors) so the retry predicate can branch on it.

Fetcher-level parse exceptions (KeyError, ValueError, etc.) are caught
**inside the fetcher**, not the client. Each fetcher's loop does:

```python
try:
    ... parse + write ...
    ok += 1
except Exception as exc:
    log.exception("Failed to parse %s: %s", fetched.path, exc)
    fail += 1
```

So one malformed row never aborts the batch.

## What the client does *not* do

- **It doesn't open or write to the DB.** The repository owns all SQL.
- **It doesn't know about endpoints.** Paths are caller-supplied; the only
  hardcoded constant is `API_BASE_URL`.
- **It doesn't authenticate.** Credentials are baked into the session headers
  at construction from `Settings.api_headers`. Refreshing the CSRF token /
  cookie is the `auth` CLI command's job (`src/wcl_data/api/credentials.py`).
- **It doesn't rate-limit.** The World Climbing API has never returned 429 in practice,
  so there's no client-side throttle. If it ever does, add a 429-aware
  predicate to `retry_on` rather than a global rate limiter.
- **It doesn't deduplicate.** Callers must pass unique IDs. The current
  fetchers all build their work list from a `dict[ifsc_id, row_id]`, so this
  is implicit.

## When to touch this file

- Adding a new failure mode to the retry policy → update `_default_retry_on`
  and document the change in a new ADR.
- Adding rate limiting / backoff → wrap `_fetch_one`. Don't add it to
  `stream_paths` — the retry loop already handles backoff for failed items.
- Changing the worker count default → update `config.Settings.max_workers`
  and `.env.example`, not the client.
