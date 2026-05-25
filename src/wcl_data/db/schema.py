"""SQLite schema for the World Climbing warehouse.

Every entity table that maps 1:1 to an API endpoint has a `last_fetched_at`
column so the hydrate step can re-fetch only stale or new rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_VERSION = 5

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    year INTEGER,
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seasons_last_fetched ON seasons(last_fetched_at);

CREATE TABLE IF NOT EXISTS leagues (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS season_leagues (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    season_id INTEGER REFERENCES seasons(id),
    league_id INTEGER REFERENCES leagues(id),
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_season_leagues_last_fetched ON season_leagues(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_season_leagues_season ON season_leagues(season_id);

CREATE TABLE IF NOT EXISTS disciplines (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    gender INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    season_id INTEGER REFERENCES seasons(id),
    league_id INTEGER REFERENCES leagues(id),
    name TEXT,
    city TEXT,
    country TEXT,
    country_iso3 TEXT,
    date_start TEXT,
    date_end TEXT,
    is_paraclimbing INTEGER,
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_last_fetched ON events(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_events_season ON events(season_id);

CREATE TABLE IF NOT EXISTS competitions (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    discipline_id INTEGER REFERENCES disciplines(id),
    category_id INTEGER REFERENCES categories(id),
    ifsc_id INTEGER NOT NULL,
    last_fetched_at TEXT,
    UNIQUE (event_id, ifsc_id)
);
CREATE INDEX IF NOT EXISTS idx_competitions_last_fetched ON competitions(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_competitions_event ON competitions(event_id);

CREATE TABLE IF NOT EXISTS athletes (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    firstname TEXT,
    lastname TEXT,
    gender INTEGER,
    height INTEGER,
    arm_span INTEGER,
    birthday TEXT,
    city TEXT,
    country TEXT,
    country_iso3 TEXT,
    photo_url TEXT,
    federation_id INTEGER,
    federation_name TEXT,
    federation_abbreviation TEXT,
    federation_url TEXT,
    paraclimbing_sport_class TEXT,
    sport_class_status TEXT,
    sport_class_review_date TEXT,
    speed_pb_time TEXT,
    speed_pb_date TEXT,
    speed_pb_event_name TEXT,
    speed_pb_round_name TEXT,
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_athletes_last_fetched ON athletes(last_fetched_at);

CREATE TABLE IF NOT EXISTS cup_rankings (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    cup_ifsc_id INTEGER NOT NULL,
    cup_name TEXT,
    season TEXT,
    discipline TEXT,
    d_cat_id INTEGER,
    rank INTEGER,
    UNIQUE (athlete_id, cup_ifsc_id, d_cat_id)
);
CREATE INDEX IF NOT EXISTS idx_cup_rankings_athlete ON cup_rankings(athlete_id);
CREATE INDEX IF NOT EXISTS idx_cup_rankings_cup ON cup_rankings(cup_ifsc_id);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    UNIQUE (competition_id, athlete_id)
);
CREATE INDEX IF NOT EXISTS idx_results_athlete ON results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_results_competition ON results(competition_id);

CREATE TABLE IF NOT EXISTS category_rounds (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    kind TEXT,
    name TEXT,
    category TEXT,
    format TEXT,
    format_identifier TEXT,
    status TEXT,
    status_as_of TEXT,
    league_round_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_category_rounds_competition ON category_rounds(competition_id);

CREATE TABLE IF NOT EXISTS round_stages (
    id INTEGER PRIMARY KEY,
    category_round_id INTEGER NOT NULL REFERENCES category_rounds(id),
    seq INTEGER NOT NULL,
    name TEXT,
    kind TEXT,
    heat_id INTEGER,
    combined_stage_ifsc_id INTEGER,
    UNIQUE (category_round_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_round_stages_round ON round_stages(category_round_id);
CREATE INDEX IF NOT EXISTS idx_round_stages_heat ON round_stages(heat_id);

CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    category_round_id INTEGER NOT NULL REFERENCES category_rounds(id),
    name TEXT
);
CREATE INDEX IF NOT EXISTS idx_routes_round ON routes(category_round_id);

CREATE TABLE IF NOT EXISTS round_results (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    category_round_id INTEGER NOT NULL REFERENCES category_rounds(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    score TEXT,
    starting_group TEXT,
    UNIQUE (category_round_id, athlete_id)
);
CREATE INDEX IF NOT EXISTS idx_round_results_round ON round_results(category_round_id);
CREATE INDEX IF NOT EXISTS idx_round_results_athlete ON round_results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_round_results_competition ON round_results(competition_id);

CREATE TABLE IF NOT EXISTS stage_results (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    round_stage_id INTEGER NOT NULL REFERENCES round_stages(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    score TEXT,
    time_ms INTEGER,
    winner INTEGER,
    UNIQUE (round_stage_id, athlete_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_results_stage ON stage_results(round_stage_id);
CREATE INDEX IF NOT EXISTS idx_stage_results_athlete ON stage_results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_stage_results_competition ON stage_results(competition_id);

CREATE TABLE IF NOT EXISTS ascents (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    round_stage_id INTEGER NOT NULL REFERENCES round_stages(id),
    route_id INTEGER NOT NULL REFERENCES routes(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    score TEXT,
    status TEXT,
    modified TEXT,
    top INTEGER,
    plus INTEGER,
    corrective_rank REAL,
    top_tries INTEGER,
    restarted INTEGER,
    time_ms INTEGER,
    dnf INTEGER,
    dns INTEGER,
    zone INTEGER,
    zone_tries INTEGER,
    low_zone INTEGER,
    low_zone_tries INTEGER,
    points REAL,
    UNIQUE (round_stage_id, athlete_id, route_id)
);
CREATE INDEX IF NOT EXISTS idx_ascents_stage ON ascents(round_stage_id);
CREATE INDEX IF NOT EXISTS idx_ascents_route ON ascents(route_id);
CREATE INDEX IF NOT EXISTS idx_ascents_athlete ON ascents(athlete_id);
CREATE INDEX IF NOT EXISTS idx_ascents_competition ON ascents(competition_id);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Idempotent.

    For DBs created before a column existed, missing columns are added via
    ALTER TABLE — guarded by a PRAGMA table_info check so the call stays
    idempotent. Symmetric `_drop_column_if_exists` / `_drop_index_if_exists`
    handle removals (e.g. schema v3 → v4 dropped `athletes.is_paraclimbing`
    in favour of the raw `paraclimbing_sport_class`; v4 → v5 dropped
    `last_fetched_at` from `category_rounds` and `routes`, which were
    reserved for a startlist hydrator that never landed and were never set
    in practice — see the 2026-05-25 note on ADR 0007).
    """
    conn.executescript(DDL)
    _add_missing_column(conn, "events", "country_iso3", "TEXT")
    _add_missing_column(conn, "athletes", "country_iso3", "TEXT")
    for col, sql_type in (
        ("federation_id", "INTEGER"),
        ("federation_name", "TEXT"),
        ("federation_abbreviation", "TEXT"),
        ("federation_url", "TEXT"),
        ("paraclimbing_sport_class", "TEXT"),
        ("sport_class_status", "TEXT"),
        ("sport_class_review_date", "TEXT"),
        ("speed_pb_time", "TEXT"),
        ("speed_pb_date", "TEXT"),
        ("speed_pb_event_name", "TEXT"),
        ("speed_pb_round_name", "TEXT"),
    ):
        _add_missing_column(conn, "athletes", col, sql_type)
    _drop_column_if_exists(conn, "athletes", "is_paraclimbing")
    _drop_index_if_exists(conn, "idx_category_rounds_last_fetched")
    _drop_index_if_exists(conn, "idx_routes_last_fetched")
    _drop_column_if_exists(conn, "category_rounds", "last_fetched_at")
    _drop_column_if_exists(conn, "routes", "last_fetched_at")
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (CURRENT_VERSION,),
    )
    conn.commit()


