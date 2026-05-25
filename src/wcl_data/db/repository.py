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
HYDRATABLE_TABLES = (
    "seasons", "season_leagues", "events", "competitions", "athletes",
)

# All tables — used as the whitelist for generic queries that take a table name
# AND as the iteration order for `wcl-data status`. Ordered hierarchically
# (parents → children → derived) rather than "hydratables first" so the status
# table reads top-to-bottom as the ingestion graph. HYDRATABLE_TABLES ⊂ ALL_TABLES.
ALL_TABLES = (
    "seasons", "leagues", "season_leagues", "disciplines",
    "categories", "events", "competitions", "athletes", "results",
    "category_rounds", "round_stages", "routes",
    "round_results", "stage_results", "ascents", "cup_rankings",
)

# Module-load invariant: every hydratable must appear in ALL_TABLES so that
# generic helpers like `count()` / `mark_fetched()` don't silently reject a
# newly-added entity. The hand-enumerated ALL_TABLES (chosen for hierarchical
# display order) trades the previous `(*HYDRATABLE_TABLES, ...)` auto-derive
# for this explicit assertion.
assert set(HYDRATABLE_TABLES) <= set(ALL_TABLES), (
    f"HYDRATABLE_TABLES {sorted(HYDRATABLE_TABLES)} not a subset of "
    f"ALL_TABLES {sorted(ALL_TABLES)}"
)

