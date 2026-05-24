# `seasons`

Top of the entity tree. One row per calendar season the World Climbing has indexed.

**Typical size:** ~38 rows (one per year of World Climbing coverage). Grows by one
each year.

**Source endpoint:** `GET /seasons/{ifsc_id}` — returns the year, the list of
leagues that ran that season, and a list of events under each league.

**Discovery:** by probe. `seasons.discover` reads `MAX(ifsc_id)` from the
table and fetches the next 5 IDs (`lookahead=5`). On an empty DB, probes
IDs 0–49 (`INITIAL_PROBE_RANGE = 50`) to bootstrap. 4xx responses are
silently dropped — see
[ADR 0003](../decisions/0003-selective-4xx-skip-retry.md).

## Columns

| Column            | Type    | Nullable | Meaning                                          |
|-------------------|---------|:--------:|--------------------------------------------------|
| `id`              | INTEGER |          | Local row PK. Used by FKs from `season_leagues`, `events`. |
| `ifsc_id`         | INTEGER |          | IFSC API ID. Path component for `/seasons/{ifsc_id}`. UNIQUE. |
| `year`            | INTEGER |    ✓     | Calendar year, e.g. `2024`. Set during hydration. |
| `last_fetched_at` | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton row, not yet hydrated. |

**Indexes:** `idx_seasons_last_fetched ON last_fetched_at`.

## Relationships

- **Parents:** none. Seasons are the root.
- **Children:**
  - `season_leagues.season_id → seasons.id` — the (season × league) junction.
  - `events.season_id → seasons.id` — events optionally carry a direct season
    link (some events are surfaced from the season payload, not the season
    league payload).

## Coverage

| Column      | Coverage |
|-------------|----------|
| `year`      | 100%     |

(Year is populated for every hydrated row. Skeletons have NULL year by
construction; once `seasons.hydrate` runs, the year is parsed from the API's
`"name"` field.)

## Gotchas

- World Climbing season IDs are mostly contiguous but not guaranteed. The probe handles
  small gaps via `lookahead=5`; bigger gaps would need a manual one-off probe
  with a different range.
- The `year` column comes from the API's `"name"` field, not a separate
  date. It's stored as INTEGER so range queries (`WHERE year >= 2020`) work
  directly.
