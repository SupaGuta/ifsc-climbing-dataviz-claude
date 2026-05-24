# `category_rounds`

One row per competition phase (Qualification / Semi-final / Final, plus
combined-event sub-categories). A modern Lead World Cup has 3 rounds per
category; Speed events typically have 2 (Qualification + elimination Final);
Combined events use a single `kind` of `"boulder&lead"`.

**Typical size:** ~17,000 rows once the full backfill completes (~3 rounds ×
5,800 competitions, minus older/incomplete payloads).

**Source endpoint:** populated as a side effect of `competitions.hydrate`
from the top-level `category_rounds[]` array in
`/events/{event_ifsc_id}/result/{comp_ifsc_id}`.

**Hydratable:** yes. `last_fetched_at` carries the timestamp of the last
hydration, leaving the door open to fetch round-specific endpoints later
(`/api/v1/category_rounds/{ifsc_id}/results`) without reshaping the schema.
The current pipeline does not yet use this column on its own — it's set as a
side effect of the parent competition's hydrate.

## Columns

| Column              | Type    | Nullable | Meaning                                                |
|---------------------|---------|:--------:|--------------------------------------------------------|
| `id`                | INTEGER |          | Local row PK. Used by FKs from `round_stages`, `routes`, `round_results`. |
| `ifsc_id`           | INTEGER |          | IFSC `category_round_id`. Globally unique across the API. |
| `competition_id`    | INTEGER |          | FK → `competitions.id`. NOT NULL.                     |
| `kind`              | TEXT    |    ✓     | `"lead"` / `"speed"` / `"boulder"` / `"boulder&lead"` (combined). |
| `name`              | TEXT    |    ✓     | `"Qualification"` / `"Semi-final"` / `"Semi-Final"` / `"Final"`. API capitalization varies. |
| `category`          | TEXT    |    ✓     | `"Men"` / `"Women"` / `"U19 Men"` etc. Redundant with `competitions.category_id`. |
| `format`            | TEXT    |    ✓     | Human-readable: `"IFSC: 2 routes"`, `"IFSC 2025: Qualification"`. |
| `format_identifier` | TEXT    |    ✓     | Machine-readable variant, parallel to `format`.        |
| `status`            | TEXT    |    ✓     | `"finished"` / `"scheduled"` / `"running"`.            |
| `status_as_of`      | TEXT    |    ✓     | Raw timestamp from the API.                            |
| `league_round_id`   | INTEGER |    ✓     | From `round.league_round_id` — ordering hint for "qualif < semi < final". |
| `last_fetched_at`   | TEXT    |    ✓     | ISO-8601 UTC. Set when the parent competition hydrates. |

**Indexes:**
- `idx_category_rounds_competition ON competition_id`
- `idx_category_rounds_last_fetched ON last_fetched_at`

**Constraints:**
- `UNIQUE (ifsc_id)` — `category_round_id` is globally unique on the IFSC API.

## Relationships

- **Parents:** `competitions` (NOT NULL).
- **Children:** `round_stages.category_round_id`, `routes.category_round_id`,
  `round_results.category_round_id`.

## Gotchas

- **`kind` is plain TEXT, not a FK to `disciplines`.** For combined events
  the round-level `kind` (`"boulder&lead"`) doesn't match
  `competitions.discipline_id` (which is `"combined"` or the resolved discipline).
  Both are kept raw on purpose; do not assume they always agree.
- **Older payloads sometimes reference a `category_round_id` in `ranking[*].rounds[]`
  without including it in the top-level `category_rounds[]` array.** The fetcher
  materializes a minimal skeleton (`ifsc_id` + `competition_id` + `name`) for
  these. Such rows have `kind = NULL` / `format = NULL`; queries that filter on
  `kind` will miss them. A `log.debug` is emitted in this case.
- **`format` vs `format_identifier`** are both raw API strings; pick whichever
  is more useful for your query. Neither is parsed into structured data.
- **The `name` field is not strictly canonical** — Olympic events use
  `"Semi-Final"` (hyphen + capital F), regular events use `"Semi-final"`.
  Compare with `LIKE 'Semi%'` or normalize on read.
