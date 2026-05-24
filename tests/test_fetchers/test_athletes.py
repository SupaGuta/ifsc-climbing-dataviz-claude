"""Test the athletes fetcher's parse logic against the captured fixture."""
from __future__ import annotations

from unittest.mock import MagicMock

from wcl_data.api.client import Fetched
from wcl_data.db.repository import Repository
from wcl_data.fetchers import athletes as athletes_fetcher


def _stub_client(fixture_data: dict) -> MagicMock:
    client = MagicMock()
    def fake_stream(endpoint, ids, *args, **kwargs):
        for ifsc_id in ids:
            yield Fetched(key=ifsc_id, path=f"/athletes/{ifsc_id}", data=fixture_data)
    client.stream.side_effect = fake_stream
    return client


def test_hydrate_populates_all_mapped_fields(memory_db, fixture):
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(1364)

    ondra = fixture("athletes-id")
    client = _stub_client(ondra)

    ok, fail = athletes_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    row = memory_db.execute("SELECT * FROM athletes WHERE ifsc_id = 1364").fetchone()
    assert row["firstname"] == "Adam"
    assert row["lastname"] == "ONDRA"
    assert row["gender"] == 0
    assert row["country"] == "CZE"
    assert row["height"] == 186
    assert row["birthday"] == "1993-02-05"
    assert row["federation_id"] == 17
    assert row["federation_name"] == "Cesky Horolezecky Svaz"
    assert row["federation_abbreviation"] == "CHS"
    assert row["federation_url"] == "https://www.horosvaz.cz/"
    assert row["paraclimbing_sport_class"] is None
    assert row["sport_class_status"] is None
    assert row["sport_class_review_date"] is None
    assert row["speed_pb_time"] == "6.86"
    assert row["speed_pb_date"] == "2021-10-12"
    assert row["speed_pb_event_name"] == "Olympic Games (C) - Tokyo (JPN) 2020"
    assert row["speed_pb_round_name"] == "Final"
    assert row["last_fetched_at"] is not None


def test_hydrate_skips_when_nothing_stale(memory_db):
    repo = Repository(memory_db)
    # No skeletons → nothing stale → no fetches.
    client = MagicMock()
    ok, fail = athletes_fetcher.hydrate(repo, client, stale_days=30)
    assert (ok, fail) == (0, 0)
    client.stream.assert_not_called()


def test_paraclimbing_sport_class_persisted_raw(memory_db, fixture):
    """The raw IFSC sport-class string is preserved verbatim — downstream
    consumers derive `IS NOT NULL` as the paraclimbing flag."""
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(9999)

    data = dict(fixture("athletes-id"))
    data["paraclimbing_sport_class"] = "AL-1"
    data["sport_class_status"] = "Confirmed"
    data["sport_class_review_date"] = "2025-03-01"
    client = _stub_client(data)

    athletes_fetcher.hydrate(repo, client, stale_days=0)
    row = memory_db.execute(
        "SELECT paraclimbing_sport_class, sport_class_status, sport_class_review_date "
        "FROM athletes WHERE ifsc_id = 9999"
    ).fetchone()
    assert row["paraclimbing_sport_class"] == "AL-1"
    assert row["sport_class_status"] == "Confirmed"
    assert row["sport_class_review_date"] == "2025-03-01"


def test_hydrate_populates_cup_rankings(memory_db, fixture):
    """Each cup-rankings entry in the payload expands to one row per discipline."""
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(1364)

    ondra = fixture("athletes-id")
    client = _stub_client(ondra)
    athletes_fetcher.hydrate(repo, client, stale_days=0)

    rows = list(memory_db.execute(
        "SELECT cup_ifsc_id, cup_name, season, discipline, d_cat_id, rank "
        "FROM cup_rankings cr JOIN athletes a ON cr.athlete_id = a.id "
        "WHERE a.ifsc_id = 1364 ORDER BY season, cup_ifsc_id, discipline"
    ))
    # Ondra's fixture has 21 cups; total discipline rows across them is 34
    # (incl. 2 European Cup 2022 entries whose discipline key is the empty string).
    assert len(rows) == 34

    # Spot-check the 2010 World Cup boulder gold.
    by_season_disc = {(r["season"], r["discipline"]): r for r in rows
                      if r["cup_name"] == "IFSC Climbing Worldcup 2010"}
    assert by_season_disc[("2010", "boulder")]["rank"] == 1
    assert by_season_disc[("2010", "lead")]["rank"] == 3


def test_hydrate_cup_rankings_is_idempotent(memory_db, fixture):
    """Re-hydrating the same athlete wipes prior cup_rankings instead of duplicating."""
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(1364)
    ondra = fixture("athletes-id")
    client = _stub_client(ondra)

    athletes_fetcher.hydrate(repo, client, stale_days=0)
    first_count = memory_db.execute(
        "SELECT COUNT(*) FROM cup_rankings"
    ).fetchone()[0]

    athletes_fetcher.hydrate(repo, client, stale_days=-1)  # force stale
    second_count = memory_db.execute(
        "SELECT COUNT(*) FROM cup_rankings"
    ).fetchone()[0]

    assert first_count == second_count > 0


def test_country_iso3_normalized_from_ifsc_variant(memory_db, fixture):
    """ADR 0008: athletes whose API country is an IFSC variant (GER, SUI, INA, …)
    get a canonical ISO3 written to the sibling country_iso3 column."""
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(7777)

    data = dict(fixture("athletes-id"))
    data["country"] = "GER"  # IFSC variant for Germany; ISO3 is DEU
    client = _stub_client(data)

    athletes_fetcher.hydrate(repo, client, stale_days=0)
    row = memory_db.execute(
        "SELECT country, country_iso3 FROM athletes WHERE ifsc_id = 7777"
    ).fetchone()
    assert row["country"] == "GER"
    assert row["country_iso3"] == "DEU"


def test_country_iso3_passes_through_when_already_iso3(memory_db, fixture):
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(8888)

    data = dict(fixture("athletes-id"))
    data["country"] = "FRA"
    client = _stub_client(data)

    athletes_fetcher.hydrate(repo, client, stale_days=0)
    row = memory_db.execute(
        "SELECT country, country_iso3 FROM athletes WHERE ifsc_id = 8888"
    ).fetchone()
    assert row["country"] == "FRA"
    assert row["country_iso3"] == "FRA"
