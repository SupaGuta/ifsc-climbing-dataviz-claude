# `results`

The join table. One row per (competition × athlete) with the athlete's
**final overall** rank in that competition — the top-level `rank` from the
IFSC payload. For per-round ranks (qualif / semi / final) see
[`round-results`](round-results.md); for per-route ascent detail see
[`ascents`](ascents.md).

**Typical size:** ~148,000 rows.

**Source:** populated as a side effect of `competitions.hydrate` —
specifically, from each competition payload's `ranking` array.

**Not hydratable:** no `last_fetched_at` column. Results are
delete-and-reinsert as part of the parent competition's hydration. Staleness
is therefore inherited from the parent competition's `last_fetched_at`.

## Columns

| Column           | Type    | Nullable | Meaning                                                  |
|------------------|---------|:--------:|----------------------------------------------------------|
| `id`             | INTEGER |          | Local row PK. Not referenced by anything.                |
| `competition_id` | INTEGER |          | FK → `competitions.id`. NOT NULL.                        |
| `athlete_id`     | INTEGER |          | FK → `athletes.id`. NOT NULL.                            |
| `rank`           | INTEGER |    ✓     | Final ranking. NULL for DSQ / DNS / DNF or unranked entries. |

**Indexes:**
- `idx_results_athlete ON athlete_id`
- `idx_results_competition ON competition_id`

**Constraints:**
- `UNIQUE (competition_id, athlete_id)` — at most one row per athlete per
  competition.

## Relationships

- **Parents:** `competitions` (NOT NULL), `athletes` (NOT NULL).
- **Children:** none.

## Coverage

| Column   | Coverage                          |
|----------|-----------------------------------|
| `rank`   | High but not 100% — NULL for DSQ / DNS / DNF / unranked entries. |

## Gotchas

- **Re-hydrating a competition wipes its results first.** The pattern in
  `src/ifsc_data/fetchers/competitions.py`:

  ```python
  with repo.transaction():
      repo.delete_results_for_competition(comp_id)
      for entry in data.get("ranking") or []:
          repo.upsert_result(...)
      repo.mark_fetched("competitions", comp_id)
  ```

  The transactional boundary means a partial failure rolls back — no half-
  written rankings. See
  [ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md)
  for the design rationale.
- **`rank` is NULL for non-finishers.** If you're computing leaderboards,
  filter `WHERE rank IS NOT NULL`. If you're computing participation, don't.
- **The big denormalized view in `src/ifsc_data/exporter.py` ("results")**
  pre-joins through `competitions`, `events`, `seasons`, `leagues`,
  `disciplines`, `categories`, and `athletes` — 14 columns of context per
  result row. Use it directly via `python -m ifsc_data export results` when
  you want a single self-contained CSV.
