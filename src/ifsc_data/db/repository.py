"""Typed CRUD wrappers around the SQLite warehouse.

Every method commits before returning so a Ctrl-C between calls only loses the
in-flight row, not the batch. Group several calls into one atomic SQLite
transaction via `with repo.transaction():` when a multi-step operation must
land or roll back as a unit.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# Tables that carry a `last_fetched_at` column.
HYDRATABLE_TABLES = ("seasons", "season_leagues", "events", "competitions", "athletes")

# All tables — used as the whitelist for generic queries that take a table name.
ALL_TABLES = (
    *HYDRATABLE_TABLES,
    "leagues", "disciplines", "categories", "results",
)

# ISO-8601 with explicit Z so downstream consumers never have to guess UTC.
# Lexicographically sortable, so TEXT comparison in find_stale still works.
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    """Current UTC timestamp formatted to `TS_FMT`. Public for use by migration."""
    return datetime.now(timezone.utc).strftime(TS_FMT)


def _validate_table(table: str, allowed: tuple[str, ...]) -> None:
    if table not in allowed:
        raise ValueError(f"table {table!r} not in allowed set {allowed}")


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self._in_transaction = False

    # ------------------------------------------------------------- Transaction

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Group multiple repo calls into one atomic SQLite transaction.

        Inside the context, per-call commits are suppressed; the final commit
        happens when the block exits cleanly, or a rollback on exception.
        Nested transactions are flattened — the outermost commits.
        """
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def _maybe_commit(self) -> None:
        if not self._in_transaction:
            self.conn.commit()

    # ---------------------------------------------------------------- Generic

    def count(self, table: str) -> int:
        _validate_table(table, ALL_TABLES)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def count_hydrated(self, table: str) -> int:
        _validate_table(table, HYDRATABLE_TABLES)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE last_fetched_at IS NOT NULL"
        ).fetchone()[0]

    def mark_fetched(self, table: str, row_id: int) -> None:
        _validate_table(table, HYDRATABLE_TABLES)
        self.conn.execute(
            f"UPDATE {table} SET last_fetched_at = ? WHERE id = ?",
            (utcnow(), row_id),
        )
        self._maybe_commit()

    def find_stale(self, table: str, *, stale_days: int) -> list[sqlite3.Row]:
        """Rows whose last_fetched_at is NULL or older than `stale_days`."""
        _validate_table(table, HYDRATABLE_TABLES)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime(TS_FMT)
        return list(self.conn.execute(
            f"SELECT id, ifsc_id FROM {table} "
            "WHERE last_fetched_at IS NULL OR last_fetched_at < ? "
            "ORDER BY id ASC",
            (cutoff,),
        ))

    def stale_cutoff(self, stale_days: int) -> str:
        """Public helper so callers running custom SQL can use the same cutoff format."""
        return (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime(TS_FMT)

    # ----------------------------------------------------------------- Seasons

    def upsert_season(self, ifsc_id: int, *, year: Optional[int] = None) -> int:
        row = self.conn.execute(
            "INSERT INTO seasons (ifsc_id, year) VALUES (?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  year = COALESCE(excluded.year, seasons.year) "
            "RETURNING id",
            (ifsc_id, year),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ----------------------------------------------------------------- Leagues

    def upsert_league(self, name: str) -> int:
        row = self.conn.execute(
            "INSERT INTO leagues (name) VALUES (?) "
            "ON CONFLICT(name) DO UPDATE SET name = excluded.name "
            "RETURNING id",
            (name,),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ----------------------------------------------------------- Season_leagues

    def upsert_season_league(
        self,
        ifsc_id: int,
        *,
        season_id: Optional[int] = None,
        league_id: Optional[int] = None,
    ) -> int:
        row = self.conn.execute(
            "INSERT INTO season_leagues (ifsc_id, season_id, league_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  season_id = COALESCE(excluded.season_id, season_leagues.season_id), "
            "  league_id = COALESCE(excluded.league_id, season_leagues.league_id) "
            "RETURNING id",
            (ifsc_id, season_id, league_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ------------------------------------------------------------- Disciplines

    def upsert_discipline(self, name: str) -> int:
        row = self.conn.execute(
            "INSERT INTO disciplines (name) VALUES (?) "
            "ON CONFLICT(name) DO UPDATE SET name = excluded.name "
            "RETURNING id",
            (name,),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # -------------------------------------------------------------- Categories

    def upsert_category(self, name: str, gender: Optional[int]) -> int:
        row = self.conn.execute(
            "INSERT INTO categories (name, gender) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  gender = COALESCE(excluded.gender, categories.gender) "
            "RETURNING id",
            (name, gender),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ------------------------------------------------------------------ Events

    def upsert_event_skeleton(
        self,
        ifsc_id: int,
        *,
        season_id: Optional[int] = None,
        league_id: Optional[int] = None,
    ) -> int:
        row = self.conn.execute(
            "INSERT INTO events (ifsc_id, season_id, league_id) VALUES (?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  season_id = COALESCE(excluded.season_id, events.season_id), "
            "  league_id = COALESCE(excluded.league_id, events.league_id) "
            "RETURNING id",
            (ifsc_id, season_id, league_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    def update_event(self, row_id: int, **fields: Any) -> None:
        allowed = {"name", "city", "country", "date_start", "date_end", "is_paraclimbing"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sets = ", ".join(f"{c} = ?" for c in cols)
        values = [fields[c] for c in cols] + [row_id]
        self.conn.execute(f"UPDATE events SET {sets} WHERE id = ?", values)
        self._maybe_commit()

    def backfill_event_country_for_row(self, row_id: int, country: str) -> None:
        self.conn.execute(
            "UPDATE events SET country = ? WHERE id = ? AND country IS NULL",
            (country, row_id),
        )
        self._maybe_commit()

    def backfill_event_country_from_siblings(self) -> int:
        """Fill NULL country on any event whose city matches a sibling row with a country.

        One SQL pass; uses MAX() to deterministically pick when multiple sibling
        countries exist (rare; alphabetical winner). Returns number of rows affected.
        """
        cur = self.conn.execute(
            "UPDATE events SET country = ("
            "  SELECT MAX(e2.country) FROM events e2 "
            "  WHERE e2.city = events.city AND e2.country IS NOT NULL"
            ") "
            "WHERE country IS NULL AND city IS NOT NULL"
        )
        affected = cur.rowcount
        self._maybe_commit()
        return affected

    # ------------------------------------------------------------ Competitions

    def upsert_competition(
        self,
        *,
        event_id: int,
        ifsc_id: int,
        discipline_id: int,
        category_id: int,
    ) -> int:
        row = self.conn.execute(
            "INSERT INTO competitions (event_id, ifsc_id, discipline_id, category_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(event_id, ifsc_id) DO UPDATE SET "
            "  discipline_id = excluded.discipline_id, "
            "  category_id = excluded.category_id "
            "RETURNING id",
            (event_id, ifsc_id, discipline_id, category_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ---------------------------------------------------------------- Athletes

    def upsert_athlete_skeleton(self, ifsc_id: int) -> int:
        row = self.conn.execute(
            "INSERT INTO athletes (ifsc_id) VALUES (?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET ifsc_id = excluded.ifsc_id "
            "RETURNING id",
            (ifsc_id,),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    def update_athlete(self, row_id: int, **fields: Any) -> None:
        allowed = {
            "firstname", "lastname", "gender", "height", "arm_span",
            "birthday", "city", "country", "photo_url", "is_paraclimbing",
        }
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sets = ", ".join(f"{c} = ?" for c in cols)
        values = [fields[c] for c in cols] + [row_id]
        self.conn.execute(f"UPDATE athletes SET {sets} WHERE id = ?", values)
        self._maybe_commit()

    # ----------------------------------------------------------------- Results

    def upsert_result(self, *, competition_id: int, athlete_id: int, rank: Optional[int]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO results (competition_id, athlete_id, rank) "
            "VALUES (?, ?, ?)",
            (competition_id, athlete_id, rank),
        )
        self._maybe_commit()

    def delete_results_for_competition(self, competition_id: int) -> None:
        """Wipe results for a competition before re-hydration (idempotent re-runs)."""
        self.conn.execute(
            "DELETE FROM results WHERE competition_id = ?", (competition_id,)
        )
        self._maybe_commit()
