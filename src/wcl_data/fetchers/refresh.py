"""Top-level orchestration: discover → hydrate the whole graph."""
from __future__ import annotations

import logging
from types import ModuleType
from typing import Callable, Optional

from ..api.client import APIClient, AuthFailureAbort
from ..db.repository import HYDRATABLE_TABLES, Repository
from . import athletes, competitions, events, season_leagues, seasons

log = logging.getLogger(__name__)

# Re-export under the historical name. ENTITIES is the order of hydration in
# `refresh_all` / `pull_new` and is referenced by the CLI's `hydrate` choices
# argument; the canonical definition lives on Repository so we don't keep two
# tuples in sync. See docs/python-api/fetchers-and-orchestrator.md.
ENTITIES = HYDRATABLE_TABLES

# Per-entity dispatch table. Keyed by the same names as HYDRATABLE_TABLES;
# values are the fetcher MODULES (not bound functions) so `hydrate_entity`
# can resolve `.hydrate` / `.discover` via attribute lookup at call time —
# this keeps the dispatch monkeypatch-friendly (a test that does
# `monkeypatch.setattr(fetchers.seasons, "hydrate", mock)` is honored by both
# the direct call sites in pull_new/refresh_all AND through hydrate_entity).
#
# The module-valued dict also avoids hiding signature drift behind
# `Callable[..., tuple[int, int]]`: each call site spells out `.hydrate(...)`
# so the caller sees the actual function signature.
_FETCHER_MODULES: dict[str, ModuleType] = {
    "seasons": seasons,
    "season_leagues": season_leagues,
    "events": events,
    "competitions": competitions,
    "athletes": athletes,
}

# Entities whose hydrate pass is preceded by a separate discovery probe.
# Today only seasons has a `.discover(repo, client)` hook; adding more entities
# to this set is the supported extension point (each entity's own module must
# expose a top-level `discover(repo, client)` callable — AttributeError on a
# misconfigured set is the fail-fast intended behavior).
_DISCOVERY_ENTITIES: frozenset[str] = frozenset({"seasons"})

# Module-load invariants: pin the dispatch tables against HYDRATABLE_TABLES
# so the three sources of truth (HYDRATABLE_TABLES, _FETCHER_MODULES,
# _DISCOVERY_ENTITIES) can't silently drift. A new hydratable entity added to
# HYDRATABLE_TABLES without a corresponding _FETCHER_MODULES entry trips this
# assert at import — fail-fast, before any CLI invocation reaches the
# misleading "Unknown entity X. Choose from (...X...)" runtime error.
assert set(_FETCHER_MODULES) == set(HYDRATABLE_TABLES), (
    f"_FETCHER_MODULES keys {sorted(_FETCHER_MODULES)} != "
    f"HYDRATABLE_TABLES {sorted(HYDRATABLE_TABLES)}"
)
assert _DISCOVERY_ENTITIES <= set(_FETCHER_MODULES), (
    f"_DISCOVERY_ENTITIES {sorted(_DISCOVERY_ENTITIES)} not subset of "
    f"_FETCHER_MODULES {sorted(_FETCHER_MODULES)}"
)


def _run_phase(
    summary: dict[str, tuple[int, int]],
    entity: str,
    hydrate_call: Callable[[], tuple[int, int]],
) -> None:
    """Run one entity's hydrate; attach partial summary to AuthFailureAbort.

    Phases commit per-item, so already-completed entities have durable rows
    in the DB. The partial-summary dict lets the CLI print "seasons: 5/0,
    events: died" instead of nothing — operator gets visibility into how
    far the run got before the abort.
    """
    try:
        summary[entity] = hydrate_call()
    except AuthFailureAbort as exc:
        exc.partial_summary = dict(summary)
        raise


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

    Order of magnitude: ~30-60s on a steady-state warehouse, vs ~45-90 min
    for `refresh --stale-days 0`. The `refresh` command remains the escape
    hatch for catching retroactive World Climbing edits to ended containers.
    """
    summary: dict[str, tuple[int, int]] = {}

    seasons.discover(repo, client)
    _run_phase(summary, "seasons", lambda: seasons.hydrate(
        repo, client, rows=repo.find_ongoing_seasons(), limit=limit,
    ))
    _run_phase(summary, "season_leagues", lambda: season_leagues.hydrate(
        repo, client, rows=repo.find_ongoing_season_leagues(), limit=limit,
    ))
    _run_phase(summary, "events", lambda: events.hydrate(
        repo, client, rows=repo.find_ongoing_events(grace_days=grace_days), limit=limit,
    ))
    _run_phase(summary, "competitions", lambda: competitions.hydrate(
        repo, client, rows=repo.find_ongoing_competitions(grace_days=grace_days), limit=limit,
    ))
    # Huge stale_days → only rows with last_fetched_at IS NULL match. That is
    # exactly the set of athletes just discovered during competitions hydration.
    _run_phase(summary, "athletes", lambda: athletes.hydrate(
        repo, client, stale_days=365_000, limit=limit,
    ))

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
    _run_phase(summary, "seasons", lambda: seasons.hydrate(
        repo, client, stale_days=stale_days, limit=limit,
    ))
    _run_phase(summary, "season_leagues", lambda: season_leagues.hydrate(
        repo, client, stale_days=stale_days, limit=limit,
    ))
    _run_phase(summary, "events", lambda: events.hydrate(
        repo, client, stale_days=stale_days, limit=limit,
    ))
    _run_phase(summary, "competitions", lambda: competitions.hydrate(
        repo, client, stale_days=stale_days, limit=limit,
    ))
    _run_phase(summary, "athletes", lambda: athletes.hydrate(
        repo, client, stale_days=stale_days, limit=limit,
    ))

    return summary


def hydrate_entity(
    repo: Repository,
    client: APIClient,
    entity: str,
    *,
    stale_days: int,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    if entity not in _FETCHER_MODULES:
        raise ValueError(
            f"Unknown entity {entity!r}. "
            f"Choose from {tuple(_FETCHER_MODULES)}."
        )
    mod = _FETCHER_MODULES[entity]
    if entity in _DISCOVERY_ENTITIES:
        # Each entity in _DISCOVERY_ENTITIES owns its own .discover(repo, client)
        # — adding a new entity to the set requires giving its module a
        # `discover` callable. AttributeError on a misconfigured set is the
        # intended fail-fast.
        mod.discover(repo, client)
    return mod.hydrate(repo, client, stale_days=stale_days, limit=limit)
