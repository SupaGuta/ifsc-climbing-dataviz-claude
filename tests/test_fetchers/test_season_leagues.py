"""Tests for the season_leagues fetcher.

Focus areas:
  * `_ingest_d_cat` discipline + gender split (the regex contract).
  * `hydrate` resolves season_id by year, creates events, marks fetched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wcl_data.api.client import Fetched
from wcl_data.db.repository import Repository
from wcl_data.fetchers import season_leagues as sl_fetcher
from wcl_data.fetchers.season_leagues import _ingest_d_cat


# --- _ingest_d_cat -----------------------------------------------------------

@pytest.mark.parametrize("d_cat_name,expected_discipline,expected_category,expected_gender", [
    ("LEAD Men", "lead", "Men", 0),
    ("SPEED Men", "speed", "Men", 0),
    ("BOULDER Men", "boulder", "Men", 0),
    ("COMBINED Men", "combined", "Men", 0),
    ("LEAD Women", "lead", "Women", 1),
    ("SPEED Women", "speed", "Women", 1),
    ("BOULDER Women", "boulder", "Women", 1),
    ("BOULDER&LEAD Women", "boulder&lead", "Women", 1),
    # "Male"/"Female" are also valid per the regex; check both branches.
    ("Lead Male", "lead", "Male", 0),
    ("Lead Female", "lead", "Female", 1),
])
def test_ingest_d_cat_extracts_discipline_and_gender(
    memory_db, d_cat_name, expected_discipline, expected_category, expected_gender,
):
    repo = Repository(memory_db)
    _ingest_d_cat(repo, d_cat_name)

    discipline_row = memory_db.execute("SELECT name FROM disciplines").fetchone()
    assert discipline_row["name"] == expected_discipline

    cat_row = memory_db.execute("SELECT name, gender FROM categories").fetchone()
    assert cat_row["name"] == expected_category
    assert cat_row["gender"] == expected_gender


def test_ingest_d_cat_skips_empty(memory_db):
    """Empty / whitespace-only / category-less names should not create rows."""
    repo = Repository(memory_db)
    _ingest_d_cat(repo, "")
    _ingest_d_cat(repo, "   ")
    _ingest_d_cat(repo, "OnlyDiscipline")  # no second token → skipped

    assert memory_db.execute("SELECT COUNT(*) FROM disciplines").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0


def test_ingest_d_cat_youth_category_has_null_gender(memory_db):
    """Categories without a recognisable gender keyword (Youth A, U18) get NULL gender."""
    repo = Repository(memory_db)
    _ingest_d_cat(repo, "LEAD U18")
    cat_row = memory_db.execute("SELECT name, gender FROM categories").fetchone()
    assert cat_row["name"] == "U18"
    assert cat_row["gender"] is None


# --- hydrate ----------------------------------------------------------------

def _stub_client_returning(payload: dict) -> MagicMock:
    client = MagicMock()
    client.stream.side_effect = lambda endpoint, ids, *_a, **_kw: iter(
        [Fetched(key=i, path=f"/{endpoint}/{i}", data=payload) for i in ids]
    )
    return client


def test_hydrate_resolves_season_id_by_year(memory_db):
    """The fetcher resolves season_id via `WHERE year = ?` (the API payload's
    `season` field is a year string). The test uses ifsc_id=443 distinct from
    year=2024 so a regression that swapped the lookup to `WHERE ifsc_id = ?`
    would point at the wrong row.

    v6 schema requires season_leagues.season_id/league_id NOT NULL, so the
    skeleton is seeded with the *decoy* season as its initial parent — the
    test then proves hydrate overwrites it with the year-resolved one
    (rather than COALESCE-preserving the seeded decoy).
    """
    repo = Repository(memory_db)
    # Sibling season with the same ifsc_id as the payload's "season" field —
    # would be picked by a wrong `WHERE ifsc_id = ?` regression. Its year is
    # NOT 2024, so the correct year-based lookup ignores it.
    decoy_season_id = repo.upsert_season(2024, year=1999)
    # The season the lookup SHOULD find — ifsc_id distinct from year.
    season_id = repo.upsert_season(443, year=2024)
    placeholder_league_id = repo.upsert_league("Placeholder League")
    sl_id = repo.upsert_season_league(
        100, season_id=decoy_season_id, league_id=placeholder_league_id,
    )

    payload = {
        "season": "2024",
        "league": "World Cup",
        "d_cats": [{"name": "LEAD Men"}],
        "events": [],
    }
    client = _stub_client_returning(payload)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    row = memory_db.execute(
        "SELECT season_id, league_id, last_fetched_at FROM season_leagues WHERE id = ?",
        (sl_id,),
    ).fetchone()
    assert row["season_id"] == season_id
    assert row["season_id"] != decoy_season_id   # year-lookup found the right row
    assert row["league_id"] is not None
    assert row["last_fetched_at"] is not None


def test_hydrate_creates_event_skeletons(memory_db):
    repo = Repository(memory_db)
    season_id = repo.upsert_season(2024, year=2024)
    league_id = repo.upsert_league("Placeholder League")
    repo.upsert_season_league(100, season_id=season_id, league_id=league_id)

    payload = {
        "season": "2024",
        "league": "World Cup",
        "d_cats": [{"name": "LEAD Men"}, {"name": "BOULDER Women"}],
        "events": [
            {"event_id": 1001},
            {"event_id": 1002},
            {"event_id": None},  # should be silently dropped
        ],
    }
    client = _stub_client_returning(payload)
    sl_fetcher.hydrate(repo, client, stale_days=0)

    event_ifscs = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM events")}
    assert event_ifscs == {1001, 1002}

    # Both d_cats produced disciplines + categories.
    disciplines = {r[0] for r in memory_db.execute("SELECT name FROM disciplines")}
    categories = {r[0] for r in memory_db.execute("SELECT name FROM categories")}
    assert disciplines == {"lead", "boulder"}
    assert categories == {"Men", "Women"}


def test_hydrate_handles_missing_d_cats_field(memory_db):
    """A payload without `d_cats` (early API versions) must not crash the loop."""
    repo = Repository(memory_db)
    season_id = repo.upsert_season(2020, year=2020)
    league_id = repo.upsert_league("Placeholder League")
    repo.upsert_season_league(7, season_id=season_id, league_id=league_id)

    payload = {"season": "2020", "league": "World Cup", "events": []}  # no d_cats
    client = _stub_client_returning(payload)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)


def test_hydrate_skips_upserts_when_season_unresolvable(memory_db, caplog):
    """v6 NOT NULL on season_leagues.season_id/league_id and events.season_id
    means we can't write a row whose FKs are unresolved. hydrate must skip
    the upsert (logging a WARN) and still mark_fetched so the row leaves the
    stale pool — otherwise the same unresolvable payload re-fails every cycle.
    """
    import logging
    repo = Repository(memory_db)
    # Stage an existing season_league row with valid parents (so the existing
    # NOT NULL state holds going in). Season's year is 1999 — the payload's
    # `season: "2024"` won't match → season_id stays None.
    s = repo.upsert_season(443, year=1999)
    ld = repo.upsert_league("Placeholder")
    sl_id = repo.upsert_season_league(100, season_id=s, league_id=ld)

    payload = {
        "season": "2024",  # no season with year=2024 exists → unresolvable
        "league": "World Cup",
        "d_cats": [{"name": "LEAD Men"}],
        "events": [{"event_id": 9999}],
    }
    client = _stub_client_returning(payload)
    with caplog.at_level(logging.WARNING):
        ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)
    assert any(
        "could not resolve" in rec.message for rec in caplog.records
    ), f"Expected a 'could not resolve' WARN, got: {[r.message for r in caplog.records]}"

    # The existing season_league row keeps its original FKs (no overwrite).
    row = memory_db.execute(
        "SELECT season_id, league_id, last_fetched_at FROM season_leagues WHERE id = ?",
        (sl_id,),
    ).fetchone()
    assert row["season_id"] == s
    assert row["league_id"] == ld
    assert row["last_fetched_at"] is not None

    # No event was created (season_id was None → event skeleton skipped).
    n = memory_db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 0


def test_hydrate_rolls_back_iteration_on_parse_failure(memory_db):
    """v6 D9 pattern: the per-iteration `with repo.transaction():` wrap means
    a mid-iteration parse failure rolls back any writes from this iteration
    (the upsert_league side-effect, partial event skeletons, etc.) rather
    than leaving the warehouse half-populated for the row that failed."""
    repo = Repository(memory_db)
    s = repo.upsert_season(443, year=2024)
    ld = repo.upsert_league("Placeholder")
    repo.upsert_season_league(100, season_id=s, league_id=ld)

    # Force a failure inside the iteration via a non-iterable `events`.
    payload = {
        "season": "2024",
        "league": "Brand New League",  # would create a new leagues row
        "d_cats": [{"name": "LEAD Men"}],
        "events": 12345,  # non-iterable triggers TypeError mid-iteration
    }
    client = _stub_client_returning(payload)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (0, 1)

    # The "Brand New League" upsert is rolled back (transaction protects it).
    leagues = {r[0] for r in memory_db.execute("SELECT name FROM leagues")}
    assert "Brand New League" not in leagues


def test_hydrate_fixture_smoke(memory_db, fixture):
    """Full captured payload → expected 5 disciplines + 2 categories.

    The 2025 fixture has 10 d_cats across 5 disciplines for Men+Women, but
    the `categories` table is UNIQUE(name), so 'Men' and 'Women' are
    deduplicated across disciplines → only 2 rows.
    """
    repo = Repository(memory_db)
    data = fixture("season_leagues-id")
    season_id = repo.upsert_season(2025, year=2025)
    league_id = repo.upsert_league("Placeholder League")
    repo.upsert_season_league(443, season_id=season_id, league_id=league_id)

    client = _stub_client_returning(data)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    disciplines = {r[0] for r in memory_db.execute("SELECT name FROM disciplines")}
    assert disciplines == {"lead", "speed", "boulder", "combined", "boulder&lead"}

    cat_rows = {r["name"]: r["gender"] for r in memory_db.execute("SELECT name, gender FROM categories")}
    assert cat_rows == {"Men": 0, "Women": 1}
