"""Shared helpers used by each fetcher's `hydrate(...)` entry point."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..db.repository import Repository

if TYPE_CHECKING:
    import sqlite3


# Expected `sqlite3.Row` column-name set for each table's `rows=` argument.
# Used by `resolve_rows` to fail fast at the contract boundary when a caller
# passes a row list with the wrong column shape — without this guard a mismatch
# would surface deep inside the fetcher loop (typically as a confusing
# `IndexError: 'No item with that key'` from a Row keyed-access).
_EXPECTED_ROW_KEYS: dict[str, frozenset[str]] = {
    "seasons": frozenset({"id", "ifsc_id"}),
    "season_leagues": frozenset({"id", "ifsc_id"}),
    "events": frozenset({"id", "ifsc_id"}),
    "athletes": frozenset({"id", "ifsc_id"}),
    "competitions": frozenset({"comp_id", "comp_ifsc", "event_ifsc"}),
}


def resolve_rows(
    repo: Repository,
    table: str,
    *,
    rows: Optional[list[sqlite3.Row]],
    stale_days: Optional[int],
    limit: Optional[int],
) -> list[sqlite3.Row]:
    """Common ``rows`` vs ``stale_days`` argument resolution for fetchers.

    Resolution rules:

    * If ``rows`` is not None it takes precedence (used by ``pull_new`` to scope
      hydration to ongoing entities). ``stale_days`` is then ignored, and a
      shape check confirms the rows expose the columns the named table's
      fetcher expects.
    * Otherwise ``stale_days`` is required (``ValueError`` if also None) and
      drives a query against the canonical stale-rows source for ``table``:
      ``find_stale_competitions_with_event_ifsc`` for competitions (3-column
      shape: ``comp_id`` / ``comp_ifsc`` / ``event_ifsc``), ``find_stale`` for
      every other hydratable (2-column shape: ``id`` / ``ifsc_id``).
    * ``limit`` clamps the result via list slicing.

    Returns a possibly-empty list. Callers should short-circuit on an empty
    return rather than entering the fetch loop.
    """
    if rows is None:
        if stale_days is None:
            raise ValueError("hydrate() requires either stale_days or rows")
        if table == "competitions":
            rows = repo.find_stale_competitions_with_event_ifsc(stale_days)
        else:
            rows = repo.find_stale(table, stale_days=stale_days)
    else:
        # Caller-provided rows: defensive list() so generators/cursors don't
        # explode on the slice below, and validate the row shape matches what
        # this fetcher expects to read.
        rows = list(rows)
        if rows and table in _EXPECTED_ROW_KEYS:
            expected = _EXPECTED_ROW_KEYS[table]
            first_keys = set(rows[0].keys())
            missing = expected - first_keys
            if missing:
                raise ValueError(
                    f"resolve_rows({table!r}) got rows missing expected "
                    f"column(s): {sorted(missing)}; first row exposes "
                    f"{sorted(first_keys)}"
                )
    if limit is not None:
        rows = rows[:limit]
    return rows
