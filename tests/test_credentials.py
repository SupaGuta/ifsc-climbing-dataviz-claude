"""Tests for the credential auto-fetch + .env updater."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wcl_data.api import credentials as credentials_mod
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


# --- F7: backup .env to .env.bak before overwrite -------------------------

def test_update_env_file_creates_bak_when_overwriting(tmp_path):
    """An existing .env is copied to .env.bak before the in-place rewrite,
    so a fat-fingered re-auth doesn't strand the user without a recovery file.
    """
    env = tmp_path / ".env"
    env.write_text(
        "WCL_CSRF_TOKEN=oldtoken\n"
        "WCL_SESSION_COOKIE=name=oldcookie\n",
        encoding="utf-8",
    )

    update_env_file(env, "newtoken", "name=newcookie")

    bak = tmp_path / ".env.bak"
    assert bak.exists()
    bak_text = bak.read_text(encoding="utf-8")
    assert "WCL_CSRF_TOKEN=oldtoken" in bak_text
    assert "WCL_SESSION_COOKIE=name=oldcookie" in bak_text


def test_update_env_file_no_bak_when_no_prior_env(tmp_path):
    """Fresh-install case: no .env yet, so there's nothing to back up."""
    env = tmp_path / ".env"
    assert not env.exists()

    update_env_file(env, "csrf", "name=val")

    assert env.exists()
    assert not (tmp_path / ".env.bak").exists()


def test_update_env_file_bak_is_byte_for_byte(tmp_path):
    """The backup must preserve the exact bytes — including CRLF line endings
    and any comments — so restoring it gives the user the file they had.
    """
    env = tmp_path / ".env"
    original_bytes = (
        b"# World Climbing API session\r\n"
        b"WCL_CSRF_TOKEN=oldtoken\r\n"
        b"WCL_SESSION_COOKIE=name=oldcookie\r\n"
        b"WCL_MAX_WORKERS=50\r\n"
    )
    env.write_bytes(original_bytes)

    update_env_file(env, "newtoken", "name=newcookie")

    bak = tmp_path / ".env.bak"
    assert bak.read_bytes() == original_bytes


def test_update_env_file_bak_overwrites_previous_bak(tmp_path):
    """A second re-auth replaces the prior .env.bak with the now-stale token —
    one-deep rolling history, not an accumulating log of every auth ever run.
    """
    env = tmp_path / ".env"
    env.write_text("WCL_CSRF_TOKEN=first\nWCL_SESSION_COOKIE=name=one\n", encoding="utf-8")

    update_env_file(env, "second", "name=two")
    first_bak = (tmp_path / ".env.bak").read_text(encoding="utf-8")
    assert "WCL_CSRF_TOKEN=first" in first_bak

    update_env_file(env, "third", "name=three")
    second_bak = (tmp_path / ".env.bak").read_text(encoding="utf-8")
    assert "WCL_CSRF_TOKEN=second" in second_bak
    assert "first" not in second_bak


# --- F3: chmod 0o600 on POSIX, no-op on Windows ---------------------------

def test_update_env_file_chmods_0600_on_posix(monkeypatch, tmp_path):
    """The freshly-written .env should be chmod 0o600 on POSIX systems so a
    leaked file can't be world-read."""
    calls: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def fake_chmod(path, mode):
        calls.append((str(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(credentials_mod.os, "name", "posix")
    monkeypatch.setattr(credentials_mod.os, "chmod", fake_chmod)
    env = tmp_path / ".env"

    update_env_file(env, "csrf", "name=val")

    assert calls == [(str(env), 0o600)]


def test_update_env_file_skips_chmod_on_windows(monkeypatch, tmp_path):
    """On Windows, `os.chmod` would only toggle the read-only bit (not the
    POSIX user-only contract we actually want), so we skip it entirely."""
    calls: list[tuple[str, int]] = []

    def fake_chmod(path, mode):  # pragma: no cover - shouldn't fire
        calls.append((str(path), mode))

    monkeypatch.setattr(credentials_mod.os, "name", "nt")
    monkeypatch.setattr(credentials_mod.os, "chmod", fake_chmod)
    env = tmp_path / ".env"

    update_env_file(env, "csrf", "name=val")

    assert calls == []


def test_update_env_file_chmods_overwrite_path_too(monkeypatch, tmp_path):
    """The chmod must fire on both code paths — create (no prior .env) AND
    in-place overwrite — AND on the .env.bak that holds the previous (still
    largely-valid) credentials. Without chmodding the bak file, the security
    posture regresses on re-auth: the bak inherits umask 0o644 and exposes
    the prior token to any local user."""
    calls: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def fake_chmod(path, mode):
        calls.append((str(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(credentials_mod.os, "name", "posix")
    monkeypatch.setattr(credentials_mod.os, "chmod", fake_chmod)
    env = tmp_path / ".env"
    bak = tmp_path / ".env.bak"
    env.write_text("WCL_CSRF_TOKEN=old\nWCL_SESSION_COOKIE=name=old\n", encoding="utf-8")

    update_env_file(env, "new", "name=new")

    # Both env and the bak must be 0o600. Order matters for the test (bak
    # first because it's chmod'd inside the overwrite branch before env's
    # final rewrite + chmod) but exact order is an implementation detail —
    # use set comparison so a future refactor that flips the order doesn't
    # falsely fail the test.
    assert set(calls) == {(str(env), 0o600), (str(bak), 0o600)}
