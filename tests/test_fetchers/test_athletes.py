"""Test the athletes fetcher's parse logic against the captured fixture."""
from __future__ import annotations

from unittest.mock import MagicMock

from ifsc_data.api.client import Fetched
from ifsc_data.db.repository import Repository
from ifsc_data.fetchers import athletes as athletes_fetcher


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
