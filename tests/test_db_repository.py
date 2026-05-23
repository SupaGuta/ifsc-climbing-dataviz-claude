"""Tests for the repository layer against an in-memory SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ifsc_data.db.repository import Repository, TS_FMT


def test_upsert_season_is_idempotent(memory_db):
    repo = Repository(memory_db)
    a = repo.upsert_season(99, year=2030)
    b = repo.upsert_season(99, year=2030)
    assert a == b
    assert repo.count("seasons") == 1


def test_mark_fetched_makes_row_non_stale(memory_db):
    repo = Repository(memory_db)
    season_id = repo.upsert_season(1, year=1990)

    # Before mark_fetched: row is stale (NULL last_fetched_at).
    stale = repo.find_stale("seasons", stale_days=30)
    assert any(r["id"] == season_id for r in stale)

    repo.mark_fetched("seasons", season_id)
    stale = repo.find_stale("seasons", stale_days=30)
    assert all(r["id"] != season_id for r in stale)


def test_find_stale_includes_old_rows(memory_db):
    repo = Repository(memory_db)
    season_id = repo.upsert_season(1)
    # Hand-set an ancient timestamp.
    memory_db.execute(
        "UPDATE seasons SET last_fetched_at = '2000-01-01 00:00:00' WHERE id = ?",
        (season_id,),
    )
    memory_db.commit()
    stale = repo.find_stale("seasons", stale_days=1)
    assert any(r["id"] == season_id for r in stale)


def test_update_athlete_only_writes_allowed_fields(memory_db):
    repo = Repository(memory_db)
    aid = repo.upsert_athlete_skeleton(1364)
    repo.update_athlete(
        aid,
        firstname="Adam",
        lastname="ONDRA",
        gender=0,
        unknown_field="should be ignored",
    )
    row = memory_db.execute("SELECT * FROM athletes WHERE id = ?", (aid,)).fetchone()
    assert row["firstname"] == "Adam"
    assert row["lastname"] == "ONDRA"
    assert row["gender"] == 0


def test_find_stale_zero_days_returns_recently_fetched_and_null(memory_db):
    """stale_days=0 ⇒ cutoff is "now"; everything older than that or NULL qualifies.

    This pins the boundary that `pull-new` relies on for forcing a refresh.
    """
    repo = Repository(memory_db)

    null_id = repo.upsert_season(100)        # last_fetched_at IS NULL

    fetched_id = repo.upsert_season(101)
    one_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(TS_FMT)
    memory_db.execute(
        "UPDATE seasons SET last_fetched_at = ? WHERE id = ?",
        (one_sec_ago, fetched_id),
    )
    memory_db.commit()

    stale = {r["id"] for r in repo.find_stale("seasons", stale_days=0)}
    assert null_id in stale
    assert fetched_id in stale


def test_transaction_commits_on_success(memory_db):
    repo = Repository(memory_db)
    with repo.transaction():
        repo.upsert_season(1, year=2024)
        repo.upsert_season(2, year=2025)
    rows = memory_db.execute("SELECT COUNT(*) FROM seasons").fetchone()[0]
    assert rows == 2


def test_transaction_rolls_back_on_exception(memory_db):
    repo = Repository(memory_db)
    pre = repo.upsert_season(99, year=1999)

    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.upsert_season(100, year=2030)
            raise RuntimeError("simulated mid-transaction failure")

    # The new row should be gone; the pre-existing one untouched.
    ids = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons").fetchall()}
    assert ids == {99}


def test_validate_table_rejects_unknown_table(memory_db):
    repo = Repository(memory_db)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.count("not_a_real_table")
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.find_stale("results", stale_days=30)  # results has no last_fetched_at


def test_backfill_event_country_from_siblings(memory_db):
    """A NULL-country event should inherit from a sibling event with the same city."""
    repo = Repository(memory_db)
    sid = repo.upsert_season(2024, year=2024)
    # Event A: has city + country
    a = repo.upsert_event_skeleton(1, season_id=sid)
    repo.update_event(a, name="A", city="Innsbruck", country="AUT")
    # Event B: same city, no country
    b = repo.upsert_event_skeleton(2, season_id=sid)
    repo.update_event(b, name="B", city="Innsbruck", country=None)

    affected = repo.backfill_event_country_from_siblings()
    assert affected == 1

    row = memory_db.execute("SELECT country FROM events WHERE id = ?", (b,)).fetchone()
    assert row["country"] == "AUT"
