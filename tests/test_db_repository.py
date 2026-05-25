"""Tests for the repository layer against an in-memory SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from wcl_data.db.repository import Repository, TS_FMT


# Stable "today" for find_ongoing_* tests — chosen mid-year so a calendar-year
# rollover near test execution can't flip the "ongoing" boundary on us.
FROZEN_NOW = "2026-06-15"


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


def test_transaction_rolls_back_on_keyboard_interrupt(memory_db):
    """Ctrl-C during a transaction body must roll back any in-flight rows.

    `KeyboardInterrupt` is a `BaseException`, not an `Exception` — the
    repository's `except BaseException` clause is what catches it. A future
    refactor that narrows that catch to `except Exception` would silently
    let half-committed transaction state leak past the interrupt; this
    test pins the contract.
    """
    repo = Repository(memory_db)
    pre = repo.upsert_season(99, year=1999)

    with pytest.raises(KeyboardInterrupt):
        with repo.transaction():
            repo.upsert_season(200, year=2031)
            raise KeyboardInterrupt

    ids = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons").fetchall()}
    assert ids == {99}
    # The transaction flag must have been reset so a second transaction works.
    with repo.transaction():
        repo.upsert_season(300, year=2032)
    ids_after = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons").fetchall()}
    assert ids_after == {99, 300}


def test_nested_transaction_outer_commits_both_rows(tmp_path):
    """Nested `with repo.transaction()` calls flatten — only the outermost commits.

    Proves the deferred-commit contract by opening a SECOND connection that
    can only see committed state (WAL isolates uncommitted writes from
    other readers). The inner `__exit__` must NOT have committed; the
    second connection sees zero rows until the outer block exits.

    A previous version of this test read `repo._in_transaction` directly —
    brittle because a refactor to depth-counter semantics for proper
    SAVEPOINT nesting would change the type without changing the
    user-visible contract.
    """
    from wcl_data.db.schema import open_db

    db_path = tmp_path / "wcl.sqlite"
    writer_conn = open_db(db_path)
    reader_conn = open_db(db_path)
    try:
        repo = Repository(writer_conn)
        with repo.transaction():
            repo.upsert_season(1, year=2020)
            with repo.transaction():
                repo.upsert_season(2, year=2021)
            # Inside outer transaction: a SEPARATE connection sees ZERO
            # committed rows — the inner __exit__ did NOT trip a commit.
            uncommitted = list(reader_conn.execute(
                "SELECT ifsc_id FROM seasons"
            ))
            assert uncommitted == [], (
                "inner transaction __exit__ must not commit; reader connection "
                "should still see the pre-outer-block state"
            )

        # Outer __exit__ committed both rows; reader sees them now.
        committed = {r[0] for r in reader_conn.execute("SELECT ifsc_id FROM seasons")}
        assert committed == {1, 2}
    finally:
        writer_conn.close()
        reader_conn.close()


def test_nested_transaction_inner_exception_rolls_back_outer(memory_db):
    """An exception in a nested block propagates through and rolls back the
    outer transaction — both rows should be absent.
    """
    repo = Repository(memory_db)

    with pytest.raises(RuntimeError, match="inner failure"):
        with repo.transaction():
            repo.upsert_season(1, year=2020)
            with repo.transaction():
                repo.upsert_season(2, year=2021)
                raise RuntimeError("inner failure")

    ids = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons")}
    assert ids == set()
    # A second transaction must still be entrable — i.e. the outer __exit__
    # cleanly reset internal state regardless of how it tracks nesting.
    with repo.transaction():
        repo.upsert_season(7, year=2030)
    assert {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons")} == {7}


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
# These power `pull-new`'s ongoing-only scope. See ADR 0006. Time is frozen so
# the "ongoing" boundary stays stable regardless of when the test runs (a
# calendar-year rollover would otherwise flip the year=current_year assertions).

@freeze_time(FROZEN_NOW)
def test_find_ongoing_seasons_includes_current_and_skeletons(memory_db):
    repo = Repository(memory_db)
    current_year = 2026  # matches FROZEN_NOW

    ongoing = repo.upsert_season(101, year=current_year)
    ended = repo.upsert_season(102, year=current_year - 5)
    skeleton = repo.upsert_season(103)  # year IS NULL

    got = {r["id"] for r in repo.find_ongoing_seasons()}
    assert ongoing in got
    assert skeleton in got
    assert ended not in got


@freeze_time(FROZEN_NOW)
def test_find_ongoing_season_leagues_follows_parent_season(memory_db):
    repo = Repository(memory_db)
    current_year = 2026

    ongoing_season = repo.upsert_season(201, year=current_year)
    ended_season = repo.upsert_season(202, year=current_year - 5)
    league = repo.upsert_league("World Cup")

    ongoing_sl = repo.upsert_season_league(301, season_id=ongoing_season, league_id=league)
    ended_sl = repo.upsert_season_league(302, season_id=ended_season, league_id=league)

    got = {r["id"] for r in repo.find_ongoing_season_leagues()}
    assert ongoing_sl in got
    assert ended_sl not in got


@freeze_time(FROZEN_NOW)
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


@freeze_time(FROZEN_NOW)
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


# ----------------------------------------------------------- Per-round upserts


def _seed_competition_minimal(memory_db):
    repo = Repository(memory_db)
    sid = repo.upsert_season(2024, year=2024)
    eid = repo.upsert_event_skeleton(1, season_id=sid)
    did = repo.upsert_discipline("lead")
    cid = repo.upsert_category("Men", gender=0)
    comp_id = repo.upsert_competition(
        event_id=eid, ifsc_id=1, discipline_id=did, category_id=cid
    )
    return repo, comp_id


def test_upsert_category_round_coalesce_preserves_values(memory_db):
    repo, comp_id = _seed_competition_minimal(memory_db)
    a = repo.upsert_category_round(
        100, competition_id=comp_id, kind="lead", name="Qualification",
    )
    # Re-upsert without name; COALESCE preserves the previous name.
    b = repo.upsert_category_round(100, competition_id=comp_id, kind="lead")
    assert a == b
    row = memory_db.execute("SELECT name FROM category_rounds WHERE id = ?", (a,)).fetchone()
    assert row["name"] == "Qualification"


def test_upsert_route_preserves_original_category_round_id(memory_db):
    """Route World Climbing ids are globally unique on the API — a collision under a
    different category_round must not silently re-parent the route (which would
    corrupt existing ascents). The conflict resolution preserves the original
    parent and only updates COALESCE'd fields. See code review tier-A fix."""
    repo, comp_id = _seed_competition_minimal(memory_db)
    cr1 = repo.upsert_category_round(100, competition_id=comp_id, name="Qualif")
    cr2 = repo.upsert_category_round(101, competition_id=comp_id, name="Final")
    rt = repo.upsert_route(500, category_round_id=cr1, name="A")
    rt2 = repo.upsert_route(500, category_round_id=cr2, name=None)
    assert rt == rt2
    row = memory_db.execute("SELECT category_round_id, name FROM routes WHERE id = ?", (rt,)).fetchone()
    assert row["category_round_id"] == cr1, "route must keep its original cr"
    assert row["name"] == "A"


