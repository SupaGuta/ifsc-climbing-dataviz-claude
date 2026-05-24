"""Denormalized CSV exports of the warehouse.

Each view in `VIEWS` is a single SELECT that pre-joins related tables so the
resulting CSV is self-contained — no need to open the SQLite file to follow
foreign keys. Gender is exported as `"male"`/`"female"` (not the integer
encoding) for readability.

Filenames carry a UTC timestamp (`<view>_YYYY-MM-DDTHHMMSSZ.csv`) so multiple
exports don't overwrite each other.

`ascents` is registered but excluded from `export_all` (size: ~900k rows of
22 columns ≈ 200 MB+). Run `python -m wcl_data export ascents` explicitly
when needed.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT

log = logging.getLogger(__name__)

DEFAULT_EXPORT_DIR = REPO_ROOT / "data" / "exports"

VIEWS: dict[str, str] = {
    "seasons": """
        SELECT
            ifsc_id AS season_ifsc_id,
            year,
            last_fetched_at
        FROM seasons
        ORDER BY year DESC
    """,
    "leagues": """
        SELECT
            id AS league_id,
            name
        FROM leagues
        ORDER BY name
    """,
    "events": """
        SELECT
            e.ifsc_id AS event_ifsc_id,
            e.name AS event_name,
            s.year AS season_year,
            l.name AS league_name,
            e.city,
            e.country,
            e.country_iso3,
            e.date_start,
            e.date_end,
            e.is_paraclimbing
        FROM events e
        LEFT JOIN seasons s ON e.season_id = s.id
        LEFT JOIN leagues l ON e.league_id = l.id
        ORDER BY e.date_start DESC, e.ifsc_id
    """,
    "competitions": """
        SELECT
            c.ifsc_id AS competition_ifsc_id,
            e.ifsc_id AS event_ifsc_id,
            e.name AS event_name,
            s.year AS season_year,
            d.name AS discipline,
            cat.name AS category,
            CASE cat.gender WHEN 0 THEN 'male' WHEN 1 THEN 'female' ELSE NULL END AS gender,
            e.date_start
        FROM competitions c
        JOIN events e ON c.event_id = e.id
        LEFT JOIN seasons s ON e.season_id = s.id
        LEFT JOIN disciplines d ON c.discipline_id = d.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        ORDER BY e.date_start DESC, c.id
    """,
    "athletes": """
        SELECT
            ifsc_id AS athlete_ifsc_id,
            firstname,
            lastname,
            CASE gender WHEN 0 THEN 'male' WHEN 1 THEN 'female' ELSE NULL END AS gender,
            height,
            arm_span,
            birthday,
            city,
            country,
            country_iso3,
            is_paraclimbing
        FROM athletes
        ORDER BY ifsc_id
    """,
    "results": """
        SELECT
            e.ifsc_id AS event_ifsc_id,
            e.name AS event_name,
            s.year AS season_year,
            l.name AS league_name,
            e.city AS event_city,
            e.country AS event_country,
            e.country_iso3 AS event_country_iso3,
            e.date_start AS event_date,
            d.name AS discipline,
            cat.name AS category,
            CASE cat.gender WHEN 0 THEN 'male' WHEN 1 THEN 'female' ELSE NULL END AS gender,
            a.ifsc_id AS athlete_ifsc_id,
            a.firstname AS athlete_firstname,
            a.lastname AS athlete_lastname,
            a.country AS athlete_country,
            a.country_iso3 AS athlete_country_iso3,
            r.rank
        FROM results r
        JOIN competitions c ON r.competition_id = c.id
        JOIN events e ON c.event_id = e.id
        LEFT JOIN seasons s ON e.season_id = s.id
        LEFT JOIN leagues l ON e.league_id = l.id
        LEFT JOIN disciplines d ON c.discipline_id = d.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        JOIN athletes a ON r.athlete_id = a.id
        ORDER BY e.date_start DESC, c.id, r.rank
    """,
    "round_results": """
        SELECT
            e.ifsc_id AS event_ifsc_id,
            e.name AS event_name,
            s.year AS season_year,
            l.name AS league_name,
            e.city AS event_city,
            e.country AS event_country,
            e.country_iso3 AS event_country_iso3,
            e.date_start AS event_date,
            d.name AS discipline,
            cat.name AS category,
            CASE cat.gender WHEN 0 THEN 'male' WHEN 1 THEN 'female' ELSE NULL END AS gender,
            cr.ifsc_id AS category_round_ifsc_id,
            cr.name AS round_name,
            cr.kind AS round_kind,
            cr.format AS round_format,
            cr.league_round_id,
            a.ifsc_id AS athlete_ifsc_id,
            a.firstname AS athlete_firstname,
            a.lastname AS athlete_lastname,
            a.country AS athlete_country,
            a.country_iso3 AS athlete_country_iso3,
            rr.rank AS round_rank,
            rr.score AS round_score,
            rr.starting_group
        FROM round_results rr
        JOIN category_rounds cr ON rr.category_round_id = cr.id
        JOIN competitions c ON rr.competition_id = c.id
        JOIN events e ON c.event_id = e.id
        LEFT JOIN seasons s ON e.season_id = s.id
        LEFT JOIN leagues l ON e.league_id = l.id
        LEFT JOIN disciplines d ON c.discipline_id = d.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        JOIN athletes a ON rr.athlete_id = a.id
        ORDER BY e.date_start DESC, c.id, cr.league_round_id, rr.rank
    """,
    "ascents": """
        SELECT
            e.ifsc_id AS event_ifsc_id,
            e.date_start AS event_date,
            d.name AS discipline,
            cat.name AS category,
            cr.name AS round_name,
            cr.kind AS round_kind,
            rs.seq AS stage_seq,
            rs.name AS stage_name,
            rs.kind AS stage_kind,
            rt.ifsc_id AS route_ifsc_id,
            rt.name AS route_name,
            a.ifsc_id AS athlete_ifsc_id,
            a.firstname AS athlete_firstname,
            a.lastname AS athlete_lastname,
            a.country AS athlete_country,
            a.country_iso3 AS athlete_country_iso3,
            asc_.rank AS ascent_rank,
            asc_.score AS ascent_score,
            asc_.top,
            asc_.plus,
            asc_.status,
            asc_.corrective_rank,
            asc_.top_tries,
            asc_.restarted,
            asc_.time_ms,
            asc_.dnf,
            asc_.dns,
            asc_.zone,
            asc_.zone_tries,
            asc_.low_zone,
            asc_.low_zone_tries,
            asc_.points,
            asc_.modified
        FROM ascents asc_
        JOIN routes rt ON asc_.route_id = rt.id
        JOIN round_stages rs ON asc_.round_stage_id = rs.id
        JOIN category_rounds cr ON rs.category_round_id = cr.id
        JOIN competitions c ON asc_.competition_id = c.id
        JOIN events e ON c.event_id = e.id
        LEFT JOIN disciplines d ON c.discipline_id = d.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        JOIN athletes a ON asc_.athlete_id = a.id
        ORDER BY e.date_start DESC, c.id, cr.league_round_id, rs.seq, rt.ifsc_id, asc_.rank
    """,
}

VIEW_NAMES: tuple[str, ...] = tuple(VIEWS.keys())

# Views included in `export_all`. `ascents` is registered but opt-in only
# (very wide and ~6× the row count of round_results).
DEFAULT_EXPORT_VIEWS: tuple[str, ...] = tuple(n for n in VIEW_NAMES if n != "ascents")


def _timestamp() -> str:
    """Filename-safe UTC stamp, e.g. `2026-05-22T185030Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def export_view(
    conn: sqlite3.Connection,
    name: str,
    *,
    output_dir: Path = DEFAULT_EXPORT_DIR,
) -> Path:
    """Run the named view and write its rows to a timestamped CSV.

    Raises ValueError if `name` isn't in `VIEWS`. Returns the output path.
    """
    if name not in VIEWS:
        raise ValueError(f"Unknown view {name!r}. Choose from {VIEW_NAMES}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}_{_timestamp()}.csv"

    cursor = conn.execute(VIEWS[name])
    columns = [d[0] for d in cursor.description]

    row_count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in cursor:
            writer.writerow(row)
            row_count += 1

    log.info("Exported %d row(s) of %s -> %s", row_count, name, path.name)
    return path


def export_all(
    conn: sqlite3.Connection,
    *,
    output_dir: Path = DEFAULT_EXPORT_DIR,
) -> dict[str, Path]:
    """Export every default view. `ascents` is opt-in; call `export_view` for it."""
    return {name: export_view(conn, name, output_dir=output_dir) for name in DEFAULT_EXPORT_VIEWS}
