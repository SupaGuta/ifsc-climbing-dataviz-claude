# `round_results`

One row per (athlete × round): the athlete's rank and score **within that
round**. Distinct from the `results` table, which records the *final overall*
ranking of the whole competition.

**Typical size:** ~440,000 rows after full backfill.

**Source endpoint:** populated by `competitions.hydrate` from the per-athlete
`ranking[*].rounds[]` array.

**Not hydratable:** no `last_fetched_at`. Delete-and-reinsert as part of the
parent competition's hydration. Staleness is inherited from the parent
competition's `last_fetched_at`.

## Columns

| Column              | Type    | Nullable | Meaning                                                  |
|---------------------|---------|:--------:|----------------------------------------------------------|
| `id`                | INTEGER |          | Local row PK.                                            |
| `competition_id`    | INTEGER |          | FK → `competitions.id`. NOT NULL. Denormalized to make per-competition wipes a single indexed scan. |
| `category_round_id` | INTEGER |          | FK → `category_rounds.id`. NOT NULL.                    |
| `athlete_id`        | INTEGER |          | FK → `athletes.id`. NOT NULL.                           |
| `rank`              | INTEGER |    ✓     | Rank **within this round**. NULL for DSQ/DNS/DNF/unranked entries. |
| `score`             | TEXT    |    ✓     | Polymorphic: `"7.75"`, `"TOP"`, `"49+"`, `"94.9"`, etc. Empty string in the payload is normalized to NULL. |
| `starting_group`    | TEXT    |    ✓     | Boulder qualif only: `"Group A"` / `"Group B"`. NULL for other disciplines/rounds. |

**Indexes:**
- `idx_round_results_round ON category_round_id`
- `idx_round_results_athlete ON athlete_id`
- `idx_round_results_competition ON competition_id`

**Constraints:**
- `UNIQUE (category_round_id, athlete_id)` — one row per athlete per round.

## Relationships

- **Parents:** `competitions` (NOT NULL), `category_rounds` (NOT NULL),
  `athletes` (NOT NULL).
- **Children:** none. (`stage_results` is a separate per-stage table; it does
  not reference `round_results` directly.)

## Gotchas

- **`score` is TEXT on purpose.** Lead scores can be `"TOP"`, `"49+"`, `"32"`,
  numeric strings like `"7.75"`; boulder scores are numeric points (sometimes
  stringified); speed shows up as a time-as-decimal-seconds string. Don't
  CAST to REAL blindly — categorical values lose information.
- **Empty `score` becomes NULL.** Payloads sometimes have `score: ""` meaning
  "no data". We normalize to NULL so `COUNT(score)` and `WHERE score IS NULL`
  behave consistently.
- **`rank` here is the *within-round* rank**, not the final ranking. The
  per-route rank is on `ascents.rank` (lead qualif). The competition-level
  overall rank is on `results.rank`.
- **`starting_group` is only populated for boulder qualif.** The API splits
  large boulder qualif fields into "Group A" / "Group B" with separate routes
  per group. The annotation tells you which subset of routes an athlete
  climbed; the routes themselves are linked via `ascents.route_id`.
