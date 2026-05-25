"""Schema-migration round-trip tests.

The migration path lives in `apply_schema`: it reads the schema_version
table, runs the smallest set of forward `_migrate_vN_to_vN+1` steps to
reach `CURRENT_VERSION`, and inserts a version row after each step.

These tests simulate each "older state" by mutating a freshly-applied
current-version DB back to its pre-migration shape AND resetting the
recorded schema_version to a pre-migration value, then re-running
`apply_schema` and asserting the post-state matches what a fresh apply
produces. By treating "fresh apply_schema" as the single source of truth
(rather than hand-coded EXPECTED_* sets that would drift with the DDL),
we test the real migration invariant: every prior state converges to the
same shape a clean install produces.
"""
from __future__ import annotations

import sqlite3

import pytest

from wcl_data.db.schema import CURRENT_VERSION, apply_schema


# Tables whose column shape is exercised by the migration chain. Any table
# whose final shape differs from its CREATE TABLE definition (added or
# dropped columns, or rebuilt for new constraints) belongs here. Tables
# created once and never altered (e.g. `leagues`, `disciplines`) don't
# need to be in this set — the CREATE TABLE IF NOT EXISTS path handles
# them.
MIGRATED_TABLES = (
    "season_leagues", "events", "categories", "athletes", "cup_rankings",
    "category_rounds", "stage_results", "ascents",
    # `routes` had `last_fetched_at` in v4; dropped in v5 (ADR 0007).
    # Including it here pins the regression: a future re-introduction of
    # the dropped column gets caught by the parametrized round-trip tests.
    "routes",
)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _fresh_current_db() -> sqlite3.Connection:
    """Return an in-memory connection with the CURRENT_VERSION schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn


@pytest.fixture(scope="module")
def fresh_current_cols() -> dict[str, set[str]]:
    """The authoritative column set per migrated table — derived from a fresh
    `apply_schema(:memory:)`, not hand-coded. The migration round-trip tests
    assert that re-applying from each prior state converges to this same shape.
    """
    conn = _fresh_current_db()
    try:
        return {table: _cols(conn, table) for table in MIGRATED_TABLES}
    finally:
        conn.close()


# --- End-to-end: a fresh DB reaches CURRENT_VERSION -----------------------

def test_fresh_apply_schema_lands_on_current_version():
    conn = _fresh_current_db()
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == CURRENT_VERSION
    finally:
        conn.close()


def test_fresh_apply_schema_produces_expected_columns(fresh_current_cols):
    """Documents what the current schema looks like — guards against accidental
    schema deletion. The exact values are intentionally not hand-coded; this
    asserts on the *shape* (key columns present / absent) so a future column
    addition that's wired into apply_schema doesn't require updating two places.
    """
    assert "country_iso3" in fresh_current_cols["events"]
    assert "country_iso3" in fresh_current_cols["athletes"]
    assert "federation_id" in fresh_current_cols["athletes"]
    assert "speed_pb_time" in fresh_current_cols["athletes"]
    # v4 → v5 dropped these:
    assert "last_fetched_at" not in fresh_current_cols["category_rounds"]
    assert "last_fetched_at" not in fresh_current_cols["routes"]
    # v3 → v4 dropped this:
    assert "is_paraclimbing" not in fresh_current_cols["athletes"]


# --- Re-arrival from each prior state ------------------------------------

# Each scenario: (pre_state_mutations, starting_version). The starting_version
# is recorded in schema_version BEFORE re-running apply_schema, so the
# version-gated migration framework knows to fire the relevant migrations.
@pytest.mark.parametrize("pre_state_mutations,starting_version", [
    # Scenario 1: an older DB lacked `events.country_iso3` (added when ADR 0008
    # landed). The v5 migration leg adds it back; the v6 leg rebuilds events
    # with the new constraints.
    (["ALTER TABLE events DROP COLUMN country_iso3"], 4),

    # Scenario 2: an older DB lacked `athletes.country_iso3` (same ADR).
    (["ALTER TABLE athletes DROP COLUMN country_iso3"], 4),

    # Scenario 3: pre-federation-columns era — drop all four federation_* cols.
    ([
        "ALTER TABLE athletes DROP COLUMN federation_id",
        "ALTER TABLE athletes DROP COLUMN federation_name",
        "ALTER TABLE athletes DROP COLUMN federation_abbreviation",
        "ALTER TABLE athletes DROP COLUMN federation_url",
    ], 4),

    # Scenario 4: pre-paraclimbing-classification era — drop the three sport_class cols.
    ([
        "ALTER TABLE athletes DROP COLUMN paraclimbing_sport_class",
        "ALTER TABLE athletes DROP COLUMN sport_class_status",
        "ALTER TABLE athletes DROP COLUMN sport_class_review_date",
    ], 4),

    # Scenario 5: pre-speed-PB era — drop the four speed_pb_* columns.
    ([
        "ALTER TABLE athletes DROP COLUMN speed_pb_time",
        "ALTER TABLE athletes DROP COLUMN speed_pb_date",
        "ALTER TABLE athletes DROP COLUMN speed_pb_event_name",
        "ALTER TABLE athletes DROP COLUMN speed_pb_round_name",
    ], 4),

    # Scenario 6: v3 era — `is_paraclimbing` still on athletes (later dropped in v4).
    (["ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER"], 3),

    # Scenario 7: v4 era — `last_fetched_at` + its index still on category_rounds & routes.
    ([
        "ALTER TABLE category_rounds ADD COLUMN last_fetched_at TEXT",
        "CREATE INDEX idx_category_rounds_last_fetched ON category_rounds(last_fetched_at)",
        "ALTER TABLE routes ADD COLUMN last_fetched_at TEXT",
        "CREATE INDEX idx_routes_last_fetched ON routes(last_fetched_at)",
    ], 4),

    # Scenario 8: combined — pre-everything (every prior state mashed together).
    ([
        "ALTER TABLE events DROP COLUMN country_iso3",
        "ALTER TABLE athletes DROP COLUMN country_iso3",
        "ALTER TABLE athletes DROP COLUMN federation_id",
        "ALTER TABLE athletes DROP COLUMN speed_pb_time",
        "ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER",
        "ALTER TABLE category_rounds ADD COLUMN last_fetched_at TEXT",
        "ALTER TABLE routes ADD COLUMN last_fetched_at TEXT",
    ], 0),
])
def test_apply_schema_recovers_current_from_prior_state(
    pre_state_mutations, starting_version, fresh_current_cols,
):
    """For each simulated prior version, re-applying converges to CURRENT_VERSION's shape.

    Resets `schema_version` to `starting_version` so the version-gated
    framework actually re-runs the migrations.
    """
    conn = _fresh_current_db()
    try:
        for sql in pre_state_mutations:
            conn.execute(sql)
        conn.execute("DELETE FROM schema_version WHERE version > ?", (starting_version,))
        conn.commit()

        # Re-run the schema apply — should drive the DB back to CURRENT_VERSION.
        apply_schema(conn)

        for table in MIGRATED_TABLES:
            assert _cols(conn, table) == fresh_current_cols[table], (
                f"Migration from prior state diverged on {table}; "
                f"missing: {fresh_current_cols[table] - _cols(conn, table)}, "
                f"extra: {_cols(conn, table) - fresh_current_cols[table]}"
            )

        # Indexes dropped in v5 must not have come back.
        assert "idx_category_rounds_last_fetched" not in _indexes(conn, "category_rounds")
        assert "idx_routes_last_fetched" not in _indexes(conn, "routes")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == CURRENT_VERSION
    finally:
        conn.close()


def test_apply_schema_is_idempotent_on_current(fresh_current_cols):
    """Running apply_schema twice on a current DB must not corrupt anything."""
    conn = _fresh_current_db()
    try:
        before_cols = {t: _cols(conn, t) for t in MIGRATED_TABLES}

        apply_schema(conn)
        apply_schema(conn)

        after_cols = {t: _cols(conn, t) for t in MIGRATED_TABLES}
        assert before_cols == after_cols

        # And schema_version stays at exactly one CURRENT_VERSION row (no
        # spurious duplicates from the idempotent reruns).
        rows = list(conn.execute(
            "SELECT version, COUNT(*) FROM schema_version WHERE version = ? GROUP BY version",
            (CURRENT_VERSION,),
        ))
        assert [tuple(r) for r in rows] == [(CURRENT_VERSION, 1)]
    finally:
        conn.close()


def test_apply_schema_preserves_user_data_across_migration():
    """A row written under a "v4" simulated state must survive the migration to current."""
    conn = _fresh_current_db()
    try:
        conn.execute(
            "INSERT INTO athletes (ifsc_id, firstname, lastname) VALUES (?, ?, ?)",
            (1, "Adam", "Ondra"),
        )
        # Simulate the v4 mismatch and reset version so the migration re-fires.
        conn.execute("ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER")
        conn.execute("DELETE FROM schema_version WHERE version > 3")
        conn.commit()
        apply_schema(conn)

        row = conn.execute("SELECT firstname, lastname FROM athletes WHERE ifsc_id = 1").fetchone()
        assert row[0] == "Adam"
        assert row[1] == "Ondra"
    finally:
        conn.close()


# --- v5 → v6 migration: constraint additions ------------------------------

def test_v5_to_v6_migration_adds_check_constraints():
    """After migrating to v6, attempting to insert a bad boolean fails CHECK."""
    conn = _fresh_current_db()
    try:
        # Bypass the upsert helper to test the raw DDL constraint.
        conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (1, 0)")
        conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (2, 1)")
        conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (3, NULL)")
        # gender = 2 violates CHECK (gender IN (0, 1) OR IS NULL).
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (4, 2)")
    finally:
        conn.close()


def test_v5_to_v6_migration_adds_date_glob_constraint():
    """events.date_start must match `YYYY-MM-DD…` GLOB or be NULL."""
    conn = _fresh_current_db()
    try:
        season_id = conn.execute(
            "INSERT INTO seasons (ifsc_id, year) VALUES (1, 2024) RETURNING id"
        ).fetchone()[0]
        # Valid ISO date prefixes pass.
        conn.execute(
            "INSERT INTO events (ifsc_id, season_id, date_start) VALUES (?, ?, ?)",
            (10, season_id, "2024-06-15"),
        )
        conn.execute(
            "INSERT INTO events (ifsc_id, season_id, date_start) VALUES (?, ?, ?)",
            (11, season_id, None),
        )
        # Reject a malformed date.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO events (ifsc_id, season_id, date_start) VALUES (?, ?, ?)",
                (12, season_id, "06/15/2024"),
            )
    finally:
        conn.close()


def test_v5_to_v6_migration_enforces_not_null_on_events_season():
    """events.season_id is NOT NULL in v6."""
    conn = _fresh_current_db()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            conn.execute("INSERT INTO events (ifsc_id) VALUES (99)")
    finally:
        conn.close()


def test_apply_schema_migrates_v5_db_with_null_d_cat_duplicates():
    """End-to-end variant of the dedupe test: a DB recorded as v5 with two
    NULL-d_cat duplicates is brought to v6 via the full apply_schema path.

    Pins the contract that v6-only artifacts (`idx_cup_rankings_uniq`) are NOT
    re-installed by the v0→v5 leg — only by v5→v6 (which dedupes first).
    Otherwise a real-world v5 DB carrying duplicates would refuse to migrate.
    """
    conn = _fresh_current_db()
    try:
        ath_id = conn.execute(
            "INSERT INTO athletes (ifsc_id) VALUES (1) RETURNING id"
        ).fetchone()[0]
        # Stage the v5 pre-state: drop the v6 expression index, insert dupes,
        # rewind schema_version to 5 so apply_schema sees a v5 DB.
        conn.execute("DROP INDEX idx_cup_rankings_uniq")
        conn.execute("INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id, rank) VALUES (?, 100, NULL, 5)", (ath_id,))
        conn.execute("INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id, rank) VALUES (?, 100, NULL, 3)", (ath_id,))
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")
        conn.commit()

        apply_schema(conn)

        rows = list(conn.execute(
            "SELECT rank FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = 100",
            (ath_id,),
        ))
        assert len(rows) == 1
        assert rows[0]["rank"] == 3
        # And the v6 index is back.
        idx = {r[1] for r in conn.execute("PRAGMA index_list(cup_rankings)")}
        assert "idx_cup_rankings_uniq" in idx
    finally:
        conn.close()


def test_v5_to_v6_migration_collapses_null_d_cat_duplicates():
    """v5 allowed two NULL-d_cat rows on the same (athlete, cup); v6's expression
    UNIQUE index COALESCEs them. The v5→v6 migration must dedupe before
    installing the index. Tests the dedupe SQL directly so the assertion isn't
    tangled with the full apply_schema orchestration."""
    from wcl_data.db.schema import _migrate_v5_to_v6

    conn = _fresh_current_db()
    try:
        ath_id = conn.execute(
            "INSERT INTO athletes (ifsc_id) VALUES (1) RETURNING id"
        ).fetchone()[0]
        # Drop the v6 expression unique index so we can stage two NULL-d_cat
        # duplicates the way v5 would have allowed.
        conn.execute("DROP INDEX idx_cup_rankings_uniq")
        conn.execute("INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id, rank) VALUES (?, 100, NULL, 5)", (ath_id,))
        conn.execute("INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id, rank) VALUES (?, 100, NULL, 3)", (ath_id,))
        conn.commit()

        # Run the migration directly. It dedupes first (keeping the higher id,
        # which holds rank=3), then rebuilds tables and reinstalls the index.
        _migrate_v5_to_v6(conn)

        rows = list(conn.execute(
            "SELECT rank FROM cup_rankings WHERE athlete_id = ? AND cup_ifsc_id = 100",
            (ath_id,),
        ))
        assert len(rows) == 1
        assert rows[0]["rank"] == 3
    finally:
        conn.close()


# --- Safety-net rerun: apply_schema self-heals dropped columns ------------

def test_apply_schema_safety_net_readds_missing_v5_column():
    """A v6 DB whose athletes.country_iso3 was dropped (manual sqlite shell edit,
    or an interrupted prior migration) self-heals on the next apply_schema run.

    Pins the safety-net rerun: even when schema_version reports CURRENT, the
    idempotent column-add chain runs at the tail of apply_schema so a missing
    column gets restored — otherwise the next v5→v6-style rebuild would crash
    on SELECT-of-missing-column.
    """
    conn = _fresh_current_db()
    try:
        # Simulate drift: drop a column that should be present at v6.
        conn.execute("ALTER TABLE athletes DROP COLUMN country_iso3")
        conn.commit()
        assert "country_iso3" not in _cols(conn, "athletes")

        apply_schema(conn)

        assert "country_iso3" in _cols(conn, "athletes")
    finally:
        conn.close()


# --- v5→v6 pre-validation: fail loud on NULL-FK rows ----------------------

def test_v5_to_v6_fails_loud_on_null_events_season_id():
    """A v5 events row with NULL season_id makes the rebuild crash mid-INSERT;
    pre-validation catches it first and reports the exact column + count so
    the operator can locate the bad row before retrying."""
    from wcl_data.db.schema import _migrate_v5_to_v6

    conn = _fresh_current_db()
    try:
        # Drop the v6 NOT NULL by rebuilding events as a v5-shape table.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript("""
            CREATE TABLE events_v5 (
                id INTEGER PRIMARY KEY,
                ifsc_id INTEGER UNIQUE NOT NULL,
                season_id INTEGER REFERENCES seasons(id),
                league_id INTEGER REFERENCES leagues(id),
                name TEXT, city TEXT, country TEXT, country_iso3 TEXT,
                date_start TEXT, date_end TEXT, is_paraclimbing INTEGER,
                last_fetched_at TEXT
            );
            DROP TABLE events;
            ALTER TABLE events_v5 RENAME TO events;
        """)
        conn.execute("INSERT INTO events (ifsc_id, season_id) VALUES (1, NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (5) ON CONFLICT DO NOTHING")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(RuntimeError, match="events.season_id"):
            _migrate_v5_to_v6(conn)
    finally:
        conn.close()


# --- v5→v6 cleanup: NULL out values violating new CHECK constraints -------

def test_v5_to_v6_cleans_bad_gender_before_rebuild():
    """A v5 athletes row with gender=9 would crash the v6 rebuild's
    CHECK (gender IN (0,1) OR IS NULL). The cleanup pre-step NULLs it
    out so the rebuild proceeds and the operator's DB stays openable."""
    from wcl_data.db.schema import _migrate_v5_to_v6

    conn = _fresh_current_db()
    try:
        # Rebuild athletes as v5-shape (no CHECK) so we can stage gender=9.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript("""
            CREATE TABLE athletes_v5 (
                id INTEGER PRIMARY KEY,
                ifsc_id INTEGER UNIQUE NOT NULL,
                firstname TEXT, lastname TEXT, gender INTEGER,
                height INTEGER, arm_span INTEGER, birthday TEXT,
                city TEXT, country TEXT, country_iso3 TEXT, photo_url TEXT,
                federation_id INTEGER, federation_name TEXT,
                federation_abbreviation TEXT, federation_url TEXT,
                paraclimbing_sport_class TEXT, sport_class_status TEXT,
                sport_class_review_date TEXT,
                speed_pb_time TEXT, speed_pb_date TEXT,
                speed_pb_event_name TEXT, speed_pb_round_name TEXT,
                last_fetched_at TEXT
            );
            DROP TABLE athletes;
            ALTER TABLE athletes_v5 RENAME TO athletes;
        """)
        conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (1, 9)")
        conn.execute("INSERT INTO athletes (ifsc_id, gender) VALUES (2, 0)")
        conn.execute("INSERT INTO schema_version (version) VALUES (5) ON CONFLICT DO NOTHING")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        _migrate_v5_to_v6(conn)

        rows = {r["ifsc_id"]: r["gender"] for r in conn.execute(
            "SELECT ifsc_id, gender FROM athletes ORDER BY ifsc_id"
        )}
        assert rows == {1: None, 2: 0}
    finally:
        conn.close()


# --- d_cat_id sentinel guard ----------------------------------------------

def test_cup_rankings_d_cat_id_rejects_non_positive():
    """v6 CHECK (d_cat_id IS NULL OR d_cat_id > 0) prevents a literal -1 from
    colliding with the COALESCE(d_cat_id, -1) NULL bucket of the expression
    UNIQUE index. NULL and any positive value still pass."""
    conn = _fresh_current_db()
    try:
        ath = conn.execute(
            "INSERT INTO athletes (ifsc_id) VALUES (1) RETURNING id"
        ).fetchone()[0]
        # NULL and a positive value are fine.
        conn.execute(
            "INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id) VALUES (?, 100, NULL)",
            (ath,),
        )
        conn.execute(
            "INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id) VALUES (?, 101, 5)",
            (ath,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id) VALUES (?, 102, -1)",
                (ath,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO cup_rankings (athlete_id, cup_ifsc_id, d_cat_id) VALUES (?, 103, 0)",
                (ath,),
            )
    finally:
        conn.close()


# --- Stricter date GLOB: rejects garbage, accepts ISO-prefixed strings ----

def test_date_check_rejects_ascii_garbage_but_accepts_iso_dates():
    """The v6 GLOB is `[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*` (digit
    classes), not `????-??-??*` (any-char wildcards). Earlier the latter would
    accept 'abcd-ef-gh' silently — the digit-class pattern catches it."""
    conn = _fresh_current_db()
    try:
        season_id = conn.execute(
            "INSERT INTO seasons (ifsc_id, year) VALUES (1, 2024) RETURNING id"
        ).fetchone()[0]
        # Valid ISO-prefixed values pass.
        for ifsc, val in [(10, "2024-06-15"), (11, "2024-06-15 07:00:10 UTC"), (12, None)]:
            conn.execute(
                "INSERT INTO events (ifsc_id, season_id, date_start) VALUES (?, ?, ?)",
                (ifsc, season_id, val),
            )
        # ASCII garbage is now rejected (the previous pattern accepted it).
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO events (ifsc_id, season_id, date_start) VALUES (?, ?, ?)",
                (20, season_id, "abcd-ef-gh"),
            )
    finally:
        conn.close()
