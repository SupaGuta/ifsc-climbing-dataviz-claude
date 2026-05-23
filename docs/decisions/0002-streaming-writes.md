# 0002 — Streaming writes

**Status:** Accepted
**Date:** 2026-05-22

## Context

A full `refresh --stale-days 0` re-fetches ~5,800 competitions and ~14,900
athlete profiles, taking ~30 minutes. The earlier ingestion scaffolding
(predecessor to this package) collected each batch fully in memory and
committed at the end. A `Ctrl-C` mid-run threw away the entire batch — and
the user did this often, either to fix a parse bug or to retry after an
auth-induced 401 storm.

The HTTP layer is concurrent (50 workers by default), so "commit after each
fetch" needs care: the writer must serialize commits while the fetchers
race ahead.

## Decision

The API client (`src/ifsc_data/api/client.py`) exposes its results as a
**generator** that yields each `Fetched` the instant its future resolves
inside `as_completed(futures)`. Per-entity fetchers iterate the generator
and call the repository between iterations. The repository commits per
row (`_maybe_commit` after every CRUD method) unless the caller wraps the
work in `with repo.transaction():`.

Concretely: `stream_paths` builds a `ThreadPoolExecutor`, submits all paths,
then `yield Fetched(...)` from inside the `as_completed` loop. The caller
controls commit cadence; the client never holds a batch.

## Consequences

**Positive**

- `Ctrl-C` only loses the in-flight row. The killed row's
  `last_fetched_at` is still NULL, so the next run picks it up. Zero work
  is wasted.
- Memory footprint is one HTTP response at a time per worker (a few KB
  per row) rather than the full batch.
- Progress is observable: log lines appear as rows land, not at the end.

**Negative**

- Per-row `commit()` is slower than one transaction over a 5,000-row
  batch. We measured the difference at ~2× slowdown on the athletes
  table. For the rare cases where atomicity matters (competitions, see
  [ADR 0005](0005-transactional-boundary-on-competitions.md)) we have the
  `transaction()` context manager. Everywhere else the durability buy is
  worth the speed cost.
- The generator pattern forces fetchers to handle exceptions per-iteration
  rather than per-batch. Each fetcher has a `try/except Exception` inside
  its loop that increments `fail` and continues. This is a small bit of
  boilerplate replicated across the five fetchers; we accepted the
  duplication.

## Alternatives considered

- **Batch + commit at the end** — what the predecessor did. Lost work on
  every Ctrl-C. Rejected.
- **Batch in chunks of N (e.g. 100)** — a compromise: smaller blast radius
  on Ctrl-C, fewer commits than per-row. Adds complexity (a chunking
  layer) and doesn't reach zero-loss on interrupt. Rejected as a
  half-measure.
- **Async I/O with `asyncio` + `aiohttp`** — would scale concurrency further
  than the thread pool, but SQLite's single-writer model means the
  bottleneck is downstream of HTTP. The thread pool is "fast enough" and
  threads play nicer with the synchronous `sqlite3` module. Reconsider
  if a future endpoint blows past 50 in-flight requests profitably.
