"""SQLite schema for the World Climbing warehouse.

Every entity table that maps 1:1 to an API endpoint has a `last_fetched_at`
column so the hydrate step can re-fetch only stale or new rows.

The schema is versioned via `schema_version`. `apply_schema` reads the
current version and runs the smallest set of forward migrations needed to
reach `CURRENT_VERSION`. A brand-new DB short-circuits to the final DDL and
records `CURRENT_VERSION` directly. Migration steps commit their own work,
then `apply_schema` writes the version row in a separate commit — so a
crash between steps leaves the warehouse at a coherent prior version
rather than half-migrated. See ADR 0011.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CURRENT_VERSION = 6

# ALTER TABLE … DROP COLUMN landed in SQLite 3.35 (2021-03). The v4→v5
# migration relies on it, and downgrading older clients silently is worse
# than failing loudly — assert at open time.
_MIN_SQLITE = (3, 35)

# Final v6 DDL. CREATE TABLE IF NOT EXISTS makes this safe to run on a DB
# whose existing tables already have the target shape (no-op) and on a
# blank DB (full create). Tables that need the v5→v6 constraints applied
# are rebuilt by `_migrate_v5_to_v6`, not by re-running this DDL.
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
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    league_id INTEGER NOT NULL REFERENCES leagues(id),
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
    gender INTEGER CHECK (gender IS NULL OR gender IN (0, 1))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    league_id INTEGER REFERENCES leagues(id),
    name TEXT,
    city TEXT,
    country TEXT,
    country_iso3 TEXT,
    date_start TEXT CHECK (date_start IS NULL OR date_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    date_end TEXT CHECK (date_end IS NULL OR date_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    is_paraclimbing INTEGER CHECK (is_paraclimbing IS NULL OR is_paraclimbing IN (0, 1)),
    last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_last_fetched ON events(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_events_season ON events(season_id);
CREATE INDEX IF NOT EXISTS idx_events_date_end ON events(date_end);

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
    gender INTEGER CHECK (gender IS NULL OR gender IN (0, 1)),
    height INTEGER,
    arm_span INTEGER,
    birthday TEXT CHECK (birthday IS NULL OR birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
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
    -- d_cat_id > 0 guards against a future API quirk where d_cat_id=-1 (or any
    -- non-positive sentinel) would collide with the NULL bucket of the v6
    -- expression UNIQUE on COALESCE(d_cat_id, -1).
    d_cat_id INTEGER CHECK (d_cat_id IS NULL OR d_cat_id > 0),
    rank INTEGER
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
    status_as_of TEXT CHECK (status_as_of IS NULL OR status_as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
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
    winner INTEGER CHECK (winner IS NULL OR winner IN (0, 1)),
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
    top INTEGER CHECK (top IS NULL OR top IN (0, 1)),
    plus INTEGER CHECK (plus IS NULL OR plus IN (0, 1)),
    corrective_rank REAL,
    top_tries INTEGER,
    restarted INTEGER CHECK (restarted IS NULL OR restarted IN (0, 1)),
    time_ms INTEGER,
    dnf INTEGER CHECK (dnf IS NULL OR dnf IN (0, 1)),
    dns INTEGER CHECK (dns IS NULL OR dns IN (0, 1)),
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

# v6-only artifacts that the shared `DDL` omits because they're unsafe to
# run on a real v5 DB. The expression UNIQUE on cup_rankings would fail
# when an inherited v5 row set contains NULL-d_cat duplicates (which v5's
# inline UNIQUE allowed under NULL≠NULL semantics). Installed by the
# fresh-DB fast path and by `_migrate_v5_to_v6` (which dedupes first).
_DDL_V6_ONLY = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_cup_rankings_uniq
    ON cup_rankings(athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1));
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Bring the DB at `conn` to `CURRENT_VERSION` via forward migrations.

    Fresh DBs short-circuit to the final DDL and record `CURRENT_VERSION`
    directly — no per-version migration work needed. Existing DBs at version
    V run each `_migrate_vN_to_vN+1` for N in [V, CURRENT_VERSION); the
    version row is written in a separate commit after each migration
    completes, so an interrupted run resumes cleanly.

    Idempotent: running twice on a current-version DB is a no-op.
    """
    _ensure_schema_version_table(conn)
    current = _read_current_version(conn)

    if current == 0 and _is_brand_new(conn):
        # Fresh install — apply final DDL once, record version, done.
        conn.executescript(DDL)
        conn.executescript(_DDL_V6_ONLY)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (CURRENT_VERSION,)
        )
        conn.commit()
        return

    for target_version in sorted(_MIGRATIONS):
        if current < target_version:
            _MIGRATIONS[target_version](conn)
            conn.commit()
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (target_version,)
            )
            conn.commit()
            current = target_version

    # Idempotent safety net: re-establish columns the migrations should have
    # added. Catches drift from prior migrations interrupted between an ALTER
    # and the version-row commit, and from out-of-band schema edits (manual
    # DROP COLUMN in a sqlite shell). Cheap — PRAGMA table_info reads are
    # microseconds and the ALTERs only fire when a column is actually missing.
    _ensure_columns_present(conn)
    conn.commit()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT DEFAULT (datetime('now'))"
        ")"
    )


