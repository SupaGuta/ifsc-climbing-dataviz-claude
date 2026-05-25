"""CLI tests — focused on the exit-code contract.

Full per-command CLI coverage is Phase C (test C1); the tests here only pin
the documented exit codes for the two errors the CLI now translates from
exceptions to friendly stderr: missing creds (4) and DB lock (3).
"""
from __future__ import annotations

import sqlite3

import pytest

from wcl_data import cli


def test_main_returns_exit_4_on_missing_creds(monkeypatch, capsys):
    """A RuntimeError from load_settings should become exit 4 + friendly stderr."""
    def fake_load_settings(*, require_credentials: bool):
        raise RuntimeError("Missing WCL_CSRF_TOKEN or WCL_SESSION_COOKIE.")

    monkeypatch.setattr(cli, "load_settings", fake_load_settings)
    code = cli.main(["refresh"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_AUTH == 4
    assert "Missing WCL_CSRF_TOKEN" in err


def test_main_returns_exit_3_on_db_locked(monkeypatch, capsys, tmp_path):
    """A `database is locked` OperationalError should become exit 3 + friendly stderr."""
    def fake_open_db(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "open_db", fake_open_db)
    # `init` is in _NO_CREDS_COMMANDS so we skip the cred-check branch and
    # land directly in _dispatch → open_db.
    code = cli.main(["init"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_DB_LOCK == 3
    assert "locked" in err.lower()
    assert "wcl-data" in err  # mentions the suggestion to check for another process


def test_main_propagates_non_locked_operational_error(monkeypatch):
    """Non-locked OperationalErrors (malformed schema, disk-image-malformed,
    no-such-column from a partial migration) MUST escape as a traceback rather
    than being silently labelled "database is locked" — sending the user to
    troubleshoot the wrong problem is worse than no friendly message."""
    def fake_open_db(*a, **kw):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(cli, "open_db", fake_open_db)
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        cli.main(["init"])


def test_main_returns_exit_4_on_auth_subcommand_runtime_error(monkeypatch, capsys):
    """A RuntimeError from inside `auth` (e.g. fetch_credentials can't parse
    the CSRF meta) becomes exit 4 + friendly stderr, not a raw traceback."""
    def fake_cmd_auth(*, dry_run, env_file):
        raise RuntimeError("Could not find <meta name=\"csrf-token\">")

    monkeypatch.setattr(cli, "_cmd_auth", fake_cmd_auth)
    code = cli.main(["auth"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_AUTH == 4
    assert "csrf-token" in err


def test_main_propagates_runtime_error_outside_auth(monkeypatch):
    """RuntimeError from a NON-auth command still escapes as a traceback —
    the EXIT_AUTH translation only fires for the `auth` subcommand because
    that's the recovery path for cred issues."""
    def fake_cmd_status(_settings):
        raise RuntimeError("something unrelated to creds")

    monkeypatch.setattr(cli, "_cmd_status", fake_cmd_status)
    with pytest.raises(RuntimeError, match="unrelated"):
        cli.main(["status"])
