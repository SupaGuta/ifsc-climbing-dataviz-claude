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
) -> dict[str, tuple[int, int]]:
    """Discover all newly-published content without re-hydrating existing athletes.

    Force-refreshes the container entities (seasons, season_leagues, events,
    competitions) so any new children they list are picked up, then hydrates
    only brand-new athlete skeletons. Existing athlete profiles (the slow part
    of `refresh --stale-days 0`) are left alone — they almost never change.

    Order of magnitude: minutes, not the 30+ of a nuclear refresh.
    """
    summary: dict[str, tuple[int, int]] = {}

    seasons.discover(repo, client)
    summary["seasons"] = seasons.hydrate(repo, client, stale_days=0, limit=limit)
    summary["season_leagues"] = season_leagues.hydrate(repo, client, stale_days=0, limit=limit)
    summary["events"] = events.hydrate(repo, client, stale_days=0, limit=limit)
    summary["competitions"] = competitions.hydrate(repo, client, stale_days=0, limit=limit)
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
