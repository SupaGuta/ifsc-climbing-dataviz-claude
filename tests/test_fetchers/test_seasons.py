"""Tests for the seasons fetcher (discover probe + hydrate)."""
from __future__ import annotations

from unittest.mock import MagicMock

from wcl_data.api.client import Fetched
from wcl_data.db.repository import Repository
from wcl_data.fetchers import seasons as seasons_fetcher
from wcl_data.fetchers.seasons import (
    DEFAULT_LOOKAHEAD,
    INITIAL_PROBE_RANGE,
    _last_int_segment,
)


def _stub_client_for_probe(success_ids: set[int]):
    """Return a client that yields a `Fetched` for ids in `success_ids` and
    silently drops the rest (mimicking the 404-permanent contract)."""
    client = MagicMock()

    def fake_stream(endpoint, ids, *_args, **_kwargs):
        for i in ids:
            if int(i) in success_ids:
                yield Fetched(key=i, path=f"/{endpoint}/{i}", data={"name": str(2000 + int(i))})

    client.stream.side_effect = fake_stream
    return client


# --- discover -----------------------------------------------------------------

def test_discover_probes_0_to_initial_range_when_db_empty(memory_db):
    """An empty seasons table triggers the bootstrap probe 0..INITIAL_PROBE_RANGE."""
    repo = Repository(memory_db)
    probed: list[int] = []
    client = MagicMock()

    def fake_stream(endpoint, ids, *_a, **_kw):
        probed.extend(int(i) for i in ids)
        return iter([])

    client.stream.side_effect = fake_stream
    seasons_fetcher.discover(repo, client)

    assert probed == list(range(0, INITIAL_PROBE_RANGE))


def test_discover_lookahead_past_current_max(memory_db):
    """With existing MAX(ifsc_id)=42, probe should span 43..47 (DEFAULT_LOOKAHEAD=5)."""
    repo = Repository(memory_db)
    repo.upsert_season(42, year=2024)
    probed: list[int] = []
    client = MagicMock()

    def fake_stream(endpoint, ids, *_a, **_kw):
        probed.extend(int(i) for i in ids)
        return iter([])

    client.stream.side_effect = fake_stream
    seasons_fetcher.discover(repo, client)

    assert probed == list(range(43, 43 + DEFAULT_LOOKAHEAD))


def test_discover_inserts_discovered_ifsc_ids(memory_db):
    """Every successful response should upsert a season skeleton."""
    repo = Repository(memory_db)
    repo.upsert_season(10)
    # Pretend ids 11 + 13 exist on the upstream; 12, 14, 15 are 404s.
    client = _stub_client_for_probe(success_ids={11, 13})
    inserted = seasons_fetcher.discover(repo, client)

    assert inserted == 2
    ids = {r[0] for r in memory_db.execute("SELECT ifsc_id FROM seasons")}
    assert {10, 11, 13}.issubset(ids)
    assert 12 not in ids
    assert 15 not in ids


def test_discover_returns_zero_when_no_candidates_succeed(memory_db):
    repo = Repository(memory_db)
    repo.upsert_season(100)
    client = _stub_client_for_probe(success_ids=set())  # all probes are 404s
    inserted = seasons_fetcher.discover(repo, client)
    assert inserted == 0


# --- _last_int_segment --------------------------------------------------------

def test_last_int_segment_extracts_trailing_id():
    assert _last_int_segment("/api/v1/season_leagues/443") == 443


def test_last_int_segment_handles_trailing_slash():
    assert _last_int_segment("/api/v1/season_leagues/443/") == 443


def test_last_int_segment_returns_none_when_no_digits():
    assert _last_int_segment("/api/v1/season_leagues/") is None
    assert _last_int_segment("") is None
    assert _last_int_segment("just-text") is None


def test_last_int_segment_picks_last_when_multiple_digit_segments():
    """The path can contain numeric segments (events/1456/result/3); take the last."""
    assert _last_int_segment("/api/v1/events/1456/result/3") == 3


# --- hydrate ------------------------------------------------------------------

