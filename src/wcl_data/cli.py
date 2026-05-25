"""Command-line interface for the World Climbing Lab ingest layer.

Exit codes — kept in sync with docs/cli-cookbook/exit-codes.md:
  0  success
  1  generic error (unhandled exception, parser issues from argparse)
  2  usage error (unknown view, unknown entity — surfaced via argparse / explicit returns)
  3  DB lock / IO problem (another wcl-data writer holds the file, or disk error)
  4  credentials missing / expired (set 4 BEFORE the upstream request; 5 is for live API failures)
  5  upstream API unrecoverable failure (e.g. AuthFailureAbort after creds-validity threshold)
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

import requests

from . import logging_setup
from .api.client import APIClient, AuthFailureAbort
from .config import load_settings
from .db.repository import Repository
from .db.schema import open_db
from .fetchers import refresh as refresh_orchestrator
from .fetchers.refresh import ENTITIES

log = logging.getLogger(__name__)

# Commands that don't need World Climbing API credentials.
_NO_CREDS_COMMANDS = {"init", "status", "export", "auth"}

# Exit-code taxonomy — see module docstring.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_DB_LOCK = 3
EXIT_AUTH = 4
EXIT_UPSTREAM = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wcl-data",
        description="Ingest the World Climbing public API (at ifsc.results.info) into a local SQLite warehouse.",
    )
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true",
        help="Keep WARNINGs on the console (default: hidden, written to logs/wcl-data.log).",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true",
        help="Silence INFO chatter on the console; only ERROR+ shown. Mutually exclusive with -v.",
    )
    p.add_argument(
        "-d", "--db-path", type=Path, default=None,
        help="Override WCL_DB_PATH for this run (any command that touches the warehouse).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create the DB schema (idempotent).")

    p_auth = sub.add_parser(
        "auth",
        help="Fetch a fresh CSRF token + session cookie from ifsc.results.info and write them to .env.",
    )
    p_auth.add_argument(
        "--dry-run", action="store_true",
        help="Print the fetched values without writing to .env.",
    )
    p_auth.add_argument(
        "--env-file", type=Path, default=None,
        help="Path to the .env file to update (default: <repo>/.env).",
    )
    p_auth.add_argument(
        "--check", action="store_true",
        help="Probe the API with the credentials in .env and exit (no fetch / .env write). "
             "Exit 0 if creds are honored, 4 on 401/403, 5 on upstream/transport failure.",
    )

    p_refresh = sub.add_parser(
        "refresh", help="Discover new entities and hydrate stale rows across the graph."
    )
    p_refresh.add_argument("--limit", type=int, default=None,
                           help="Cap the number of rows hydrated per entity (smoke testing).")
    p_refresh.add_argument("--stale-days", type=int, default=None,
                           help="Override WCL_STALE_DAYS for this run.")
    p_refresh.add_argument("--workers", type=int, default=None,
                           help="Override WCL_MAX_WORKERS for this run (default 50; useful range 50-100).")

    p_pull = sub.add_parser(
        "pull-new",
        help="Force-refresh ongoing container entities to discover new content; hydrate only newly-discovered athletes."
    )
    p_pull.add_argument("--limit", type=int, default=None,
                        help="Cap rows per entity (smoke testing).")
    p_pull.add_argument("--workers", type=int, default=None,
                        help="Override WCL_MAX_WORKERS for this run.")
    p_pull.add_argument("--grace-days", type=int, default=None,
                        help="Override WCL_GRACE_DAYS for this run (default 15). "
                             "Days past an event's date_end during which it's still treated as ongoing.")
    p_pull.add_argument("--stale-days", type=int, default=None,
                        help="Override WCL_STALE_DAYS for this run (newly-discovered athletes only).")

    p_hydrate = sub.add_parser("hydrate", help="Hydrate one or more entities.")
    p_hydrate.add_argument(
        "entity", choices=ENTITIES, nargs="+",
        help="One or more entities to hydrate, in the given order.",
    )
    p_hydrate.add_argument("--limit", type=int, default=None)
    p_hydrate.add_argument("--stale-days", type=int, default=None)
    p_hydrate.add_argument("--workers", type=int, default=None,
                           help="Override WCL_MAX_WORKERS for this run (default 50; useful range 50-100).")

    p_status = sub.add_parser("status", help="Print row counts and hydration coverage.")
    p_status.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit a single JSON object instead of the human-readable table (for scripts).",
    )

    from .exporter import (
        DEFAULT_EXPORT_VIEWS,
        SUPPORTED_FORMATS as _EXPORT_FORMATS,
        VIEW_NAMES as _EXPORT_VIEW_NAMES,
    )
    _opt_in_views = tuple(v for v in _EXPORT_VIEW_NAMES if v not in DEFAULT_EXPORT_VIEWS)
    p_export = sub.add_parser(
        "export",
        help="Export denormalized views to timestamped files in data/exports/.",
    )
    p_export.add_argument(
        "view", nargs="?", default=None,
        help=(
            f"Optional view name. Default (no arg): export the {len(DEFAULT_EXPORT_VIEWS)} non-bulky views "
            f"({', '.join(DEFAULT_EXPORT_VIEWS)}). "
            f"Opt-in (excluded from default, pass explicitly): {', '.join(_opt_in_views) or 'none'}."
        ),
    )
    p_export.add_argument(
        "--output-dir", type=Path, default=None,
        help="Override the default exports directory (data/exports/).",
    )
    p_export.add_argument(
        "--format", choices=_EXPORT_FORMATS, default="csv",
        help=f"Output format (default: csv). `parquet` requires `pip install pyarrow`.",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    logging_setup.configure(verbose=args.verbose, quiet=args.quiet)

    # `auth --check` is the only `auth` invocation that needs valid creds
    # up-front (it's probing them); the default `auth` flow is the recovery
    # path when creds are missing/expired, so it must stay in the no-creds
    # bucket. `init`/`status`/`export` never touch the API.
    require_creds = args.command not in _NO_CREDS_COMMANDS or (
        args.command == "auth" and getattr(args, "check", False)
    )
    try:
        settings = load_settings(require_credentials=require_creds)
    except RuntimeError as exc:
        # Missing or empty WCL_CSRF_TOKEN / WCL_SESSION_COOKIE — print a
        # one-line friendly message instead of dumping the traceback.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_AUTH

    # Global --db-path override. Resolved before _dispatch so every command
    # sees the same setting; absolute paths are honored as-is, relative paths
    # resolve against the current working directory (NOT REPO_ROOT — the user
    # passed it on the CLI so cwd-relative is the expected convention).
    if args.db_path is not None:
        settings = replace(settings, db_path=args.db_path.resolve())

    try:
        return _dispatch(args, settings, parser)
    except AuthFailureAbort as exc:
        # Threshold-tripped 401/403 storm during a run. The client has already
        # logged the ERROR; surface a short stderr line and exit 5. If the
        # orchestrator attached a partial summary (entities that completed
        # before the abort), print it so the user sees what progress was
        # made — important because phases commit per-item, so completed
        # entities have durable rows.
        print(f"error: {exc}", file=sys.stderr)
        if exc.partial_summary:
            print("\nPartial progress before abort:", file=sys.stderr)
            _print_summary(exc.partial_summary, file=sys.stderr)
        return EXIT_UPSTREAM
    except sqlite3.OperationalError as exc:
        # Only "database is locked" maps to EXIT_DB_LOCK. Other OperationalErrors
        # (malformed schema, no-such-column from a partial migration, "database
        # disk image is malformed") indicate genuine data-corruption / code bugs
        # — propagate them as tracebacks rather than silently labelling them as
        # "another process may be running", which sends the user troubleshooting
        # the wrong problem.
        message = str(exc)
        if "locked" not in message.lower():
            raise
        print(
            f"error: SQLite reports the database is locked. "
            f"Another wcl-data process may be running, or a notebook "
            f"holds an open write transaction. ({message})",
            file=sys.stderr,
        )
        return EXIT_DB_LOCK
    except (RuntimeError, requests.RequestException, OSError) as exc:
        # `_cmd_auth` is the recovery path for expired credentials, but its own
        # failure modes (RuntimeError when fetch_credentials can't parse the
        # CSRF meta tag, requests.RequestException on network failure, OSError
        # when update_env_file can't write .env) used to escape as tracebacks
        # + exit 1 — contradicting the documented exit-4 contract for cred-
        # related errors. Funnel them all through EXIT_AUTH with a friendly
        # stderr line. Only fires for the `auth` subcommand; other commands
        # don't raise these exception types from their _cmd_* helpers.
        if args.command != "auth":
            raise
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_AUTH


def _dispatch(args: argparse.Namespace, settings, parser: argparse.ArgumentParser) -> int:
    if args.command == "init":
        conn = open_db(settings.db_path)
        conn.close()
        log.info("DB initialised at %s", settings.db_path)
        return EXIT_OK

    if args.command == "auth":
        return _cmd_auth(
            settings,
            dry_run=args.dry_run,
            env_file=args.env_file,
            check=args.check,
        )
    if args.command == "status":
        return _cmd_status(settings, as_json=args.as_json)
    if args.command == "export":
        return _cmd_export(
            settings,
            view=args.view,
            output_dir=args.output_dir,
            format=args.format,
        )
    if args.command == "refresh":
        return _cmd_refresh(settings, limit=args.limit, stale_days=args.stale_days, workers=args.workers)
    if args.command == "pull-new":
        return _cmd_pull_new(
            settings,
            limit=args.limit,
            workers=args.workers,
            grace_days=args.grace_days,
            stale_days=args.stale_days,
        )
    if args.command == "hydrate":
        return _cmd_hydrate(settings, args.entity, limit=args.limit, stale_days=args.stale_days, workers=args.workers)
    parser.error(f"Unknown command {args.command}")


def _cmd_auth(settings, *, dry_run: bool, env_file: Optional[Path], check: bool) -> int:
    if check:
        return _cmd_auth_check(settings)

    from .api.credentials import REFERER_URL, fetch_credentials, update_env_file
    from .config import REPO_ROOT

    creds = fetch_credentials()
    print(f"Fetched fresh credentials from {REFERER_URL}")
    print(f"  CSRF token:     {creds.csrf_token[:16]}... ({len(creds.csrf_token)} chars)")
    cookie_name, _, cookie_value = creds.session_cookie.partition("=")
    print(f"  Session cookie: {cookie_name}=... ({len(creds.session_cookie)} chars)")

    if dry_run:
        print()
        print("--dry-run: not writing to .env. Tokens truncated for safety; "
              "rerun without --dry-run to write the full values.")
        print(f"  WCL_CSRF_TOKEN={creds.csrf_token[:16]}... ({len(creds.csrf_token)} chars)")
        print(f"  WCL_SESSION_COOKIE={cookie_name}={cookie_value[:16]}... ({len(creds.session_cookie)} chars)")
        return 0

    target = env_file if env_file is not None else REPO_ROOT / ".env"
    update_env_file(target, creds.csrf_token, creds.session_cookie)
    print(f"Wrote {target}")
    return 0


def _cmd_auth_check(settings) -> int:
    """Probe `/seasons/0` with current creds. Sidesteps the streaming/retry
    machinery in `APIClient.stream` — we want to surface the response code
    directly, not bury it in `_default_retry_on` for 5xx or have it consumed
    by the auth-abort threshold.

    Exit semantics:
      - 401/403: EXIT_AUTH, print "creds rejected".
      - 429: EXIT_UPSTREAM, print "inconclusive" — server may have
        short-circuited on rate-limit before evaluating creds.
      - 5xx or transport: EXIT_UPSTREAM, print "upstream error" — creds
        appear valid but the server side is unhealthy.
      - Any other status (200, 404, 400, …): EXIT_OK, print "creds OK".
        Rationale: 401/403 is the upstream's contract for rejected creds, so
        any non-auth response means the request reached past auth into the
        endpoint-level handler. We bias permissive here — false-OK is recoverable
        on the next real fetch, false-fail trains users to ignore the probe.

    `allow_redirects=False` because the World Climbing API is a REST API, not
    a browser app — a 302 to a login HTML page that returns 200 would
    otherwise be misclassified as "creds OK" if we silently followed it.
    """
    from .api.client import API_BASE_URL

    url = f"{API_BASE_URL}/seasons/0"
    try:
        resp = requests.get(
            url,
            headers=settings.api_headers,
            timeout=(settings.connect_timeout, settings.read_timeout),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        print(f"probe failed (transport): {exc}", file=sys.stderr)
        return EXIT_UPSTREAM

    if resp.status_code in (401, 403):
        print(
            f"creds rejected: HTTP {resp.status_code} {resp.reason} from /seasons/0. "
            f"Run `python -m wcl_data auth` to refresh.",
            file=sys.stderr,
        )
        return EXIT_AUTH
    if resp.status_code == 429:
        # Rate-limit short-circuit: the upstream may have rejected the request
        # before evaluating the auth headers, so a 429 confirms neither valid
        # nor invalid creds. Classifying this as EXIT_OK would mislead the
        # user into thinking the probe was conclusive.
        print(
            f"probe inconclusive: HTTP 429 Too Many Requests (rate-limited; "
            f"server may not have evaluated creds). Try again in a minute.",
            file=sys.stderr,
        )
        return EXIT_UPSTREAM
    if resp.status_code >= 500:
        print(
            f"upstream error: HTTP {resp.status_code} {resp.reason} (creds appear valid)",
            file=sys.stderr,
        )
        return EXIT_UPSTREAM
    print(f"creds OK (HTTP {resp.status_code} from /seasons/0)")
    return EXIT_OK


def _cmd_status(settings, *, as_json: bool = False) -> int:
    from .db.repository import ALL_TABLES, HYDRATABLE_TABLES

    conn = open_db(settings.db_path)
    try:
        repo = Repository(conn)
        # Compute first, render second — keeps the JSON / table branches
        # consuming the same data so a future query change can't drift them.
        rows: list[dict] = []
        for table in ALL_TABLES:
            entry: dict = {"table": table, "rows": repo.count(table)}
            if table in HYDRATABLE_TABLES:
                entry["hydrated"] = repo.count_hydrated(table)
                latest = repo.latest_fetched_at(table)
                entry["last_hydrated"] = latest[:10] if latest else None
            else:
                entry["hydrated"] = None
                entry["last_hydrated"] = None
            rows.append(entry)

        if as_json:
            payload = {
                "db_path": str(settings.db_path),
                "schema_version": repo.schema_version(),
                "tables": rows,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"DB: {settings.db_path}")
            print(f"schema_version: {repo.schema_version()}")
            print()
            print(f"{'table':<20} {'rows':>10} {'hydrated':>10} {'last_hydrated':>14}")
            for entry in rows:
                hydrated = "-" if entry["hydrated"] is None else str(entry["hydrated"])
                last_hydrated = entry["last_hydrated"] or "-"
                print(f"{entry['table']:<20} {entry['rows']:>10} {hydrated:>10} {last_hydrated:>14}")
    finally:
        conn.close()
    return 0


def _cmd_refresh(settings, *, limit, stale_days, workers) -> int:
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    stale = stale_days if stale_days is not None else settings.stale_days
    conn = open_db(settings.db_path)
    try:
        repo = Repository(conn)
        client = APIClient(settings)
        summary = refresh_orchestrator.refresh_all(repo, client, stale_days=stale, limit=limit)
        _print_summary(summary)
    finally:
        conn.close()
    return 0


def _cmd_pull_new(settings, *, limit, workers, grace_days, stale_days) -> int:
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    grace = grace_days if grace_days is not None else settings.grace_days
    # `stale_days` is forwarded as-is (including None) so pull_new can keep
    # its default "athletes: NULL-only" semantics — settings.stale_days is
    # NOT used here because pull_new's stale_days kwarg only applies to the
    # athletes phase and has different default semantics from refresh's.
    conn = open_db(settings.db_path)
    try:
        repo = Repository(conn)
        client = APIClient(settings)
        summary = refresh_orchestrator.pull_new(
            repo, client, limit=limit, grace_days=grace, stale_days=stale_days,
        )
        _print_summary(summary)
    finally:
        conn.close()
    return 0


def _cmd_hydrate(settings, entities, *, limit, stale_days, workers) -> int:
    """Hydrate one-or-more entities in the order given on the CLI.

    Each entity runs sequentially against the same open connection — running
    `seasons → events → athletes` in one invocation is materially cheaper than
    three separate `wcl-data hydrate` calls (single conn, single APIClient
    session, single auth-failure counter).

    Duplicates in `entities` are dropped while preserving first-seen order
    (`hydrate seasons athletes seasons` runs each exactly once); argparse
    accepts the dupe because each token individually satisfies the `choices`
    constraint, and re-running a hydrate phase just wastes API budget.
    """
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    stale = stale_days if stale_days is not None else settings.stale_days
    entities = list(dict.fromkeys(entities))
    conn = open_db(settings.db_path)
    try:
        repo = Repository(conn)
        client = APIClient(settings)
        for entity in entities:
            ok, fail = refresh_orchestrator.hydrate_entity(
                repo, client, entity, stale_days=stale, limit=limit
            )
            print(f"{entity}: {ok} hydrated, {fail} failed.")
    finally:
        conn.close()
    return 0


def _cmd_export(
    settings,
    *,
    view: Optional[str],
    output_dir: Optional[Path],
    format: str = "csv",
) -> int:
    from .exporter import DEFAULT_EXPORT_DIR, VIEW_NAMES, export_all, export_view

    out = output_dir if output_dir is not None else DEFAULT_EXPORT_DIR
    conn = open_db(settings.db_path)
    try:
        if view is None:
            try:
                paths = export_all(conn, output_dir=out, format=format)
            except ImportError as exc:
                # Parquet path without pyarrow installed — friendly message
                # instead of a raw traceback, mapped to EXIT_USAGE so scripts
                # can tell "missing optional dep" apart from EXIT_GENERIC.
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            print(f"Exported {len(paths)} view(s) to {out}:")
            for name, path in paths.items():
                print(f"  {name:<14}  {path.name}")
        else:
            if view not in VIEW_NAMES:
                print(
                    f"Unknown view {view!r}. Available: {', '.join(VIEW_NAMES)}.",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            try:
                path = export_view(conn, view, output_dir=out, format=format)
            except ImportError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            print(f"Exported {view} -> {path}")
    finally:
        conn.close()
    return 0


def _reconfigure_stdio_utf8() -> None:
    """Force stdout/stderr to UTF-8 on Windows so non-ASCII (Žilina, St. Pölten,
    🧗) in printed warehouse content doesn't crash on the default cp1252 console.

    Best-effort: pytest's capsys and other test/redirect substitutes wrap the
    streams in objects that either don't expose `.reconfigure()` (AttributeError)
    or have it but can't honor the call — closed/detached streams raise OSError
    or ValueError. All three are swallowed; this is best-effort, not a hard
    requirement. POSIX terminals are UTF-8 by default — no-op there.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            # Captured / wrapped / closed stream — nothing to do; leave it alone.
            pass


def _print_summary(summary: dict[str, tuple[int, int]], *, file=None) -> None:
    out = file if file is not None else sys.stdout
    print(f"{'entity':<20} {'hydrated':>10} {'failed':>10}", file=out)
    for entity, (ok, fail) in summary.items():
        print(f"{entity:<20} {ok:>10} {fail:>10}", file=out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
