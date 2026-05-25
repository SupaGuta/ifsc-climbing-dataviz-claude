"""HTTP client for the World Climbing public API.

Streams responses as they arrive instead of collecting the whole batch before
yielding control — callers can write to the DB after every fetch, so an
interrupt doesn't lose work.

Retries default to 5xx, 429 (with `Retry-After` honored), and transport
errors. Other 4xx (404 in particular) is treated as permanent so the
seasons-discovery probe doesn't burn 6 seconds per non-existent id.

Repeated 401/403 across the worker pool trips `AuthFailureAbort` (default:
5 consecutive failures, no 200 between them) — the run stops loudly instead
of silently dropping every remaining row to the WARN file log.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Callable, Hashable, Iterable, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter

from ..config import Settings
from . import credentials as _credentials_mod

log = logging.getLogger(__name__)

API_BASE_URL = "https://ifsc.results.info/api/v1"

# Status codes that count toward the consecutive-auth-failure abort.
# 401 = upstream rejected the session cookie / token; 403 = Rails CSRF mismatch.
_AUTH_FAILURE_CODES = frozenset({401, 403})

# Hard cap on Retry-After honored by the client. The HTTP RFC permits any
# non-negative integer, but a misbehaving or malicious server returning
# `Retry-After: inf` (or any astronomical value) would otherwise pin the
# main thread in time.sleep — Ctrl+C is queued but only delivered when the
# syscall returns. 300s = 5 min is well past the longest 429 cooldown
# Cloudflare's own docs cite.
_MAX_RETRY_AFTER_SECS = 300.0


@dataclass(frozen=True)
class Fetched[K: Hashable]:
    """A single successful fetch result."""

    key: K               # Whatever the caller passed in (typically the ifsc_id)
    path: str
    data: dict[str, Any]


class FetchError(Exception):
    """Raised on any non-200 / transport failure. Surfaces `status_code` so the
    retry predicate can distinguish transient (5xx, 429, transport) from
    permanent (other 4xx).

    `retry_after` carries the server's `Retry-After` hint (in seconds) when
    set; the batch-level retry waits at least this long before the next pass.
    """

    def __init__(
        self,
        msg: str,
        *,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(msg)
        self.status_code = status_code
        self.retry_after = retry_after


class AuthFailureAbort(RuntimeError):
    """Raised when consecutive 401/403 responses cross the abort threshold.

    Propagates out of `stream_paths` so the caller's run terminates instead
    of quietly dropping every remaining item to the WARN file log — the
    silent-fail mode that motivated this safeguard.

    `partial_summary` is an optional dict that orchestrators attach when
    re-raising mid-run; the CLI prints it so the user sees "seasons + leagues
    completed, events died at the abort" instead of nothing at all.
    """

    def __init__(self, threshold: int, path: str):
        super().__init__(
            f"{threshold} consecutive 401/403 responses (latest: {path}); "
            f"credentials likely expired — run `python -m wcl_data auth` "
            f"to refresh and re-try."
        )
        self.threshold = threshold
        self.path = path
        self.partial_summary: Optional[dict[str, tuple[int, int]]] = None


def _default_retry_on(exc: FetchError) -> bool:
    """Retry transport errors, 5xx, and 429 (rate-limit). Other 4xx is permanent."""
    if exc.status_code is None:
        return True
    if exc.status_code == 429:
        return True
    return exc.status_code >= 500


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse the HTTP `Retry-After` header — seconds-as-int form only.

    The HTTP spec also allows an HTTP-date form, but Rails (the upstream
    server) and Cloudflare both emit the integer-seconds form for 429s, so
    the date parser isn't worth the surface area here.

    Negative, non-finite (inf / nan), or huge values are clamped: negatives
    return None, infinities and finite-but-huge values are capped at
    `_MAX_RETRY_AFTER_SECS` so a misbehaving server can't pin the client in
    `time.sleep` indefinitely.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        # inf, nan, or negative — fall back to no-hint rather than block
        # forever or pretend the server said something useful.
        if math.isinf(parsed) and parsed > 0:
            return _MAX_RETRY_AFTER_SECS
        return None
    return min(parsed, _MAX_RETRY_AFTER_SECS)


class APIClient:
    """Thin streaming client.

    `stream` and `stream_paths` are generators that yield `Fetched` as soon as
    each individual request completes. The caller controls commit cadence.
    """

    # Default cap on response body size. The largest legitimate payload
    # observed in production (a multi-discipline event with full per-round
    # detail) is ~5 MB; 50 MB leaves 10x headroom while still catching an
    # unbounded response (typically: the upstream serving an HTML interstitial
    # without setting status≠200).
    _MAX_RESPONSE_BYTES = 50 * 1024 * 1024

    # Consecutive 401/403 responses across the worker pool before aborting.
    # 5 gives one or two transient races a chance to clear before tripping;
    # sustained auth failure crosses it within a few hundred ms on a 50-worker
    # pool, so the run dies promptly rather than silently dropping rows.
    _AUTH_FAILURE_THRESHOLD = 5

    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update(settings.api_headers)
        adapter = HTTPAdapter(
            pool_connections=settings.max_workers,
            pool_maxsize=settings.max_workers,
            pool_block=False,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Auth-failure tracking. Workers share the counter; updates need a
        # lock so two simultaneous 401s don't both read N-1 and both store N.
        self._auth_failure_lock = threading.Lock()
        self._consecutive_auth_failures = 0

    # ------------------------------------------------------------------ Public

    def stream(
        self,
        endpoint: str,
        ifsc_ids: Iterable[int],
        *,
        max_retries: int = 2,
        retry_delay: float = 2.0,
        retry_on: Optional[Callable[[FetchError], bool]] = None,
    ) -> Iterator[Fetched[int]]:
        """Fetch `/{endpoint}/{id}` for each id. Yields as each completes."""
        items = [(ifsc_id, f"/{endpoint}/{ifsc_id}") for ifsc_id in ifsc_ids]
        yield from self.stream_paths(
            items,
            max_retries=max_retries,
            retry_delay=retry_delay,
            retry_on=retry_on,
        )

    def stream_paths[K: Hashable](
        self,
        items: Iterable[tuple[K, str]],
        *,
        max_retries: int = 2,
        retry_delay: float = 2.0,
        retry_on: Optional[Callable[[FetchError], bool]] = None,
    ) -> Iterator[Fetched[K]]:
        """Fetch arbitrary paths concurrently; yield `Fetched` as each succeeds.

        `items` is an iterable of (caller_key, path) pairs.
        `retry_on(exc)` decides whether a failure should be retried; defaults
        to 5xx + 429 + transport (other 4xx is treated as permanent).

        Raises `AuthFailureAbort` if consecutive 401/403 responses cross the
        threshold — protects against silent-fail mode where an expired token
        drops every remaining row.
        """
        if retry_on is None:
            retry_on = _default_retry_on
        pending = list(items)
        attempt = 0
        max_retry_after_seen = 0.0
        while pending:
            if attempt > 0:
                # Exponential backoff with light jitter, clamped up by the
                # max Retry-After hint seen on the prior batch (so we don't
                # undercut a server-supplied cooldown). `2 ** (attempt - 1)`
                # so the first retry sleeps `retry_delay`, the second
                # `2*retry_delay`, etc. — matching the standard convention
                # and the operator-intuition "first retry waits one base
                # interval". Jitter is bounded by retry_delay itself so
                # tests passing retry_delay=0 stay instant; the spread
                # desynchronizes concurrent callers on a shared upstream
                # rate limit.
                backoff = retry_delay * (2 ** (attempt - 1)) + random.uniform(0, min(0.5, retry_delay))
                sleep_for = max(backoff, max_retry_after_seen)
                log.info(
                    "Retry attempt %d for %d items (sleeping %.1fs).",
                    attempt, len(pending), sleep_for,
                )
                if sleep_for > 0:
                    time.sleep(sleep_for)
                max_retry_after_seen = 0.0

            failures: list[tuple[K, str]] = []
            # Manual pool lifecycle (rather than `with ThreadPoolExecutor`)
            # so AuthFailureAbort can shut down with wait=False — the `with`
            # __exit__ always calls shutdown(wait=True), which would block
            # the re-raise for up to read_timeout seconds while in-flight
            # workers drain. With manual shutdown we still wait on normal
            # completion (workers have already returned by then, so it's
            # fast) but bail immediately on abort.
            pool = ThreadPoolExecutor(max_workers=self.settings.max_workers)
            aborted = False
            try:
                future_to_item = {
                    pool.submit(self._fetch_one, path): (key, path)
                    for key, path in pending
                }
                for future in as_completed(future_to_item):
                    key, path = future_to_item[future]
                    try:
                        data = future.result()
                    except AuthFailureAbort as exc:
                        # `exc.threshold` is captured atomically at the raise
                        # site; reading self._consecutive_auth_failures here
                        # would race with workers still in flight (200s reset
                        # to 0, 401s increment) and produce misleading log
                        # output.
                        log.error(
                            "Aborting: threshold of %d consecutive 401/403 "
                            "responses reached (latest: %s). "
                            "Run `python -m wcl_data auth` to refresh credentials.",
                            exc.threshold, exc.path,
                        )
                        aborted = True
                        raise
                    except FetchError as exc:
                        log.warning("Fetch failed for %s: %s", path, exc)
                        if exc.retry_after is not None:
                            max_retry_after_seen = max(max_retry_after_seen, exc.retry_after)
                        if retry_on(exc):
                            failures.append((key, path))
                        # else: permanent failure (e.g. 404) — drop silently after the warning
                        continue
                    yield Fetched(key=key, path=path, data=data)
            finally:
                # On abort: wait=False keeps the re-raise from blocking the
                # caller for up to read_timeout — in-flight workers continue
                # in the background and the process exits when the main
                # thread does. On normal completion: as_completed has
                # already drained the pool, so wait=True is cheap.
                pool.shutdown(wait=not aborted, cancel_futures=aborted)

            if not failures or attempt >= max_retries:
                if failures:
                    log.error(
                        "Giving up on %d items after %d retries: %s",
                        len(failures),
                        max_retries,
                        [p for _, p in failures],
                    )
                return
            pending = failures
            attempt += 1

    def refresh_credentials(self) -> None:
        """Fetch fresh CSRF + session cookie from the WC landing page and
        update the in-memory session headers + Settings.

        Provided as an opt-in hook for callers that want to attempt in-place
        recovery before re-trying a failed batch. **Does not write to .env**;
        the next process start will still see the old credentials unless
        `python -m wcl_data auth` is also run.

        Not wired into the default retry path because uncontrolled refresh
        during a run can mask legitimate auth-config bugs (e.g. a stale
        `WCL_REFERER`). The default safeguard is `AuthFailureAbort`, which
        makes the failure mode loud.

        `fetch_credentials` is reached via the module (not a from-import) so
        tests can `monkeypatch.setattr(credentials_mod, 'fetch_credentials',
        ...)` and have the substitution take effect at call time.
        """
        creds = _credentials_mod.fetch_credentials()
        self.settings = replace(
            self.settings,
            csrf_token=creds.csrf_token,
            session_cookie=creds.session_cookie,
        )
        self._session.headers.update(self.settings.api_headers)
        with self._auth_failure_lock:
            self._consecutive_auth_failures = 0
        log.info("APIClient credentials refreshed in-memory (CSRF + session cookie).")

    # ------------------------------------------------------------------ Internal

    def _fetch_one(self, path: str) -> dict[str, Any]:
        url = API_BASE_URL + path
        try:
            resp = self._session.get(
                url,
                timeout=(self.settings.connect_timeout, self.settings.read_timeout),
                stream=True,
            )
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc

        try:
            # Parse Retry-After once so EVERY raise path (status≠200, HTML-200,
            # oversize, JSON-decode) can attach it. Cloudflare interstitials
            # sometimes ship status 200 + Retry-After together, so the older
            # "only on non-200" parsing was leaving the cooldown signal on the
            # floor.
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))

            if resp.status_code != 200:
                if resp.status_code in _AUTH_FAILURE_CODES:
                    self._note_auth_failure(path)
                raise FetchError(
                    f"HTTP {resp.status_code} {resp.reason}",
                    status_code=resp.status_code,
                    retry_after=retry_after,
                )

            # Reject HTML-200 (the WC site occasionally ships an interstitial
            # with status 200 when the API would have returned a soft error)
            # and oversized bodies. Both are treated as TRANSIENT (status_code
            # left None) so `_default_retry_on` retries them via the transport-
            # error path — a CDN routing blip on one worker would otherwise
            # silently drop the row even though a retry on a different
            # connection would have succeeded.
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.lower().startswith("application/json"):
                raise FetchError(
                    f"Expected application/json, got Content-Type={content_type!r}",
                    status_code=None,
                    retry_after=retry_after,
                )

            # Fast-fail when Content-Length advertises an oversized body, then
            # stream-cap the actual bytes so chunked-transfer-encoded responses
            # (no Content-Length) can't allocate an unbounded parse buffer
            # either. Cloudflare and most modern reverse proxies use chunked
            # for streamed payloads, so the header-check path is best-effort,
            # not the safety net.
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    advertised = int(content_length)
                except ValueError:
                    advertised = None
                if advertised is not None and advertised > self._MAX_RESPONSE_BYTES:
                    raise FetchError(
                        f"Response too large: Content-Length={advertised} bytes > "
                        f"{self._MAX_RESPONSE_BYTES}",
                        status_code=None,
                        retry_after=retry_after,
                    )

            max_bytes = self._MAX_RESPONSE_BYTES
            total = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(
                        f"Response body exceeded {max_bytes} bytes "
                        f"(chunked / no Content-Length advertised)",
                        status_code=None,
                        retry_after=retry_after,
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                # Without this, JSONDecodeError would escape past the
                # `except FetchError` in stream_paths, out of the generator,
                # and silently terminate the fetcher's for-loop — every
                # remaining stale row dropped to nothing. Funnel it back
                # through the retry predicate as a transient (status_code=None
                # → treated like a transport error).
                raise FetchError(
                    f"Invalid JSON body ({len(body)} bytes): {exc}",
                    status_code=None,
                    retry_after=retry_after,
                ) from exc

            # Only reset the auth-failure counter once the response has
            # passed every body guard. Resetting earlier (before the
            # Content-Type check) would have defeated AuthFailureAbort for
            # the case where the upstream returns an HTML-200 interstitial
            # on expired auth — each interstitial would silently zero the
            # counter and the abort would never trip.
            with self._auth_failure_lock:
                self._consecutive_auth_failures = 0
            return data
        finally:
            # Release the streamed connection back to the pool even when an
            # exception unwinds before iter_content completes.
            resp.close()

    def _note_auth_failure(self, path: str) -> None:
        """Increment the auth-failure counter; raise `AuthFailureAbort` if
        the threshold is crossed."""
        with self._auth_failure_lock:
            self._consecutive_auth_failures += 1
            crossed = self._consecutive_auth_failures >= self._AUTH_FAILURE_THRESHOLD
        if crossed:
            raise AuthFailureAbort(self._AUTH_FAILURE_THRESHOLD, path)
