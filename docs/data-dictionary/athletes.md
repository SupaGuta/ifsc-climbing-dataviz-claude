# `athletes`

Climber profiles. One row per athlete the World Climbing has ever indexed in a result.

**Typical size:** ~14,900 rows.

**Source endpoint:** `GET /athletes/{ifsc_id}` — returns name, gender,
country, plus optional biometric and biographical fields.

**Discovery:** skeletons inserted by `competitions.hydrate` from each
competition's ranking. A skeleton has only `ifsc_id`; everything else
populates during `athletes.hydrate`.

## Columns

| Column                       | Type    | Nullable | Meaning                                                              |
|------------------------------|---------|:--------:|----------------------------------------------------------------------|
| `id`                         | INTEGER |          | Local row PK. Used by FKs from `results`.                            |
| `ifsc_id`                    | INTEGER |          | IFSC API ID. Path component for `/athletes/{ifsc_id}`. UNIQUE.       |
| `firstname`                  | TEXT    |    ✓     | Given name. From API `firstname`.                                    |
| `lastname`                   | TEXT    |    ✓     | Family name. From API `lastname`.                                    |
| `gender`                     | INTEGER |    ✓     | `0` = male, `1` = female, NULL = unknown. Parsed from API `"male"`/`"female"` string. |
| `height`                     | INTEGER |    ✓     | Centimetres. Self-reported on World Climbing; usually NULL.          |
| `arm_span`                   | INTEGER |    ✓     | Centimetres. Self-reported; usually NULL.                            |
| `birthday`                   | TEXT    |    ✓     | `YYYY-MM-DD`. Often NULL for older athletes or privacy reasons.      |
| `city`                       | TEXT    |    ✓     | Free-text city. NULL if API didn't have it.                          |
| `country`                    | TEXT    |    ✓     | Raw federation code from the API — mix of ISO 3166-1 alpha-3 and IFSC/IOC variants (GER, SUI, NED, INA, IRI, MAS, SIN, …). For ISO3-only aggregation, use `country_iso3`. |
| `country_iso3`               | TEXT    |    ✓     | Canonical ISO 3166-1 alpha-3, derived from `country` via the static IFSC→ISO3 map in `parsers/event_location.py`. NULL iff `country` is NULL. See [ADR 0008](../decisions/0008-country-iso3-sibling-column.md). |
| `photo_url`                  | TEXT    |    ✓     | URL to a profile photo. Most athletes don't have one.                |
| `federation_id`              | INTEGER |    ✓     | IFSC federation ID. The federation that licenses the athlete — distinct from `country`. |
| `federation_name`            | TEXT    |    ✓     | Federation display name (e.g. `"Cesky Horolezecky Svaz"`).           |
| `federation_abbreviation`    | TEXT    |    ✓     | Federation abbreviation (e.g. `"CHS"`).                              |
| `federation_url`             | TEXT    |    ✓     | Federation home page URL.                                            |
| `paraclimbing_sport_class`   | TEXT    |    ✓     | Raw IFSC paraclimbing sport class (e.g. `"AL-1"`, `"B2"`). NULL for non-paraclimbing athletes — use `IS NOT NULL` as the paraclimbing flag. See [ADR 0009](../decisions/0009-athletes-payload-expansion.md). |
| `sport_class_status`         | TEXT    |    ✓     | Status of the sport-class assignment (e.g. `"Confirmed"`, `"Review"`). |
| `sport_class_review_date`    | TEXT    |    ✓     | Date of the next sport-class review, `YYYY-MM-DD` or NULL.           |
| `speed_pb_time`              | TEXT    |    ✓     | Speed personal best, as the API string (`"6.86"`). TEXT — the API does not render this as a number. |
| `speed_pb_date`              | TEXT    |    ✓     | Date of the speed PB, `YYYY-MM-DD`.                                  |
| `speed_pb_event_name`        | TEXT    |    ✓     | Event where the speed PB was set.                                    |
| `speed_pb_round_name`        | TEXT    |    ✓     | Round name within that event (e.g. `"Final"`).                       |
| `last_fetched_at`            | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton, not yet hydrated.                     |

**Indexes:** `idx_athletes_last_fetched ON last_fetched_at`.

## Relationships

- **Parents:** none.
- **Children:** `results.athlete_id → athletes.id`,
  `cup_rankings.athlete_id → athletes.id` (see [cup-rankings](cup-rankings.md)).

## Coverage

Measured 2026-05-25 on hydrated rows only (14,922 athletes), after the
post-ADR-0009 full re-hydrate.

