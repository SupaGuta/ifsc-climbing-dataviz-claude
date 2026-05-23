"""Tests for the streaming API client."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ifsc_data.api.client import APIClient, Fetched
from ifsc_data.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        csrf_token="x", session_cookie="y", referer="z",
        max_workers=4, request_timeout=5,
        db_path=tmp_path / "db.sqlite",
        stale_days=30,
        grace_days=15,
    )


def _stub_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "OK" if status_code == 200 else "Err"
    resp.json.return_value = json_data or {}
    return resp


def test_stream_yields_one_per_id(monkeypatch, tmp_path):
    client = APIClient(make_settings(tmp_path))
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        ifsc_id = int(url.rsplit("/", 1)[-1])
        return _stub_response(200, {"id": ifsc_id})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3]))
    assert {r.key for r in results} == {1, 2, 3}
    assert all(isinstance(r, Fetched) for r in results)
    assert {r.data["id"] for r in results} == {1, 2, 3}


def test_stream_retries_failures(monkeypatch, tmp_path):
    client = APIClient(make_settings(tmp_path))
    call_count = {"n": 0}

    def fake_get(url, timeout):
        # Fail the first call for id=2; succeed thereafter.
        if url.endswith("/2") and call_count["n"] < 2:
            call_count["n"] += 1
            return _stub_response(500)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], retry_delay=0))
    assert {r.key for r in results} == {1, 2, 3}


def test_stream_gives_up_after_max_retries(monkeypatch, tmp_path, caplog):
    client = APIClient(make_settings(tmp_path))

    def fake_get(url, timeout):
        if url.endswith("/2"):
            return _stub_response(500)  # 5xx so it IS retried under the default policy
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], max_retries=2, retry_delay=0))
    keys = {r.key for r in results}
    assert keys == {1, 3}      # 2 is never delivered


def test_default_retry_on_skips_404(monkeypatch, tmp_path):
    """4xx should not be retried under the default policy — the discovery probe relies on this."""
    client = APIClient(make_settings(tmp_path))
    call_counts = {"/2": 0}

    def fake_get(url, timeout):
        if url.endswith("/2"):
            call_counts["/2"] += 1
            return _stub_response(404)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], retry_delay=0))

    # /2 hit exactly once — no retries because 404 is permanent under default policy.
    assert call_counts["/2"] == 1
    assert {r.key for r in results} == {1, 3}


def test_custom_retry_on_can_override_default(monkeypatch, tmp_path):
    """Caller can pass a custom predicate to retry everything, even 4xx."""
    client = APIClient(make_settings(tmp_path))
    call_counts = {"/2": 0}

    def fake_get(url, timeout):
        if url.endswith("/2"):
            call_counts["/2"] += 1
            return _stub_response(404)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    list(client.stream("athletes", [1, 2, 3], retry_delay=0,
                       max_retries=2, retry_on=lambda exc: True))

    # 1 initial + 2 retries = 3 calls when override forces retry on 404.
    assert call_counts["/2"] == 3


def test_retry_success_yields_each_id_exactly_once(monkeypatch, tmp_path):
    """A 5xx that succeeds on retry must not produce a duplicate yield for that id."""
    client = APIClient(make_settings(tmp_path))
    attempts = {"/2": 0}

    def fake_get(url, timeout):
        if url.endswith("/2"):
            attempts["/2"] += 1
            if attempts["/2"] == 1:
                return _stub_response(500)
            return _stub_response(200, {"ok": True})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = [r.key for r in client.stream("athletes", [1, 2, 3], retry_delay=0)]

    # Exactly one yield per id, regardless of how many times /2 was retried.
    assert sorted(keys) == [1, 2, 3]
    assert len(keys) == 3
