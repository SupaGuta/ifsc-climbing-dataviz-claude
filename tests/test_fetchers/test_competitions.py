"""Test the competitions fetcher's parse logic + transactional safety."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ifsc_data.api.client import Fetched
from ifsc_data.db.repository import Repository
from ifsc_data.fetchers import competitions as competitions_fetcher


def _seed_competition(memory_db) -> tuple[Repository, int]:
    repo = Repository(memory_db)
    season_id = repo.upsert_season(2024, year=2024)
    event_id = repo.upsert_event_skeleton(100, season_id=season_id)
    discipline_id = repo.upsert_discipline("lead")
    category_id = repo.upsert_category("Men", 0)
    comp_id = repo.upsert_competition(
        event_id=event_id, ifsc_id=5,
        discipline_id=discipline_id, category_id=category_id,
    )
    return repo, comp_id


def _stub_client(ranking: list[dict]) -> MagicMock:
    client = MagicMock()
    def fake_stream_paths(items, *args, **kwargs):
        for key, path in items:
            yield Fetched(key=key, path=path, data={"ranking": ranking})
    client.stream_paths.side_effect = fake_stream_paths
    return client


def test_hydrate_writes_results(memory_db):
    repo, comp_id = _seed_competition(memory_db)
    client = _stub_client([
        {"athlete_id": 111, "rank": 1},
        {"athlete_id": 222, "rank": 2},
    ])

    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    rows = memory_db.execute(
        "SELECT rank FROM results WHERE competition_id = ? ORDER BY rank", (comp_id,)
    ).fetchall()
    assert [r["rank"] for r in rows] == [1, 2]


def test_hydrate_is_idempotent(memory_db):
    """Re-hydrating the same competition should leave the same result rows."""
    repo, comp_id = _seed_competition(memory_db)
    client = _stub_client([
        {"athlete_id": 111, "rank": 1},
        {"athlete_id": 222, "rank": 2},
    ])

    competitions_fetcher.hydrate(repo, client, stale_days=0)
    first = memory_db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    competitions_fetcher.hydrate(repo, client, stale_days=0)
    second = memory_db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    assert first == second == 2


def test_hydrate_rolls_back_on_failure(memory_db, monkeypatch):
    """If something throws mid-loop, the per-competition transaction rolls back —
    pre-existing result rows are preserved instead of being deleted."""
    repo, comp_id = _seed_competition(memory_db)

    # Pre-existing result that the failed hydrate must NOT delete.
    pre_athlete_id = repo.upsert_athlete_skeleton(999)
    repo.upsert_result(competition_id=comp_id, athlete_id=pre_athlete_id, rank=1)

    client = _stub_client([{"athlete_id": 111, "rank": 1}])

    # Sabotage upsert_result so the in-loop body raises.
    original = repo.upsert_result
    def explosive(**kwargs):
        if kwargs.get("rank") == 1 and kwargs.get("athlete_id") != pre_athlete_id:
            raise RuntimeError("simulated failure")
        return original(**kwargs)
    monkeypatch.setattr(repo, "upsert_result", explosive)

    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (0, 1)

    # The pre-existing row should still be there (delete was rolled back).
    rows = memory_db.execute(
        "SELECT athlete_id, rank FROM results WHERE competition_id = ?", (comp_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["athlete_id"] == pre_athlete_id

    # And the competition should still be stale (mark_fetched was rolled back too).
    row = memory_db.execute(
        "SELECT last_fetched_at FROM competitions WHERE id = ?", (comp_id,)
    ).fetchone()
    assert row["last_fetched_at"] is None
