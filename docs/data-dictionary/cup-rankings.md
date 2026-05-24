# `cup_rankings`

One row per (athlete × cup × discipline): the athlete's **season-end
overall standing** for that cup in that discipline. Distinct from the
per-event rank stored on `results.rank` — this is the season output that
aggregates across all events of the cup.

**Typical size:** depends on how many athletes have competed in cup-
scored events; on a steady-state warehouse expect ~30-40 rows per
hydrated top-tier athlete, dropping to 0 for skeleton-only rows.

**Source endpoint:** populated by `athletes.hydrate` from the
`cup_rankings[]` array of `GET /athletes/{ifsc_id}`.

**Not hydratable:** no `last_fetched_at`. Delete-and-reinsert as part of
the parent athlete's hydration (`delete_cup_rankings_for_athlete` runs
inside the same transaction as `update_athlete`). Staleness is inherited
from `athletes.last_fetched_at`.

## Columns

| Column         | Type    | Nullable | Meaning                                                                 |
|----------------|---------|:--------:|-------------------------------------------------------------------------|
| `id`           | INTEGER |          | Local row PK.                                                           |
| `athlete_id`   | INTEGER |          | FK → `athletes.id`. NOT NULL.                                           |
| `cup_ifsc_id`  | INTEGER |          | IFSC cup ID (e.g. `63` = "IFSC Climbing World Cup 2019"). NOT NULL.    |
| `cup_name`     | TEXT    |    ✓     | Cup display name as published by the IFSC.                              |
| `season`       | TEXT    |    ✓     | Season as a string (`"2019"`). TEXT because the API renders it as such. |
| `discipline`   | TEXT    |    ✓     | `"lead"` / `"boulder"` / `"speed"` / `"combined"` / `""` (cf. gotcha).  |
| `d_cat_id`     | INTEGER |    ✓     | IFSC discipline-category ID. Stable across seasons; the safe pivot key. |
| `rank`         | INTEGER |    ✓     | Final season rank for this (athlete × cup × discipline).                |

**Indexes:**
- `idx_cup_rankings_athlete ON athlete_id`
- `idx_cup_rankings_cup ON cup_ifsc_id`

**Constraints:**
- `UNIQUE (athlete_id, cup_ifsc_id, d_cat_id)` — one row per athlete per
  cup per discipline. Unicity uses `d_cat_id` rather than `discipline`
  because the discipline string can drift (see gotcha below) while
  `d_cat_id` is stable.

## Relationships

- **Parents:** `athletes` (NOT NULL).
- **Children:** none.
- **Implicit reference:** `cup_ifsc_id` mirrors an IFSC cup ID, but
  there is no `cups` table in the warehouse — the `/cups/{id}` endpoint
  isn't ingested. Treat `cup_ifsc_id` as an external identifier suitable
  for joins to the IFSC website but not currently joinable inside the DB.

## Gotchas

- **European Cup discipline backfill.** European Cup payloads ship
  their ranking under an empty-string JSON key and put the discipline
  in the cup name itself (e.g. `"IFSC-Europe Climbing European Cup 2022 - Lead"`
  or `"IFSC-Europe Climbing European Cup Lead 2024"`). The fetcher
  recovers the label from the cup name and writes it to `discipline`,
  so on a freshly-hydrated DB you should not see `discipline = ""` for
  any current European Cup row. If you do encounter `""`, it means the
  upstream cup name didn't match the expected suffix patterns — fall
  back to `cup_name` parsing or `d_cat_id` and please open a follow-up.
- **Not a competition-level ranking.** A `rank = 1` in `cup_rankings`
  means "1st in the overall season standings of this cup", not "1st in
  any single event". For event-level ranks see `results.rank`.
- **Re-hydration is wipe-and-rewrite.** Re-running
  `athletes.hydrate` for an athlete deletes their entire
  `cup_rankings` block before re-inserting. There is no per-row diff;
  this matches how the API returns the data (always the full list).
- **No `last_fetched_at`.** Use `athletes.last_fetched_at` as the
  freshness proxy — if the parent athlete is fresh, the cup_rankings
  rows are fresh.
- **Cup rankings are not derivable from `results` alone.** Cup scoring
  rules (point ladders, drop-the-worst-N) vary by season and cup; the
  API does this computation for us. Don't try to reconstruct
  `cup_rankings` from per-event results — diverging from the IFSC's
  own arithmetic is silent and confusing.
