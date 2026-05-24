# `round_stages`

Sub-division of a `category_round`. Most rounds have exactly **one default
stage** (seq=0); two cases create multiple stages:

- **Speed Final** — one stage per elimination heat (1/8, 1/4, 1/2, Small Final,
  Final). Same athlete can climb the same route across multiple heats — the
  stage FK is what gives the `ascents` table its uniqueness key in that case.
- **Combined events** (Boulder&Lead) — one stage per sub-discipline
  ("Boulder", "Lead").

**Typical size:** ~20,000 rows after full backfill (one default per round +
extra rows for speed-finals and combined sub-stages).

**Source endpoint:** populated by `competitions.hydrate`. Default stages are
created lazily on first use; combined sub-stages are pre-created from
`category_rounds[*].combined_stages[]`; speed-final heats are discovered
during the per-athlete `ranking[*].rounds[*].speed_elimination_stages[]` walk.

**Not hydratable:** no `last_fetched_at`. Wiped + re-inserted as part of the
parent competition's hydration; structural state, not fetched independently.

## Columns

| Column                   | Type    | Nullable | Meaning                                            |
|--------------------------|---------|:--------:|----------------------------------------------------|
| `id`                     | INTEGER |          | Local row PK. Used by FKs from `stage_results`, `ascents`. |
| `category_round_id`      | INTEGER |          | FK → `category_rounds.id`. NOT NULL.              |
| `seq`                    | INTEGER |          | Order within the round (0 = default). NOT NULL.   |
| `name`                   | TEXT    |    ✓     | `"Boulder"` / `"Lead"` (combined); `"1/8"` / `"1/4"` / `"1/2"` / `"Small Final"` / `"Final"` (speed). NULL for the default stage. |
| `kind`                   | TEXT    |    ✓     | `"boulder"` / `"lead"` for combined sub-stages; NULL otherwise. |
| `heat_id`                | INTEGER |    ✓     | IFSC heat id for speed-final stages; NULL otherwise. Globally stable. |
| `combined_stage_ifsc_id` | INTEGER |    ✓     | IFSC `combined_stages[].id` for combined sub-stages; NULL otherwise. |

**Indexes:**
- `idx_round_stages_round ON category_round_id`
- `idx_round_stages_heat ON heat_id`

**Constraints:**
- `UNIQUE (category_round_id, seq)` — each round numbers its stages independently.

## Relationships

- **Parents:** `category_rounds` (NOT NULL).
- **Children:** `stage_results.round_stage_id`, `ascents.round_stage_id`.

## Gotchas

- **Default stages (seq=0, name=NULL) are created lazily.** A round with no
  athletes will have zero stages. Joins from `category_rounds` to `round_stages`
  via LEFT JOIN, not INNER JOIN, when you want to count empty rounds.
- **Speed heat naming** is mapped via `SPEED_HEAT_SEQ` in
  `src/ifsc_data/fetchers/competitions.py`:
  `{"1/8": 0, "1/4": 1, "1/2": 2, "Small Final": 3, "Final": 4}`. Unknown
  heat names get `seq = 999` (preserves the data without losing it but breaks
  bracket ordering). Audit periodically with:
  ```sql
  SELECT DISTINCT name FROM round_stages WHERE heat_id IS NOT NULL;
  ```
- **`seq` is not globally meaningful** — it's an in-round ordering. Stage 0
  in round X is unrelated to stage 0 in round Y.
- **For combined Olympic events with three sub-disciplines (Speed+Boulder+Lead)**,
  `seq` will go 0/1/2 in the parsing order. Paris 2024 is two stages (Boulder+Lead).
  Tokyo 2020-style three-stage combined was not observed in our fixtures but
  should fit naturally.
