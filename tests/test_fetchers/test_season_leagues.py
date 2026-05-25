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
    """
    repo = Repository(memory_db)
    # Sibling season with the same ifsc_id as the payload's "season" field —
    # would be picked by a wrong `WHERE ifsc_id = ?` regression. Its year is
    # NOT 2024, so the correct year-based lookup ignores it.
    decoy_season_id = repo.upsert_season(2024, year=1999)
    # The season the lookup SHOULD find — ifsc_id distinct from year.
    season_id = repo.upsert_season(443, year=2024)
    sl_id = repo.upsert_season_league(100)  # not yet linked to a season

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
    repo.upsert_season(2024, year=2024)
    repo.upsert_season_league(100)

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
    repo.upsert_season(2020, year=2020)
    repo.upsert_season_league(7)

    payload = {"season": "2020", "league": "World Cup", "events": []}  # no d_cats
    client = _stub_client_returning(payload)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)


def test_hydrate_fixture_smoke(memory_db, fixture):
    """Full captured payload → expected 5 disciplines + 2 categories.

    The 2025 fixture has 10 d_cats across 5 disciplines for Men+Women, but
    the `categories` table is UNIQUE(name), so 'Men' and 'Women' are
    deduplicated across disciplines → only 2 rows.
    """
    repo = Repository(memory_db)
    data = fixture("season_leagues-id")
    repo.upsert_season(2025, year=2025)
    repo.upsert_season_league(443)

    client = _stub_client_returning(data)
    ok, fail = sl_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    disciplines = {r[0] for r in memory_db.execute("SELECT name FROM disciplines")}
    assert disciplines == {"lead", "speed", "boulder", "combined", "boulder&lead"}

    cat_rows = {r["name"]: r["gender"] for r in memory_db.execute("SELECT name, gender FROM categories")}
    assert cat_rows == {"Men": 0, "Women": 1}