def test_hydrate_populates_leagues_and_season_leagues(memory_db, fixture):
    """A full season payload should upsert leagues + season_leagues skeletons.

    The captured `seasons-id.json` fixture is for year=2025: 13 leagues + many
    events. ifsc_id is set to 443 (distinct from year) so a regression that
    accidentally passed `year=season_ifsc` instead of `year=int(data["name"])`
    would fail the year-update assertion.
    """
    repo = Repository(memory_db)
    data = fixture("seasons-id")
    season_ifsc = 443  # distinct from year=2025 so the year-lift path is provable
    repo.upsert_season(season_ifsc)   # year IS NULL

    client = MagicMock()

    def fake_stream(endpoint, ids, *_a, **_kw):
        for i in ids:
            yield Fetched(key=i, path=f"/{endpoint}/{i}", data=data)

    client.stream.side_effect = fake_stream
    ok, fail = seasons_fetcher.hydrate(repo, client, stale_days=0)
    assert (ok, fail) == (1, 0)

    # Year was lifted from data["name"] = "2025" — NOT from the ifsc_id.
    year_row = memory_db.execute(
        "SELECT year, last_fetched_at FROM seasons WHERE ifsc_id = ?", (season_ifsc,)
    ).fetchone()
    assert year_row["year"] == 2025, "hydrate must lift year from payload, not ifsc_id"
    assert year_row["last_fetched_at"] is not None

    # 13 leagues in the fixture's "leagues" list.
    n_leagues = memory_db.execute("SELECT COUNT(*) FROM leagues").fetchone()[0]
    assert n_leagues == len(data["leagues"])

    # Each league.url ends in /season_leagues/<id>; every parseable one becomes
    # a season_league row.
    n_sl = memory_db.execute("SELECT COUNT(*) FROM season_leagues").fetchone()[0]
    assert n_sl == sum(1 for l in data["leagues"] if l.get("url"))

    # Event skeletons created for every event in the fixture.
    n_events = memory_db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n_events == sum(1 for e in data["events"] if e.get("event_id") is not None)


def test_hydrate_skips_leagues_without_name(memory_db):
    """A league with name=None / empty must not create a row."""
    repo = Repository(memory_db)
    repo.upsert_season(1)
    payload = {
        "name": "2024",
        "leagues": [
            {"name": "World Cup", "url": "/api/v1/season_leagues/1"},
            {"name": None, "url": "/api/v1/season_leagues/2"},  # dropped
            {"name": "", "url": "/api/v1/season_leagues/3"},     # dropped
        ],
        "events": [],
    }

    client = MagicMock()
    client.stream.side_effect = lambda endpoint, ids, *_a, **_kw: iter(
        [Fetched(key=i, path=f"/{endpoint}/{i}", data=payload) for i in ids]
    )
    seasons_fetcher.hydrate(repo, client, stale_days=0)

    league_names = {r[0] for r in memory_db.execute("SELECT name FROM leagues")}
    assert league_names == {"World Cup"}


def test_hydrate_failure_counts_failures(memory_db):
    """Parser exceptions are caught per-item; fail count surfaces in the summary.

    Pins the v6 per-iteration-transaction contract (Phase D9): hydrate
    wraps each season's writes in `with repo.transaction():`, so when the
    malformed `leagues` field raises mid-iteration, the prior
    `upsert_season(year=...)` rolls back too. The row is left with both
    `year` and `last_fetched_at` NULL so the next refresh picks it up
    cleanly — no half-populated state for the retry to step around.
    """
    repo = Repository(memory_db)
    repo.upsert_season(1)
    # Force a TypeError by giving `leagues` a non-iterable type.
    payload = {"name": "2024", "leagues": 12345, "events": []}

    client = MagicMock()
    client.stream.side_effect = lambda endpoint, ids, *_a, **_kw: iter(
        [Fetched(key=i, path=f"/{endpoint}/{i}", data=payload) for i in ids]
    )
    ok, fail = seasons_fetcher.hydrate(repo, client, stale_days=0)
    assert ok == 0
    assert fail == 1

    row = memory_db.execute(
        "SELECT year, last_fetched_at FROM seasons WHERE ifsc_id = 1"
    ).fetchone()
    assert row["year"] is None, "year update should roll back with the failed transaction"
    assert row["last_fetched_at"] is None, "row must remain stale so retry re-fetches it"
