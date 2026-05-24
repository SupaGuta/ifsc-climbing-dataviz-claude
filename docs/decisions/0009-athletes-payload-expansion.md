# 0009 — Expanded capture of the `/athletes/{id}` payload

**Status:** Accepted
**Date:** 2026-05-25

## Context

The World Climbing public API's `GET /athletes/{ifsc_id}` endpoint
returns ~30 fields per athlete. The Phase 0-3 ingestion captured 11 of
them on the `athletes` table (`firstname`, `lastname`, `gender`,
`height`, `arm_span`, `birthday`, `city`, `country`, `country_iso3`,
`photo_url`, and the heuristic `is_paraclimbing`).

With the project moving toward downstream analytics and ML phases
(`wcl_analytics`, `wcl_ml`), an audit of the full payload — using the
Adam Ondra fixture at `tests/fixtures/athletes-id.json` — identified
four categories that are **not trivially derivable** from the already-
captured competition tables (`results`, `round_results`, `ascents`):

1. **`cup_rankings`** — the season-end overall standing per (cup ×
   discipline). The IFSC's official rule books for cup scoring vary by
   season and by cup (point ladders change, drop-the-worst-N rules
   change). Reconstructing these standings from per-event results is
   neither stable nor cheap; the API hands them to us pre-computed.
2. **`federation`** — the licensing federation (`{id, name,
   abbreviation, url}`). Distinct from the climbing nationality
   (`country`) and useful when studying federation-led pipelines
   (training programmes, neutral-flag athletes, dual-nationality
   licensing).
3. **Paraclimbing structure** — the raw `paraclimbing_sport_class`
   (e.g. `"AL-1"`, `"B2"`), the `sport_class_status`, and the
   `sport_class_review_date`. The previously-stored `is_paraclimbing`
   bool was a heuristic (`paraclimbing_sport_class IS NOT NULL`) and
   already documented as unreliable.
4. **`speed_personal_best`** — the IFSC's officially recorded PB on
   the speed wall (`{time, date, event_name, round_name}`). Sanity-
   checks anything we'd derive from `ascents.time_ms`, where qualifying
   times, corrected runs, and re-starts make naive `MIN(time_ms)`
   misleading.

The remainder of the payload was rejected from this scope (see
"Alternatives considered" below).

## Decision

Bump schema version `3 → 4`. On `athletes`, add 11 new columns,
populate them in `fetchers/athletes.py`, and surface them in
`exporter.VIEWS["athletes"]`. Add a new `cup_rankings` table with one
row per (athlete × cup × discipline). Drop the now-redundant
`athletes.is_paraclimbing` column.

### `athletes` — added

| Column | Type | Source |
|---|---|---|
| `federation_id` | INTEGER | `federation.id` |
| `federation_name` | TEXT | `federation.name` |
| `federation_abbreviation` | TEXT | `federation.abbreviation` |
| `federation_url` | TEXT | `federation.url` |
| `paraclimbing_sport_class` | TEXT | `paraclimbing_sport_class` |
| `sport_class_status` | TEXT | `sport_class_status` |
| `sport_class_review_date` | TEXT | `sport_class_review_date` |
| `speed_pb_time` | TEXT | `speed_personal_best.time` (kept as text — API renders `"6.86"` not a float) |
| `speed_pb_date` | TEXT | `speed_personal_best.date` |
| `speed_pb_event_name` | TEXT | `speed_personal_best.event_name` |
| `speed_pb_round_name` | TEXT | `speed_personal_best.round_name` |

### `athletes` — dropped

`is_paraclimbing` becomes strictly redundant with
`paraclimbing_sport_class IS NOT NULL`. The data-dictionary entry
already flagged it as a heuristic that should not be used for
authoritative status; downstream consumers wanting the bool can compute
it on the fly or join through `results → competitions →
events.is_paraclimbing` (which is authoritative and remains
unchanged — `events.is_paraclimbing` comes from a different API field,
`is_paraclimbing_event`, on a different table).

Migration uses `ALTER TABLE athletes DROP COLUMN is_paraclimbing`,
guarded by a new `_drop_column_if_exists` helper symmetric to
`_add_missing_column`. Requires SQLite ≥ 3.35 (March 2021) — bundled
SQLite on Python 3.12+ satisfies this.

### New table `cup_rankings`