def test_upsert_category_round_preserves_original_competition_id(memory_db):
    """IFSC category_round_id is globally unique — re-upsert under a different
    competition must not flip competition_id. See code review tier-A fix."""
    repo, comp_a = _seed_competition_minimal(memory_db)
    # Seed a second competition under the same event.
    sid = repo.upsert_season(2025, year=2025)
    eid = repo.upsert_event_skeleton(2, season_id=sid)
    did = repo.upsert_discipline("lead")
    cid = repo.upsert_category("Men", gender=0)
    comp_b = repo.upsert_competition(
        event_id=eid, ifsc_id=2, discipline_id=did, category_id=cid
    )

    cr_a = repo.upsert_category_round(777, competition_id=comp_a, name="Qualif")
    cr_b = repo.upsert_category_round(777, competition_id=comp_b, name=None)
    assert cr_a == cr_b
    row = memory_db.execute(
        "SELECT competition_id, name FROM category_rounds WHERE id = ?", (cr_a,)
    ).fetchone()
    assert row["competition_id"] == comp_a, "category_round must keep its original comp"
    assert row["name"] == "Qualif"


def test_upsert_round_stage_unique_on_round_seq(memory_db):
    repo, comp_id = _seed_competition_minimal(memory_db)
    cr = repo.upsert_category_round(100, competition_id=comp_id)
    a = repo.upsert_round_stage(category_round_id=cr, seq=0)
    b = repo.upsert_round_stage(category_round_id=cr, seq=0, name="Default")
    assert a == b
    row = memory_db.execute("SELECT name FROM round_stages WHERE id = ?", (a,)).fetchone()
    assert row["name"] == "Default"


