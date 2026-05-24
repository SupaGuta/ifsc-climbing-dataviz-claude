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
    assert row["is_paraclimbing"] == 0
    assert row["last_fetched_at"] is not None


def test_hydrate_skips_when_nothing_stale(memory_db):
    repo = Repository(memory_db)
    # No skeletons → nothing stale → no fetches.
    client = MagicMock()
    ok, fail = athletes_fetcher.hydrate(repo, client, stale_days=30)
    assert (ok, fail) == (0, 0)
    client.stream.assert_not_called()


def test_paraclimbing_flag_set_when_sport_class_present(memory_db, fixture):
    repo = Repository(memory_db)
    repo.upsert_athlete_skeleton(9999)

    data = dict(fixture("athletes-id"))
    data["paraclimbing_sport_class"] = "AL1"
    client = _stub_client(data)

    athletes_fetcher.hydrate(repo, client, stale_days=0)
    row = memory_db.execute("SELECT is_paraclimbing FROM athletes WHERE ifsc_id = 9999").fetchone()
    assert row["is_paraclimbing"] == 1


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