| Column                       | Coverage |
|------------------------------|----------|
| `firstname`                  | 100.0%   |
| `lastname`                   | 100.0%   |
| `gender`                     | 100.0%   |
| `country`                    | 100.0%   |
| `country_iso3`               | 100.0%   |
| `federation_id`              | 100.0%   |
| `federation_name`            | 100.0%   |
| `federation_abbreviation`    |  99.6%   |
| `federation_url`             |  71.9%   |
| `city`                       |  70.6%   |
| `birthday`                   |  52.0%   |
| `speed_pb_time`              |  18.1%   |
| `speed_pb_date`              |  18.1%   |
| `speed_pb_event_name`        |  18.1%   |
| `speed_pb_round_name`        |  18.1%   |
| `photo_url`                  |  14.3%   |
| `height`                     |   9.1%   |
| `arm_span`                   |   4.1%   |
| `paraclimbing_sport_class`   |   3.8%   |
| `sport_class_status`         |   3.8%   |
| `sport_class_review_date`    |   0.0%   |

These percentages drift slowly as new athletes are added. **The NULLs are
real** — the World Climbing API genuinely doesn't have most heights, arm spans, or
photos. They're not parser bugs. The README's recompute snippet works on any
column here.

A few specifics worth knowing:
- **`speed_pb_*` ≈ 18%** corresponds to the ~2.7k athletes who've ever
  contested a speed round at IFSC level — the rest never raced speed.
- **`paraclimbing_sport_class` and `sport_class_status` track each other
  perfectly (both 3.8% = 569 rows)**: an athlete either has both or
  neither.
- **`sport_class_review_date` is 0%** — the API exposes the field but
  never populates it in current data. Don't treat NULL as "no review";
  treat the field as not-yet-available upstream.
- **`federation_*` is essentially complete** because the IFSC requires
  every licensed athlete to be tied to a member federation.

## Gotchas

- **Paraclimbing status:** the v3 `is_paraclimbing` bool was dropped in
  v4 (see [ADR 0009](../decisions/0009-athletes-payload-expansion.md)).
  For the paraclimbing flag, use `paraclimbing_sport_class IS NOT NULL`.
  Even that remains a heuristic on the athlete level — a paraclimbing
  athlete without a sport-class assignment (rare, but happens) reads as
  NULL. For *authoritative* per-competition status, join `results` →
  `competitions` → `events` and read
  [`events.is_paraclimbing`](events.md), which comes from the
  unambiguous `is_paraclimbing_event` API field. See
  [`../architecture/parsing-and-heuristics.md`](../architecture/parsing-and-heuristics.md).
- **Gender is INTEGER, not TEXT**, for consistency with the `categories.gender`
  column. The CSV exports (`exporter.VIEWS["athletes"]`) translate back to
  `"male"` / `"female"` strings via a `CASE` expression.
- **One known permanent 404:** athlete `ifsc_id = 12334`. This row exists as
  a skeleton forever and surfaces in logs as a 404 WARNING during
  `athletes.hydrate`. Silent drop, no action needed; it's documented as a
  known World Climbing-side artifact.
- **`city` here is free-text from the API**, not parsed. It's not normalized
  and shouldn't be treated as authoritative — same city often appears with
  different spellings ("Saint-Petersburg" vs "St. Petersburg").
- **`country` vs `country_iso3`:** the raw `country` mirrors the
  federation's own code (Switzerland shows as `SUI`, Indonesia as `INA`,
  etc.); `country_iso3` is the canonical ISO3 form (CHE / IDN). Group by
  whichever fits your audience — IFSC podium-style summaries use `country`,
  joins to external ISO3-keyed datasets use `country_iso3`. See
  [ADR 0008](../decisions/0008-country-iso3-sibling-column.md).
- **`ifsc_id` is reassigned across athletes over time** — confirmed during
  a Wikidata cross-match (2026-05-24): of 572 climbers with an IFSC ID on
  Wikidata, 257 (46%) had the same ID as one of our athletes but pointed
  to a completely different person. Best-known example: `ifsc_id = 6487`
  is Pierre MASSCHELEIN in our warehouse but was David Lama (deceased
  2019) on Wikidata. When the IFSC deletes an inactive/deceased athlete's
  profile, the ID may later be recycled for a new athlete. **Implications:**
  (a) any future enrichment layer that matches on `ifsc_id` alone *must*
  verify by name before writing — otherwise it'll silently graft Lama's
  height onto Masschelein's row; (b) our own historical results for the
  prior occupant of a reused ID are at risk of contamination if the IFSC
  reassigns mid-season — not observed yet but worth monitoring; (c) cross-
  references to athlete profiles in external tools (Wikipedia, Wikidata,
  national federation sites) should be assumed stale unless cross-checked
  by name + birthday.
