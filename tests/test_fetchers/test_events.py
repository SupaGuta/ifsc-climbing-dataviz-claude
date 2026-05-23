"""Test the events fetcher's parse logic against the captured fixture."""
from __future__ import annotations

from unittest.mock import MagicMock

from ifsc_data.api.client import Fetched
from ifsc_data.db.repository import Repository
from ifsc_data.fetchers import events as events_fetcher


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
