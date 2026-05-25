"""End-to-end contract: a 401-storm mid-run aborts and surfaces nonzero exit.

The legacy silent-fail behavior dropped every remaining row to the WARN file
log when credentials expired mid-fetch. Phase B2 introduced `AuthFailureAbort`
to halt the run after 5 consecutive 401/403 responses across the worker pool.

These tests pin the no-longer-silent contract end-to-end:
  * The real `APIClient.stream` raises `AuthFailureAbort` once the threshold trips.
  * The orchestrator catches it, attaches `partial_summary`, and re-raises.
  * `cli.main` translates it to `EXIT_UPSTREAM` (5) and prints partial progress.

We use the real `APIClient` (not a MagicMock) so a future refactor of the
auth-failure counter, retry-on predicate, or `stream_paths` lifecycle gets
caught here rather than only in `test_api_client.py`'s narrower unit tests.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wcl_data import cli
from wcl_data.api.client import APIClient, AuthFailureAbort
from wcl_data.config import Settings
from wcl_data.db.repository import Repository
from wcl_data.fetchers import athletes as athletes_fetcher
from wcl_data.fetchers import refresh as refresh_orchestrator


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        csrf_token="csrf",
        session_cookie="cookie",
        referer="https://ifsc.results.info",
        max_workers=1,           # serial — makes 401 ordering deterministic
        connect_timeout=1.0,
        read_timeout=1.0,
        db_path=tmp_path / "wcl.sqlite",
        stale_days=30,
        grace_days=15,
    )


def _stub_response(status: int, body: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.reason = "OK" if status == 200 else "Unauthorized"
    encoded = json.dumps(body or {}).encode("utf-8")
    resp.iter_content.return_value = [encoded]
    resp.content = encoded
    resp.headers = {"Content-Type": "application/json"}
    return resp


def test_apiclient_raises_auth_failure_abort_after_threshold(tmp_path, monkeypatch):
    """5 consecutive 401s anywhere in the stream should trip `AuthFailureAbort`."""
    settings = _settings(tmp_path)
    client = APIClient(settings)

    call_count = {"n": 0}

    def fake_get(url, timeout, **kw):
        call_count["n"] += 1
        return _stub_response(401)

    monkeypatch.setattr(client._session, "get", fake_get)

    with pytest.raises(AuthFailureAbort) as exc_info:
        # 10 ids, but the 5th 401 should trigger the abort.
        list(client.stream("athletes", list(range(1, 11)), retry_delay=0))

    assert exc_info.value.threshold == 5
    assert "401/403" in str(exc_info.value)


def test_auth_abort_mid_stream_after_some_200s(tmp_path, monkeypatch):
    """Mixed 200s then a 401 storm: the first few rows yield, then abort fires.

    Mirrors the production silent-fail scenario: credentials work briefly, then
    expire, then every subsequent fetch is rejected. Without `AuthFailureAbort`
    those remaining rows would silently drop to the WARN log.
    """
    settings = _settings(tmp_path)
    client = APIClient(settings)

    call_count = {"n": 0}

    def fake_get(url, timeout, **kw):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return _stub_response(200, {"id": call_count["n"]})
        return _stub_response(401)

    monkeypatch.setattr(client._session, "get", fake_get)

    yielded = []
    with pytest.raises(AuthFailureAbort):
        for f in client.stream("athletes", list(range(1, 20)), retry_delay=0):
            yielded.append(f.key)

    # At least the first two 200s landed before the abort kicked in.
    assert len(yielded) >= 2


def test_orchestrator_attaches_partial_summary_on_abort(tmp_path, monkeypatch):
    """`refresh_all` should attach a partial summary so the CLI can show progress.

    Seed at least one season so `seasons.discover`'s `MAX(ifsc_id) IS NOT NULL`
    branch short-circuits to a small lookahead (DEFAULT_LOOKAHEAD=5) instead
    of the 50-id bootstrap probe — keeps the test fast AND makes the asserted
    summary values predictable.
    """
    settings = _settings(tmp_path)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from wcl_data.db.schema import apply_schema
    apply_schema(conn)
    repo = Repository(conn)

    # Seed one season so `seasons.discover` lookahead-probes 1001..1005 (which
    # all 404 in the fake_get below) instead of bootstrapping 0..49.
    repo.upsert_season(1000, year=2026)
    repo.mark_fetched("seasons", 1)   # not stale → `seasons.hydrate` is a no-op
    # Seed 10 athletes so the 401 storm crosses the abort threshold of 5.
    for i in range(1, 11):
        repo.upsert_athlete_skeleton(i)

    client = APIClient(settings)

    # Athletes return 401 (abort path); seasons-discover probes (1001..1005)
    # return 404 (permanent under default policy, silently dropped); other
    # phases find no stale rows. Net: seasons (0, 0), athletes raises mid-loop.
    def fake_get(url, timeout, **kw):
        if "/athletes/" in url:
            return _stub_response(401)
        if "/seasons/" in url:
            return _stub_response(404)
        return _stub_response(200, {})

    monkeypatch.setattr(client._session, "get", fake_get)

    with pytest.raises(AuthFailureAbort) as exc_info:
        refresh_orchestrator.refresh_all(repo, client, stale_days=0)

    summary = exc_info.value.partial_summary
    assert summary is not None
    # Earlier phases completed; athletes died mid-stream.
    assert summary == {
        "seasons": (0, 0),
        "season_leagues": (0, 0),
        "events": (0, 0),
        "competitions": (0, 0),
    }
    assert "athletes" not in summary
    conn.close()


def test_cli_translates_auth_abort_to_exit_5(tmp_path, monkeypatch, capsys):
    """End-to-end: an AuthFailureAbort raised mid-`refresh` returns EXIT_UPSTREAM=5."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda **kw: settings)

    # First create the DB so `open_db` doesn't probe missing parents.
    cli.main(["init"])

    # Pre-seed 10 athletes via the now-existing DB so the 401 storm crosses the
    # abort threshold of 5 consecutive failures.
    from wcl_data.db.schema import open_db
    conn = open_db(settings.db_path)
    try:
        repo = Repository(conn)
        for i in range(1, 11):
            repo.upsert_athlete_skeleton(i)
    finally:
        conn.close()

    # Pin the APIClient so its session returns 401 for every athlete request and
    # 200 (empty payload) for the upstream phases.
    real_apiclient_cls = cli.APIClient

    def patched_get(url, timeout, **kw):
        if "/athletes/" in url:
            return _stub_response(401)
        return _stub_response(200, {})

    def patched_apiclient(s):
        c = real_apiclient_cls(s)
        c._session.get = patched_get   # bypass requests.Session
        return c

    monkeypatch.setattr(cli, "APIClient", patched_apiclient)

    rc = cli.main(["refresh", "--stale-days", "0"])
    err = capsys.readouterr().err

    assert rc == cli.EXIT_UPSTREAM == 5
    assert "401/403" in err
    # Partial progress summary printed.
    assert "Partial progress" in err
