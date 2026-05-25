"""Schema-migration round-trip tests.

The migration path lives in `apply_schema`: it CREATEs the current DDL
idempotently, then walks an ALTER-list to add columns that older versions
were missing and drop columns/indexes that newer versions removed.

These tests simulate each "older state" by mutating a freshly-applied v5
DB back to its pre-migration shape, then re-running `apply_schema` and
asserting the post-state matches what a fresh apply_schema produces. By
treating "fresh apply_schema" as the single source of truth (rather than
hand-coded EXPECTED_* sets that would drift with the DDL), we test the
real migration invariant: every prior state converges to the same shape
a clean install produces.
"""
from __future__ import annotations

import sqlite3

import pytest

from wcl_data.db.schema import CURRENT_VERSION, apply_schema


# Tables whose column shape is exercised by the migration ALTER list. Any
# table whose v5 shape differs from its CREATE TABLE definition (added or
# dropped columns) belongs here. Tables created once and never altered
# (e.g. `leagues`, `disciplines`) don't need to be in this set — the
# CREATE TABLE IF NOT EXISTS path handles them.
MIGRATED_TABLES = ("events", "athletes", "category_rounds", "routes")


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _fresh_v5_db() -> sqlite3.Connection:
    """Return an in-memory connection that has the current DDL applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn


@pytest.fixture(scope="module")
def fresh_v5_cols() -> dict[str, set[str]]:
    """The authoritative column set per migrated table — derived from a fresh
    `apply_schema(:memory:)`, not hand-coded. The migration round-trip tests
    assert that re-applying from each prior state converges to this same shape.
    """
    conn = _fresh_v5_db()
    try:
        return {table: _cols(conn, table) for table in MIGRATED_TABLES}
    finally:
        conn.close()


# --- End-to-end: a fresh DB reaches v5 ------------------------------------

def test_fresh_apply_schema_lands_on_current_version():
    conn = _fresh_v5_db()
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == CURRENT_VERSION
    finally:
        conn.close()


def test_fresh_apply_schema_produces_expected_v5_columns(fresh_v5_cols):
    """Documents what v5 looks like — guards against accidental schema deletion.

    The exact values are intentionally not hand-coded; this asserts on the
    *shape* (set sizes, key columns present) so a future column addition
    that's wired into apply_schema doesn't require updating two places.
    """
    assert "country_iso3" in fresh_v5_cols["events"]
    assert "country_iso3" in fresh_v5_cols["athletes"]
    assert "federation_id" in fresh_v5_cols["athletes"]
    assert "speed_pb_time" in fresh_v5_cols["athletes"]
    # v4 → v5 dropped these:
    assert "last_fetched_at" not in fresh_v5_cols["category_rounds"]
    assert "last_fetched_at" not in fresh_v5_cols["routes"]
    # v3 → v4 dropped this:
    assert "is_paraclimbing" not in fresh_v5_cols["athletes"]


# --- Re-arrival from each prior state ------------------------------------

@pytest.mark.parametrize("pre_state_mutations", [
    # Scenario 1: an older DB lacked `events.country_iso3` (added when ADR 0008 landed).
    ["ALTER TABLE events DROP COLUMN country_iso3"],

    # Scenario 2: an older DB lacked `athletes.country_iso3` (same ADR).
    ["ALTER TABLE athletes DROP COLUMN country_iso3"],

    # Scenario 3: pre-federation-columns era — drop all four federation_* cols.
    [
        "ALTER TABLE athletes DROP COLUMN federation_id",
        "ALTER TABLE athletes DROP COLUMN federation_name",
        "ALTER TABLE athletes DROP COLUMN federation_abbreviation",
        "ALTER TABLE athletes DROP COLUMN federation_url",
    ],

    # Scenario 4: pre-paraclimbing-classification era — drop the three sport_class cols.
    [
        "ALTER TABLE athletes DROP COLUMN paraclimbing_sport_class",
        "ALTER TABLE athletes DROP COLUMN sport_class_status",
        "ALTER TABLE athletes DROP COLUMN sport_class_review_date",
    ],

    # Scenario 5: pre-speed-PB era — drop the four speed_pb_* columns.
    [
        "ALTER TABLE athletes DROP COLUMN speed_pb_time",
        "ALTER TABLE athletes DROP COLUMN speed_pb_date",
        "ALTER TABLE athletes DROP COLUMN speed_pb_event_name",
        "ALTER TABLE athletes DROP COLUMN speed_pb_round_name",
    ],

    # Scenario 6: v3 era — `is_paraclimbing` still on athletes (later dropped in v4).
    ["ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER"],

    # Scenario 7: v4 era — `last_fetched_at` + its index still on category_rounds & routes.
    [
        "ALTER TABLE category_rounds ADD COLUMN last_fetched_at TEXT",
        "CREATE INDEX idx_category_rounds_last_fetched ON category_rounds(last_fetched_at)",
        "ALTER TABLE routes ADD COLUMN last_fetched_at TEXT",
        "CREATE INDEX idx_routes_last_fetched ON routes(last_fetched_at)",
    ],

    # Scenario 8: combined — pre-everything (every prior state mashed together).
    [
        "ALTER TABLE events DROP COLUMN country_iso3",
        "ALTER TABLE athletes DROP COLUMN country_iso3",
        "ALTER TABLE athletes DROP COLUMN federation_id",
        "ALTER TABLE athletes DROP COLUMN speed_pb_time",
        "ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER",
        "ALTER TABLE category_rounds ADD COLUMN last_fetched_at TEXT",
        "ALTER TABLE routes ADD COLUMN last_fetched_at TEXT",
    ],
])
def test_apply_schema_recovers_v5_from_prior_state(pre_state_mutations, fresh_v5_cols):
    """For each simulated prior version, re-applying converges to v5's shape.

    Asserts post-state matches `fresh_v5_cols` (derived once from a clean
    apply_schema) so this test stays green automatically when the schema
    grows new columns — provided the migration code is updated too.
    """
    conn = _fresh_v5_db()
    try:
        for sql in pre_state_mutations:
            conn.execute(sql)
        conn.commit()

        # Re-run the schema apply — should drive the DB back to v5.
        apply_schema(conn)

        for table in MIGRATED_TABLES:
            assert _cols(conn, table) == fresh_v5_cols[table], (
                f"Migration from prior state diverged on {table}; "
                f"missing: {fresh_v5_cols[table] - _cols(conn, table)}, "
                f"extra: {_cols(conn, table) - fresh_v5_cols[table]}"
            )

        # Indexes dropped in v5 must not have come back.
        assert "idx_category_rounds_last_fetched" not in _indexes(conn, "category_rounds")
        assert "idx_routes_last_fetched" not in _indexes(conn, "routes")

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == CURRENT_VERSION
    finally:
        conn.close()


def test_apply_schema_is_idempotent_on_v5():
    """Running apply_schema twice on a fresh v5 DB must not corrupt anything."""
    conn = _fresh_v5_db()
    try:
        before_cols = {t: _cols(conn, t) for t in MIGRATED_TABLES}

        apply_schema(conn)
        apply_schema(conn)

        after_cols = {t: _cols(conn, t) for t in MIGRATED_TABLES}
        assert before_cols == after_cols
    finally:
        conn.close()


def test_apply_schema_preserves_user_data_across_migration():
    """A row written under a "v4" simulated state must survive the migration to v5."""
    conn = _fresh_v5_db()
    try:
        conn.execute(
            "INSERT INTO athletes (ifsc_id, firstname, lastname) VALUES (?, ?, ?)",
            (1, "Adam", "Ondra"),
        )
        conn.commit()

        # Simulate the v4 mismatch and re-migrate.
        conn.execute("ALTER TABLE athletes ADD COLUMN is_paraclimbing INTEGER")
        conn.commit()
        apply_schema(conn)

        row = conn.execute("SELECT firstname, lastname FROM athletes WHERE ifsc_id = 1").fetchone()
        assert row[0] == "Adam"
        assert row[1] == "Ondra"
    finally:
        conn.close()
