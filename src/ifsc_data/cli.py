"""Command-line interface for the IFSC ingest layer."""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from . import logging_setup
from .api.client import APIClient
from .config import load_settings
from .db.repository import Repository
from .db.schema import open_db
from .fetchers import refresh as refresh_orchestrator
from .fetchers.refresh import ENTITIES

log = logging.getLogger(__name__)

# Commands that don't need IFSC API credentials.
_NO_CREDS_COMMANDS = {"init", "status", "export", "auth"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ifsc-data",
        description="Ingest the IFSC public API into a local SQLite warehouse.",
    )
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Keep WARNINGs on the console (default: hidden, written to logs/ifsc-data.log).")
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

    p_refresh = sub.add_parser(
        "refresh", help="Discover new entities and hydrate stale rows across the graph."
    )
    p_refresh.add_argument("--limit", type=int, default=None,
                           help="Cap the number of rows hydrated per entity (smoke testing).")
    p_refresh.add_argument("--stale-days", type=int, default=None,
                           help="Override IFSC_STALE_DAYS for this run.")
    p_refresh.add_argument("--workers", type=int, default=None,
                           help="Override IFSC_MAX_WORKERS for this run (default 50; useful range 50-100).")

    p_pull = sub.add_parser(
        "pull-new",
        help="Force-refresh container entities to discover new content; hydrate only newly-discovered athletes."
    )
    p_pull.add_argument("--limit", type=int, default=None,
                        help="Cap rows per entity (smoke testing).")
    p_pull.add_argument("--workers", type=int, default=None,
                        help="Override IFSC_MAX_WORKERS for this run.")

    p_hydrate = sub.add_parser("hydrate", help="Hydrate one entity only.")
    p_hydrate.add_argument("entity", choices=ENTITIES, help="Which entity to hydrate.")
    p_hydrate.add_argument("--limit", type=int, default=None)
    p_hydrate.add_argument("--stale-days", type=int, default=None)
    p_hydrate.add_argument("--workers", type=int, default=None,
                           help="Override IFSC_MAX_WORKERS for this run (default 50; useful range 50-100).")

    sub.add_parser("status", help="Print row counts and hydration coverage.")

    from .exporter import VIEW_NAMES as _EXPORT_VIEW_NAMES
    p_export = sub.add_parser(
        "export",
        help="Export denormalized views to timestamped CSV files in data/exports/.",
    )
    p_export.add_argument(
        "view", nargs="?", default=None,
        help=f"Optional view name (default: export all). Available: {', '.join(_EXPORT_VIEW_NAMES)}.",
    )
    p_export.add_argument(
        "--output-dir", type=Path, default=None,
        help="Override the default exports directory (data/exports/).",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging_setup.configure(verbose=args.verbose)

    settings = load_settings(require_credentials=args.command not in _NO_CREDS_COMMANDS)

    if args.command == "init":
        with open_db(settings.db_path) as _:
            pass
        log.info("DB initialised at %s", settings.db_path)
        return 0

    if args.command == "auth":
        return _cmd_auth(dry_run=args.dry_run, env_file=args.env_file)
    if args.command == "status":
        return _cmd_status(settings)
    if args.command == "export":
        return _cmd_export(settings, view=args.view, output_dir=args.output_dir)
    if args.command == "refresh":
        return _cmd_refresh(settings, limit=args.limit, stale_days=args.stale_days, workers=args.workers)
    if args.command == "pull-new":
        return _cmd_pull_new(settings, limit=args.limit, workers=args.workers)
    if args.command == "hydrate":
        return _cmd_hydrate(settings, args.entity, limit=args.limit, stale_days=args.stale_days, workers=args.workers)
    parser.error(f"Unknown command {args.command}")


def _cmd_auth(*, dry_run: bool, env_file: Optional[Path]) -> int:
    from .api.credentials import REFERER_URL, fetch_credentials, update_env_file
    from .config import REPO_ROOT

    creds = fetch_credentials()
    print(f"Fetched fresh credentials from {REFERER_URL}")
    print(f"  CSRF token:     {creds.csrf_token[:16]}... ({len(creds.csrf_token)} chars)")
    cookie_name = creds.session_cookie.split("=", 1)[0]
    print(f"  Session cookie: {cookie_name}=... ({len(creds.session_cookie)} chars)")

    if dry_run:
        print()
        print("--dry-run: not writing to .env. Lines that would be written:")
        print(f"  IFSC_CSRF_TOKEN={creds.csrf_token}")
        print(f"  IFSC_SESSION_COOKIE={creds.session_cookie}")
        return 0

    target = env_file if env_file is not None else REPO_ROOT / ".env"
    update_env_file(target, creds.csrf_token, creds.session_cookie)
    print(f"Wrote {target}")
    return 0


def _cmd_status(settings) -> int:
    conn = open_db(settings.db_path)
    repo = Repository(conn)
    print(f"DB: {settings.db_path}")
    print(f"{'table':<20} {'rows':>10} {'hydrated':>10}")
    for table in ("seasons", "leagues", "season_leagues", "disciplines",
                  "categories", "events", "competitions", "athletes", "results"):
        total = repo.count(table)
        hydrated = "-"
        if table in ("seasons", "season_leagues", "events", "competitions", "athletes"):
            hydrated = str(repo.count_hydrated(table))
        print(f"{table:<20} {total:>10} {hydrated:>10}")
    conn.close()
    return 0


def _cmd_refresh(settings, *, limit, stale_days, workers) -> int:
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    stale = stale_days if stale_days is not None else settings.stale_days
    conn = open_db(settings.db_path)
    repo = Repository(conn)
    client = APIClient(settings)
    summary = refresh_orchestrator.refresh_all(repo, client, stale_days=stale, limit=limit)
    _print_summary(summary)
    conn.close()
    return 0


def _cmd_pull_new(settings, *, limit, workers) -> int:
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    conn = open_db(settings.db_path)
    repo = Repository(conn)
    client = APIClient(settings)
    summary = refresh_orchestrator.pull_new(repo, client, limit=limit)
    _print_summary(summary)
    conn.close()
    return 0


def _cmd_hydrate(settings, entity, *, limit, stale_days, workers) -> int:
    if workers is not None:
        settings = replace(settings, max_workers=workers)
    stale = stale_days if stale_days is not None else settings.stale_days
    conn = open_db(settings.db_path)
    repo = Repository(conn)
    client = APIClient(settings)
    ok, fail = refresh_orchestrator.hydrate_entity(
        repo, client, entity, stale_days=stale, limit=limit
    )
    print(f"{entity}: {ok} hydrated, {fail} failed.")
    conn.close()
    return 0


def _cmd_export(settings, *, view: Optional[str], output_dir: Optional[Path]) -> int:
    from .exporter import DEFAULT_EXPORT_DIR, VIEW_NAMES, export_all, export_view

    out = output_dir if output_dir is not None else DEFAULT_EXPORT_DIR
    conn = open_db(settings.db_path)
    try:
        if view is None:
            paths = export_all(conn, output_dir=out)
            print(f"Exported {len(paths)} view(s) to {out}:")
            for name, path in paths.items():
                print(f"  {name:<14}  {path.name}")
        else:
            if view not in VIEW_NAMES:
                print(
                    f"Unknown view {view!r}. Available: {', '.join(VIEW_NAMES)}.",
                    file=sys.stderr,
                )
                return 2
            path = export_view(conn, view, output_dir=out)
            print(f"Exported {view} -> {path}")
    finally:
        conn.close()
    return 0


def _print_summary(summary: dict[str, tuple[int, int]]) -> None:
    print(f"{'entity':<20} {'hydrated':>10} {'failed':>10}")
    for entity, (ok, fail) in summary.items():
        print(f"{entity:<20} {ok:>10} {fail:>10}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
