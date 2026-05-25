"""Seasons + their leagues + their season_leagues.

Seasons have no parent endpoint — discovery probes a small range of unknown
ifsc_ids past the highest one we've seen. Hydration also populates leagues
and creates season_league skeletons for the next phase.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ..api.client import APIClient
from ..db.repository import Repository
from ._logging import ProgressLogger, RateLimitedExceptionLogger

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

DEFAULT_LOOKAHEAD = 5
INITIAL_PROBE_RANGE = 50  # When the DB is empty, probe 0..N to bootstrap.


def discover(repo: Repository, client: APIClient, *, lookahead: int = DEFAULT_LOOKAHEAD) -> int:
    """Probe new season ifsc_ids past the current max. Returns count of new rows."""
    row = repo.conn.execute("SELECT MAX(ifsc_id) FROM seasons").fetchone()
    current_max = row[0]
    if current_max is None:
        candidates = list(range(0, INITIAL_PROBE_RANGE))
    else:
        candidates = list(range(current_max + 1, current_max + 1 + lookahead))

    if candidates:
        log.info("Probing seasons %d..%d (lookahead=%d).",
                 candidates[0], candidates[-1], len(candidates))

    inserted = 0
    for fetched in client.stream("seasons", candidates):
        repo.upsert_season(int(fetched.key))
        inserted += 1
    log.info("Discovered %d new season(s) (probed %d candidates).", inserted, len(candidates))
    return inserted


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Refresh stale/null seasons; populate leagues + season_leagues skeletons.

    Pass either `stale_days` (default behavior, used by `refresh`/`hydrate`) or
    `rows` (used by `pull_new` to scope to ongoing seasons only).
    """
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        rows = repo.find_stale("seasons", stale_days=stale_days)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    ifsc_to_id = {row["ifsc_id"]: row["id"] for row in rows}
    log.info("Hydrating %d season(s).", len(rows))

    ok = fail = 0
    exc_log = RateLimitedExceptionLogger(log)
    progress = ProgressLogger(log, len(rows), "seasons")
    for fetched in client.stream("seasons", ifsc_to_id.keys()):
        progress.tick()
        season_ifsc = int(fetched.key)
        season_row_id = ifsc_to_id[season_ifsc]
        data = fetched.data
        # Group the per-season writes so a parse failure halfway through
        # doesn't leave a season half-populated (e.g. some leagues but not
        # all events, mark_fetched without children). Matches the
        # competitions.hydrate pattern (ADR 0005).
        try:
            with repo.transaction():
                year = data.get("name")
                repo.upsert_season(season_ifsc, year=int(year) if year is not None else None)

                for league in data.get("leagues") or []:
                    league_name = league.get("name")
                    if not league_name:
                        continue
                    league_id = repo.upsert_league(league_name)
                    url = league.get("url") or ""
                    # url shape: /api/v1/season_leagues/{id} — pull the numeric id
                    sl_ifsc = _last_int_segment(url)
                    if sl_ifsc is not None:
                        repo.upsert_season_league(sl_ifsc, season_id=season_row_id, league_id=league_id)

                # The seasons endpoint also lists events directly — register skeletons.
                for event in data.get("events") or []:
                    ev_ifsc = event.get("event_id")
                    if ev_ifsc is not None:
                        repo.upsert_event_skeleton(int(ev_ifsc), season_id=season_row_id)

                repo.mark_fetched("seasons", season_row_id)
            ok += 1
        except Exception as exc:
            exc_log.log("Failed to parse /seasons/%s: %s", season_ifsc, exc)
            fail += 1

    log.info("Seasons: %d hydrated, %d failed.", ok, fail)
    return ok, fail


def _last_int_segment(url: str) -> Optional[int]:
    for part in reversed(url.strip("/").split("/")):
        if part.isdigit():
            return int(part)
    return None
