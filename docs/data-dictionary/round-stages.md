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
| `seq`                    | INTEGER |          | Within-round identifier. NOT NULL. Semantics vary by discipline (see Gotchas). |
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

- **`seq` semantics vary by discipline** — same column, different meanings:
  - **Default stage** (Lead/Boulder/Speed-qualif): `seq = 0`, single row per round.
  - **Combined sub-stages**: `seq = enumerate index` of the sub-discipline within `category_rounds[*].combined_stages[]` (typically 0=Boulder, 1=Lead for modern combined). The semantic linkage uses `kind` (set on the same row), not seq.
  - **Speed-final heats**: `seq = heat_id` (the IFSC heat identifier, monotonically allocated). One row per physical heat — eight 1/8 heats, four 1/4 heats, etc. heat_ids are large integers (e.g. 77865), so seq values for speed-final are much bigger than for other disciplines. Order by seq still yields chronological bracket order because IFSC allocates heat_ids in time order.
- **The `seq == heat_id` convention for speed-final** preserves per-heat granularity. Before this convention (fixed during the post-merge code-review), multiple physical heats sharing a bracket name collapsed into one row and the bracket structure was lost.
- **Legacy / unknown speed heat names** that arrive without a `heat_id` fall back to `_speed_seq(name)` from `SPEED_HEAT_SEQ` (`{"1/8": 0, "1/4": 1, "1/2": 2, "Small Final": 3, "Final": 4}`, default 999). A warning is logged when this happens. Audit periodically with:
  ```sql
  SELECT DISTINCT name FROM round_stages WHERE heat_id IS NULL AND name IS NOT NULL;
  ```
- **Default stages (seq=0, name=NULL) are created lazily.** A round with no athletes will have zero stages. Use LEFT JOIN from `category_rounds` to `round_stages` when you want to count empty rounds.
- **`seq` is not globally meaningful** — it's an in-round identifier. Stage 0 in round X is unrelated to stage 0 in round Y.
- **For combined events with three sub-disciplines (Speed+Boulder+Lead)** (Tokyo-2020-style), `seq` will go 0/1/2 in the parsing order. Paris 2024 only has two sub-stages (Boulder+Lead).
