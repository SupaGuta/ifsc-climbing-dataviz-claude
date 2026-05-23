# `competitions`

One row per (event × discipline × category) triple. A single event in
Chamonix 2019 might have four competitions: Lead Men, Lead Women, Boulder
Men, Boulder Women.

**Typical size:** ~5,800 rows.

**Source endpoint:** `GET /events/{event_ifsc_id}/result/{comp_ifsc_id}` —
returns the ranking (list of athlete + rank pairs) for one competition.

**Discovery:** skeletons inserted by `events.hydrate` from the `d_cats`
array on an event payload, with `discipline_id` and `category_id` resolved
at insert time.

## Columns

| Column            | Type    | Nullable | Meaning                                                     |
|-------------------|---------|:--------:|-------------------------------------------------------------|
| `id`              | INTEGER |          | Local row PK. Used by FKs from `results`.                   |
| `event_id`        | INTEGER |          | FK → `events.id`. NOT NULL — every competition belongs to an event. |
| `discipline_id`   | INTEGER |    ✓     | FK → `disciplines.id`. Set at skeleton-insert time.         |
| `category_id`     | INTEGER |    ✓     | FK → `categories.id`. Set at skeleton-insert time.          |
| `ifsc_id`         | INTEGER |          | IFSC API ID. Path component (the `d_cat_id`). **Not** globally unique. |
| `last_fetched_at` | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton, not yet hydrated.            |

**Indexes:**
- `idx_competitions_last_fetched ON last_fetched_at`
- `idx_competitions_event ON event_id`

**Constraints:**
- `UNIQUE (event_id, ifsc_id)` — see the gotcha below.

## Relationships

- **Parents:** `events` (NOT NULL), `disciplines`, `categories`.
- **Children:** `results.competition_id → competitions.id`.

## Coverage

| Column          | Coverage |
|-----------------|----------|
| `event_id`      | 100% (NOT NULL) |
| `discipline_id` | ~100% on hydrated rows |
| `category_id`   | ~100% on hydrated rows |

A NULL discipline or category would indicate an event with a malformed
`d_cats` entry — not observed.

## Gotchas

- **`ifsc_id` is NOT globally unique.** The IFSC API reuses competition IDs
  across different events. That's why the table uses `UNIQUE (event_id,
  ifsc_id)` instead of a unique constraint on `ifsc_id` alone. Queries that
  identify a competition by ifsc_id alone are wrong; always pair it with
  `event_id` or join through `events`.
- **The hydration of this table writes to `results` and `athletes` too.**
  Each competition's hydration wipes its existing `results` rows and
  reinserts them, in a single transaction (see
  [ADR 0005](../decisions/0005-transactional-boundary-on-competitions.md)),
  and also inserts athlete skeletons for any new athletes it sees.
- **Athlete skeletons created here have `last_fetched_at = NULL`** and will
  be hydrated in the *next* phase (`athletes.hydrate`). `pull-new` exploits
  this: container phases run with `stale_days=0`, then athletes runs with
  `stale_days=365_000` to hydrate only the brand-new skeletons.
