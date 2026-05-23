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


# --------------------------------------------------------------- find_ongoing_*
# These power `pull-new`'s ongoing-only scope. See ADR 0006.

def test_find_ongoing_seasons_includes_current_and_skeletons(memory_db):
    repo = Repository(memory_db)
    current_year = datetime.now(timezone.utc).year

    ongoing = repo.upsert_season(101, year=current_year)
    ended = repo.upsert_season(102, year=current_year - 5)
    skeleton = repo.upsert_season(103)  # year IS NULL

    got = {r["id"] for r in repo.find_ongoing_seasons()}
    assert ongoing in got
    assert skeleton in got
    assert ended not in got


def test_find_ongoing_season_leagues_follows_parent_season(memory_db):
    repo = Repository(memory_db)
    current_year = datetime.now(timezone.utc).year

    ongoing_season = repo.upsert_season(201, year=current_year)
    ended_season = repo.upsert_season(202, year=current_year - 5)
    league = repo.upsert_league("World Cup")

    ongoing_sl = repo.upsert_season_league(301, season_id=ongoing_season, league_id=league)
    ended_sl = repo.upsert_season_league(302, season_id=ended_season, league_id=league)

    got = {r["id"] for r in repo.find_ongoing_season_leagues()}
    assert ongoing_sl in got
    assert ended_sl not in got


def test_find_ongoing_events_respects_grace_days(memory_db):
    repo = Repository(memory_db)
    today = datetime.now(timezone.utc).date()
    sid = repo.upsert_season(401, year=today.year)

    future = repo.upsert_event_skeleton(501, season_id=sid)
    repo.update_event(future, date_end=(today + timedelta(days=10)).isoformat())

    recent_ended = repo.upsert_event_skeleton(502, season_id=sid)
    repo.update_event(recent_ended, date_end=(today - timedelta(days=10)).isoformat())

    long_ended = repo.upsert_event_skeleton(503, season_id=sid)
    repo.update_event(long_ended, date_end=(today - timedelta(days=60)).isoformat())

    skeleton = repo.upsert_event_skeleton(504, season_id=sid)  # date_end IS NULL

    # Default grace (15 days): future + recent_ended (10 days < 15) + NULL skeleton.
    got = {r["id"] for r in repo.find_ongoing_events()}
    assert future in got
    assert recent_ended in got
    assert skeleton in got
    assert long_ended not in got

    # Strict (grace_days=0): only future + skeleton; recent_ended now excluded.
    strict = {r["id"] for r in repo.find_ongoing_events(grace_days=0)}
    assert future in strict
    assert skeleton in strict
    assert recent_ended not in strict


def test_find_ongoing_competitions_joins_through_event(memory_db):
    repo = Repository(memory_db)
    today = datetime.now(timezone.utc).date()
    sid = repo.upsert_season(601, year=today.year)
    discipline = repo.upsert_discipline("lead")
    category = repo.upsert_category("Men", gender=0)

    ongoing_event = repo.upsert_event_skeleton(701, season_id=sid)
    repo.update_event(ongoing_event, date_end=(today + timedelta(days=5)).isoformat())

    ended_event = repo.upsert_event_skeleton(702, season_id=sid)
    repo.update_event(ended_event, date_end=(today - timedelta(days=60)).isoformat())

    ongoing_comp = repo.upsert_competition(
        event_id=ongoing_event, ifsc_id=801,
        discipline_id=discipline, category_id=category,
    )
    ended_comp = repo.upsert_competition(
        event_id=ended_event, ifsc_id=802,
        discipline_id=discipline, category_id=category,
    )

    got = {r["comp_id"] for r in repo.find_ongoing_competitions()}
    assert ongoing_comp in got
    assert ended_comp not in got

    # Shape check: caller relies on these columns.
    row = next(r for r in repo.find_ongoing_competitions() if r["comp_id"] == ongoing_comp)
    assert row["comp_ifsc"] == 801
    assert row["event_ifsc"] == 701
