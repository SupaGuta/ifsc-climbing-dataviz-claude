# `APIClient`

Streaming HTTP client over the IFSC API. Lives in
[`src/ifsc_data/api/client.py`](https://github.com/SupaGuta/ifsc-climbing-dataviz-claude/blob/main/src/ifsc_data/api/client.py). For
the design (streaming generator, retry policy, urllib3 pool sizing) see
[`../architecture/api-client.md`](../architecture/api-client.md).

## Constructing

```python
from ifsc_data.config import load_settings
from ifsc_data.api.client import APIClient

settings = load_settings()                  # requires IFSC_CSRF_TOKEN + IFSC_SESSION_COOKIE
client = APIClient(settings)
```

The session is built once at construction time with headers from
`settings.api_headers` and an `HTTPAdapter` sized to
`settings.max_workers`. Reuse the same `APIClient` for the lifetime of a
process — don't reconstruct per request.

## Streaming by ifsc_id

```python
for fetched in client.stream("athletes", [123, 456, 789]):
    print(fetched.key, fetched.path, fetched.data["firstname"])
```

`stream(endpoint, ids)` builds paths of the form `/{endpoint}/{id}`. The
generator yields each `Fetched` *as soon as its HTTP request completes*,
not after the whole batch. The caller controls commit cadence.

## Streaming by arbitrary path

When the URL needs more than one ID — e.g. `/events/{event_id}/result/{comp_id}`
— use `stream_paths`:

```python
items = [(local_comp_id, f"/events/{event_ifsc}/result/{comp_ifsc}")
         for local_comp_id, event_ifsc, comp_ifsc in rows]

for fetched in client.stream_paths(items):
    # fetched.key is the local_comp_id we passed in
    # fetched.path is the full path
    # fetched.data is the parsed JSON
    process(fetched)
```

`stream_paths` is fully generic — `key` can be any `Hashable`, and the
caller decides what to put in it.

## `Fetched[K]`

```python
from ifsc_data.api.client import Fetched

# @dataclass(frozen=True)
# class Fetched[K: Hashable]:
#     key: K               # whatever caller passed in
#     path: str            # the request path
#     data: dict[str, Any] # parsed JSON
```

Frozen so it can live in sets / be used as a dict key if you want to
deduplicate.

## Retry policy

Default: retry transport errors and HTTP 5xx; **4xx is permanent and the
item is silently dropped** (with a WARNING log line). See
[ADR 0003](../decisions/0003-selective-4xx-skip-retry.md).

Defaults: `max_retries=2`, `retry_delay=2.0`.

Override:

```python
for fetched in client.stream("athletes", ids, max_retries=5, retry_delay=1.0):
    ...
```

For a custom retry predicate (e.g. retry 429 once):

```python
from ifsc_data.api.client import FetchError

def retry_on_429_too(exc: FetchError) -> bool:
    if exc.status_code is None:
        return True
    return exc.status_code >= 500 or exc.status_code == 429

for fetched in client.stream("athletes", ids, retry_on=retry_on_429_too):
    ...
```

The predicate takes a `FetchError` and returns `True` to retry. Transport
errors have `status_code=None`.

## Concurrency

Controlled by `settings.max_workers` (default 50, configurable via
`IFSC_MAX_WORKERS` env var). The thread pool size *and* the urllib3
connection pool size are both bound to this value at `APIClient`
construction. Don't try to override per call — adjust at the `Settings`
level:

```python
from dataclasses import replace

settings = replace(settings, max_workers=100)
client = APIClient(settings)
```

## Error handling

```python
from ifsc_data.api.client import FetchError

try:
    for fetched in client.stream("athletes", ids):
        process(fetched)
except FetchError as exc:
    # Should not happen during streaming — failures are absorbed by the
    # retry loop or silently dropped after max_retries.
    print(f"Unexpected: {exc} (status {exc.status_code})")
```

The retry loop absorbs all `FetchError`s. After `max_retries` exhausted,
failing items are logged at ERROR and dropped from the generator's output
— the caller never sees them as exceptions. To detect drops, compare the
input count to the output count, or scan `logs/ifsc-data.log` for ERROR
lines.

Parse exceptions inside the caller's loop are the caller's responsibility:

```python
for fetched in client.stream("athletes", ids):
    try:
        firstname = fetched.data["firstname"]
    except KeyError as exc:
        log.exception("Bad payload for %s: %s", fetched.path, exc)
        continue
```

This is exactly the pattern every fetcher in `src/ifsc_data/fetchers/`
uses.

## Bypassing the streaming layer

For one-off fetches you can call the internal `_fetch_one`:

```python
data = client._fetch_one(f"/seasons/{ifsc_id}")     # underscore signals internal
```

But the streaming layer is cheap even for a single item, and you keep
retry behavior for free:

```python
data = next(client.stream_paths([("just-one", f"/seasons/{ifsc_id}")])).data
```

(Substitute `ifsc_id` with a real ID — e.g. the current highest from
`SELECT MAX(ifsc_id) FROM seasons`.)
