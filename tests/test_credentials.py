"""Tests for the credential auto-fetch + .env updater."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wcl_data.api.credentials import (
    FetchedCredentials,
    fetch_credentials,
    update_env_file,
)


# --- fetch_credentials -----------------------------------------------------

def _stub_response(body: str = "", cookies: list[tuple[str, str]] | None = None):
    """Build a MagicMock standing in for a requests Response."""
    resp = MagicMock()
    resp.text = body
    resp.raise_for_status.return_value = None
    # Mimic requests.cookies: an iterable of objects with .name + .value
    cookie_objs = []
    for name, value in (cookies or []):
        c = MagicMock()
        c.name = name
        c.value = value
        cookie_objs.append(c)
    resp.cookies = cookie_objs
    return resp


def test_fetch_credentials_parses_csrf_and_cookie(monkeypatch):
    html = """
        <html><head>
        <meta name="csrf-param" content="authenticity_token" />
        <meta name="csrf-token" content="ABCxyz123_token-value" />
        </head></html>
    """
    monkeypatch.setattr(
        "wcl_data.api.credentials.requests.get",
        lambda url, timeout: _stub_response(
            body=html,
            cookies=[("_ifsc_resultservice_session", "cookieValue123")],
        ),
    )
    creds = fetch_credentials()
    assert isinstance(creds, FetchedCredentials)
    assert creds.csrf_token == "ABCxyz123_token-value"
    assert creds.session_cookie == "_ifsc_resultservice_session=cookieValue123"


def test_fetch_credentials_raises_when_meta_missing(monkeypatch):
    monkeypatch.setattr(
        "wcl_data.api.credentials.requests.get",
        lambda url, timeout: _stub_response(
            body="<html>no meta tag</html>",
            cookies=[("session", "x")],
        ),
    )
    with pytest.raises(RuntimeError, match="csrf-token"):
        fetch_credentials()


def test_fetch_credentials_raises_when_no_session_cookie(monkeypatch):
    monkeypatch.setattr(
        "wcl_data.api.credentials.requests.get",
        lambda url, timeout: _stub_response(
            body='<meta name="csrf-token" content="x" />',
            cookies=[("unrelated_cookie", "y")],
        ),
    )
    with pytest.raises(RuntimeError, match="session-like cookie"):
        fetch_credentials()


# --- update_env_file -------------------------------------------------------

def test_update_env_file_creates_when_missing(tmp_path):
    env = tmp_path / ".env"
    assert not env.exists()

    update_env_file(env, "csrf123", "name=value")

    text = env.read_text(encoding="utf-8")
    assert "WCL_CSRF_TOKEN=csrf123" in text
    assert "WCL_SESSION_COOKIE=name=value" in text


def test_update_env_file_replaces_existing_keys_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# World Climbing API session\n"
        "WCL_CSRF_TOKEN=oldtoken\n"
        "WCL_SESSION_COOKIE=oldcookie\n"
        "WCL_MAX_WORKERS=50\n"
        "WCL_DB_PATH=data/wcl.sqlite\n",
        encoding="utf-8",
    )

    update_env_file(env, "newtoken", "name=newcookieval")

    lines = env.read_text(encoding="utf-8").splitlines()
    # Order preserved, other keys untouched.
    assert lines[0] == "# World Climbing API session"
    assert lines[1] == "WCL_CSRF_TOKEN=newtoken"
    assert lines[2] == "WCL_SESSION_COOKIE=name=newcookieval"
    assert lines[3] == "WCL_MAX_WORKERS=50"
    assert lines[4] == "WCL_DB_PATH=data/wcl.sqlite"


def test_update_env_file_appends_missing_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("WCL_MAX_WORKERS=50\n", encoding="utf-8")

    update_env_file(env, "csrfX", "name=valueX")

    text = env.read_text(encoding="utf-8")
    assert "WCL_MAX_WORKERS=50" in text
    assert "WCL_CSRF_TOKEN=csrfX" in text
    assert "WCL_SESSION_COOKIE=name=valueX" in text