def _add_missing_column(
    conn: sqlite3.Connection, table: str, column: str, sql_type: str
) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _drop_column_if_exists(
    conn: sqlite3.Connection, table: str, column: str
) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _drop_index_if_exists(conn: sqlite3.Connection, index: str) -> None:
    conn.execute(f"DROP INDEX IF EXISTS {index}")


def open_db(path: Path) -> sqlite3.Connection:
    """Open the DB at `path`, applying schema if missing.

    WAL journal mode lets a reader (notebook, status query) coexist with the
    long-running ingest writer instead of blocking on a SHARED lock; the
    `-wal` and `-shm` sidecar files appear alongside `wcl.sqlite` and are
    rolled back into the main file on a clean close. `synchronous=NORMAL`
    is the WAL-recommended pairing — durable across process crashes (only a
    power loss between WAL frame writes can lose the last transaction),
    materially faster than FULL on the per-item commit cadence used by the
    fetchers.

    SQLite silently falls back from WAL to a rollback journal on filesystems
    that can't host the shared-memory `-shm` file (CIFS / SMB mounts, some
    FUSE drivers, WSL2 `/mnt/c` paths in certain modes). We read the result
    row back and log a WARNING when that happens so the reader-coexistence
    guarantee isn't silently false.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    mode_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    applied_mode = (mode_row[0] if mode_row else "").lower()
    if applied_mode != "wal":
        import logging
        logging.getLogger(__name__).warning(
            "PRAGMA journal_mode=WAL refused by SQLite for %s (got %r). "
            "Reader-vs-writer concurrency falls back to SHARED-lock semantics; "
            "common cause: network filesystem (CIFS/SMB, some FUSE/WSL paths).",
            path, applied_mode,
        )
    conn.execute("PRAGMA synchronous = NORMAL")
    apply_schema(conn)
    return conn