def test_upsert_round_result_replace_semantics(memory_db):
    repo, comp_id = _seed_competition_minimal(memory_db)
    cr = repo.upsert_category_round(100, competition_id=comp_id)
    ath = repo.upsert_athlete_skeleton(42)
    repo.upsert_round_result(
        competition_id=comp_id, category_round_id=cr, athlete_id=ath, rank=10, score="7.0"
    )
    # Second insert replaces the first.
    repo.upsert_round_result(
        competition_id=comp_id, category_round_id=cr, athlete_id=ath, rank=5, score="8.5",
        starting_group="Group A",
    )
    rows = memory_db.execute("SELECT rank, score, starting_group FROM round_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["rank"] == 5
    assert rows[0]["score"] == "8.5"
    assert rows[0]["starting_group"] == "Group A"


def test_upsert_ascent_unique_on_stage_athlete_route(memory_db):
    """Speed-final allows the same (athlete, route) across different heats;
    UNIQUE on (round_stage_id, athlete_id, route_id) lets this in."""
    repo, comp_id = _seed_competition_minimal(memory_db)
    cr = repo.upsert_category_round(100, competition_id=comp_id)
    s0 = repo.upsert_round_stage(category_round_id=cr, seq=0, name="1/8")
    s1 = repo.upsert_round_stage(category_round_id=cr, seq=1, name="1/4")
    ath = repo.upsert_athlete_skeleton(42)
    rt = repo.upsert_route(500, category_round_id=cr, name="A")

    repo.upsert_ascent(
        competition_id=comp_id, round_stage_id=s0, route_id=rt, athlete_id=ath, time_ms=4827
    )
    repo.upsert_ascent(
        competition_id=comp_id, round_stage_id=s1, route_id=rt, athlete_id=ath, time_ms=4797
    )
    n = memory_db.execute("SELECT COUNT(*) FROM ascents").fetchone()[0]
    assert n == 2


def test_delete_round_data_for_competition_preserves_structural_rows(memory_db):
    repo, comp_id = _seed_competition_minimal(memory_db)
    cr = repo.upsert_category_round(100, competition_id=comp_id)
    rt = repo.upsert_route(500, category_round_id=cr)
    stage = repo.upsert_round_stage(category_round_id=cr, seq=0)
    ath = repo.upsert_athlete_skeleton(42)
    repo.upsert_round_result(
        competition_id=comp_id, category_round_id=cr, athlete_id=ath, rank=1, score="T"
    )
    repo.upsert_stage_result(
        competition_id=comp_id, round_stage_id=stage, athlete_id=ath, rank=1
    )
    repo.upsert_ascent(
        competition_id=comp_id, round_stage_id=stage, route_id=rt, athlete_id=ath, top=1
    )

    repo.delete_round_data_for_competition(comp_id)

    assert memory_db.execute("SELECT COUNT(*) FROM ascents").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM stage_results").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM round_results").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM round_stages").fetchone()[0] == 0
    # Structural rows preserved.
    assert memory_db.execute("SELECT COUNT(*) FROM category_rounds").fetchone()[0] == 1
    assert memory_db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1


# ---------------------------------------------------------------- Schema v5 migration