# Public allowed-column sets for the per-row update helpers below. Hoisted to
# module level so callers (athletes.hydrate / events.hydrate) can introspect
# them, and so test suites can pin "hydrate's kwargs ⊆ allowed columns" — the
# strict raise in update_event/update_athlete is unforgiving inside
# `with repo.transaction():` (a typo'd kwarg rolls back the whole iteration).
UPDATE_EVENT_ALLOWED: frozenset[str] = frozenset({
    "name", "city", "country", "country_iso3",
    "date_start", "date_end", "is_paraclimbing",
})
UPDATE_ATHLETE_ALLOWED: frozenset[str] = frozenset({
    "firstname", "lastname", "gender", "height", "arm_span",
    "birthday", "city", "country", "country_iso3", "photo_url",
    "federation_id", "federation_name", "federation_abbreviation", "federation_url",
    "paraclimbing_sport_class", "sport_class_status", "sport_class_review_date",
    "speed_pb_time", "speed_pb_date", "speed_pb_event_name", "speed_pb_round_name",
})

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

    def latest_fetched_at(self, table: str) -> Optional[str]:
        """MAX(last_fetched_at) for a hydratable table, or None if all-NULL/empty."""
        _validate_table(table, HYDRATABLE_TABLES)
        row = self.conn.execute(
            f"SELECT MAX(last_fetched_at) FROM {table}"
        ).fetchone()
        return row[0] if row else None

    def schema_version(self) -> int:
        """Highest version recorded in `schema_version`; 0 on an empty table."""
        row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

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

    # --------------------------------------------------------------- Ongoing-only
    # These power `pull-new`'s optimized scope. "Ongoing" means a row whose
    # parent timeframe hasn't ended yet — i.e. one that the World Climbing might still
    # add structural children to. See ADR 0006.

    def find_ongoing_seasons(self) -> list[sqlite3.Row]:
        """Seasons in the current calendar year or later, plus skeletons (NULL year)."""
        current_year = datetime.now(timezone.utc).year
        return list(self.conn.execute(
            "SELECT id, ifsc_id FROM seasons "
            "WHERE year IS NULL OR year >= ? "
            "ORDER BY id ASC",
            (current_year,),
        ))

    def find_ongoing_season_leagues(self) -> list[sqlite3.Row]:
        """Season_leagues whose parent season is ongoing."""
        current_year = datetime.now(timezone.utc).year
        return list(self.conn.execute(
            "SELECT sl.id, sl.ifsc_id FROM season_leagues sl "
            "JOIN seasons s ON sl.season_id = s.id "
            "WHERE s.year IS NULL OR s.year >= ? "
            "ORDER BY sl.id ASC",
            (current_year,),
        ))

    def find_ongoing_events(self, *, grace_days: int = 15) -> list[sqlite3.Row]:
        """Events that haven't ended yet, plus a `grace_days` tail for late corrections."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).date().isoformat()
        return list(self.conn.execute(
            "SELECT id, ifsc_id FROM events "
            "WHERE date_end IS NULL OR date_end >= ? "
            "ORDER BY id ASC",
            (cutoff,),
        ))

    def find_ongoing_competitions(self, *, grace_days: int = 15) -> list[sqlite3.Row]:
        """Competitions whose parent event is ongoing (within grace_days of date_end)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).date().isoformat()
        return list(self.conn.execute(
            "SELECT c.id AS comp_id, c.ifsc_id AS comp_ifsc, e.ifsc_id AS event_ifsc "
            "FROM competitions c JOIN events e ON c.event_id = e.id "
            "WHERE e.date_end IS NULL OR e.date_end >= ? "
            "ORDER BY c.id ASC",
            (cutoff,),
        ))

    def find_stale_competitions_with_event_ifsc(self, stale_days: int) -> list[sqlite3.Row]:
        """Competitions never fetched (or older than `stale_days`), joined with their event's ifsc_id.

        Shape mirrors `find_ongoing_competitions` — `(comp_id, comp_ifsc, event_ifsc)`
        — so competitions.hydrate can accept either source via its `rows=` kwarg.
        """
        cutoff = self.stale_cutoff(stale_days)
        return list(self.conn.execute(
            "SELECT c.id AS comp_id, c.ifsc_id AS comp_ifsc, e.ifsc_id AS event_ifsc "
            "FROM competitions c JOIN events e ON c.event_id = e.id "
            "WHERE c.last_fetched_at IS NULL OR c.last_fetched_at < ? "
            "ORDER BY c.id ASC",
            (cutoff,),
        ))

    def max_season_ifsc_id(self) -> Optional[int]:
        """Highest seasons.ifsc_id seen so far, or None on an empty table.

        Used by `seasons.discover` to bound the lookahead probe past the current max.
        """
        row = self.conn.execute("SELECT MAX(ifsc_id) FROM seasons").fetchone()
        return row[0] if row else None

    def find_season_by_year(self, year: int) -> Optional[sqlite3.Row]:
        """Look up a season by calendar `year` (the seasons.year column).

        Returns None if no season with that year exists — callers must handle
        the missing-parent case explicitly (season_leagues.hydrate currently
        skips writes and retries on the next pass).
        """
        return self.conn.execute(
            "SELECT id FROM seasons WHERE year = ?", (year,),
        ).fetchone()

    def find_category_by_name(self, name: str) -> Optional[sqlite3.Row]:
        """Look up a category by its `name` column. Returns None if absent."""
        return self.conn.execute(
            "SELECT id FROM categories WHERE name = ?", (name,),
        ).fetchone()

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
        season_id: int,
        league_id: int,
    ) -> int:
        # v6 made season_leagues.season_id/league_id NOT NULL. The signature
        # is required-positional-kwarg to match: callers that can't resolve
        # both FKs must guard at the call site (e.g. season_leagues.hydrate
        # logs and skips). SQLite UPSERT only intercepts UNIQUE/PK
        # violations — NOT NULL fires on the INSERT side and would abort the
        # statement before ON CONFLICT DO UPDATE could COALESCE-preserve the
        # prior value, so the old `Optional[int] = None` contract was a lie.
        row = self.conn.execute(
            "INSERT INTO season_leagues (ifsc_id, season_id, league_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  season_id = excluded.season_id, "
            "  league_id = excluded.league_id "
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
        season_id: int,
        league_id: Optional[int] = None,
    ) -> int:
        # v6 made events.season_id NOT NULL — same NULL-INSERT-aborts-before-
        # UPSERT story as upsert_season_league. `league_id` stays Optional
        # because events first seen via /seasons/{id}.events[] legitimately
        # arrive without a league association (only /season_leagues/{id}
        # later fills it in via the COALESCE-on-update path below).
        row = self.conn.execute(
            "INSERT INTO events (ifsc_id, season_id, league_id) VALUES (?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  season_id = excluded.season_id, "
            "  league_id = COALESCE(excluded.league_id, events.league_id) "
            "RETURNING id",
            (ifsc_id, season_id, league_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    def update_event(self, row_id: int, **fields: Any) -> None:
        """Partial update by column name; see `UPDATE_EVENT_ALLOWED`.

        Strict whitelist — unknown kwargs raise `ValueError`. Production callers
        spell out every column explicitly, so this is a typo guard for future
        callers. CAUTION: called from inside `with repo.transaction():`, an
        unknown kwarg rolls back the entire transaction (every other write in
        the same block).
        """
        unknown = set(fields) - UPDATE_EVENT_ALLOWED
        if unknown:
            raise ValueError(
                f"update_event got unknown column(s): {sorted(unknown)}; "
                f"allowed: {sorted(UPDATE_EVENT_ALLOWED)}"
            )
        if not fields:
            return
        cols = list(fields)
        sets = ", ".join(f"{c} = ?" for c in cols)
        values = [fields[c] for c in cols] + [row_id]
        self.conn.execute(f"UPDATE events SET {sets} WHERE id = ?", values)
        self._maybe_commit()

    def backfill_event_country_for_row(
        self, row_id: int, country: str, *, country_iso3: Optional[str] = None
    ) -> None:
        self.conn.execute(
            "UPDATE events SET country = ?, country_iso3 = ? "
            "WHERE id = ? AND country IS NULL",
            (country, country_iso3, row_id),
        )
        self._maybe_commit()

    def backfill_event_country_from_siblings(self) -> int:
        """Fill NULL country (and country_iso3) on any event whose city matches a sibling row.

        One SQL pass per column; uses MAX() to deterministically pick when
        multiple sibling countries exist (rare; alphabetical winner). Returns
        the number of rows touched on the `country` pass — `country_iso3` is
        kept in sync but its rowcount is not separately reported.
        """
        cur = self.conn.execute(
            "UPDATE events SET country = ("
            "  SELECT MAX(e2.country) FROM events e2 "
            "  WHERE e2.city = events.city AND e2.country IS NOT NULL"
            ") "
            "WHERE country IS NULL AND city IS NOT NULL"
        )
        affected = cur.rowcount
        self.conn.execute(
            "UPDATE events SET country_iso3 = ("
            "  SELECT MAX(e2.country_iso3) FROM events e2 "
            "  WHERE e2.city = events.city AND e2.country_iso3 IS NOT NULL"
            ") "
            "WHERE country_iso3 IS NULL AND city IS NOT NULL"
        )
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
        """Partial update by column name; see `UPDATE_ATHLETE_ALLOWED`.

        Strict whitelist; see `update_event` for the rationale and the
        in-transaction-rollback caveat.
        """
        unknown = set(fields) - UPDATE_ATHLETE_ALLOWED
        if unknown:
            raise ValueError(
                f"update_athlete got unknown column(s): {sorted(unknown)}; "
                f"allowed: {sorted(UPDATE_ATHLETE_ALLOWED)}"
            )
        if not fields:
            return
        cols = list(fields)
        sets = ", ".join(f"{c} = ?" for c in cols)
        values = [fields[c] for c in cols] + [row_id]
        self.conn.execute(f"UPDATE athletes SET {sets} WHERE id = ?", values)
        self._maybe_commit()

    # ----------------------------------------------------------- Cup rankings

    def upsert_cup_ranking(
        self,
        *,
        athlete_id: int,
        cup_ifsc_id: int,
        cup_name: Optional[str] = None,
        season: Optional[str] = None,
        discipline: Optional[str] = None,
        d_cat_id: Optional[int] = None,
        rank: Optional[int] = None,
    ) -> None:
        # Conflict target is the v6 expression UNIQUE index
        # `idx_cup_rankings_uniq` on (athlete_id, cup_ifsc_id,
        # COALESCE(d_cat_id, -1)). Used in isolation (e.g. notebook write
        # paths), ON CONFLICT … DO UPDATE preserves the row id; v5's INSERT
        # OR REPLACE deleted + re-inserted and churned it.
        #
        # NOTE: athletes.hydrate (the only production caller) still wipes
        # an athlete's cup_rankings via `delete_cup_rankings_for_athlete`
        # before this loop, so id stability does NOT hold across full
        # re-hydration today — rowids continue past the table max on each
        # cycle. The wipe-and-rewrite pattern keeps cup_rankings in sync
        # with the athlete's current payload (rankings that disappeared
        # from the payload need to leave the DB) and predates v6. If a
        # downstream consumer ever needs id stability across re-hydration,
        # athletes.hydrate is the place to fix it (track seen keys, then
        # `DELETE … WHERE NOT IN (...)` instead of the blanket wipe).
        self.conn.execute(
            "INSERT INTO cup_rankings "
            "(athlete_id, cup_ifsc_id, cup_name, season, discipline, d_cat_id, rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (athlete_id, cup_ifsc_id, COALESCE(d_cat_id, -1)) DO UPDATE SET "
            "  cup_name = excluded.cup_name, "
            "  season = excluded.season, "
            "  discipline = excluded.discipline, "
            "  d_cat_id = excluded.d_cat_id, "
            "  rank = excluded.rank",
            (athlete_id, cup_ifsc_id, cup_name, season, discipline, d_cat_id, rank),
        )
        self._maybe_commit()

    def delete_cup_rankings_for_athlete(self, athlete_id: int) -> None:
        """Wipe cup_rankings for an athlete before re-hydration (idempotent re-runs)."""
        self.conn.execute(
            "DELETE FROM cup_rankings WHERE athlete_id = ?", (athlete_id,)
        )
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

    # --------------------------------------------------------- Category rounds

    def upsert_category_round(
        self,
        ifsc_id: int,
        *,
        competition_id: int,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        category: Optional[str] = None,
        format: Optional[str] = None,
        format_identifier: Optional[str] = None,
        status: Optional[str] = None,
        status_as_of: Optional[str] = None,
        league_round_id: Optional[int] = None,
    ) -> int:
        # On conflict we intentionally do NOT touch `competition_id`: the World Climbing
        # category_round_id is supposed to be globally unique, so a collision
        # would mean either an World Climbing quirk or our own bug — silently re-parenting
        # the row would corrupt joins for any pre-existing round_results /
        # round_stages of the original comp.
        row = self.conn.execute(
            "INSERT INTO category_rounds "
            "(ifsc_id, competition_id, kind, name, category, format, format_identifier, "
            " status, status_as_of, league_round_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  kind = COALESCE(excluded.kind, category_rounds.kind), "
            "  name = COALESCE(excluded.name, category_rounds.name), "
            "  category = COALESCE(excluded.category, category_rounds.category), "
            "  format = COALESCE(excluded.format, category_rounds.format), "
            "  format_identifier = COALESCE(excluded.format_identifier, category_rounds.format_identifier), "
            "  status = COALESCE(excluded.status, category_rounds.status), "
            "  status_as_of = COALESCE(excluded.status_as_of, category_rounds.status_as_of), "
            "  league_round_id = COALESCE(excluded.league_round_id, category_rounds.league_round_id) "
            "RETURNING id",
            (ifsc_id, competition_id, kind, name, category, format,
             format_identifier, status, status_as_of, league_round_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ----------------------------------------------------------- Round stages

    def upsert_round_stage(
        self,
        *,
        category_round_id: int,
        seq: int,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        heat_id: Optional[int] = None,
        combined_stage_ifsc_id: Optional[int] = None,
    ) -> int:
        row = self.conn.execute(
            "INSERT INTO round_stages "
            "(category_round_id, seq, name, kind, heat_id, combined_stage_ifsc_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(category_round_id, seq) DO UPDATE SET "
            "  name = COALESCE(excluded.name, round_stages.name), "
            "  kind = COALESCE(excluded.kind, round_stages.kind), "
            "  heat_id = COALESCE(excluded.heat_id, round_stages.heat_id), "
            "  combined_stage_ifsc_id = COALESCE(excluded.combined_stage_ifsc_id, round_stages.combined_stage_ifsc_id) "
            "RETURNING id",
            (category_round_id, seq, name, kind, heat_id, combined_stage_ifsc_id),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ----------------------------------------------------------------- Routes

    def upsert_route(
        self,
        ifsc_id: int,
        *,
        category_round_id: int,
        name: Optional[str] = None,
    ) -> int:
        # On conflict, preserve the existing `category_round_id` — World Climbing route
        # ids are globally unique on the API, so a collision means the row is
        # either being re-seen from the same round (no-op) or there's an World Climbing
        # quirk we should not silently re-parent.
        row = self.conn.execute(
            "INSERT INTO routes (ifsc_id, category_round_id, name) VALUES (?, ?, ?) "
            "ON CONFLICT(ifsc_id) DO UPDATE SET "
            "  name = COALESCE(excluded.name, routes.name) "
            "RETURNING id",
            (ifsc_id, category_round_id, name),
        ).fetchone()
        self._maybe_commit()
        return row[0]

    # ----------------------------------------------------------- Round results

    def upsert_round_result(
        self,
        *,
        competition_id: int,
        category_round_id: int,
        athlete_id: int,
        rank: Optional[int],
        score: Optional[str],
        starting_group: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO round_results "
            "(competition_id, category_round_id, athlete_id, rank, score, starting_group) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (competition_id, category_round_id, athlete_id, rank, score, starting_group),
        )
        self._maybe_commit()

    # ----------------------------------------------------------- Stage results

    def upsert_stage_result(
        self,
        *,
        competition_id: int,
        round_stage_id: int,
        athlete_id: int,
        rank: Optional[int] = None,
        score: Optional[str] = None,
        time_ms: Optional[int] = None,
        winner: Optional[int] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO stage_results "
            "(competition_id, round_stage_id, athlete_id, rank, score, time_ms, winner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (competition_id, round_stage_id, athlete_id, rank, score, time_ms, winner),
        )
        self._maybe_commit()

    # ---------------------------------------------------------------- Ascents

    def upsert_ascent(
        self,
        *,
        competition_id: int,
        round_stage_id: int,
        route_id: int,
        athlete_id: int,
        rank: Optional[int] = None,
        score: Optional[str] = None,
        status: Optional[str] = None,
        modified: Optional[str] = None,
        top: Optional[int] = None,
        plus: Optional[int] = None,
        corrective_rank: Optional[float] = None,
        top_tries: Optional[int] = None,
        restarted: Optional[int] = None,
        time_ms: Optional[int] = None,
        dnf: Optional[int] = None,
        dns: Optional[int] = None,
        zone: Optional[int] = None,
        zone_tries: Optional[int] = None,
        low_zone: Optional[int] = None,
        low_zone_tries: Optional[int] = None,
        points: Optional[float] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ascents "
            "(competition_id, round_stage_id, route_id, athlete_id, "
            " rank, score, status, modified, "
            " top, plus, corrective_rank, top_tries, restarted, time_ms, "
            " dnf, dns, zone, zone_tries, low_zone, low_zone_tries, points) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (competition_id, round_stage_id, route_id, athlete_id,
             rank, score, status, modified,
             top, plus, corrective_rank, top_tries, restarted, time_ms,
             dnf, dns, zone, zone_tries, low_zone, low_zone_tries, points),
        )
        self._maybe_commit()

    def delete_round_data_for_competition(self, competition_id: int) -> None:
        """Wipe ascents, stage_results, round_results, round_stages for a competition.

        `category_rounds` and `routes` are preserved by design — the caller
        re-UPSERTs them via `ifsc_id` in the same transaction. A delete +
        reinsert would assign fresh local `id`s on each re-hydration,
        silently invalidating any out-of-band references that cached the
        previous `category_round_id` / `route_id` (analytics notebooks,
        external joins). UPSERT keeps identity stable. See ADR 0007.
        """
        for table in ("ascents", "stage_results", "round_results", "round_stages"):
            if table == "round_stages":
                # round_stages has no competition_id column; cascade via category_rounds.
                self.conn.execute(
                    "DELETE FROM round_stages WHERE category_round_id IN ("
                    "  SELECT id FROM category_rounds WHERE competition_id = ?"
                    ")",
                    (competition_id,),
                )
            else:
                self.conn.execute(
                    f"DELETE FROM {table} WHERE competition_id = ?", (competition_id,)
                )
        self._maybe_commit()
