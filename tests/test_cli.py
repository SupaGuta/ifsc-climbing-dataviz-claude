"""CLI tests — exit-code contract + per-command dispatch coverage.

Exit-code tests pin the documented codes the CLI translates from exceptions
to friendly stderr: missing creds (4), DB lock (3), unknown view (2), and
the upstream auth-abort path (5). Dispatch tests cover each `_cmd_*`
handler against a tmp-path SQLite file, stubbing the upstream fetcher
calls so we don't touch the network.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wcl_data import cli
from wcl_data.api.client import AuthFailureAbort
from wcl_data.config import Settings
from wcl_data.db.repository import Repository
from wcl_data.db.schema import open_db


def _status_row(out: str, table: str) -> list[str]:
    """Return the fields of `out`'s status row for `table`, or fail the test.

    Anchors the match with a word boundary so 'seasons' doesn't shadow
    'season_leagues' and 'results' doesn't shadow 'round_results'.
    """
    pat = re.compile(rf"^{re.escape(table)}\b.*$", re.MULTILINE)
    m = pat.search(out)
    assert m is not None, f"no row for {table!r} in status output:\n{out}"
    return m.group(0).split()


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
    def fake_cmd_auth(_settings, *, dry_run, env_file, check):
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
    def fake_cmd_status(_settings, *, as_json):
        raise RuntimeError("something unrelated to creds")

    monkeypatch.setattr(cli, "_cmd_status", fake_cmd_status)
    with pytest.raises(RuntimeError, match="unrelated"):
        cli.main(["status"])


# ===========================================================================
# Phase C1 — per-command dispatch coverage
# ===========================================================================

def _make_settings(tmp_path: Path, *, with_creds: bool = True) -> Settings:
    return Settings(
        csrf_token="x" if with_creds else "",
        session_cookie="y" if with_creds else "",
        referer="z",
        max_workers=4,
        connect_timeout=5.0,
        read_timeout=5.0,
        db_path=tmp_path / "wcl.sqlite",
        stale_days=30,
        grace_days=15,
    )


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    """Inject a tmp-path Settings into every `cli.main` call in this test."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda **kw: s)
    return s


# --- init ----------------------------------------------------------------

def test_init_creates_db_file(cli_settings):
    assert not cli_settings.db_path.exists()
    rc = cli.main(["init"])
    assert rc == cli.EXIT_OK
    assert cli_settings.db_path.exists()


def test_init_is_idempotent(cli_settings):
    """Re-running `init` on an existing DB returns 0 without error."""
    assert cli.main(["init"]) == cli.EXIT_OK
    assert cli.main(["init"]) == cli.EXIT_OK


# --- status --------------------------------------------------------------

def test_status_prints_row_counts(cli_settings, capsys):
    """Status walks every table and prints rows + hydration coverage.

    Uses `_status_row` (line-anchored regex) so 'seasons' isn't shadowed by
    'season_leagues' and 'results' isn't shadowed by 'round_results'.
    """
    cli.main(["init"])
    rc = cli.main(["status"])
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "schema_version:" in out
    for table in ("seasons", "season_leagues", "events", "competitions",
                  "athletes", "results", "category_rounds", "cup_rankings"):
        _status_row(out, table)   # raises if the row is missing


def test_status_shows_hydrated_count_for_seeded_seasons(cli_settings, capsys):
    """A hydrated season surfaces a non-zero hydration count.

    Seeds 2 seasons but marks only 1 as fetched, so the assertion can
    distinguish the 'total' column from the 'hydrated' column (a regression
    that printed total in the hydrated slot would land 2 instead of 1).
    """
    cli.main(["init"])
    conn = open_db(cli_settings.db_path)
    try:
        repo = Repository(conn)
        sid_hydrated = repo.upsert_season(99, year=2024)
        repo.upsert_season(100, year=2025)   # second row, NOT marked fetched
        repo.mark_fetched("seasons", sid_hydrated)
    finally:
        conn.close()

    cli.main(["status"])
    out = capsys.readouterr().out
    parts = _status_row(out, "seasons")
    assert parts[0] == "seasons"
    assert parts[1] == "2"   # total rows
    assert parts[2] == "1"   # hydrated rows (only one marked fetched)


# --- export --------------------------------------------------------------

