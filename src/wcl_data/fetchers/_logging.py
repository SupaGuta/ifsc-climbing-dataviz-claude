"""Per-fetcher logging helpers.

When a parser defect trips every row of a batch (e.g. an unexpected key
type after an upstream payload change), the per-item `log.exception` calls
inside hydration loops produce one full traceback per row — hundreds or
thousands of repeated 30-line stacks in `logs/wcl-data.log`, drowning every
other signal. `RateLimitedExceptionLogger` keeps the first N tracebacks
(so the defect is debuggable) then degrades to one-line WARNINGs (so the
batch-wide count stays visible without flooding the file).

`ProgressLogger` complements that: in a multi-minute batch the user
otherwise sees one "Hydrating N item(s)" line and nothing until the
summary. The heartbeat tells them the run is alive, gives a rate, and
projects an ETA.
"""
from __future__ import annotations

import logging
import time
from typing import Any


class RateLimitedExceptionLogger:
    """Surface the first `full_traceback_limit` failures as ERROR + traceback;
    log the remainder as WARNING without traceback.

    On the (limit+1)-th failure, emits a one-time sentinel WARNING so the
    transition from full-traceback to summary mode is visible in the log.
    Total failure count is exposed via `.count` for the caller's end-of-run
    summary line.
    """

    def __init__(self, logger: logging.Logger, *, full_traceback_limit: int = 5):
        self._logger = logger
        self._limit = full_traceback_limit
        self.count = 0

    def log(self, msg: str, *args: Any) -> None:
        self.count += 1
        if self.count <= self._limit:
            self._logger.exception(msg, *args)
            return
        if self.count == self._limit + 1:
            self._logger.warning(
                "Further parse failures will be logged at WARNING without traceback "
                "(first %d full tracebacks are above).",
                self._limit,
            )
        self._logger.warning(msg, *args)


class ProgressLogger:
    """Periodic INFO heartbeat for long-running per-item fetcher loops.

    Call `.tick()` once per processed item; emits a log line every
    `interval_secs` (default 30s) with `done/total`, percent complete,
    rolling rate (items/s over the whole batch), and an ETA. The first
    heartbeat fires no earlier than `interval_secs` after construction,
    so small batches that finish in seconds produce no progress noise.
    """

    def __init__(
        self,
        logger: logging.Logger,
        total: int,
        label: str,
        *,
        interval_secs: float = 30.0,
    ):
        self._logger = logger
        self._total = total
        self._label = label
        self._interval = interval_secs
        now = time.monotonic()
        self._start = now
        self._last_log = now
        self._done = 0

    def tick(self) -> None:
        self._done += 1
        now = time.monotonic()
        if now - self._last_log < self._interval:
            return
        elapsed = now - self._start
        rate = self._done / elapsed if elapsed > 0 else 0.0
        if rate > 0 and self._total > self._done:
            eta_secs = (self._total - self._done) / rate
            eta_str = f"ETA ~{eta_secs:.0f}s"
        else:
            eta_str = "ETA —"
        pct = (100.0 * self._done / self._total) if self._total else 0.0
        self._logger.info(
            "%s progress: %d/%d (%.1f%%, %.1f items/s, %s)",
            self._label, self._done, self._total, pct, rate, eta_str,
        )
        self._last_log = now
