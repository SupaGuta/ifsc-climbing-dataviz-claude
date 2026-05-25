# `ascents`

One row per (athlete × route × stage): the full per-route performance detail
that the World Climbing API exposes for every climb. This is the most granular and
biggest table in the warehouse.

**Typical size:** ~880,000 rows after full backfill.

**Coverage caveat:** populated only for competitions that the API exposes
`category_rounds[]` for — i.e. mostly ≥2018. See
[category-rounds.md#gotchas](category-rounds.md#gotchas) for the full
pre-2018 sparsity note.

**Source endpoint:** populated by `competitions.hydrate`.

**Not hydratable:** no `last_fetched_at`. Delete-and-reinsert per competition.

## Columns

The table is **wide**: discipline-specific fields are all nullable. A row
populates only the subset of columns that the source ascent payload had.

| Column            | Type    | Nullable | Lead | Speed | Boulder | Meaning |
|-------------------|---------|:--------:|:----:|:-----:|:-------:|---------|
| `id`              | INTEGER |          |      |       |         | Local row PK. |
| `competition_id`  | INTEGER |          | ✓ | ✓ | ✓ | FK → `competitions.id`. Denormalized for per-competition wipes. |
| `round_stage_id`  | INTEGER |          | ✓ | ✓ | ✓ | FK → `round_stages.id`. Disambiguates speed-final heats. |
| `route_id`        | INTEGER |          | ✓ | ✓ | ✓ | FK → `routes.id`. |
| `athlete_id`      | INTEGER |          | ✓ | ✓ | ✓ | FK → `athletes.id`. |
| `rank`            | INTEGER |    ✓     | ✓ |   |   | Per-route rank (lead qualif). |
| `score`           | TEXT    |    ✓     | ✓ |   |   | Polymorphic: `"TOP"`, `"49+"`, numeric strings. Empty → NULL. |
| `status`          | TEXT    |    ✓     | ✓ | ✓ | ✓ | `"locked"` / `"confirmed"` / `"pending"`. |
| `modified`        | TEXT    |    ✓     | ✓ | ✓ | ✓ | Raw modification timestamp from the API. |
| `top`             | INTEGER |    ✓     | ✓ |   | ✓ | Boolean 0/1: did the athlete top the route. |
| `plus`            | INTEGER |    ✓     | ✓ |   |   | Lead-specific: the `+` half-grade. |
| `corrective_rank` | REAL    |    ✓     | ✓ |   |   | Lead qualif: tiebreaker rank (fractional). |
| `top_tries`       | INTEGER |    ✓     | ✓ |   | ✓ | Attempts needed to top. NULL = didn't top. |
| `restarted`       | INTEGER |    ✓     | ✓ |   |   | Lead semi/final boolean: route restarted mid-attempt. |
| `time_ms`         | INTEGER |    ✓     | ✓ | ✓ |   | Lead semi/final + speed: time/duration. **0 means "no recorded time"**, not "instant". |
| `dnf`             | INTEGER |    ✓     |   | ✓ |   | Speed boolean: Did Not Finish. |
| `dns`             | INTEGER |    ✓     |   | ✓ |   | Speed boolean: Did Not Start. |
| `zone`            | INTEGER |    ✓     |   |   | ✓ | Boulder boolean: zone reached. |
| `zone_tries`      | INTEGER |    ✓     |   |   | ✓ | Boulder: attempts to reach zone. |
| `low_zone`        | INTEGER |    ✓     |   |   | ✓ | Boulder (recent formats): low-zone reached. Often NULL even within boulder. |
| `low_zone_tries`  | INTEGER |    ✓     |   |   | ✓ | Boulder (recent formats): attempts to reach low-zone. |
| `points`          | REAL    |    ✓     |   |   | ✓ | Boulder: numeric points (e.g. 10.0, 24.6). The boulder "score" in numeric form — there is no string `score` on boulder ascents. |

**Indexes:**
- `idx_ascents_stage ON round_stage_id`
- `idx_ascents_route ON route_id`
- `idx_ascents_athlete ON athlete_id`
- `idx_ascents_competition ON competition_id`

**Constraints:**
- `UNIQUE (round_stage_id, athlete_id, route_id)` — within one stage, an
  athlete climbs each route at most once. The stage scope is what allows
  the same `(athlete, route)` pair to appear in multiple speed-final heats.

## Relationships

- **Parents:** `competitions`, `round_stages`, `routes`, `athletes` (all NOT NULL).
- **Children:** none.

## Gotchas

- **`time_ms = 0` ≠ instantaneous climb.** It's the API's encoding of "no
  recorded time" for non-speed disciplines. Speed times are typically 4000-8000 ms.
- **Boulder uses `points` (REAL), not `score` (TEXT).** A boulder qualif top
  ascent looks like `top=1, top_tries=1, zone=1, zone_tries=1, points=25.0,
  score=NULL`. Don't expect `score` to be populated on boulder rows.
- **`status` values vary by discipline.** Lead and Speed default to
  `"locked"`; combined ascents (where the source endpoint is different) use
  `"confirmed"`. Treat as a string, not as a boolean alias.
- **The table is excluded from `export_all` by default.** Default exports
  generate 8 CSVs (everything except `ascents`); `ascents` on its own is
  ~200 MB — several times the combined size of the eight default CSVs —
  because of this table's row count. Run `python -m wcl_data export
  ascents` to generate it on demand. See
  [`exporter.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/exporter.py)'s
  `DEFAULT_EXPORT_VIEWS`.
- **Re-hydrating a competition wipes its ascents first** (along with
  `round_results`, `stage_results`, `round_stages`). Inside the per-competition
  transaction, so a parse failure rolls back cleanly. See
  [ADR 0007](../decisions/0007-per-round-ingestion.md).
