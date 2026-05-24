# 0007 — Per-round ingestion: rounds, stages, ascents

**Status:** Accepted
**Date:** 2026-05-24

## Context

The `results` table only stored final overall rank per (competition, athlete).
The World Climbing payload at `/events/{event}/result/{comp}` carries far richer
information: each competition's phase structure (qualif / semi / final), each
athlete's rank and score *within each phase*, and each ascent route-by-route.
None of this was ingested.

Three structural quirks of the payload force the schema beyond a simple
"add a column" approach:

1. The per-athlete `ranking[*].rounds[*]` dict contains **one of three
   alternative children**, depending on discipline/phase:
   - `ascents[]` (Lead / Boulder semi+final / Speed qualif)
   - `combined_stages[]` (Olympic combined: Boulder + Lead sub-stages)
   - `speed_elimination_stages[]` (Speed final: 1/8 → 1/4 → 1/2 → Small Final / Final heats)
2. Speed final heats reuse the same routes — the same athlete climbs lane A
   in 1/8 *and* 1/4. A unique key of `(route, athlete)` on per-route data
   would collide.
3. Routes can be nested under `category_rounds[*].routes[]` *or*
   `category_rounds[*].starting_groups[*].routes[]` (boulder qualif) *or*
   `category_rounds[*].combined_stages[*].routes[]` (combined). All three
   sources must feed the same `routes` table.

## Decision

Six new tables, all inside the existing per-competition transactional
boundary (ADR 0005):

- **`category_rounds`** (hydratable) — one per round, with `kind` / `name` /
  `format` / `status` and a `last_fetched_at` reserved for future per-round
  endpoint hydration.
- **`round_stages`** — sub-division of a round. Default `seq=0` stage for
  simple rounds; one row per combined sub-stage or speed-final heat. The
  `(category_round_id, seq)` uniqueness + `(round_stage_id, athlete_id,
  route_id)` uniqueness on `ascents` together solve the speed-final collision.
- **`routes`** (hydratable) — one per (round, route), populated from any of
  the three payload locations, deduplicated by IFSC route id.
- **`round_results`** — one per (round, athlete), with `rank` / `score` /
  `starting_group`. Replaces the per-round subset of what `results` used to
  not capture.
- **`stage_results`** — one per (stage, athlete). Redundant with
  `round_results` for simple rounds (same `rank` / `score` recopied), but lets
  every per-stage query join uniformly on `round_stage_id` without branching
  on discipline.
- **`ascents`** — wide table with discipline-specific nullable columns (lead:
  `top`/`plus`/`corrective_rank` ...; speed: `time_ms`/`dnf`/`dns`; boulder:
  `zone`/`zone_tries`/`points` ...). FK to `round_stages` (not
  `category_rounds`) for uniqueness.

The fetcher dispatches on the three alternative structures and discovers
speed-final heats lazily during the per-athlete walk.

`delete_round_data_for_competition` wipes `ascents`, `stage_results`,
`round_results`, `round_stages` on re-hydrate. `category_rounds` and `routes`
are UPSERTed (preserving `last_fetched_at`) — they're structural rows that
are stable across re-fetches, and wiping them would lose any future
startlist-hydration state.

## Consequences

**Positive**

- Per-round queries are first-class: "what was Adam Ondra's qualif score in
  Briançon 2020?" is a one-table read.
- Speed-final brackets and combined-event sub-stages are fully captured,
  including who won each heat.
- `category_rounds.last_fetched_at` and `routes.last_fetched_at` let a future
  startlist hydrator target `WHERE last_fetched_at IS NULL` rows without
  re-running the whole competitions hydrate.
- The wide `ascents` table is forward-compatible: new discipline-specific
  fields land via `ALTER TABLE ... ADD COLUMN` without a migration story.

**Negative**

- Row-count balloons by ~6× (148k results → ~880k ascents + 440k round_results
  + 440k stage_results + 17k category_rounds + 20k round_stages + 30k routes).
  Disk goes from ~150 MB to ~600 MB. Backfill via
  `refresh --stale-days 0` takes ~45-90 min on a modern laptop.
- `ascents` was excluded from `export_all` (~200 MB CSV otherwise). Users who
  want it must `python -m wcl_data export ascents` explicitly.
