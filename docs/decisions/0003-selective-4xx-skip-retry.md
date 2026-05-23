# 0003 — Selective 4xx-skip retry

**Status:** Accepted
**Date:** 2026-05-22

## Context

Seasons have no parent endpoint — discovery walks unknown `ifsc_id`s past the
highest one we've seen, probing with `/seasons/{id}` until the API returns
404 (and probably a few empty slots beyond, because IFSC season IDs aren't
strictly contiguous). With a `requests`-default retry behaviour (retry on
any error), each 404 would burn 2s × 2 retries = ~6s per non-existent ID,
and a `lookahead` of 5 → 30s of pointless retries on every `pull-new`.

Beyond probing, the API has one known permanently-404 athlete (`ifsc_id =
12334`). Treating *that* as transient means a 6-second retry stall on every
`hydrate athletes` forever.

Meanwhile, transient failures (5xx, transport errors during the API's
occasional flakes) *do* need retrying — they account for a low single-digit
percentage of requests on a typical run.

## Decision

`api/client.py` ships a default retry predicate that **retries transport
errors and 5xx only**:

```python
def _default_retry_on(exc: FetchError) -> bool:
    if exc.status_code is None:
        return True            # transport: retry
    return exc.status_code >= 500   # 5xx: retry, 4xx: drop
```

4xx is treated as **permanent**: the failure is logged at WARNING and the
item silently dropped from the work list. The caller (the per-entity
fetcher) gets nothing for that ID and moves on.

Callers may override by passing `retry_on=<callable>` to
`client.stream(...)` / `client.stream_paths(...)`. Nothing currently does.

Defaults: `max_retries=2`, `retry_delay=2.0`.

## Consequences

**Positive**

- Seasons discovery is effectively free: the 404 hits return immediately
  and the loop continues.
- Known-bad IDs (the one permanently-404 athlete) don't pollute every
  run's log with 6 seconds of retry attempts.
- Transient API flakes still get retried.

**Negative**

- A genuine 403/401 (auth lapsed) is treated as permanent and silently
  dropped. The user gets WARNING-level log lines (not an obvious
  failure) but no row updates. Mitigation: the `auth` CLI command and
  the README documenting "re-run auth when refresh starts failing
  silently." This is a known UX rough edge; a future ADR may revisit.
- A 429 (rate limit) would also be dropped. The IFSC API has never
  returned 429 in production. If that changes, add a 429-aware predicate
  and document in a new ADR.

## Alternatives considered

- **Retry everything** — the `requests` default. Wastes time on 404s,
  doesn't help correctness. Rejected.
- **Retry nothing** — would lose ~3% of rows on every run due to transient
  5xx. Rejected.
- **Per-fetcher retry policies** — `seasons.discover` knows it's probing
  and `athletes.hydrate` knows about the bad ID. We could push the
  policy down to those callers. But the default policy is correct for
  *both*, so the override is unnecessary today. The `retry_on` parameter
  is there for the day a caller needs to differ.
