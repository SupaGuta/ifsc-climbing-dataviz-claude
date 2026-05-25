"""Tests for `wcl_data.config` and the resulting `APIClient` session headers.

These pin the outbound-header contract: every request to the World Climbing API
must carry `X-Csrf-Token`, `Cookie`, and `Referer`. Without all three the
upstream Rails server returns 401 (missing session) or 403 (CSRF mismatch).
A future refactor of `Settings.api_headers` or `APIClient.__init__` that
silently drops one of them would have produced silent-fail mode before
B2 — and would still cause every request to fail now, just with a louder
crash via `AuthFailureAbort`. Either way: don't let that drift land.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import requests

from wcl_data.api.client import APIClient
from wcl_data.config import Settings, load_settings


# Every WCL_* env var consulted by load_settings — kept in sync with config.py
# so a test wrapper can `delenv` all of them before asserting on overrides.
ALL_WCL_ENV_VARS = (
    "WCL_CSRF_TOKEN", "WCL_SESSION_COOKIE", "WCL_REFERER",
    "WCL_MAX_WORKERS", "WCL_CONNECT_TIMEOUT", "WCL_READ_TIMEOUT",
    "WCL_DB_PATH", "WCL_STALE_DAYS", "WCL_GRACE_DAYS",
)


def _clean_env(monkeypatch):
    """Strip every WCL_* env var so a developer's .env file can't pollute the test."""
    for name in ALL_WCL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    defaults = dict(
        csrf_token="csrf-token-value",
        session_cookie="_results_session=abc123",
        referer="https://ifsc.results.info",
        max_workers=4,
        connect_timeout=5.0,
        read_timeout=120.0,
        db_path=Path("data/wcl.sqlite"),
        stale_days=30,
        grace_days=15,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- Settings.api_headers --------------------------------------------------

def test_api_headers_contains_required_three():
    s = _settings()
    headers = s.api_headers
    assert set(headers) == {"X-Csrf-Token", "Cookie", "Referer"}


def test_api_headers_values_round_trip():
    s = _settings(
        csrf_token="my-csrf",
        session_cookie="_x_sess=value",
        referer="https://example",
    )
    h = s.api_headers
    assert h["X-Csrf-Token"] == "my-csrf"
    assert h["Cookie"] == "_x_sess=value"
    assert h["Referer"] == "https://example"


# --- APIClient session uses api_headers -----------------------------------

def test_apiclient_session_carries_required_headers():
    """The session-level persistent headers are the floor — even if production
    later passes `headers=` per-call, the persistent set still defines defaults."""
    s = _settings()
    client = APIClient(s)
    assert client._session.headers["X-Csrf-Token"] == s.csrf_token
    assert client._session.headers["Cookie"] == s.session_cookie
    assert client._session.headers["Referer"] == s.referer


def test_apiclient_outbound_request_carries_required_headers_on_wire(tmp_path, monkeypatch):
    """Verifies the headers ACTUALLY on the wire, not just session persistent state.

    We monkeypatch `Session.send` (the layer below `Session.get`) so we receive
    the fully-`PreparedRequest` — its `.headers` reflect the merge of session
    persistent headers AND any per-call `headers=` kwarg. A future regression
    where `_fetch_one` passed `headers={}` per-call would empty the wire dict
    here, even though `_session.headers` still holds the right values.
    """
    import json
    from unittest.mock import MagicMock

    s = _settings()
    client = APIClient(s)

    captured = {}

    def fake_send(prepared_request, **kwargs):
        captured["headers"] = dict(prepared_request.headers)
        resp = MagicMock()
        resp.status_code = 200
        resp.reason = "OK"
        body = json.dumps({"ok": True}).encode()
        resp.iter_content.return_value = [body]
        resp.content = body
        resp.headers = {"Content-Type": "application/json"}
        # `Session.send` is what populates `Response.connection` etc. — the
        # fields our stub doesn't expose. Anything else `_fetch_one` reads
        # off the response is set explicitly above.
        return resp

    monkeypatch.setattr(client._session, "send", fake_send)
    list(client.stream("athletes", [1]))

    assert captured["headers"]["X-Csrf-Token"] == s.csrf_token
    assert captured["headers"]["Cookie"] == s.session_cookie
    assert captured["headers"]["Referer"] == s.referer


# --- load_settings env binding --------------------------------------------

def test_load_settings_reads_creds_from_env(monkeypatch):
    """Every WCL_* env var should map onto the corresponding Settings field.

    Strips ALL WCL_* vars first so a developer's local .env can't pollute
    the assertion (in particular, WCL_DB_PATH would otherwise leak into
    `s.db_path` and the default-path assertion would fail unpredictably).
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("WCL_CSRF_TOKEN", "csrf-from-env")
    monkeypatch.setenv("WCL_SESSION_COOKIE", "_sess=env-value")
    monkeypatch.setenv("WCL_REFERER", "https://test.example/")
    monkeypatch.setenv("WCL_MAX_WORKERS", "33")
    monkeypatch.setenv("WCL_CONNECT_TIMEOUT", "9.5")
    monkeypatch.setenv("WCL_READ_TIMEOUT", "77.0")
    monkeypatch.setenv("WCL_STALE_DAYS", "12")
    monkeypatch.setenv("WCL_GRACE_DAYS", "7")

    s = load_settings()
    assert s.csrf_token == "csrf-from-env"
    assert s.session_cookie == "_sess=env-value"
    assert s.referer == "https://test.example/"
    assert s.max_workers == 33
    assert s.connect_timeout == 9.5
    assert s.read_timeout == 77.0
    assert s.stale_days == 12
    assert s.grace_days == 7
    # WCL_DB_PATH was delenv'd; the default kicks in.
    assert s.db_path.name == "wcl.sqlite"


def test_load_settings_db_path_override(monkeypatch, tmp_path):
    """`WCL_DB_PATH` overrides the default; relative paths resolve under REPO_ROOT."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("WCL_CSRF_TOKEN", "x")
    monkeypatch.setenv("WCL_SESSION_COOKIE", "y")
    monkeypatch.setenv("WCL_DB_PATH", str(tmp_path / "custom.sqlite"))
    s = load_settings()
    assert s.db_path == tmp_path / "custom.sqlite"


def test_load_settings_raises_runtime_error_when_creds_missing(monkeypatch):
    """The default `require_credentials=True` branch must surface a clear error."""
    _clean_env(monkeypatch)
    with pytest.raises(RuntimeError, match="Missing WCL_CSRF_TOKEN"):
        load_settings()


def test_load_settings_allows_missing_creds_for_no_api_commands(monkeypatch):
    """`require_credentials=False` (used by init/status/export) should not raise."""
    _clean_env(monkeypatch)
    s = load_settings(require_credentials=False)
    assert s.csrf_token == ""
    assert s.session_cookie == ""