def test_v5_migration_drops_dead_last_fetched_at(memory_db):
    """v4 → v5 drops `last_fetched_at` from category_rounds + routes (and their
    indexes). The columns were reserved for a startlist hydrator that never
    landed and were never set by `mark_fetched` — see 2026-05-25 note on ADR 0007.

    We simulate a v4 DB by re-adding the columns and indexes by hand, then
    re-run apply_schema and verify they're gone.
    """
    from wcl_data.db.schema import apply_schema

    memory_db.execute("ALTER TABLE category_rounds ADD COLUMN last_fetched_at TEXT")
    memory_db.execute(
        "CREATE INDEX idx_category_rounds_last_fetched "
        "ON category_rounds(last_fetched_at)"
    )
    memory_db.execute("ALTER TABLE routes ADD COLUMN last_fetched_at TEXT")
    memory_db.execute(
        "CREATE INDEX idx_routes_last_fetched ON routes(last_fetched_at)"
    )
    memory_db.commit()

    apply_schema(memory_db)

    cr_cols = {r[1] for r in memory_db.execute("PRAGMA table_info(category_rounds)")}
    rt_cols = {r[1] for r in memory_db.execute("PRAGMA table_info(routes)")}
    assert "last_fetched_at" not in cr_cols
    assert "last_fetched_at" not in rt_cols

    cr_idx = {r[1] for r in memory_db.execute("PRAGMA index_list(category_rounds)")}
    rt_idx = {r[1] for r in memory_db.execute("PRAGMA index_list(routes)")}
    assert "idx_category_rounds_last_fetched" not in cr_idx
    assert "idx_routes_last_fetched" not in rt_idx


def test_hydratable_tables_excludes_category_rounds_and_routes(memory_db):
    """category_rounds and routes are no longer hydratable since v5."""
    from wcl_data.db.repository import HYDRATABLE_TABLES

    assert "category_rounds" not in HYDRATABLE_TABLES
    assert "routes" not in HYDRATABLE_TABLES

    repo = Repository(memory_db)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.find_stale("category_rounds", stale_days=0)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.find_stale("routes", stale_days=0)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.mark_fetched("category_rounds", 1)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.count_hydrated("routes")


def test_category_rounds_and_routes_still_in_all_tables(memory_db):
    """They remain countable via the generic Repository.count()."""
    repo = Repository(memory_db)
    assert repo.count("category_rounds") == 0
    assert repo.count("routes") == 0


# ---------------------------------------------------------------- Status helpers

def test_schema_version_matches_current(memory_db):
    from wcl_data.db.schema import CURRENT_VERSION

    repo = Repository(memory_db)
    assert repo.schema_version() == CURRENT_VERSION


def test_latest_fetched_at_returns_max(memory_db):
    repo = Repository(memory_db)
    older = repo.upsert_season(1, year=2020)
    newer = repo.upsert_season(2, year=2021)
    memory_db.execute(
        "UPDATE seasons SET last_fetched_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (older,),
    )
    memory_db.execute(
        "UPDATE seasons SET last_fetched_at = '2026-05-25T12:00:00Z' WHERE id = ?",
        (newer,),
    )
    memory_db.commit()
    assert repo.latest_fetched_at("seasons") == "2026-05-25T12:00:00Z"


def test_latest_fetched_at_returns_none_on_empty(memory_db):
    repo = Repository(memory_db)
    assert repo.latest_fetched_at("seasons") is None


def test_latest_fetched_at_rejects_non_hydratable(memory_db):
    repo = Repository(memory_db)
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.latest_fetched_at("leagues")
    with pytest.raises(ValueError, match="not in allowed set"):
        repo.latest_fetched_at("category_rounds")


# ---------------------------------------------------------------- cup_rankings
# `upsert_cup_ranking` uses `INSERT OR REPLACE`, which deletes the conflicting
# row and inserts a fresh one — so the autoincrement `id` churns on every
# re-hydration. Phase D4 in REVIEW.md proposes switching this to a UNIQUE
# index + `ON CONFLICT ... UPDATE` (which preserves ids). When that lands,
# the `test_cup_ranking_id_churns_on_replace` test will fail — making the
# contract switch visible rather than silent.

def test_cup_ranking_upsert_replaces_value(memory_db):
    """A re-upsert on the same UNIQUE key (athlete, cup, d_cat) overwrites the row.

    `d_cat_id=1` (non-NULL) is needed: SQLite UNIQUE treats NULL d_cat_id as
    distinct on every insert, so a NULL→NULL "re-upsert" would actually
    insert a second row. See `test_cup_ranking_unique_treats_null_d_cat_as_distinct`.
    """
    repo = Repository(memory_db)
    ath = repo.upsert_athlete_skeleton(42)

    repo.upsert_cup_ranking(
        athlete_id=ath, cup_ifsc_id=100, cup_name="World Cup", d_cat_id=1, rank=5,
    )
    repo.upsert_cup_ranking(
        athlete_id=ath, cup_ifsc_id=100, cup_name="World Cup", d_cat_id=1, rank=1,
    )
    rows = list(memory_db.execute(
        "SELECT rank FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = ?",
        (ath, 100),
    ))
    assert len(rows) == 1
    assert rows[0]["rank"] == 1