def test_export_default_writes_every_default_view(cli_settings, tmp_path):
    """Every default view produces a non-empty CSV with at least a header row.

    Verifies that the file *contains data* (header row) rather than only
    checking filenames — a regression that wrote zero-byte CSVs at the right
    paths would otherwise pass silently.
    """
    from wcl_data.exporter import DEFAULT_EXPORT_VIEWS

    cli.main(["init"])
    out_dir = tmp_path / "exports"
    rc = cli.main(["export", "--output-dir", str(out_dir)])
    assert rc == cli.EXIT_OK
    files = list(out_dir.iterdir())
    files_by_view: dict[str, Path] = {}
    for path in files:
        for name in DEFAULT_EXPORT_VIEWS:
            if path.name.startswith(f"{name}_") and path.suffix == ".csv":
                files_by_view[name] = path
                break
    missing = set(DEFAULT_EXPORT_VIEWS) - files_by_view.keys()
    assert not missing, f"missing CSVs for views: {sorted(missing)}"

    for name, path in files_by_view.items():
        size = path.stat().st_size
        assert size > 0, f"{name} CSV is zero bytes: {path}"
        with path.open("r", encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), None)
        assert header, f"{name} CSV has no header row"
        # Every header field should be a non-empty string.
        assert all(field for field in header), f"{name} CSV has empty header field(s): {header}"


def test_export_unknown_view_returns_usage_error(cli_settings, tmp_path, capsys):
    """`export <unknown>` must return EXIT_USAGE (2) and print to stderr."""
    cli.main(["init"])
    rc = cli.main(["export", "not_a_view", "--output-dir", str(tmp_path / "out")])
    assert rc == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "Unknown view" in err


