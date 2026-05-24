# `stage_results`

One row per (athlete × stage). For simple rounds (Lead/Boulder/Speed-qualif)
this duplicates `round_results.rank`/`score` (there's exactly one default
stage per round). For Combined and Speed Final, it captures per-stage detail
that `round_results` can't represent.

**Typical size:** ~440,000+ rows after full backfill — slightly more than
`round_results` because speed-finals and combined rounds multiply rows.

**Coverage caveat:** populated only for competitions that the API exposes
`category_rounds[]` for — i.e. mostly ≥2018. See
[category-rounds.md#gotchas](category-rounds.md#gotchas) for the full
pre-2018 sparsity note.

**Source endpoint:** populated by `competitions.hydrate`.

**Not hydratable:** no `last_fetched_at`. Delete-and-reinsert per competition.

## Columns

| Column            | Type    | Nullable | Meaning                                                |
|-------------------|---------|:--------:|--------------------------------------------------------|
| `id`              | INTEGER |          | Local row PK.                                          |
| `competition_id`  | INTEGER |          | FK → `competitions.id`. NOT NULL. Denormalized for per-competition wipes. |
| `round_stage_id`  | INTEGER |          | FK → `round_stages.id`. NOT NULL.                    |
| `athlete_id`      | INTEGER |          | FK → `athletes.id`. NOT NULL.                        |
| `rank`            | INTEGER |    ✓     | Combined: `stage_rank` from the payload. Other disciplines: copy of `round_results.rank` for the default stage. NULL for speed heats. |
| `score`           | TEXT    |    ✓     | Combined: `stage_score` (e.g. `"54.1"`). Speed heats: heat `score` (e.g. `"4.82"`). Other: copy of `round_results.score`. |
| `time_ms`         | INTEGER |    ✓     | Speed final: heat-level time. NULL otherwise.        |
| `winner`          | INTEGER |    ✓     | Speed final: 0/1 — did this athlete win the heat. NULL otherwise. |

**Indexes:**
- `idx_stage_results_stage ON round_stage_id`
- `idx_stage_results_athlete ON athlete_id`
- `idx_stage_results_competition ON competition_id`

**Constraints:**
- `UNIQUE (round_stage_id, athlete_id)` — one row per athlete per stage.

## Relationships

- **Parents:** `competitions`, `round_stages`, `athletes` (all NOT NULL).
- **Children:** none.

## Gotchas

- **Redundant with `round_results` for simple rounds.** For Lead/Boulder/
  Speed-qualif (everything except speed-final and combined), there's exactly
  one stage per round and `stage_results.rank`/`score` recopy
  `round_results.rank`/`score`. The redundancy is intentional: it lets
  downstream queries always join through `round_stage_id` uniformly, instead
  of branching on the round's structure.
- **Speed `time_ms`** is the heat time (e.g. 4827 ms = 4.827s). The `score`
  field on speed heats is a decimal-seconds string like `"4.82"` (rounded);
  for analysis, prefer `time_ms`.
- **Combined `rank` is the stage-internal rank**, not the round rank. An
  athlete who finished 3rd at the Boulder sub-stage but 1st at the Lead
  sub-stage can still win the round overall — the round-level rank lives in
  `round_results.rank`.
