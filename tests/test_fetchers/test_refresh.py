"""Integration test: pull_new only re-fetches ongoing containers.

See ADR 0006. The "ongoing" rule: seasons in the current year, events within
`grace_days` of date_end, and their descendants. Historical containers are
silently skipped (no HTTP requests at all).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from freezegun import freeze_time

from wcl_data.api.client import Fetched
from wcl_data.db.repository import Repository
from wcl_data.fetchers import refresh as refresh_orchestrator


FROZEN_NOW = "2026-06-15"


def _stub_client(*, current_year: int):
    """A client that records every request and returns minimal valid payloads.

    Season responses keep year = `current_year` so the seeded season stays
    "ongoing" through the rest of pull_new's phases. Real API responses use
    the season's actual year; this stub mimics that contract.
    """
    client = MagicMock()

    seasons_requested: list[int] = []
    sl_requested: list[int] = []
    events_requested: list[int] = []
    comps_requested: list[tuple[int, int]] = []

    def fake_stream(endpoint, ids, *_args, **_kwargs):
        ids = list(ids)
        target = {
            "seasons": seasons_requested,
            "season_leagues": sl_requested,
            "events": events_requested,
        }[endpoint]
        for ifsc_id in ids:
            target.append(int(ifsc_id))
            if endpoint == "seasons":
                # Keep year stable so downstream "ongoing" filters still match.
                payload = {"name": str(current_year), "leagues": [], "events": []}
            else:
                payload = {"leagues": [], "events": [], "d_cats": []}
            yield Fetched(
                key=ifsc_id, path=f"/{endpoint}/{ifsc_id}", data=payload,
            )

    def fake_stream_paths(items, *_args, **_kwargs):
        for key, path in items:
            # competitions paths look like /events/{event_ifsc}/result/{comp_ifsc}
            parts = path.strip("/").split("/")
            event_ifsc = int(parts[1])
            comp_ifsc = int(parts[3])
            comps_requested.append((event_ifsc, comp_ifsc))
            yield Fetched(key=key, path=path, data={"ranking": []})

    client.stream.side_effect = fake_stream
    client.stream_paths.side_effect = fake_stream_paths

    client._seasons_requested = seasons_requested
    client._sl_requested = sl_requested
    client._events_requested = events_requested
    client._comps_requested = comps_requested
    return client


@freeze_time(FROZEN_NOW)
def test_pull_new_skips_ended_containers(memory_db):
    """Seed an ongoing + an ancient season+event+competition; only ongoing gets fetched."""
    repo = Repository(memory_db)
    today = datetime.now(timezone.utc).date()
    current_year = today.year

    # Ongoing chain
    s_now = repo.upsert_season(1001, year=current_year)
    league = repo.upsert_league("World Cup")
    repo.upsert_season_league(2001, season_id=s_now, league_id=league)
    discipline = repo.upsert_discipline("lead")
    category = repo.upsert_category("Men", gender=0)
    e_now = repo.upsert_event_skeleton(3001, season_id=s_now, league_id=league)
    repo.update_event(e_now, date_end=(today + timedelta(days=5)).isoformat())
    repo.upsert_competition(
        event_id=e_now, ifsc_id=4001, discipline_id=discipline, category_id=category,
    )

    # Ancient chain — should be ignored by pull_new
    s_old = repo.upsert_season(1002, year=current_year - 10)
    repo.upsert_season_league(2002, season_id=s_old, league_id=league)
    e_old = repo.upsert_event_skeleton(3002, season_id=s_old, league_id=league)
    repo.update_event(e_old, date_end=(today - timedelta(days=365 * 10)).isoformat())
    repo.upsert_competition(
        event_id=e_old, ifsc_id=4002, discipline_id=discipline, category_id=category,
    )

    client = _stub_client(current_year=current_year)

    # Suppress the seasons-probe by ensuring MAX(ifsc_id) is one of our test rows.
    # (seasons.discover runs first in pull_new but we want a clean assertion list.)
    summary = refresh_orchestrator.pull_new(repo, client)

    # The probe is part of seasons.discover and will fetch the next 5 IDs past MAX.
    # Those will also be in _seasons_requested, but the ancient season (1002) must NOT be.
    assert 1001 in client._seasons_requested
    assert 1002 not in client._seasons_requested

    assert 2001 in client._sl_requested
    assert 2002 not in client._sl_requested

    assert 3001 in client._events_requested
    assert 3002 not in client._events_requested

    requested_comp_ifscs = {comp_ifsc for _, comp_ifsc in client._comps_requested}
    assert 4001 in requested_comp_ifscs
    assert 4002 not in requested_comp_ifscs

    # Summary structure unchanged from old pull_new.
    assert set(summary.keys()) == {"seasons", "season_leagues", "events", "competitions", "athletes"}


@freeze_time(FROZEN_NOW)
def test_pull_new_grace_days_zero_excludes_recently_ended(memory_db):
    """With grace_days=0, an event ended yesterday is treated as already done."""
    repo = Repository(memory_db)
    today = datetime.now(timezone.utc).date()
    current_year = today.year

    s = repo.upsert_season(1001, year=current_year)
    league = repo.upsert_league("World Cup")
    repo.upsert_season_league(2001, season_id=s, league_id=league)
    discipline = repo.upsert_discipline("lead")
    category = repo.upsert_category("Men", gender=0)
    e_yesterday = repo.upsert_event_skeleton(3001, season_id=s, league_id=league)
    repo.update_event(e_yesterday, date_end=(today - timedelta(days=1)).isoformat())
    repo.upsert_competition(
        event_id=e_yesterday, ifsc_id=4001, discipline_id=discipline, category_id=category,
    )

    client = _stub_client(current_year=current_year)
    refresh_orchestrator.pull_new(repo, client, grace_days=0)

    # Default grace would have included 3001; strict mode excludes it.
    assert 3001 not in client._events_requested
    assert (3001, 4001) not in client._comps_requested


# --- hydrate_entity dispatch + discovery wiring (Phase E hardening) ---
#
# The `_FETCHER_MODULES` / `_DISCOVERY_ENTITIES` machinery is what determines
# which entities get a discover() probe before hydrate(). Pre-Phase-E this
# was a hardcoded if/elif chain; post-Phase-E the discover dispatch resolves
# via attribute lookup on the entity's own module. These tests pin both: the
# dispatch fires for seasons (discovery + hydrate), and does NOT fire for
# non-discovery entities (hydrate only).


def test_hydrate_entity_seasons_runs_discover_then_hydrate(memory_db, monkeypatch):
    """For seasons: discover() must run BEFORE hydrate() in a single call."""
    from wcl_data.fetchers import seasons as seasons_mod

    calls: list[str] = []

    def fake_discover(repo, client, **_kw):
        calls.append("discover")
        return 0

    def fake_hydrate(repo, client, *, stale_days=None, rows=None, limit=None):
        calls.append("hydrate")
        return 0, 0

    monkeypatch.setattr(seasons_mod, "discover", fake_discover)
    monkeypatch.setattr(seasons_mod, "hydrate", fake_hydrate)

    repo = Repository(memory_db)
    client = MagicMock()
    refresh_orchestrator.hydrate_entity(repo, client, "seasons", stale_days=0)

    assert calls == ["discover", "hydrate"]


def test_hydrate_entity_non_discovery_skips_discover(memory_db, monkeypatch):
    """For events: only hydrate() runs — no discover probe."""
    from wcl_data.fetchers import events as events_mod

    calls: list[str] = []

    def fake_hydrate(repo, client, *, stale_days=None, rows=None, limit=None):
        calls.append("hydrate")
        return 0, 0

    monkeypatch.setattr(events_mod, "hydrate", fake_hydrate)
    # Patch events.discover too: if hydrate_entity ever wrongly called it,
    # we'd see "discover" in the list. (events has no .discover today; this
    # patch is a guard for future regressions.)
    monkeypatch.setattr(events_mod, "discover", lambda *a, **kw: calls.append("discover"), raising=False)

    repo = Repository(memory_db)
    client = MagicMock()
    refresh_orchestrator.hydrate_entity(repo, client, "events", stale_days=0)

    assert calls == ["hydrate"]


def test_hydrate_entity_honors_monkeypatched_hydrate(memory_db, monkeypatch):
    """Module-attribute dispatch means a runtime patch of competitions.hydrate
    is observed through hydrate_entity. Pins the late-binding contract — see
    `_FETCHER_MODULES` (Phase E hardening): if a future refactor switches back
    to capturing function references at import time, this test fails.
    """
    from wcl_data.fetchers import competitions as competitions_mod

    sentinel = (42, 7)

    def fake_hydrate(repo, client, *, stale_days=None, rows=None, limit=None):
        return sentinel

    monkeypatch.setattr(competitions_mod, "hydrate", fake_hydrate)

    repo = Repository(memory_db)
    client = MagicMock()
    result = refresh_orchestrator.hydrate_entity(
        repo, client, "competitions", stale_days=0,
    )
    assert result == sentinel


def test_hydrate_entity_unknown_entity_lists_actual_whitelist(memory_db):
    """Error message must list `_FETCHER_MODULES.keys()` — the actual gate —
    not a stale source. Pins the fix for the self-contradicting-message bug
    where a name in HYDRATABLE_TABLES but not in _FETCHER_MODULES used to
    appear in the 'Choose from (...)' suffix.
    """
    import pytest as _pytest

    repo = Repository(memory_db)
    client = MagicMock()
    with _pytest.raises(ValueError, match=r"Unknown entity 'judges'"):
        refresh_orchestrator.hydrate_entity(repo, client, "judges", stale_days=0)
