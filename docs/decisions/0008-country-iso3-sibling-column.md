# 0008 — `country_iso3` sibling column on events and athletes

**Status:** Accepted
**Date:** 2026-05-24

## Context

The World Climbing public API mixes ISO 3166-1 alpha-3 codes (`FRA`, `JPN`, `USA`,
`CHN`, `BRA`, …) with the federation's own IOC-derived variants (`GER` instead
of `DEU`, `SUI` for `CHE`, `NED` for `NLD`, `POR` for `PRT`, `SLO` for `SVN`,
`INA` for `IDN`, `IRI` for `IRN`, `MAS` for `MYS`, `SIN` for `SGP`, `BUL` for
`BGR`, `CRO` for `HRV`, `GRE` for `GRC`, `RSA` for `ZAF`, `PHI` for `PHL`,
`KSA` for `SAU`, `GUA` for `GTM`, `CHI` for `CHL`, `TPE` for `TWN`), plus
historical IFSC-internal codes that aren't either standard (`CFR` for the
Russian Climbing Federation, seen on 2 Moscow 2021 events). The same
country therefore appears under two or three different codes across the
warehouse — `events.country` and `athletes.country` both inherit this
heterogeneity from the source.

Concrete impact: `SELECT country, COUNT(*) FROM events GROUP BY country`
splits Indonesia into INA (10) + IDN (2), Switzerland into SUI (51) + CHE
(0 today but possible upstream), Russia into RUS (39) + CFR (4), etc.
Joins with non-IFSC datasets (Olympics rosters, geo-coded city tables,
country demographics) break for every IFSC-variant row.

We need a code suitable for **standards-compliant joins** without losing
the federation's own labelling — which is what athletes wear on their kits
and what the IFSC publishes officially.

## Decision

Add a sibling column on both `events` and `athletes`:

- `country` — preserves the raw value from the World Climbing API (or, for
  events whose name carried `(XXX)`, the literal parsed code). Source of
  truth for "what the federation said". Schema-unchanged.
- `country_iso3` — derived value, ISO 3166-1 alpha-3 only. Computed by
  applying `IFSC_TO_ISO3` (a static dict in `src/wcl_data/parsers/event_location.py`)
  to `country` at write time in both fetchers and the sibling-backfill
  helper. Codes already matching ISO3 pass through unchanged.

Schema version bumps `2 → 3`. The migration is a guarded `ALTER TABLE …
ADD COLUMN country_iso3 TEXT` on each of `events` and `athletes`,
performed inside `apply_schema()` via `_add_missing_column` — idempotent
on a fresh `init` and on an existing v2 DB.

The dual-column approach intentionally avoids a one-way normalization
because: (a) the IFSC's own code is a useful signal in its own right —
two athletes whose passports both say SVN can still differ on whether
they competed under SUI (Switzerland's federation-of-record) vs CHE;
(b) the mapping isn't a closed system — IFSC adds variants when new
member federations join, and locking the warehouse to a snapshot of the
map would create a maintenance trap; (c) post-hoc joins on
`country = 'INA'` from prior analytics code keep working without a
migration.

## Consequences

**Positive**

- Aggregations on `country_iso3` (`GROUP BY athlete_country_iso3`, etc.)
  collapse all variants of one nation into one bucket. Indonesia is one
  row, not two.
- Joins with external ISO3-keyed datasets (Olympics, World Bank, geo-coded
  city tables, weather data, …) become straightforward — analytics
  downstream can keep `country_iso3` as the foreign key without
  per-source normalization layers.
- The raw `country` is preserved, so traceability back to "what the
  federation said" is intact.
- Bug-shaped values like `CMA` (a known typo for CHN in event
  ifsc_id=511) and the historical `CFR` for Moscow events get folded
  into the correct ISO3 bucket without losing the original signal in
  `country`.
- Exporter views surface both columns (`country` / `country_iso3` on
  the events + athletes views; `event_country` / `event_country_iso3`
  and `athlete_country` / `athlete_country_iso3` on the joined views).

**Negative**

- Two columns to keep in sync. The discipline lives in three places:
  the two fetchers (`fetchers/events.py`, `fetchers/athletes.py`) and the
  sibling-backfill helper in `db/repository.py`. A missing call site
  would silently leave `country_iso3` NULL on rows where `country` is
  set — there's a regression test (`tests/test_fetchers/test_athletes.py`,
  `test_events.py`) but the invariant isn't enforced by the schema.
- The `IFSC_TO_ISO3` map is hand-curated and must be extended when IFSC
  adds new variants. Practically this is rare (one new federation every
  few years), but missing entries cause silent passthrough (the IFSC
  code lands in `country_iso3`), not an error — discoverable only by
  audit.
- The migration is `ALTER TABLE ADD COLUMN`, which SQLite handles
  cheaply. But re-populating the new column on the existing ~15k athlete
  rows requires `python -m wcl_data hydrate athletes --stale-days 0`
  (~15 min). Performed once, on adoption.

## Alternatives considered

- **Normalize `country` in place** (overwrite the IFSC variants with
  ISO3, drop the raw value). Rejected: loses the federation-of-record
  signal; breaks any existing analytics that filters on the IFSC code;
  collapses CFR vs RUS into an unrecoverable singularity.
- **Don't normalize at all; document the mapping in `docs/`.** Rejected:
  pushes the same map into every analytics consumer. The point of Layer 0
  is to make Layer 1 simple.
- **Add `country_iso3` only on `events` and not on `athletes`.**
  Rejected: cross-dimension queries ("athletes from Indonesia in events
  hosted in Switzerland") need both. Doing one without the other moves
  the inconsistency rather than fixing it.
- **Defer until analytics needs it.** Rejected: cheaper to do now while
  the warehouse is small enough that a 15-minute re-hydrate is the cost.
  Once analytics starts depending on either column, migration becomes
  invasive.
- **Build a proper numbered migrations directory** (the `apply_migrations`
  story hinted at in ADR 0001). Rejected for this round: a single
  additive `ADD COLUMN` doesn't justify the framework — `_add_missing_column`
  is idempotent and covers the case. Revisit when the second non-trivial
  schema change lands.