```sql
CREATE TABLE cup_rankings (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    cup_ifsc_id INTEGER NOT NULL,
    cup_name TEXT,
    season TEXT,
    discipline TEXT,
    d_cat_id INTEGER,
    rank INTEGER,
    UNIQUE (athlete_id, cup_ifsc_id, d_cat_id)
);
```

One row per (athlete × cup × discipline). The API payload nests
disciplines as keys under each cup-rankings entry (Ondra's 2010 World
Cup has sibling `lead`/`boulder`/`combined` blocks), so a single
fixture athlete expands to ~30-40 rows. The empty-string discipline key
(observed on European Cup entries) is preserved as `""` rather than
filtered, so the data is faithful to the source.

Unicity is on `d_cat_id` rather than `discipline` because the discipline
key labelling can drift (the empty-string case) while `d_cat_id` is a
stable IFSC category identifier.

`cup_rankings` has **no `last_fetched_at`**. It is a derived child of
`athletes` and is wiped + re-inserted inside the same transaction as
each athlete hydrate (the same pattern as `results`).

## Consequences

**Positive**

- The "season output" target variable for ML models (final cup
  standing) is now a first-class column rather than something each
  consumer must reconstruct.
- The federation dimension enables analyses that separate
  *nationality* from *licensing federation*.
- Paraclimbing analytics are no longer gated on the unreliable bool.
- The speed-PB ground truth is captured exactly once, in the place
  where it's authoritative.
- The hydrate pass adds ~30 INSERT statements per athlete; at 14.9k
  athletes that's ~450k row inserts on a full `refresh`. SQLite
  handles this in seconds; the bottleneck stays HTTP fetch, not
  storage.

**Negative**

- The transaction wrapping in `fetchers/athletes.py` now spans the
  athlete UPDATE + cup-rankings DELETE + N INSERTs + `mark_fetched`.
  If a single insert fails mid-loop, the entire athlete is rolled
  back rather than partially persisted. This is the same behaviour
  the per-round ingestion (ADR 0007) already uses.
- 11 new mostly-NULL columns on a wide table (`athletes`). Coverage
  will be measured after the first full re-hydrate; `federation_*`
  is expected to be high coverage, `sport_class_*` low (only
  paraclimbers), `speed_pb_*` medium (only competitors who ran a
  speed round at IFSC level).
- The `cup_rankings` discipline column carries empty-string values for
  some European Cup entries. Downstream queries that pivot on
  discipline need to handle `""` deliberately (filter, bucket, etc.).
- A full re-hydrate is needed to backfill the new columns. ~15 min on
  the steady-state warehouse (already required for the v3 → v4 column
  drop, so no incremental cost).

## Alternatives considered

- **Store the whole payload as a JSON blob (`raw_payload`).** Rejected
  by user preference (2026-05-25): the project prefers explicit
  columns, both for type safety and so consumers can grep the schema.
  Re-hydrating later if a new field becomes interesting is a one-time
  ~15-min cost; living with a half-typed blob in every consumer is
  forever.
- **Keep `is_paraclimbing` for backwards compat.** Rejected: the field
  was already flagged as a heuristic, has no external consumers
  beyond internal notebooks, and `paraclimbing_sport_class IS NOT
  NULL` is a one-character difference for anyone who needs the bool.
  Keeping it would propagate confusion (which one is authoritative?).
- **Normalize `federation` into its own table.** Rejected for this
  round: it would mirror the existing `country` pattern (flat string
  on a wide table) rather than `leagues` / `disciplines` (referenced
  from many tables). If union queries on federations become heavy,
  promote to a table; the migration is `CREATE TABLE` + `UPDATE`.
- **Capture lifestyle fields (`favourite_movie`, `personal_story`,
  `coach`, `nickname`, `residence`, `spoken_languages`, social URLs).**
  Rejected: coverage on the Ondra fixture is mostly empty, none of
  these have an obvious analytics or ML use, and the cost of adding
  them later is symmetric (one re-hydrate). User decision.
- **Materialize the API's `discipline_podiums` /
  `world_championships_discipline_podiums` /
  `continental_championships_discipline_podiums` / `all_results`
  pre-aggregates.** Rejected: these are derivable from `results`,
  `events.is_world_championships` etc. They belong in a Layer 1
  analytics view, not in the Layer 0 warehouse. Storing them would
  introduce a stale-vs-fresh problem (the agg in the warehouse can
  contradict what the API computes on the fly).
- **Add `pronouns` as a low-cost capture.** Rejected by user
  preference: rare, low signal-to-noise for ML.
