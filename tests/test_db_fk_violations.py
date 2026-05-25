"""Negative tests pinning every declared foreign-key constraint.

Each parametrized case inserts a row referencing a non-existent parent row
and expects `IntegrityError`. If a future schema change accidentally drops
a REFERENCES clause, the matching test goes silent — surfacing the loss
of referential integrity before it lands in production.

SQLite needs `PRAGMA foreign_keys = ON` for these checks to fire; the
`memory_db` fixture in `tests/conftest.py` sets that already.
"""
from __future__ import annotations

import sqlite3

import pytest

from wcl_data.db.repository import Repository

# Each case: (description, prep_callable, bad_insert_sql, bad_insert_params).
#
# `prep` seeds the rows that *would* be the legitimate parents so the test
# isolates a single FK violation rather than tripping over a chained one
# (e.g. ascents needs competition_id + round_stage_id + route_id + athlete_id
# — `prep` provides three valid ones so only the parameter under test fails).
FK_CASES: list[tuple[str, callable, str, tuple]] = []


def _add(name: str, prep, sql: str, params: tuple) -> None:
    FK_CASES.append((name, prep, sql, params))


def _seed_event(conn: sqlite3.Connection) -> dict:
    """Insert a minimal event chain; return ids the parametrized tests can use."""
    repo = Repository(conn)
    sid = repo.upsert_season(1, year=2024)
    lid = repo.upsert_league("World Cup")
    eid = repo.upsert_event_skeleton(1, season_id=sid, league_id=lid)
    did = repo.upsert_discipline("lead")
    cid = repo.upsert_category("Men", gender=0)
    comp_id = repo.upsert_competition(
        event_id=eid, ifsc_id=1, discipline_id=did, category_id=cid,
    )
    ath = repo.upsert_athlete_skeleton(42)
    cr = repo.upsert_category_round(100, competition_id=comp_id)
    rt = repo.upsert_route(500, category_round_id=cr)
    stage = repo.upsert_round_stage(category_round_id=cr, seq=0)
    return {
        "season_id": sid, "league_id": lid, "event_id": eid,
        "discipline_id": did, "category_id": cid, "comp_id": comp_id,
        "athlete_id": ath, "category_round_id": cr, "route_id": rt,
        "round_stage_id": stage,
    }


BAD_ID = 999_999


_add(
    "season_leagues.season_id",
    _seed_event,
    "INSERT INTO season_leagues (ifsc_id, season_id) VALUES (?, ?)",
    (1, BAD_ID),
)
_add(
    "season_leagues.league_id",
    _seed_event,
    "INSERT INTO season_leagues (ifsc_id, league_id) VALUES (?, ?)",
    (2, BAD_ID),
)
_add(
    "events.season_id",
    _seed_event,
    "INSERT INTO events (ifsc_id, season_id) VALUES (?, ?)",
    (99, BAD_ID),
)
_add(
    "events.league_id",
    _seed_event,
    "INSERT INTO events (ifsc_id, league_id) VALUES (?, ?)",
    (98, BAD_ID),
)
_add(
    "competitions.event_id",
    _seed_event,
    "INSERT INTO competitions (event_id, ifsc_id) VALUES (?, ?)",
    (BAD_ID, 1),
)


_add(
    "competitions.discipline_id",
    _seed_event,
    # SELECT-in-VALUES grabs a valid event_id; the test parameter supplies the
    # invalid discipline_id so this case isolates one FK violation at a time.
    "INSERT INTO competitions (event_id, ifsc_id, discipline_id) "
    "VALUES ((SELECT id FROM events ORDER BY id LIMIT 1), 200, ?)",
    (BAD_ID,),
)
_add(
    "competitions.category_id",
    _seed_event,
    "INSERT INTO competitions (event_id, ifsc_id, category_id) "
    "VALUES ((SELECT id FROM events LIMIT 1), 201, ?)",
    (BAD_ID,),
)

_add(
    "cup_rankings.athlete_id",
    _seed_event,
    "INSERT INTO cup_rankings (athlete_id, cup_ifsc_id) VALUES (?, ?)",
    (BAD_ID, 1),
)

_add(
    "results.competition_id",
    _seed_event,
    "INSERT INTO results (competition_id, athlete_id) "
    "VALUES (?, (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "results.athlete_id",
    _seed_event,
    "INSERT INTO results (competition_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), ?)",
    (BAD_ID,),
)

_add(
    "category_rounds.competition_id",
    _seed_event,
    "INSERT INTO category_rounds (ifsc_id, competition_id) VALUES (?, ?)",
    (300, BAD_ID),
)

_add(
    "round_stages.category_round_id",
    _seed_event,
    "INSERT INTO round_stages (category_round_id, seq) VALUES (?, ?)",
    (BAD_ID, 1),
)

_add(
    "routes.category_round_id",
    _seed_event,
    "INSERT INTO routes (ifsc_id, category_round_id) VALUES (?, ?)",
    (700, BAD_ID),
)

_add(
    "round_results.competition_id",
    _seed_event,
    "INSERT INTO round_results (competition_id, category_round_id, athlete_id) "
    "VALUES (?, (SELECT id FROM category_rounds LIMIT 1), "
    "        (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "round_results.category_round_id",
    _seed_event,
    "INSERT INTO round_results (competition_id, category_round_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), ?, "
    "        (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "round_results.athlete_id",
    _seed_event,
    "INSERT INTO round_results (competition_id, category_round_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), "
    "        (SELECT id FROM category_rounds LIMIT 1), ?)",
    (BAD_ID,),
)

_add(
    "stage_results.competition_id",
    _seed_event,
    "INSERT INTO stage_results (competition_id, round_stage_id, athlete_id) "
    "VALUES (?, (SELECT id FROM round_stages LIMIT 1), "
    "        (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "stage_results.round_stage_id",
    _seed_event,
    "INSERT INTO stage_results (competition_id, round_stage_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), ?, "
    "        (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "stage_results.athlete_id",
    _seed_event,
    "INSERT INTO stage_results (competition_id, round_stage_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), "
    "        (SELECT id FROM round_stages LIMIT 1), ?)",
    (BAD_ID,),
)

_add(
    "ascents.competition_id",
    _seed_event,
    "INSERT INTO ascents (competition_id, round_stage_id, route_id, athlete_id) "
    "VALUES (?, (SELECT id FROM round_stages LIMIT 1), "
    "        (SELECT id FROM routes LIMIT 1), (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "ascents.round_stage_id",
    _seed_event,
    "INSERT INTO ascents (competition_id, round_stage_id, route_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), ?, "
    "        (SELECT id FROM routes LIMIT 1), (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "ascents.route_id",
    _seed_event,
    "INSERT INTO ascents (competition_id, round_stage_id, route_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), "
    "        (SELECT id FROM round_stages LIMIT 1), ?, "
    "        (SELECT id FROM athletes LIMIT 1))",
    (BAD_ID,),
)
_add(
    "ascents.athlete_id",
    _seed_event,
    "INSERT INTO ascents (competition_id, round_stage_id, route_id, athlete_id) "
    "VALUES ((SELECT id FROM competitions LIMIT 1), "
    "        (SELECT id FROM round_stages LIMIT 1), "
    "        (SELECT id FROM routes LIMIT 1), ?)",
    (BAD_ID,),
)


@pytest.mark.parametrize(
    "fk_name,prep,sql,params",
    FK_CASES,
    ids=[case[0] for case in FK_CASES],
)
def test_fk_violation_raises_integrity_error(memory_db, fk_name, prep, sql, params):
    prep(memory_db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        memory_db.execute(sql, params)