- `stage_results` is redundant with `round_results` for simple rounds. Cost:
  ~440k extra rows of nearly-duplicated data. The redundancy lets downstream
  queries always join through `round_stage_id` uniformly — a worthwhile
  trade for the analytics ergonomics.
- Speed heat names beyond `1/8 / 1/4 / 1/2 / Small Final / Final` get
  `seq=999`. Preserves data but breaks bracket ordering. Mitigate by
  periodic audit of `SELECT DISTINCT name FROM round_stages WHERE heat_id
  IS NOT NULL`.

## Alternatives considered

- **Three discipline-specific ascent tables (lead_ascents, speed_ascents,
  boulder_ascents).** Rejected. Combined events would need rows in multiple
  tables for the same athlete in the same round. Wide-with-nullable is
  simpler.
- **Storing `score` as REAL with a separate categorical column.** Rejected.
  The polymorphism (`"TOP"`, `"49+"`, `"7.75"`, `"4.82"`) is real and
  different per discipline; committing to a parsing rule now would lock in
  a wrong assumption. TEXT preserves source-of-truth.
- **Wiping `category_rounds` / `routes` on every re-hydrate.** Rejected.
  Would erase any future `last_fetched_at` set by a planned startlist
  hydrator. Upsert-with-COALESCE is the structural-row analog of the
  athletes table's pattern.
- **A single nullable `round_stage_id` on `ascents` instead of always-required.**
  Rejected. SQLite treats NULLs as distinct in UNIQUE constraints, so
  `UNIQUE (route_id, athlete_id, NULL)` repeated twice wouldn't conflict.
  Mandatory FK + a default stage row per round gives stronger invariants.
- **Lead-first, defer speed/boulder/combined.** Rejected at the user's
  explicit request ("Je veux qu'on en profite pour tout faire tout de
  suite"). Verified upfront against speed/boulder/combined fixtures
  downloaded during Phase 0 exploration.

## Post-merge corrections (code review on commit f1afd0c)

A multi-angle code review surfaced seven defects after the initial backfill.
Tier-A (3 silent data-correctness bugs + 2 user-visible CLI gaps) and Tier-B
(2 robustness gaps observable on plausible payload shapes) were addressed
together. Tier-C/D findings were intentionally deferred until a real trigger
materializes.

- **Speed-final heat collapse.** `_speed_seq` mapped every heat with the same
  bracket name (e.g. eight `"1/8"` heats) to `seq = 0`, collapsing them into a
  single `round_stages` row and overwriting `heat_id` via COALESCE. Fix:
  `_ensure_speed_stage` now uses `heat_id` as both the cache key and the `seq`
  value, so each physical heat owns its own row. See `round-stages.md`.
- **`upsert_route` and `upsert_category_round` would silently re-parent.**
  Their `ON CONFLICT` clauses unconditionally set the parent FK (
  `category_round_id` and `competition_id`, respectively) from `excluded`.
  Any IFSC id reused under a different parent would re-parent the existing
  row, corrupting joins for the original comp's children. Fix: drop the
  parent FK from the SET clause so the original assignment is preserved.
- **`_cmd_status` hardcoded a 9-table list and missed the 6 new tables.**
  Fix: extend the list and derive the "hydrated" column from
  `HYDRATABLE_TABLES`.
- **`export --help` advertised "default: export all" while `export_all`
  silently excluded `ascents`.** Fix: import `DEFAULT_EXPORT_VIEWS` in the
  CLI and rephrase the help text to describe default vs opt-in.
- **Combined sub-stage matching by position was fragile.** Phase A enumerated
  `cr["combined_stages"]`, phase B enumerated `rnd["combined_stages"]`
  independently — any divergence in order or length would cross-link an
  athlete's data to the wrong sub-discipline. Fix: phase A also records
  `combined_stage_by_kind` (lowercased kind → stage id); phase B looks up by
  kind instead of by position.
- **Lazy combined fallback dropped `kind`.** When phase B materializes a
  combined sub-stage on the fly (round missing from top-level), the fix
  derives `kind` from `stage_name.lower()` so the exporter's `stage_kind`
  column is no longer NULL for these rows.

Post-fix backfill scope: only Speed competitions needed re-hydration to pick
up per-heat granularity. The other six fixes either don't change the on-disk
shape (CLI plumbing) or only trigger on payload shapes that weren't observed
in the live data.
