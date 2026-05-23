"""SQLite schema for the IFSC warehouse.

Every entity table that maps 1:1 to an API endpoint has a `last_fetched_at`
column so the hydrate step can re-fetch only stale or new rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_VERSION = 1

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
    photo_url TEXT,
    is_paraclimbing INTEGER,
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_athletes_last_fetched ON athletes(last_fetched_at);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    UNIQUE (competition_id, athlete_id)
);
CREATE INDEX IF NOT EXISTS idx_results_athlete ON results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_results_competition ON results(competition_id);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Idempotent."""
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (CURRENT_VERSION,),
    )
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    """Open the DB at `path`, applying schema if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn
