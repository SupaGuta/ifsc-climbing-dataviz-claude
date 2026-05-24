# `events`

A single competition event. Has a city, country, date range, and zero or more
competitions (typically 2–6: one per discipline × category combination).

**Typical size:** ~1,400 rows.

**Source endpoint:** `GET /events/{ifsc_id}` — returns name, dates,
discipline/category combos for this event, plus the
`is_paraclimbing_event` flag.

**Discovery:** skeletons are inserted by `seasons.hydrate` (from the
season's `events` array) and `season_leagues.hydrate` (from each
season_league's `events` array). NULL `season_id` / `league_id` is filled
in if either parent surfaced this event.

## Columns

| Column             | Type    | Nullable | Meaning                                                            |
|--------------------|---------|:--------:|--------------------------------------------------------------------|
| `id`               | INTEGER |          | Local row PK. Used by FKs from `competitions`.                     |
| `ifsc_id`          | INTEGER |          | IFSC API ID. Path component for `/events/{ifsc_id}`. UNIQUE.       |
| `season_id`        | INTEGER |    ✓     | FK → `seasons.id`. Populated if the event was surfaced via its season. |
| `league_id`        | INTEGER |    ✓     | FK → `leagues.id`. Populated if surfaced via a season_league.      |
| `name`             | TEXT    |    ✓     | Full event name, e.g. `"IFSC Climbing World Cup - Chamonix (FRA) 2019"`. |
| `city`             | TEXT    |    ✓     | Title-cased city. Parsed from `name` first, then API `location` field. |
| `country`          | TEXT    |    ✓     | Raw federation code — mix of ISO 3166-1 alpha-3 (FRA, JPN, USA …) and IFSC/IOC variants (GER, SUI, NED, INA, IRI, MAS, SIN, …). Parsed from `name` first, then API `country` field. Sibling backfill recovers many NULLs. For ISO3-only aggregation, use `country_iso3`. |
| `country_iso3`     | TEXT    |    ✓     | Canonical ISO 3166-1 alpha-3, derived from `country` via the static IFSC→ISO3 map in `parsers/event_location.py`. Codes already ISO3 pass through unchanged. NULL iff `country` is NULL. See [ADR 0008](../decisions/0008-country-iso3-sibling-column.md). |
| `date_start`       | TEXT    |    ✓     | `YYYY-MM-DD`, local date (no timezone). From API `local_start_date`. |
| `date_end`         | TEXT    |    ✓     | `YYYY-MM-DD`, local date. From API `local_end_date`.               |
| `is_paraclimbing`  | INTEGER |    ✓     | `0` / `1`. From API `is_paraclimbing_event`. Authoritative — *not* the heuristic that lives on `athletes`. |
| `last_fetched_at`  | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton, not yet hydrated.                   |

**Indexes:**
- `idx_events_last_fetched ON last_fetched_at`
- `idx_events_season ON season_id`

## Relationships

- **Parents:** `seasons`, `leagues` (both nullable).
- **Children:** `competitions.event_id → events.id` (NOT NULL — every
  competition belongs to exactly one event).

## Coverage

Measured 2026-05-23 on hydrated rows only:

| Column            | Coverage |
|-------------------|----------|
| `name`            | 100.0%   |
| `date_start`      | 100.0%   |
| `is_paraclimbing` | 100.0%   |
| `city`            | 99.4%    |
| `country`         | 96.3%    |
| `country_iso3`    | 96.3%    |

The remaining city/country NULLs are events whose name doesn't match any of
the city/country parser anchors and whose API fields are blank. See
[../architecture/parsing-and-heuristics.md](../architecture/parsing-and-heuristics.md)
for the parser's rules and why it returns NULL rather than guessing.

## Gotchas

- **City/country provenance:** the parser in
  [`src/wcl_data/parsers/event_location.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/parsers/event_location.py)
  runs *first*; the API's own `location` / `country` fields are fallback.
  This is because older events store the location only in the name.
- **`is_paraclimbing` here is authoritative**, unlike the same-named field on
  `athletes` (which is heuristic — see [athletes.md](athletes.md)). For a
  reliable paraclimbing flag at the result level, join `results` →
  `competitions` → `events` and read `events.is_paraclimbing`.
- **Date fields are local to the event** — no timezone info, no time of day.
  If you need UTC dates, you'll need an external mapping from event country
  to timezone.
- **`date_end` is load-bearing for `pull-new`**. It's the field that decides
  whether an event is "ongoing" (re-fetched on every `pull-new`) or "ended"
  (skipped by `pull-new` once more than 15 days past `date_end`). A NULL
  `date_end` is treated as ongoing. See
  [ADR 0006](../decisions/0006-ongoing-only-pull-new.md). If you ever
  hand-edit `date_end` to a far-past date, you'll silently exclude the event
  from `pull-new`'s scope.
- **Sibling backfill:** an event with a city but no country can inherit the
  country from a sibling event in the same city (cross-batch backfill runs
  after every events hydration). This is the main reason `country` coverage
  beats raw parser output. The backfill keeps `country_iso3` in sync via a
  parallel `UPDATE` on the same join.
- **`country` vs `country_iso3`:** group by `country` to see the
  federation's own labels (Switzerland shows as `SUI` etc.); group by
  `country_iso3` for ISO3-clean aggregations (Switzerland becomes `CHE`).
  Joining to external ISO3-keyed datasets should always use `country_iso3`.
  See [ADR 0008](../decisions/0008-country-iso3-sibling-column.md) for the
  rationale and the full IFSC→ISO3 mapping.