def test_export_named_view_writes_one_file(cli_settings, tmp_path):
    cli.main(["init"])
    out_dir = tmp_path / "exports"
    rc = cli.main(["export", "seasons", "--output-dir", str(out_dir)])
    assert rc == cli.EXIT_OK
    files = list(out_dir.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("seasons_") and files[0].suffix == ".csv"


# --- hydrate -------------------------------------------------------------

def test_hydrate_unknown_entity_is_rejected_by_argparse(cli_settings, capsys):
    """argparse `choices=ENTITIES` should reject unknown entities with exit 2."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["hydrate", "not_a_real_entity"])
    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_hydrate_known_entity_dispatches(cli_settings, monkeypatch, capsys):
    """`hydrate seasons` should call the orchestrator's hydrate_entity hook
    AND print the per-entity summary to stdout."""
    cli.main(["init"])
    captured = {}

    def fake_hydrate_entity(repo, client, entity, *, stale_days, limit):
        captured["entity"] = entity
        captured["stale_days"] = stale_days
        captured["limit"] = limit
        return (4, 1)   # distinct values so the summary print is unambiguous

    monkeypatch.setattr(
        cli.refresh_orchestrator, "hydrate_entity", fake_hydrate_entity
    )
    # Stub APIClient so we don't try to set up a real session.
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())

    rc = cli.main(["hydrate", "seasons", "--limit", "3", "--stale-days", "7"])
    assert rc == cli.EXIT_OK
    assert captured == {"entity": "seasons", "stale_days": 7, "limit": 3}
    out = capsys.readouterr().out
    assert "seasons: 4 hydrated, 1 failed." in out


def test_hydrate_multiple_entities_dispatches_each_in_order(cli_settings, monkeypatch, capsys):
    """`hydrate seasons athletes` (nargs='+') hydrates both entities in the
    CLI-given order, sharing a single APIClient + repo connection — three
    separate `wcl-data hydrate` calls would otherwise reopen each."""
    cli.main(["init"])
    calls: list[str] = []
    clients: list[object] = []

    def fake_hydrate_entity(repo, client, entity, *, stale_days, limit):
        calls.append(entity)
        clients.append(client)
        return (1, 0)

    monkeypatch.setattr(cli.refresh_orchestrator, "hydrate_entity", fake_hydrate_entity)
    fake_client_sentinel = object()
    monkeypatch.setattr(cli, "APIClient", lambda settings: fake_client_sentinel)

    rc = cli.main(["hydrate", "seasons", "athletes"])
    assert rc == cli.EXIT_OK
    assert calls == ["seasons", "athletes"]
    # One client instance reused across both entities (single auth state).
    assert clients == [fake_client_sentinel, fake_client_sentinel]
    out = capsys.readouterr().out
    assert "seasons: 1 hydrated, 0 failed." in out
    assert "athletes: 1 hydrated, 0 failed." in out


# --- pull-new ------------------------------------------------------------

def test_pull_new_happy_path_calls_orchestrator(cli_settings, monkeypatch, capsys):
    """`pull-new` on an empty DB should dispatch to refresh_orchestrator.pull_new."""
    cli.main(["init"])

    captured = {}

    def fake_pull_new(repo, client, *, limit, grace_days, stale_days):
        captured["limit"] = limit
        captured["grace_days"] = grace_days
        captured["stale_days"] = stale_days
        return {"seasons": (0, 0), "events": (0, 0)}

    monkeypatch.setattr(cli.refresh_orchestrator, "pull_new", fake_pull_new)
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())

    rc = cli.main(["pull-new", "--limit", "5", "--grace-days", "30"])
    assert rc == cli.EXIT_OK
    assert captured == {"limit": 5, "grace_days": 30, "stale_days": None}
    out = capsys.readouterr().out
    assert "seasons" in out and "events" in out


def test_refresh_happy_path_calls_orchestrator(cli_settings, monkeypatch):
    """`refresh` should call refresh_orchestrator.refresh_all with the right args."""
    cli.main(["init"])
    captured = {}

    def fake_refresh_all(repo, client, *, stale_days, limit):
        captured["stale_days"] = stale_days
        captured["limit"] = limit
        return {}

    monkeypatch.setattr(cli.refresh_orchestrator, "refresh_all", fake_refresh_all)
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())

    rc = cli.main(["refresh", "--stale-days", "60", "--limit", "10"])
    assert rc == cli.EXIT_OK
    assert captured == {"stale_days": 60, "limit": 10}


# --- AuthFailureAbort propagation (exit 5) -------------------------------

def test_main_returns_exit_5_on_auth_failure_abort(cli_settings, monkeypatch, capsys):
    """An AuthFailureAbort raised by the orchestrator should map to EXIT_UPSTREAM (5)
    + a friendly stderr line + the partial-progress summary.

    We use entity name 'cup_rankings' in the partial_summary because it
    does NOT appear in the AuthFailureAbort message itself (which mentions
    the failing path /events/42). That way the 'cup_rankings' assertion
    proves the partial-summary print ran — it can't be satisfied by the
    error-message line above.
    """
    cli.main(["init"])

    def fake_refresh_all(repo, client, *, stale_days, limit):
        exc = AuthFailureAbort(5, "/events/42")
        exc.partial_summary = {"cup_rankings": (3, 0)}
        raise exc

    monkeypatch.setattr(cli.refresh_orchestrator, "refresh_all", fake_refresh_all)
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())

    rc = cli.main(["refresh"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_UPSTREAM == 5
    assert "401/403" in err
    assert "Partial progress" in err
    # 'cup_rankings' is in partial_summary but NOT in the AuthFailureAbort
    # error string — so this assertion can only be satisfied by the summary
    # block actually running.
    assert "cup_rankings" in err
    # Pin the formatted summary row too: header + "cup_rankings 3 0".
    assert re.search(r"^cup_rankings\s+3\s+0\s*$", err, re.MULTILINE) is not None


# ===========================================================================
# Phase F4 — new CLI flags
# ===========================================================================

# --- -d/--db-path global override ------------------------------------------

def test_db_path_override_redirects_init_to_alt_file(monkeypatch, tmp_path, capsys):
    """`wcl-data -d <alt.db> init` should create the schema at <alt.db>,
    not at the default WCL_DB_PATH."""
    default_db = tmp_path / "default.sqlite"
    alt_db = tmp_path / "alt.sqlite"
    default_settings = Settings(
        csrf_token="x", session_cookie="y", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=default_db, stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: default_settings)

    rc = cli.main(["-d", str(alt_db), "init"])

    assert rc == cli.EXIT_OK
    assert alt_db.exists()
    assert not default_db.exists()


def test_db_path_override_resolved_to_absolute(monkeypatch, tmp_path, capsys):
    """Relative paths to -d should resolve to absolute (cwd-relative). The
    settings.db_path consumed by _cmd_* must always be absolute so a later
    `os.chdir` in the same process can't break it."""
    default_settings = Settings(
        csrf_token="x", session_cookie="y", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=tmp_path / "default.sqlite", stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: default_settings)
    captured: dict = {}

    def fake_cmd_status(settings, *, as_json):
        captured["db_path"] = settings.db_path
        return 0

    monkeypatch.setattr(cli, "_cmd_status", fake_cmd_status)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["-d", "rel.sqlite", "status"])

    assert rc == 0
    assert captured["db_path"].is_absolute()
    assert captured["db_path"].name == "rel.sqlite"


# --- -q/--quiet global flag ------------------------------------------------

