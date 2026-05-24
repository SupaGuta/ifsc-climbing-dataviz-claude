"""Test the events fetcher's parse logic against the captured fixture."""
from __future__ import annotations

from unittest.mock import MagicMock

from wcl_data.api.client import Fetched
from wcl_data.db.repository import Repository
from wcl_data.fetchers import events as events_fetcher


def _stub_client_one(ifsc_id: int, data: dict) -> MagicMock:
    client = MagicMock()
    def fake_stream(endpoint, ids, *args, **kwargs):
        for i in ids:
            yield Fetched(key=i, path=f"/events/{i}", data=data)
    client.stream.side_effect = fake_stream
    return client


def test_hydrate_populates_event_and_competitions(memory_db, fixture):
    repo = Repository(memory_db)
    data = fixture("events-id")
    repo.upsert_event_skeleton(int(data["ifsc_id"]) if "ifsc_id" in data else 1)

    # The fixture's own id field varies — work out the ifsc_id we just inserted.
    ev_ifsc = memory_db.execute("SELECT ifsc_id FROM events").fetchone()["ifsc_id"]

    client = _stub_client_one(ev_ifsc, data)
    ok, fail = events_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    row = memory_db.execute("SELECT * FROM events WHERE ifsc_id = ?", (ev_ifsc,)).fetchone()
    assert row["name"] == data["name"]
    assert row["last_fetched_at"] is not None

    # At least one competition was registered.
    n = memory_db.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
    assert n >= 1


def test_hydrate_populates_country_iso3(memory_db, fixture):
    """ADR 0008: events whose name yields an IFSC variant (or whose API country
    field is a variant) get a canonical ISO3 in country_iso3."""
    repo = Repository(memory_db)
    data = dict(fixture("events-id"))
    # Override the fixture name to anchor on a known IFSC variant (GER → DEU).
    data["name"] = "IFSC World Cup - Munich (GER) 2024"
    data["country"] = None  # Force parser-from-name path
    repo.upsert_event_skeleton(int(data.get("id") or 1))
    ev_ifsc = memory_db.execute("SELECT ifsc_id FROM events").fetchone()["ifsc_id"]

    client = _stub_client_one(ev_ifsc, data)
    events_fetcher.hydrate(repo, client, stale_days=0)

    row = memory_db.execute(
        "SELECT country, country_iso3 FROM events WHERE ifsc_id = ?", (ev_ifsc,)
    ).fetchone()
    assert row["country"] == "GER"
    assert row["country_iso3"] == "DEU"
