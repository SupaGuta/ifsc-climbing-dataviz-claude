# `routes`

One row per route (lead route, speed lane, boulder problem). Routes belong to
a `category_round`, not to a stage — even when athletes climb the same route
in multiple speed-final heats, there's still only one `routes` row.

**Typical size:** ~30,000 rows after full backfill (~5 routes per round on
average).

**Coverage caveat:** populated only for competitions that the API exposes
`category_rounds[]` for — i.e. mostly ≥2018. See
[category-rounds.md#gotchas](category-rounds.md#gotchas) for the full
pre-2018 sparsity note.

**Source endpoint:** populated by `competitions.hydrate`. Routes are
collected from three possible locations in the payload and deduplicated by
`ifsc_id`:
1. `category_rounds[*].routes[]` (Lead all, Boulder semi/final, Speed)
2. `category_rounds[*].starting_groups[*].routes[]` (Boulder qualif —
   `cr.routes` is `[]`)
3. `category_rounds[*].combined_stages[*].routes[]` (Combined — `cr.routes`
   is `[]`)

**Hydratable:** yes. `last_fetched_at` is reserved for future per-route
endpoint hydration (`/api/v1/routes/{ifsc_id}/startlist`,
`/api/v1/routes/{ifsc_id}/results`) — currently set as a side effect of the
parent competition's hydrate.

## Columns

| Column              | Type    | Nullable | Meaning                                              |
|---------------------|---------|:--------:|------------------------------------------------------|
| `id`                | INTEGER |          | Local row PK. Used by FK from `ascents`.            |
| `ifsc_id`           | INTEGER |          | IFSC route id. Globally unique on the API.          |
| `category_round_id` | INTEGER |          | FK → `category_rounds.id`. NOT NULL.                |
| `name`              | TEXT    |    ✓     | `"1"`, `"2"`, `"A"`, `"B"`, `"M1"` — the API's per-round label. |
| `last_fetched_at`   | TEXT    |    ✓     | ISO-8601 UTC. Set by `competitions.hydrate`.        |

**Indexes:**
- `idx_routes_round ON category_round_id`
- `idx_routes_last_fetched ON last_fetched_at`

**Constraints:**
- `UNIQUE (ifsc_id)` — IFSC route ids are globally unique.

## Relationships

- **Parents:** `category_rounds` (NOT NULL).
- **Children:** `ascents.route_id`.

## Gotchas

- **Routes belong to a round, not a stage.** In speed-final, lanes A and B are
  re-used across heats (1/8, 1/4, 1/2, Final) — but only one `routes` row per
  lane. The `ascents.round_stage_id` FK is what disambiguates which heat an
  ascent occurred in.
- **The API exposes `startlist` and `ranking` URL strings on each route entry**
  (e.g. `/api/v1/routes/15867/startlist`). These are not stored — both are
  derivable from `ifsc_id`. Removing them keeps the table narrow and avoids
  denormalization drift.
- **Re-hydration upserts; it does not delete.** If a route ever vanishes from
  the API on a re-fetch (extremely rare — World Climbing doesn't unpublish completed
  rounds), the orphan row remains. The orphan has zero ascents pointing at it,
  so it's statistically invisible. Accept-list this trade-off, documented in
  [ADR 0007](../decisions/0007-per-round-ingestion.md).