def test_verbose_and_quiet_are_mutually_exclusive(capsys):
    """Argparse should reject `-v -q` together — they're in a mutually
    exclusive group."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["-v", "-q", "status"])
    assert exc_info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_quiet_flag_propagates_to_logging_setup(cli_settings, monkeypatch):
    """`-q` should pass quiet=True into logging_setup.configure so INFO
    chatter is suppressed."""
    captured: dict = {}

    def fake_configure(*, verbose, quiet):
        captured["verbose"] = verbose
        captured["quiet"] = quiet

    monkeypatch.setattr(cli.logging_setup, "configure", fake_configure)
    cli.main(["-q", "init"])
    assert captured == {"verbose": False, "quiet": True}


# --- --stale-days on pull-new ----------------------------------------------

def test_pull_new_stale_days_forwarded_to_orchestrator(cli_settings, monkeypatch):
    """`pull-new --stale-days 60` must reach pull_new() as a real kwarg —
    asserting on a mutated settings.stale_days is not sufficient because
    pull_new() doesn't read settings (it only consumes the kwarg it's given).
    """
    cli.main(["init"])
    captured: dict = {}

    def fake_pull_new(repo, client, *, limit, grace_days, stale_days):
        captured["stale_days"] = stale_days
        return {}

    monkeypatch.setattr(cli.refresh_orchestrator, "pull_new", fake_pull_new)
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())
    rc = cli.main(["pull-new", "--stale-days", "60"])
    assert rc == cli.EXIT_OK
    assert captured["stale_days"] == 60


def test_pull_new_without_stale_days_forwards_none(cli_settings, monkeypatch):
    """Omitting --stale-days must forward stale_days=None so pull_new keeps
    its 'newly-discovered athletes only' (huge stale_days) default — passing
    settings.stale_days (default 30) would silently re-hydrate every athlete
    older than a month, contradicting the documented contract."""
    cli.main(["init"])
    captured: dict = {}

    def fake_pull_new(repo, client, *, limit, grace_days, stale_days):
        captured["stale_days"] = stale_days
        return {}

    monkeypatch.setattr(cli.refresh_orchestrator, "pull_new", fake_pull_new)
    monkeypatch.setattr(cli, "APIClient", lambda settings: object())
    rc = cli.main(["pull-new"])
    assert rc == cli.EXIT_OK
    assert captured["stale_days"] is None


# --- auth --check ----------------------------------------------------------

def test_auth_check_returns_ok_on_200(monkeypatch, capsys):
    """A 200 response to /seasons/0 confirms the cred path is honored."""
    settings = Settings(
        csrf_token="goodtoken", session_cookie="name=goodcookie", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=Path("unused.sqlite"), stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: settings)
    resp = MagicMock()
    resp.status_code = 200
    resp.reason = "OK"
    monkeypatch.setattr(cli.requests, "get", lambda *a, **kw: resp)

    rc = cli.main(["auth", "--check"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "creds OK" in out


def test_auth_check_returns_exit_4_on_401(monkeypatch, capsys):
    """401 from the probe means the API rejected the creds — EXIT_AUTH."""
    settings = Settings(
        csrf_token="badtoken", session_cookie="name=badcookie", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=Path("unused.sqlite"), stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: settings)
    resp = MagicMock()
    resp.status_code = 401
    resp.reason = "Unauthorized"
    monkeypatch.setattr(cli.requests, "get", lambda *a, **kw: resp)

    rc = cli.main(["auth", "--check"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_AUTH
    assert "creds rejected" in err
    assert "401" in err


def test_auth_check_returns_exit_5_on_429(monkeypatch, capsys):
    """429 means rate-limited; the server may not have evaluated creds at
    all, so we treat the probe as inconclusive rather than 'creds OK'."""
    settings = Settings(
        csrf_token="goodtoken", session_cookie="name=goodcookie", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=Path("unused.sqlite"), stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: settings)
    resp = MagicMock()
    resp.status_code = 429
    resp.reason = "Too Many Requests"
    monkeypatch.setattr(cli.requests, "get", lambda *a, **kw: resp)

    rc = cli.main(["auth", "--check"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_UPSTREAM
    assert "inconclusive" in err
    assert "429" in err


def test_auth_check_returns_exit_5_on_5xx(monkeypatch, capsys):
    """5xx from the probe means upstream is unhealthy — creds appear valid."""
    settings = Settings(
        csrf_token="goodtoken", session_cookie="name=goodcookie", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=Path("unused.sqlite"), stale_days=30, grace_days=15,
    )
    monkeypatch.setattr(cli, "load_settings", lambda **kw: settings)
    resp = MagicMock()
    resp.status_code = 502
    resp.reason = "Bad Gateway"
    monkeypatch.setattr(cli.requests, "get", lambda *a, **kw: resp)

    rc = cli.main(["auth", "--check"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_UPSTREAM
    assert "upstream error" in err


def test_auth_check_requires_creds(monkeypatch, capsys):
    """`auth --check` needs valid creds in .env (unlike default `auth` which
    is the recovery path for missing creds). Should map RuntimeError to
    EXIT_AUTH via the cred-load path."""
    def fake_load_settings(*, require_credentials):
        if require_credentials:
            raise RuntimeError("Missing WCL_CSRF_TOKEN or WCL_SESSION_COOKIE.")
        # Not expected to hit here, but return something usable just in case.
        return Settings(
            csrf_token="", session_cookie="", referer="z",
            max_workers=4, connect_timeout=5.0, read_timeout=5.0,
            db_path=Path("unused.sqlite"), stale_days=30, grace_days=15,
        )

    monkeypatch.setattr(cli, "load_settings", fake_load_settings)
    rc = cli.main(["auth", "--check"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_AUTH
    assert "Missing WCL_CSRF_TOKEN" in err


# --- status --json ---------------------------------------------------------

def test_status_json_emits_machine_readable_object(cli_settings, capsys):
    """`status --json` should emit one JSON object with db_path,
    schema_version, and a tables array of {name, rows, hydrated, last_hydrated}."""
    cli.main(["init"])
    conn = open_db(cli_settings.db_path)
    try:
        repo = Repository(conn)
        sid = repo.upsert_season(99, year=2024)
        repo.mark_fetched("seasons", sid)
    finally:
        conn.close()

    rc = cli.main(["status", "--json"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK

    payload = json.loads(out)
    assert "db_path" in payload
    assert payload["schema_version"] >= 1
    seasons_entry = next(t for t in payload["tables"] if t["table"] == "seasons")
    assert seasons_entry["rows"] == 1
    assert seasons_entry["hydrated"] == 1
    # last_hydrated is the date prefix of the ISO-8601 last_fetched_at.
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", seasons_entry["last_hydrated"])

    # Non-hydratable tables surface as null hydrated/last_hydrated.
    leagues_entry = next(t for t in payload["tables"] if t["table"] == "leagues")
    assert leagues_entry["hydrated"] is None
    assert leagues_entry["last_hydrated"] is None


# --- export --format -------------------------------------------------------

def test_export_format_jsonl_writes_jsonl_extension(cli_settings, tmp_path):
    """`export <view> --format jsonl` should write a .jsonl file whose lines
    are individual JSON objects."""
    cli.main(["init"])
    out_dir = tmp_path / "exports"
    rc = cli.main(["export", "seasons", "--output-dir", str(out_dir), "--format", "jsonl"])
    assert rc == cli.EXIT_OK
    files = list(out_dir.iterdir())
    assert len(files) == 1
    path = files[0]
    assert path.suffix == ".jsonl"
    # Empty seasons table → file should still exist (header is implicit in
    # JSONL — no rows means an empty file).
    assert path.stat().st_size == 0


def test_export_format_jsonl_writes_one_object_per_row(cli_settings, tmp_path):
    """Seed two rows and verify the JSONL output has one parseable object
    per line, keyed by the view's column names."""
    cli.main(["init"])
    conn = open_db(cli_settings.db_path)
    try:
        repo = Repository(conn)
        repo.upsert_season(11, year=2024)
        repo.upsert_season(12, year=2025)
    finally:
        conn.close()

    out_dir = tmp_path / "exports"
    cli.main(["export", "seasons", "--output-dir", str(out_dir), "--format", "jsonl"])
    path = next(out_dir.iterdir())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    objs = [json.loads(line) for line in lines]
    years = sorted(o["year"] for o in objs)
    assert years == [2024, 2025]
    # Column names from the seasons view.
    assert {"season_ifsc_id", "year", "last_fetched_at"} <= set(objs[0].keys())


def test_export_format_parquet_without_pyarrow_returns_usage_error(cli_settings, tmp_path, capsys, monkeypatch):
    """`--format parquet` without pyarrow installed should map the ImportError
    to a friendly EXIT_USAGE message — not a raw traceback."""
    # Force the lazy import to fail even if pyarrow is installed in the env.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("simulated missing pyarrow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cli.main(["init"])
    rc = cli.main(["export", "seasons", "--output-dir", str(tmp_path / "out"), "--format", "parquet"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_USAGE
    assert "pyarrow" in err
