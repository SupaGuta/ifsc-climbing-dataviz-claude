# `athletes`

Climber profiles. One row per athlete the World Climbing has ever indexed in a result.

**Typical size:** ~14,900 rows.

**Source endpoint:** `GET /athletes/{ifsc_id}` — returns name, gender,
country, plus optional biometric and biographical fields.

**Discovery:** skeletons inserted by `competitions.hydrate` from each
competition's ranking. A skeleton has only `ifsc_id`; everything else
populates during `athletes.hydrate`.

## Columns

| Column             | Type    | Nullable | Meaning                                                              |
|--------------------|---------|:--------:|----------------------------------------------------------------------|
| `id`               | INTEGER |          | Local row PK. Used by FKs from `results`.                            |
| `ifsc_id`          | INTEGER |          | IFSC API ID. Path component for `/athletes/{ifsc_id}`. UNIQUE.       |
| `firstname`        | TEXT    |    ✓     | Given name. From API `firstname`.                                    |
| `lastname`         | TEXT    |    ✓     | Family name. From API `lastname`.                                    |
| `gender`           | INTEGER |    ✓     | `0` = male, `1` = female, NULL = unknown. Parsed from API `"male"`/`"female"` string. |
| `height`           | INTEGER |    ✓     | Centimetres. Self-reported on World Climbing; usually NULL.                    |
| `arm_span`         | INTEGER |    ✓     | Centimetres. Self-reported; usually NULL.                            |
| `birthday`         | TEXT    |    ✓     | `YYYY-MM-DD`. Often NULL for older athletes or privacy reasons.      |
| `city`             | TEXT    |    ✓     | Free-text city. NULL if API didn't have it.                          |
| `country`          | TEXT    |    ✓     | Raw federation code from the API — mix of ISO 3166-1 alpha-3 and IFSC/IOC variants (GER, SUI, NED, INA, IRI, MAS, SIN, …). For ISO3-only aggregation, use `country_iso3`. |
| `country_iso3`     | TEXT    |    ✓     | Canonical ISO 3166-1 alpha-3, derived from `country` via the static IFSC→ISO3 map in `parsers/event_location.py`. NULL iff `country` is NULL. See [ADR 0008](../decisions/0008-country-iso3-sibling-column.md). |
| `photo_url`        | TEXT    |    ✓     | URL to a profile photo. Most athletes don't have one.                |
| `is_paraclimbing`  | INTEGER |    ✓     | `0` / `1`. **Heuristic** — see gotcha below.                         |
| `last_fetched_at`  | TEXT    |    ✓     | ISO-8601 UTC. NULL = skeleton, not yet hydrated.                     |

**Indexes:** `idx_athletes_last_fetched ON last_fetched_at`.

## Relationships

- **Parents:** none.
- **Children:** `results.athlete_id → athletes.id`.

## Coverage

Measured 2026-05-23 on hydrated rows only (14,922 athletes):

| Column            | Coverage |
|-------------------|----------|
| `firstname`       | 100.0%   |
| `lastname`        | 100.0%   |
| `gender`          | 100.0%   |
| `country`         | 100.0%   |
| `country_iso3`    | 100.0%   |
| `city`            | 70.6%    |
| `birthday`        | 52.0%    |
| `photo_url`       | 14.3%    |
| `height`          | 9.1%     |
| `arm_span`        | 4.1%     |

These percentages drift slowly as new athletes are added. **The NULLs are
real** — the World Climbing API genuinely doesn't have most heights, arm spans, or
photos. They're not parser bugs. The README's recompute snippet works on any
column here.

## Gotchas

- **`is_paraclimbing` is a heuristic**, not authoritative. It's set as:

  ```python
  is_paraclimbing=1 if data.get("paraclimbing_sport_class") is not None else 0
  ```

  i.e. an athlete is "paraclimbing" iff they have an assigned sport class.
  This matches the API's modelling but is lossy: a paraclimbing athlete
  without a sport-class assignment (rare, but happens) is flagged 0. If you
  need authoritative status, join `results` → `competitions` → `events` and
  read [`events.is_paraclimbing`](events.md), which comes from the
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
