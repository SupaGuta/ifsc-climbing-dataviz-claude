"""Hydrate competitions: fetch per-competition rankings, register athletes + results.

Competitions are indexed by (event_ifsc_id, comp_ifsc_id) so the API path is
/events/{event_ifsc_id}/result/{comp_ifsc_id}.

Each per-competition unit of work (delete-then-reinsert + mark_fetched) runs
inside a single SQL transaction via `repo.transaction()`, so a mid-loop failure
rolls back the partial state rather than leaving the warehouse with an empty
competition.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, cast

from ..api.client import APIClient
from ..db.repository import Repository

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Pass either `stale_days` (default) or `rows` (used by `pull_new`).

    Rows shape: `(comp_id, comp_ifsc, event_ifsc)` — the inline JOIN provides this
    when `stale_days` is used; callers passing `rows=` must use the same shape
    (e.g. `repo.find_ongoing_competitions()`).
    """
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        cutoff = repo.stale_cutoff(stale_days)
        rows = list(repo.conn.execute(
            "SELECT c.id AS comp_id, c.ifsc_id AS comp_ifsc, e.ifsc_id AS event_ifsc "
            "FROM competitions c JOIN events e ON c.event_id = e.id "
            "WHERE c.last_fetched_at IS NULL OR c.last_fetched_at < ? "
            "ORDER BY c.id ASC",
            (cutoff,),
        ))
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    log.info("Hydrating %d competition(s).", len(rows))

    items: list[tuple[int, str]] = [
        (cast(int, r["comp_id"]), f"/events/{r['event_ifsc']}/result/{r['comp_ifsc']}")
        for r in rows
    ]

    ok = fail = 0
    for fetched in client.stream_paths(items):
        comp_id = int(fetched.key)
        data = fetched.data
        try:
            with repo.transaction():
                repo.delete_results_for_competition(comp_id)
                for entry in data.get("ranking") or []:
                    athlete_ifsc = entry.get("athlete_id")
                    if athlete_ifsc is None:
                        continue
                    athlete_id = repo.upsert_athlete_skeleton(int(athlete_ifsc))
                    repo.upsert_result(
                        competition_id=comp_id,
                        athlete_id=athlete_id,
                        rank=entry.get("rank"),
                    )
                repo.mark_fetched("competitions", comp_id)
            ok += 1
        except Exception as exc:
            log.exception("Failed to parse %s: %s", fetched.path, exc)
            fail += 1

    log.info("Competitions: %d hydrated, %d failed.", ok, fail)
    return ok, fail
