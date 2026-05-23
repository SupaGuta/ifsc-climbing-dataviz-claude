"""Top-level orchestration: discover → hydrate the whole graph."""
from __future__ import annotations

import logging
from typing import Optional

from ..api.client import APIClient
from ..db.repository import Repository
from . import athletes, competitions, events, season_leagues, seasons

log = logging.getLogger(__name__)

ENTITIES = ("seasons", "season_leagues", "events", "competitions", "athletes")


def pull_new(
    repo: Repository,
    client: APIClient,
    *,
    limit: Optional[int] = None,
    grace_days: int = 15,
) -> dict[str, tuple[int, int]]:
    """Discover all newly-published content without re-hydrating existing data.

    Scopes the container re-fetch to **ongoing** rows only — seasons in the
    current calendar year, events whose `date_end` is within `grace_days` of
    today (default 15), and their descendants. Historical containers are skipped
    because ended seasons never gain new leagues/events and ended events never
    gain new competitions. See ADR 0006.

    Athletes are unchanged: only brand-new skeletons (NULL `last_fetched_at`)
    discovered during this run get hydrated.

    Order of magnitude: ~30-60s on a steady-state warehouse, vs ~30+ for
    `refresh --stale-days 0`. The `refresh` command remains the escape hatch
    for catching retroactive IFSC edits to ended containers.
    """
    summary: dict[str, tuple[int, int]] = {}

    seasons.discover(repo, client)
    summary["seasons"] = seasons.hydrate(
        repo, client, rows=repo.find_ongoing_seasons(), limit=limit,
    )
    summary["season_leagues"] = season_leagues.hydrate(
        repo, client, rows=repo.find_ongoing_season_leagues(), limit=limit,
    )
    summary["events"] = events.hydrate(
        repo, client, rows=repo.find_ongoing_events(grace_days=grace_days), limit=limit,
    )
    summary["competitions"] = competitions.hydrate(
        repo, client, rows=repo.find_ongoing_competitions(grace_days=grace_days), limit=limit,
    )
    # Huge stale_days → only rows with last_fetched_at IS NULL match. That is
    # exactly the set of athletes just discovered during competitions hydration.
    summary["athletes"] = athletes.hydrate(repo, client, stale_days=365_000, limit=limit)

    return summary


def refresh_all(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: int,
    limit: Optional[int] = None,
) -> dict[str, tuple[int, int]]:
    """Run discovery + hydration across the full entity graph.

    Each phase commits per item, so a kill mid-run preserves progress —
    re-running picks up where it left off (stale rows stay stale).
    """
    summary: dict[str, tuple[int, int]] = {}

    seasons.discover(repo, client)
    summary["seasons"] = seasons.hydrate(repo, client, stale_days=stale_days, limit=limit)
    summary["season_leagues"] = season_leagues.hydrate(repo, client, stale_days=stale_days, limit=limit)
    summary["events"] = events.hydrate(repo, client, stale_days=stale_days, limit=limit)
    summary["competitions"] = competitions.hydrate(repo, client, stale_days=stale_days, limit=limit)
    summary["athletes"] = athletes.hydrate(repo, client, stale_days=stale_days, limit=limit)

    return summary


def hydrate_entity(
    repo: Repository,
    client: APIClient,
    entity: str,
    *,
    stale_days: int,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    if entity == "seasons":
        seasons.discover(repo, client)
        return seasons.hydrate(repo, client, stale_days=stale_days, limit=limit)
    if entity == "season_leagues":
        return season_leagues.hydrate(repo, client, stale_days=stale_days, limit=limit)
    if entity == "events":
        return events.hydrate(repo, client, stale_days=stale_days, limit=limit)
    if entity == "competitions":
        return competitions.hydrate(repo, client, stale_days=stale_days, limit=limit)
    if entity == "athletes":
        return athletes.hydrate(repo, client, stale_days=stale_days, limit=limit)
    raise ValueError(f"Unknown entity {entity!r}. Choose from {ENTITIES}.")