def _read_current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _is_brand_new(conn: sqlite3.Connection) -> bool:
    """True if the DB has no data tables yet (only `schema_version`).

    A pre-versioning DB (created before `schema_version` existed) returns
    False — its tables exist with an older shape and need migrating.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'schema_version'"
    ).fetchone()
    return row[0] == 0


# ---------------------------------------------------------------- migrations
# Each _migrate_vN_to_vN+1 transforms the DB *in place* from version N to
# version N+1. The orchestrator (`apply_schema`) handles version recording
# and commits — migrations may issue their own commits as a means to a
# specific end (e.g. dedupe before constraint), but the final state must be
# left committed (no open transaction).


def _migrate_pre_v1_to_v5(conn: sqlite3.Connection) -> None:
    """Bring any pre-v5 DB (including pre-versioning) to v5 shape.

    The original code path lumped v0→v5 into one idempotent script; we
    preserve that here rather than reconstruct snapshots for v1-v4 we never
    captured. The CREATE TABLE IF NOT EXISTS pattern lets this run on a
    blank DB too — the v6 DDL above is a superset, but CREATE IF NOT EXISTS
    on existing tables (any prior shape) is a no-op, leaving the column-add
    / column-drop chain to actually reshape them.
    """
    conn.executescript(DDL)
    _ensure_columns_present(conn)
    _drop_column_if_exists(conn, "athletes", "is_paraclimbing")
    _drop_index_if_exists(conn, "idx_category_rounds_last_fetched")
    _drop_index_if_exists(conn, "idx_routes_last_fetched")
    _drop_column_if_exists(conn, "category_rounds", "last_fetched_at")
    _drop_column_if_exists(conn, "routes", "last_fetched_at")


def _ensure_columns_present(conn: sqlite3.Connection) -> None:
    """Re-add any v5+ column missing from existing tables.

    Idempotent — `_add_missing_column` is a no-op when the column is
    already there. Called both as the column-add leg of
    `_migrate_pre_v1_to_v5` and as a safety-net rerun at the tail of
    every `apply_schema` so a DB whose ALTER chain was interrupted (or
    whose column was manually dropped via the sqlite shell) self-heals
    on the next open instead of crashing the v5→v6 rebuild three steps
    later.
    """
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


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add NOT NULL on FKs, CHECK on boolean/date columns, expression-UNIQUE
    on cup_rankings, and the date_end index.

    SQLite can't ALTER TABLE ADD CONSTRAINT — adding NOT NULL/CHECK to an
    existing table requires the 12-step rebuild dance documented at
    https://sqlite.org/lang_altertable.html. The pattern: PRAGMA
    foreign_keys=OFF (so DROP TABLE doesn't cascade) → BEGIN → rebuild each
    affected table → foreign_key_check → COMMIT → PRAGMA foreign_keys=ON.

    Pre-steps before the rebuild — all idempotent, all outside the
    FK-toggle so they survive a mid-migration crash:

    1. Re-establish missing v5 columns (defensive: `country_iso3`,
       `federation_*`, etc. should already be there, but a previous
       interrupted migration could have left a gap).
    2. Deduplicate `cup_rankings` rows with NULL d_cat_id on the same
       (athlete, cup) — v5's UNIQUE allowed them under NULL≠NULL
       semantics, but v6's expression index COALESCEs them so they'd
       collide.
    3. Pre-validate NOT NULL columns. The rebuild would fail mid-flight
       with a confusing IntegrityError; fail loud at the top instead so
       the operator gets the exact column + row count to investigate.
    4. NULL-out values that violate the new CHECK constraints
       (gender ∉ {0,1,NULL}, malformed dates, etc.). Discarding bad
       legacy data is the same outcome as a failed rebuild, just
       reached without locking the operator out of the warehouse.

    Switches to manual `isolation_level=None` for the rebuild because
    Python's sqlite3 module otherwise commits implicitly around DDL, which
    races with the explicit BEGIN/ROLLBACK pair this migration needs.
    """
    _ensure_columns_present(conn)
    conn.commit()

    conn.execute(
        "DELETE FROM cup_rankings WHERE id NOT IN ("
        "  SELECT MAX(id) FROM cup_rankings "
        "  GROUP BY athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1)"
        ")"
    )
    conn.commit()

    _v5_to_v6_assert_not_null_preconditions(conn)
    _v5_to_v6_clean_check_violations(conn)
    conn.commit()

    conn.execute("PRAGMA foreign_keys = OFF")
    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN")
        try:
            for stmt in _split_sql(_V5_TO_V6_REBUILD):
                conn.execute(stmt)
            violations = list(conn.execute("PRAGMA foreign_key_check"))
            if violations:
                raise RuntimeError(
                    f"v5→v6 migration would orphan rows; aborting. "
                    f"Violations: {violations[:5]}{' …' if len(violations) > 5 else ''}"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prev_isolation
        conn.execute("PRAGMA foreign_keys = ON")


def _split_sql(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    Strips `--`-style line comments first so semicolons inside comments
    don't fragment a statement, then splits on top-level `;`. Used because
    `executescript` implicitly commits the open transaction before running,
    conflicting with the explicit BEGIN/COMMIT/ROLLBACK sequencing required
    by the v5→v6 table-rebuild migration. The script has no string
    literals containing `;`, so the naive top-level split is safe.
    """
    comment_free = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in comment_free.split(";") if stmt.strip()]


_MIGRATIONS = {
    5: _migrate_pre_v1_to_v5,
    6: _migrate_v5_to_v6,
}


# Columns that v6 makes NOT NULL but v5 allowed nullable. The pre-validation
# helper queries each pair and refuses to start the rebuild if any row would
# trip the new constraint.
_V6_NOT_NULL_CHECKS: tuple[tuple[str, str], ...] = (
    ("events", "season_id"),
    ("season_leagues", "season_id"),
    ("season_leagues", "league_id"),
)


def _v5_to_v6_assert_not_null_preconditions(conn: sqlite3.Connection) -> None:
    """Refuse to start the v6 rebuild if any row would trip a new NOT NULL.

    The rebuild's `INSERT INTO X_new SELECT ... FROM X` raises
    `IntegrityError: NOT NULL constraint failed` partway through and the
    operator gets a confusing stack with no row counts. Fail loud here
    instead, naming the column + row count so a one-line cleanup query
    is obvious.
    """
    failures: list[str] = []
    for table, column in _V6_NOT_NULL_CHECKS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
        ).fetchone()[0]
        if n:
            failures.append(f"{table}.{column}: {n} row(s)")
    if failures:
        raise RuntimeError(
            "Cannot migrate to v6: the following columns become NOT NULL but "
            "have NULL rows. Investigate with `SELECT id, ifsc_id FROM <table> "
            "WHERE <column> IS NULL`, then either populate the column or "
            "delete the orphan rows before retrying.\n  " + "\n  ".join(failures)
        )


# Each entry: (table, column, predicate_for_BAD_rows). The rebuild's CHECK
# would reject these, aborting the migration. NULL-ing them out is the same
# net result as failing, just without locking the warehouse closed.
_V6_CHECK_CLEANUP: tuple[tuple[str, str, str], ...] = (
    ("athletes", "gender", "gender IS NOT NULL AND gender NOT IN (0, 1)"),
    ("categories", "gender", "gender IS NOT NULL AND gender NOT IN (0, 1)"),
    ("events", "is_paraclimbing",
        "is_paraclimbing IS NOT NULL AND is_paraclimbing NOT IN (0, 1)"),
    ("stage_results", "winner",
        "winner IS NOT NULL AND winner NOT IN (0, 1)"),
    ("ascents", "top", "top IS NOT NULL AND top NOT IN (0, 1)"),
    ("ascents", "plus", "plus IS NOT NULL AND plus NOT IN (0, 1)"),
    ("ascents", "dnf", "dnf IS NOT NULL AND dnf NOT IN (0, 1)"),
    ("ascents", "dns", "dns IS NOT NULL AND dns NOT IN (0, 1)"),
    ("ascents", "restarted",
        "restarted IS NOT NULL AND restarted NOT IN (0, 1)"),
    ("events", "date_start",
        "date_start IS NOT NULL "
        "AND date_start NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"),
    ("events", "date_end",
        "date_end IS NOT NULL "
        "AND date_end NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"),
    ("athletes", "birthday",
        "birthday IS NOT NULL "
        "AND birthday NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"),
    ("category_rounds", "status_as_of",
        "status_as_of IS NOT NULL "
        "AND status_as_of NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"),
    ("cup_rankings", "d_cat_id",
        "d_cat_id IS NOT NULL AND d_cat_id <= 0"),
)


def _v5_to_v6_clean_check_violations(conn: sqlite3.Connection) -> None:
    """NULL-out values that the new v6 CHECK constraints would reject.

    Discards legacy bad data (gender=9, malformed dates, sentinel -1 in
    `cup_rankings.d_cat_id`, etc.) rather than aborting the migration
    mid-rebuild. Logs an INFO line per affected (table, column, count)
    so the operator has a paper trail of what was cleaned.
    """
    for table, column, predicate in _V6_CHECK_CLEANUP:
        cur = conn.execute(
            f"UPDATE {table} SET {column} = NULL WHERE {predicate}"
        )
        if cur.rowcount:
            log.info(
                "v5→v6 pre-validation: NULLed %d row(s) of %s.%s that violated "
                "the new CHECK constraint.", cur.rowcount, table, column,
            )


# Each statement is one rebuild: CREATE …_new, INSERT … SELECT, DROP, RENAME,
# then any indexes (the rename preserves indexes that referenced the old
# table by name? No — indexes attach to the table, and ALTER … RENAME does
# rename them in SQLite ≥ 3.25. But we explicitly recreate to be safe and
# to add new ones like idx_events_date_end.)
_V5_TO_V6_REBUILD = """
-- season_leagues: NOT NULL on season_id, league_id.
CREATE TABLE season_leagues_new (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    league_id INTEGER NOT NULL REFERENCES leagues(id),
    last_fetched_at TEXT
);
INSERT INTO season_leagues_new (id, ifsc_id, season_id, league_id, last_fetched_at)
    SELECT id, ifsc_id, season_id, league_id, last_fetched_at FROM season_leagues;
DROP TABLE season_leagues;
ALTER TABLE season_leagues_new RENAME TO season_leagues;
CREATE INDEX IF NOT EXISTS idx_season_leagues_last_fetched ON season_leagues(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_season_leagues_season ON season_leagues(season_id);

-- events: NOT NULL on season_id; CHECK on date_start, date_end, is_paraclimbing;
-- plus the new idx_events_date_end (D5).
CREATE TABLE events_new (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    league_id INTEGER REFERENCES leagues(id),
    name TEXT,
    city TEXT,
    country TEXT,
    country_iso3 TEXT,
    date_start TEXT CHECK (date_start IS NULL OR date_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    date_end TEXT CHECK (date_end IS NULL OR date_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    is_paraclimbing INTEGER CHECK (is_paraclimbing IS NULL OR is_paraclimbing IN (0, 1)),
    last_fetched_at TEXT
);
INSERT INTO events_new (id, ifsc_id, season_id, league_id, name, city, country,
    country_iso3, date_start, date_end, is_paraclimbing, last_fetched_at)
    SELECT id, ifsc_id, season_id, league_id, name, city, country, country_iso3,
        date_start, date_end, is_paraclimbing, last_fetched_at FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX IF NOT EXISTS idx_events_last_fetched ON events(last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_events_season ON events(season_id);
CREATE INDEX IF NOT EXISTS idx_events_date_end ON events(date_end);

-- categories: CHECK on gender.
CREATE TABLE categories_new (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    gender INTEGER CHECK (gender IS NULL OR gender IN (0, 1))
);
INSERT INTO categories_new (id, name, gender)
    SELECT id, name, gender FROM categories;
DROP TABLE categories;
ALTER TABLE categories_new RENAME TO categories;

-- athletes: CHECK on gender, birthday GLOB.
CREATE TABLE athletes_new (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    firstname TEXT,
    lastname TEXT,
    gender INTEGER CHECK (gender IS NULL OR gender IN (0, 1)),
    height INTEGER,
    arm_span INTEGER,
    birthday TEXT CHECK (birthday IS NULL OR birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
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
INSERT INTO athletes_new (id, ifsc_id, firstname, lastname, gender, height, arm_span,
    birthday, city, country, country_iso3, photo_url, federation_id, federation_name,
    federation_abbreviation, federation_url, paraclimbing_sport_class, sport_class_status,
    sport_class_review_date, speed_pb_time, speed_pb_date, speed_pb_event_name,
    speed_pb_round_name, last_fetched_at)
    SELECT id, ifsc_id, firstname, lastname, gender, height, arm_span, birthday, city,
        country, country_iso3, photo_url, federation_id, federation_name,
        federation_abbreviation, federation_url, paraclimbing_sport_class,
        sport_class_status, sport_class_review_date, speed_pb_time, speed_pb_date,
        speed_pb_event_name, speed_pb_round_name, last_fetched_at FROM athletes;
DROP TABLE athletes;
ALTER TABLE athletes_new RENAME TO athletes;
CREATE INDEX IF NOT EXISTS idx_athletes_last_fetched ON athletes(last_fetched_at);

-- cup_rankings: drop inline UNIQUE (athlete_id, cup_ifsc_id, d_cat_id), replace
-- with expression UNIQUE on COALESCE(d_cat_id, -1). CHECK on d_cat_id > 0
-- guards the COALESCE-to-(-1) sentinel from colliding with a real -1 value.
CREATE TABLE cup_rankings_new (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    cup_ifsc_id INTEGER NOT NULL,
    cup_name TEXT,
    season TEXT,
    discipline TEXT,
    d_cat_id INTEGER CHECK (d_cat_id IS NULL OR d_cat_id > 0),
    rank INTEGER
);
INSERT INTO cup_rankings_new (id, athlete_id, cup_ifsc_id, cup_name, season, discipline,
    d_cat_id, rank)
    SELECT id, athlete_id, cup_ifsc_id, cup_name, season, discipline, d_cat_id, rank
    FROM cup_rankings;
DROP TABLE cup_rankings;
ALTER TABLE cup_rankings_new RENAME TO cup_rankings;
CREATE INDEX IF NOT EXISTS idx_cup_rankings_athlete ON cup_rankings(athlete_id);
CREATE INDEX IF NOT EXISTS idx_cup_rankings_cup ON cup_rankings(cup_ifsc_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cup_rankings_uniq
    ON cup_rankings(athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1));

-- category_rounds: CHECK on status_as_of GLOB.
CREATE TABLE category_rounds_new (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE NOT NULL,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    kind TEXT,
    name TEXT,
    category TEXT,
    format TEXT,
    format_identifier TEXT,
    status TEXT,
    status_as_of TEXT CHECK (status_as_of IS NULL OR status_as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    league_round_id INTEGER
);
INSERT INTO category_rounds_new (id, ifsc_id, competition_id, kind, name, category,
    format, format_identifier, status, status_as_of, league_round_id)
    SELECT id, ifsc_id, competition_id, kind, name, category, format, format_identifier,
        status, status_as_of, league_round_id FROM category_rounds;
DROP TABLE category_rounds;
ALTER TABLE category_rounds_new RENAME TO category_rounds;
CREATE INDEX IF NOT EXISTS idx_category_rounds_competition ON category_rounds(competition_id);

-- stage_results: CHECK on winner.
CREATE TABLE stage_results_new (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    round_stage_id INTEGER NOT NULL REFERENCES round_stages(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    score TEXT,
    time_ms INTEGER,
    winner INTEGER CHECK (winner IS NULL OR winner IN (0, 1)),
    UNIQUE (round_stage_id, athlete_id)
);
INSERT INTO stage_results_new (id, competition_id, round_stage_id, athlete_id, rank,
    score, time_ms, winner)
    SELECT id, competition_id, round_stage_id, athlete_id, rank, score, time_ms, winner
    FROM stage_results;
DROP TABLE stage_results;
ALTER TABLE stage_results_new RENAME TO stage_results;
CREATE INDEX IF NOT EXISTS idx_stage_results_stage ON stage_results(round_stage_id);
CREATE INDEX IF NOT EXISTS idx_stage_results_athlete ON stage_results(athlete_id);
CREATE INDEX IF NOT EXISTS idx_stage_results_competition ON stage_results(competition_id);

-- ascents: CHECK on top, plus, restarted, dnf, dns.
CREATE TABLE ascents_new (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    round_stage_id INTEGER NOT NULL REFERENCES round_stages(id),
    route_id INTEGER NOT NULL REFERENCES routes(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    rank INTEGER,
    score TEXT,
    status TEXT,
    modified TEXT,
    top INTEGER CHECK (top IS NULL OR top IN (0, 1)),
    plus INTEGER CHECK (plus IS NULL OR plus IN (0, 1)),
    corrective_rank REAL,
    top_tries INTEGER,
    restarted INTEGER CHECK (restarted IS NULL OR restarted IN (0, 1)),
    time_ms INTEGER,
    dnf INTEGER CHECK (dnf IS NULL OR dnf IN (0, 1)),
    dns INTEGER CHECK (dns IS NULL OR dns IN (0, 1)),
    zone INTEGER,
    zone_tries INTEGER,
    low_zone INTEGER,
    low_zone_tries INTEGER,
    points REAL,
    UNIQUE (round_stage_id, athlete_id, route_id)
);
INSERT INTO ascents_new (id, competition_id, round_stage_id, route_id, athlete_id,
    rank, score, status, modified, top, plus, corrective_rank, top_tries, restarted,
    time_ms, dnf, dns, zone, zone_tries, low_zone, low_zone_tries, points)
    SELECT id, competition_id, round_stage_id, route_id, athlete_id, rank, score, status,
        modified, top, plus, corrective_rank, top_tries, restarted, time_ms, dnf, dns,
        zone, zone_tries, low_zone, low_zone_tries, points FROM ascents;
DROP TABLE ascents;
ALTER TABLE ascents_new RENAME TO ascents;
CREATE INDEX IF NOT EXISTS idx_ascents_stage ON ascents(round_stage_id);
CREATE INDEX IF NOT EXISTS idx_ascents_route ON ascents(route_id);
CREATE INDEX IF NOT EXISTS idx_ascents_athlete ON ascents(athlete_id);
CREATE INDEX IF NOT EXISTS idx_ascents_competition ON ascents(competition_id);
"""


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


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs the warehouse relies on for correctness.

    Centralized so tests and the production `open_db` path can't drift.
    `foreign_keys` is a connection-level setting (off by default in
    SQLite); the warehouse depends on it to keep referential integrity, so
    every connection must opt in.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")


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

    Asserts SQLite ≥ 3.35 because `ALTER TABLE … DROP COLUMN` (used by the
    v4→v5 leg of `_migrate_pre_v1_to_v5`) landed there. Failing loudly at
    open time beats a confusing migration error mid-refresh.
    """
    if sqlite3.sqlite_version_info < _MIN_SQLITE:
        raise RuntimeError(
            f"SQLite >= {_MIN_SQLITE[0]}.{_MIN_SQLITE[1]} required for migrations "
            f"(ALTER TABLE DROP COLUMN), found {sqlite3.sqlite_version}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _configure_connection(conn)
    mode_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    applied_mode = (mode_row[0] if mode_row else "").lower()
    if applied_mode != "wal":
        log.warning(
            "PRAGMA journal_mode=WAL refused by SQLite for %s (got %r). "
            "Reader-vs-writer concurrency falls back to SHARED-lock semantics; "
            "common cause: network filesystem (CIFS/SMB, some FUSE/WSL paths).",
            path, applied_mode,
        )
    conn.execute("PRAGMA synchronous = NORMAL")
    apply_schema(conn)
    return conn
