"""HTTP client for the IFSC public API.

Streams responses as they arrive instead of collecting the whole batch before
yielding control — callers can write to the DB after every fetch, so an
interrupt doesn't lose work.

Retries default to 5xx + transport errors only. 4xx (404 in particular)
is treated as permanent so the seasons-discovery probe doesn't burn 6
seconds per non-existent id.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterable, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter

from ..config import Settings

log = logging.getLogger(__name__)

API_BASE_URL = "https://ifsc.results.info/api/v1"


@dataclass(frozen=True)
class Fetched[K: Hashable]:
    """A single successful fetch result."""

    key: K               # Whatever the caller passed in (typically the ifsc_id)
    path: str
    data: dict[str, Any]


class FetchError(Exception):
    """Raised on any non-200 / transport failure. Surfaces `status_code` so the
    retry predicate can distinguish transient (5xx, transport) from permanent (4xx)."""

    def __init__(self, msg: str, *, status_code: Optional[int] = None):
        super().__init__(msg)
        self.status_code = status_code


def _default_retry_on(exc: FetchError) -> bool:
    """Retry only transport errors (no status_code) and 5xx. 4xx is permanent."""
    if exc.status_code is None:
        return True
    return exc.status_code >= 500


class APIClient:
    """Thin streaming client.

    `stream` and `stream_paths` are generators that yield `Fetched` as soon as
    each individual request completes. The caller controls commit cadence.
    """

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
        to 5xx + transport only (4xx is treated as permanent).
        """
        if retry_on is None:
            retry_on = _default_retry_on
        pending = list(items)
        attempt = 0
        while pending:
            if attempt > 0:
                log.info("Retry attempt %d for %d items.", attempt, len(pending))
                time.sleep(retry_delay)

            failures: list[tuple[K, str]] = []
            with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
                future_to_item = {
                    pool.submit(self._fetch_one, path): (key, path)
                    for key, path in pending
                }
                for future in as_completed(future_to_item):
                    key, path = future_to_item[future]
                    try:
                        data = future.result()
                    except FetchError as exc:
                        log.warning("Fetch failed for %s: %s", path, exc)
                        if retry_on(exc):
                            failures.append((key, path))
                        # else: permanent failure (e.g. 404) — drop silently after the warning
                        continue
                    yield Fetched(key=key, path=path, data=data)

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

    # ------------------------------------------------------------------ Internal

    def _fetch_one(self, path: str) -> dict[str, Any]:
        url = API_BASE_URL + path
        try:
            resp = self._session.get(url, timeout=self.settings.request_timeout)
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc
        if resp.status_code != 200:
            raise FetchError(
                f"HTTP {resp.status_code} {resp.reason}",
                status_code=resp.status_code,
            )
        return resp.json()
