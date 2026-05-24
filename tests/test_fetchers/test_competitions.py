"""Test the competitions fetcher's parse logic + transactional safety."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ifsc_data.api.client import Fetched
from ifsc_data.db.repository import Repository
from ifsc_data.fetchers import competitions as competitions_fetcher


def _seed_competition(memory_db, *, discipline: str = "lead") -> tuple[Repository, int]:
    repo = Repository(memory_db)
    season_id = repo.upsert_season(2024, year=2024)
    event_id = repo.upsert_event_skeleton(100, season_id=season_id)
    discipline_id = repo.upsert_discipline(discipline)
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


def _stub_client_payload(payload: dict) -> MagicMock:
    """Same as `_stub_client` but takes a full payload dict (with category_rounds, etc.)."""
    client = MagicMock()
    def fake_stream_paths(items, *args, **kwargs):
        for key, path in items:
            yield Fetched(key=key, path=path, data=payload)
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


# ---------------------------------------------------------------- Per-round tests


@pytest.mark.parametrize("label,disc,expected", [
    # disc, expected: round count, route count, has_ascents, optional flags
    ("",        "lead",    {"rounds": 3, "has_lead_ascents": True}),
    ("-speed",  "speed",   {"rounds": 2, "has_speed_ascents": True}),
    ("-boulder","boulder", {"rounds": 3, "has_boulder_ascents": True, "has_starting_group": True}),
    ("-combined","boulder&lead", {"rounds": 2, "has_combined_stages": True}),
])
def test_hydrate_writes_per_round_data(label, disc, expected, fixture, memory_db):
    """Parametrized: each discipline fixture exercises a different code path."""
    payload = fixture(f"events-id-result-id{label}")
    repo, comp_id = _seed_competition(memory_db, discipline=disc)
    client = _stub_client_payload(payload)

    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    # Round count matches.
    n_rounds = memory_db.execute(
        "SELECT COUNT(*) FROM category_rounds WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    assert n_rounds == expected["rounds"]

    # Routes were collected.
    n_routes = memory_db.execute(
        "SELECT COUNT(*) FROM routes r "
        "JOIN category_rounds cr ON r.category_round_id = cr.id "
        "WHERE cr.competition_id = ?", (comp_id,)
    ).fetchone()[0]
    assert n_routes > 0, f"{disc}: no routes collected"

    # round_results filled.
    n_rr = memory_db.execute(
        "SELECT COUNT(*) FROM round_results WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    assert n_rr > 0

    # stage_results filled.
    n_sr = memory_db.execute(
        "SELECT COUNT(*) FROM stage_results WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    assert n_sr > 0

    # ascents filled.
    n_asc = memory_db.execute(
        "SELECT COUNT(*) FROM ascents WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0]
    assert n_asc > 0

    # Discipline-specific assertions.
    if expected.get("has_lead_ascents"):
        n_top = memory_db.execute(
            "SELECT COUNT(*) FROM ascents WHERE competition_id = ? AND top IS NOT NULL", (comp_id,)
        ).fetchone()[0]
        assert n_top > 0
    if expected.get("has_speed_ascents"):
        n_time = memory_db.execute(
            "SELECT COUNT(*) FROM ascents WHERE competition_id = ? AND time_ms IS NOT NULL AND dnf IS NOT NULL", (comp_id,)
        ).fetchone()[0]
        assert n_time > 0
        # Speed final must produce multiple stages (heats).
        n_speed_stages = memory_db.execute(
            "SELECT COUNT(*) FROM round_stages rs "
            "JOIN category_rounds cr ON rs.category_round_id = cr.id "
            "WHERE cr.competition_id = ? AND rs.heat_id IS NOT NULL", (comp_id,)
        ).fetchone()[0]
        assert n_speed_stages > 0
    if expected.get("has_boulder_ascents"):
        n_points = memory_db.execute(
            "SELECT COUNT(*) FROM ascents WHERE competition_id = ? AND points IS NOT NULL AND zone IS NOT NULL", (comp_id,)
        ).fetchone()[0]
        assert n_points > 0
    if expected.get("has_starting_group"):
        n_sg = memory_db.execute(
            "SELECT COUNT(*) FROM round_results WHERE competition_id = ? AND starting_group IS NOT NULL", (comp_id,)
        ).fetchone()[0]
        assert n_sg > 0
    if expected.get("has_combined_stages"):
        n_combined = memory_db.execute(
            "SELECT COUNT(*) FROM round_stages rs "
            "JOIN category_rounds cr ON rs.category_round_id = cr.id "
            "WHERE cr.competition_id = ? AND rs.kind IN ('boulder','lead')", (comp_id,)
        ).fetchone()[0]
        assert n_combined > 0


def test_per_round_rehydrate_clears_stale_rows(fixture, memory_db):
    """Re-running hydrate leaves no orphan stage/round/ascent rows; structural
    rows (category_rounds, routes) are upserted, not deleted."""
    payload = fixture("events-id-result-id")
    repo, comp_id = _seed_competition(memory_db)
    client = _stub_client_payload(payload)

    competitions_fetcher.hydrate(repo, client, stale_days=0)
    counts_first = {
        t: memory_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("category_rounds", "routes", "round_stages",
                  "round_results", "stage_results", "ascents")
    }
    competitions_fetcher.hydrate(repo, client, stale_days=0)
    counts_second = {
        t: memory_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("category_rounds", "routes", "round_stages",
                  "round_results", "stage_results", "ascents")
    }
    assert counts_first == counts_second


def test_per_round_transaction_rolls_back_on_failure(fixture, memory_db, monkeypatch):
    """A mid-ingest failure rolls back the entire per-competition transaction —
    no half-written rounds/stages/ascents are left behind."""
    payload = fixture("events-id-result-id")
    repo, comp_id = _seed_competition(memory_db)
    client = _stub_client_payload(payload)

    original = repo.upsert_ascent
    call_count = {"n": 0}
    def explosive(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 5:
            raise RuntimeError("simulated mid-ascent failure")
        return original(**kwargs)
    monkeypatch.setattr(repo, "upsert_ascent", explosive)

    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (0, 1)

    for tbl in ("category_rounds", "routes", "round_stages",
                "round_results", "stage_results", "ascents", "results"):
        n = memory_db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n == 0, f"{tbl} should be empty after rollback, got {n}"


def test_hydrate_skips_none_items_in_lists(memory_db):
    """The IFSC API occasionally returns None entries inside `ranking[*].rounds[]`
    (paraclimbing categories at events 1223+) and possibly in other arrays.
    The fetcher must skip None gracefully instead of crashing on `.get()`."""
    repo, comp_id = _seed_competition(memory_db)
    payload = {
        "event": "robust event",
        "dcat": "LEAD Men",
        "category_rounds": [
            None,  # malformed top-level entry
            {
                "category_round_id": 100,
                "kind": "lead",
                "name": "Qualification",
                "routes": [None, {"id": 555, "name": "1"}],
                "starting_groups": [None],
                "combined_stages": [None],
            },
        ],
        "ranking": [
            None,  # malformed ranking entry
            {
                "athlete_id": 42,
                "rank": 1,
                "rounds": [
                    None,  # this is the actual case observed
                    {
                        "category_round_id": 100,
                        "round_name": "Qualification",
                        "rank": 1,
                        "score": "TOP",
                        "ascents": [None, {"route_id": 555, "route_name": "1", "top": True}],
                    },
                ],
            },
        ],
    }
    client = _stub_client_payload(payload)
    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)
    # One valid ascent landed.
    assert memory_db.execute(
        "SELECT COUNT(*) FROM ascents WHERE competition_id = ?", (comp_id,)
    ).fetchone()[0] == 1


def test_old_payload_speed_elimination_stages_as_dict(memory_db):
    """Pre-2018 events return `speed_elimination_stages` as a dict (not a list)
    with the per-athlete ascents nested under `ascents[]`. The fetcher must
    not crash trying to iterate the dict's string keys."""
    repo, comp_id = _seed_competition(memory_db, discipline="speed")
    payload = {
        "event": "old event",
        "dcat": "SPEED Men",
        "category_rounds": [
            {
                "category_round_id": 850,
                "kind": "speed",
                "name": "Qualification",
                "routes": [
                    {"id": 2386, "name": "1"},
                    {"id": 2387, "name": "2"},
                ],
            }
        ],
        "ranking": [
            {
                "athlete_id": 12345,
                "rank": 1,
                "rounds": [
                    {
                        "category_round_id": 850,
                        "round_name": "Qualification",
                        "rank": 1,
                        "score": "10.5",
                        # Old format: dict instead of list-of-heats.
                        "speed_elimination_stages": {
                            "ascent": None,
                            "ascents": [
                                {"route_id": 2386, "route_name": "1", "time_ms": 5234, "dnf": False, "dns": False},
                                {"route_id": 2387, "route_name": "2", "time_ms": 5189, "dnf": False, "dns": False},
                            ],
                            "group_name": "A",
                            "route_ranks": {"2386": 1, "2387": 1},
                        },
                    }
                ],
            }
        ],
    }
    client = _stub_client_payload(payload)
    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    # Ascents from the nested dict.ascents[] should land on the default stage.
    rows = memory_db.execute(
        "SELECT time_ms, dnf, dns FROM ascents WHERE competition_id = ? ORDER BY time_ms",
        (comp_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["time_ms"] == 5189
    assert rows[0]["dnf"] == 0
    assert rows[0]["dns"] == 0


def test_speed_final_route_reuse_does_not_violate_unique(fixture, memory_db):
    """The same athlete climbs the same route across multiple speed-final heats.
    UNIQUE (round_stage_id, athlete_id, route_id) must allow this."""
    payload = fixture("events-id-result-id-speed")
    repo, comp_id = _seed_competition(memory_db, discipline="speed")
    client = _stub_client_payload(payload)

    ok, fail = competitions_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    # Find an athlete who appears on the same route in 2+ heats.
    rows = memory_db.execute(
        "SELECT athlete_id, route_id, COUNT(DISTINCT round_stage_id) AS n_stages "
        "FROM ascents WHERE competition_id = ? "
        "GROUP BY athlete_id, route_id HAVING n_stages > 1",
        (comp_id,),
    ).fetchall()
    assert len(rows) > 0, "expected at least one athlete to climb the same route in multiple heats"
