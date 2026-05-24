"""Hydrate season_leagues → disciplines, categories, event skeletons."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from ..api.client import APIClient
from ..db.repository import Repository

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)

_GENDER_RE = re.compile(r"\b(?P<g>men|male|women|female)\b", re.IGNORECASE)


def hydrate(
    repo: Repository,
    client: APIClient,
    *,
    stale_days: Optional[int] = None,
    rows: Optional[list[sqlite3.Row]] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Pass either `stale_days` (default) or `rows` (used by `pull_new`)."""
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        rows = repo.find_stale("season_leagues", stale_days=stale_days)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    ifsc_to_id = {row["ifsc_id"]: row["id"] for row in rows}
    log.info("Hydrating %d season_league(s).", len(rows))

    ok = fail = 0
    for fetched in client.stream("season_leagues", ifsc_to_id.keys()):
        sl_ifsc = int(fetched.key)
        sl_row_id = ifsc_to_id[sl_ifsc]
        data = fetched.data
        try:
            # Resolve season + league IDs (rows may have been created with NULLs).
            year = data.get("season")
            season_id = None
            if year is not None:
                row = repo.conn.execute(
                    "SELECT id FROM seasons WHERE year = ?", (int(year),)
                ).fetchone()
                if row:
                    season_id = row[0]

            league_name = data.get("league")
            league_id = repo.upsert_league(league_name) if league_name else None

            repo.upsert_season_league(sl_ifsc, season_id=season_id, league_id=league_id)

            # Disciplines + categories
            for d_cat in data.get("d_cats") or []:
                _ingest_d_cat(repo, d_cat.get("name") or "")

            # Event skeletons (with season + league association)
            for event in data.get("events") or []:
                ev_ifsc = event.get("event_id")
                if ev_ifsc is not None:
                    repo.upsert_event_skeleton(int(ev_ifsc), season_id=season_id, league_id=league_id)

            repo.mark_fetched("season_leagues", sl_row_id)
            ok += 1
        except Exception as exc:
            log.exception("Failed to parse /season_leagues/%s: %s", sl_ifsc, exc)
            fail += 1

    log.info("Season_leagues: %d hydrated, %d failed.", ok, fail)
    return ok, fail


def _ingest_d_cat(repo: Repository, d_cat_name: str) -> None:
    """`d_cat.name` is "<discipline> <category>" e.g. 'Lead Men'."""
    parts = d_cat_name.strip().split(maxsplit=1)
    if not parts:
        return
    discipline_name = parts[0].lower()
    category_name = parts[1] if len(parts) > 1 else ""
    if not category_name:
        return

    gender: Optional[int] = None
    m = _GENDER_RE.search(category_name)
    if m:
        gender = 0 if m.group("g").lower() in ("men", "male") else 1

    repo.upsert_discipline(discipline_name)
    repo.upsert_category(category_name, gender)
