# `season_leagues`

The (season × league) junction. One row per league instance per season — e.g.
"World Cup 2024," "Continental Championships 2023."

**Typical size:** ~450 rows.

**Source endpoint:** `GET /season_leagues/{ifsc_id}` — returns the season's
year, league name, list of (discipline × category) pairs, and event
skeletons.

**Discovery:** skeletons are inserted by `seasons.hydrate` when it walks the
`leagues` array on a season's payload. The skeleton has NULL `season_id` /
`league_id` if the parent season hasn't been fully resolved yet; the
hydration phase backfills those.

## Columns

| Column            | Type    | Nullable | Meaning                                                    |
|-------------------|---------|:--------:|------------------------------------------------------------|
| `id`              | INTEGER |          | Local row PK. Used by FKs from `events`.                   |
| `ifsc_id`         | INTEGER |          | IFSC API ID. Path component for `/season_leagues/{ifsc_id}`. UNIQUE. |
| `season_id`       | INTEGER |    ✓     | FK → `seasons.id`. NULL until parent is resolved.          |
| `league_id`       | INTEGER |    ✓     | FK → `leagues.id`. NULL until hydration extracts the league name. |
| `last_fetched_at` | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton, not yet hydrated.           |

**Indexes:**
- `idx_season_leagues_last_fetched ON last_fetched_at`
- `idx_season_leagues_season ON season_id`

## Relationships

- **Parents:**
  - `seasons` via `season_id`.
  - `leagues` via `league_id`.
- **Children:** none directly. The endpoint emits *event* skeletons during
  hydration, which land in `events` with their `season_id` and `league_id`
  set.

## Coverage

| Column      | Coverage |
|-------------|----------|
| `season_id` | ~100% on hydrated rows |
| `league_id` | ~100% on hydrated rows |

Both FKs are populated reliably during hydration. A NULL on a hydrated row
would indicate an API payload that's missing its `"season"` year or
`"league"` name — neither has been observed.

## Gotchas

- The junction role: don't query `season_leagues` directly for analytics —
  this table only exists so the orchestrator knows which season-leagues to
  hydrate. Use `seasons` ⋈ `leagues` ⋈ `events` via the export views in
  `src/wcl_data/exporter.py`.
- Hydrating a season_league does **not** fully hydrate its events. Event
  skeletons are inserted with NULL `last_fetched_at`; they're picked up by
  the `events` phase that runs immediately after.
- A season_league hydration also seeds `disciplines` and `categories` (see
  [reference-tables.md](reference-tables.md)).