def test_cup_ranking_id_churns_on_replace(memory_db):
    """`INSERT OR REPLACE` deletes + re-inserts → the autoincrement id changes.

    This is the current behavior; D4 proposes switching to UPSERT (which
    preserves ids). When the switch lands, flip this test to assert
    `id1 == id2` to pin the new contract.
    """
    repo = Repository(memory_db)
    ath = repo.upsert_athlete_skeleton(42)

    repo.upsert_cup_ranking(
        athlete_id=ath, cup_ifsc_id=100, cup_name="World Cup", d_cat_id=1, rank=5,
    )
    id1 = memory_db.execute(
        "SELECT id FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = ? AND d_cat_id = ?",
        (ath, 100, 1),
    ).fetchone()[0]

    repo.upsert_cup_ranking(
        athlete_id=ath, cup_ifsc_id=100, cup_name="World Cup", d_cat_id=1, rank=1,
    )
    id2 = memory_db.execute(
        "SELECT id FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = ? AND d_cat_id = ?",
        (ath, 100, 1),
    ).fetchone()[0]

    assert id1 != id2, (
        "cup_ranking id should churn under INSERT OR REPLACE. If this fails, "
        "the implementation likely switched to UPSERT — update the assertion "
        "to id1 == id2 and revise REVIEW.md D4."
    )


def test_cup_ranking_unique_treats_null_d_cat_as_distinct(memory_db):
    """SQLite UNIQUE treats NULL as distinct per-NULL — two NULL-d_cat rows on
    the same (athlete, cup) co-exist. This is the SQLite default; REVIEW.md D4
    proposes a partial unique index on COALESCE(d_cat_id, -1) to fold them.
    """
    repo = Repository(memory_db)
    ath = repo.upsert_athlete_skeleton(42)

    repo.upsert_cup_ranking(athlete_id=ath, cup_ifsc_id=100, d_cat_id=None, rank=1)
    repo.upsert_cup_ranking(athlete_id=ath, cup_ifsc_id=100, d_cat_id=None, rank=2)

    rows = list(memory_db.execute(
        "SELECT rank FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = ?",
        (ath, 100),
    ))
    # Two rows — NULL ≠ NULL in SQLite's default UNIQUE semantics.
    assert len(rows) == 2


def test_cup_ranking_unique_with_d_cat_collapses_to_one(memory_db):
    """Same (athlete, cup) with a concrete d_cat_id collapses to one row."""
    repo = Repository(memory_db)
    ath = repo.upsert_athlete_skeleton(42)

    repo.upsert_cup_ranking(athlete_id=ath, cup_ifsc_id=100, d_cat_id=1, rank=1)
    repo.upsert_cup_ranking(athlete_id=ath, cup_ifsc_id=100, d_cat_id=1, rank=2)

    rows = list(memory_db.execute(
        "SELECT rank FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = ?",
        (ath, 100),
    ))
    assert len(rows) == 1
    assert rows[0]["rank"] == 2


def test_delete_cup_rankings_for_athlete(memory_db):
    """`delete_cup_rankings_for_athlete` should wipe only that athlete's rows."""
    repo = Repository(memory_db)
    ath_a = repo.upsert_athlete_skeleton(42)
    ath_b = repo.upsert_athlete_skeleton(43)

    repo.upsert_cup_ranking(athlete_id=ath_a, cup_ifsc_id=100, rank=1)
    repo.upsert_cup_ranking(athlete_id=ath_a, cup_ifsc_id=101, rank=2)
    repo.upsert_cup_ranking(athlete_id=ath_b, cup_ifsc_id=100, rank=3)

    repo.delete_cup_rankings_for_athlete(ath_a)

    remaining = {(r["athlete_id"], r["cup_ifsc_id"], r["rank"])
                 for r in memory_db.execute(
                     "SELECT athlete_id, cup_ifsc_id, rank FROM cup_rankings"
                 )}
    assert remaining == {(ath_b, 100, 3)}
