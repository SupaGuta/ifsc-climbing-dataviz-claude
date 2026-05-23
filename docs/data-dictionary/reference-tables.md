# Reference tables: `leagues`, `disciplines`, `categories`

Small, mostly-static tables that exist so the larger tables can FK to a stable
ID instead of repeating string values. None of them carry `last_fetched_at` —
they're rebuilt on every parent hydration via upsert, so they can't go stale
in isolation.

## `leagues`

**Typical size:** ~15 rows.

**Source:** populated by `seasons.hydrate` from the `leagues` array on each
season payload, and by `season_leagues.hydrate` from each season_league's
`league` field. Name-unique.

| Column | Type    | Nullable | Meaning                                |
|--------|---------|:--------:|----------------------------------------|
| `id`   | INTEGER |          | Local row PK. Used by FKs from `season_leagues`, `events`. |
| `name` | TEXT    |          | Full league name, e.g. `"World Cup"`, `"Continental Championship"`. UNIQUE. |

**Children:** `season_leagues.league_id`, `events.league_id` (both nullable).

**Gotchas:** league names are stored verbatim from the API. No normalization,
no aliases. If the API renames a league, the new name becomes a new row
rather than updating the existing one — historical events keep pointing at
the old `league_id`.

---

## `disciplines`

**Typical size:** ~5 rows (`lead`, `boulder`, `speed`, `combined`,
`boulder&lead`).

**Source:** populated by `season_leagues.hydrate` and `events.hydrate`.
Discipline names are **lowercased** before insert so `"Lead"`, `"lead"`,
`"LEAD"` all collapse to one row.

| Column | Type    | Nullable | Meaning                                          |
|--------|---------|:--------:|--------------------------------------------------|
| `id`   | INTEGER |          | Local row PK. Used by FKs from `competitions`.   |
| `name` | TEXT    |          | Discipline name, lowercased. UNIQUE.             |

**Children:** `competitions.discipline_id` (nullable).

**Gotchas:** names are lowercase by convention. CSV exports
(`exporter.VIEWS["competitions"]`, `["results"]`) emit them as-is —
downstream consumers should expect `"lead"`, not `"Lead"`.

---

## `categories`

**Typical size:** ~70 rows.

**Source:** populated by `season_leagues.hydrate` from each
`d_cat.name` ("Lead Men", "Boulder Women", "Youth A Male", paraclimbing
classes like "AL1", "B1", etc.). The gender bit is regex-extracted from the
name.

| Column   | Type    | Nullable | Meaning                                                          |
|----------|---------|:--------:|------------------------------------------------------------------|
| `id`     | INTEGER |          | Local row PK. Used by FKs from `competitions`.                   |
| `name`   | TEXT    |          | Category name verbatim, e.g. `"Men"`, `"Women"`, `"Youth A Male"`, `"AL1"`. UNIQUE. |
| `gender` | INTEGER |    ✓     | `0` = male, `1` = female, NULL = other / mixed / paraclimbing classes / age groups that don't disambiguate. |

**Children:** `competitions.category_id` (nullable).

**Gender extraction:** the regex in `src/ifsc_data/fetchers/season_leagues.py`
matches `\b(men|male|women|female)\b` (case-insensitive). Categories like
`"Youth A Male"` match → gender = 0; `"AL1"` doesn't match → gender = NULL.

**Gotchas:**
- Paraclimbing categories have NULL gender even though they're
  gender-specific in practice (e.g. `"AL1"` is men's arm-amputee lead). The
  category name doesn't carry the gender word, so the regex falls through.
  To filter by gender for paraclimbing analyses, you'd need an external
  mapping table — not provided.
- The CSV exports emit `gender` as `"male"` / `"female"` / NULL strings via
  a `CASE` in the SQL. If you query directly, expect the INTEGER encoding.
- One historical category-name fix is hard-coded:
  `CATEGORY_NAME_FIXES = {"AL1": "Men AL1"}` for event 1462 in
  `src/ifsc_data/fetchers/events.py`. This is a one-off API quirk patched at
  ingestion.
