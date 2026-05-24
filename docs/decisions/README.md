# Architecture decision records (ADRs)

An **ADR** is a short, immutable record of one non-obvious design choice: what
was decided, why, and what we gave up in exchange. The goal is for a future
contributor (or future-you) reading the code six months from now to find the
answer to "but *why* is it like this?" without re-running the original
conversation.

## Rules

- **One decision per file.** Numbered sequentially (`0001-…`, `0002-…`,
  …), kebab-case slug. Number padding stays at 4 even though we'll never
  hit 10,000.
- **Immutable once merged.** Don't edit an ADR to fix a typo unless the typo
  changes the meaning — instead, if the decision is revisited, write a new
  ADR that supersedes the old one and update the old one's *Status* line to
  `Superseded by 00NN`.
- **Short.** Aim for 30–80 lines. If it doesn't fit, the decision is
  probably two decisions.
- **About the design, not the implementation.** "We use SQLite" is an ADR.
  "We named this function `find_stale`" is not.

## When to add an ADR

Write one when you make a choice that:

- Has at least one defensible alternative that would have shipped if the call
  had gone the other way.
- Constrains future work (locks in a format, a dependency, a topology, a
  failure mode).
- A new contributor would otherwise ask about. If reviewing your own PR
  raises a "wait, why didn't you just do X?" comment, the answer belongs in
  an ADR.

You probably *don't* need an ADR for: routine bug fixes, choosing between two
mostly-equivalent libraries for a tiny job, internal renames, performance
tweaks that don't change behavior.

## Template

```markdown
# 00NN — Short title in active voice

**Status:** Accepted    <!-- Accepted | Superseded by 00NN | Deprecated -->
**Date:** YYYY-MM-DD

## Context

One paragraph. What problem prompted the decision? What constraints
applied? What was already true that we couldn't change?

## Decision

What we chose. One paragraph, often a single sentence followed by a few
specifics (default values, edge cases).

## Consequences

The trade-offs we accepted. Both positive (what this buys us) and negative
(what it costs us). Be honest about the downsides — future-you will be
grateful when something the ADR predicted turns out to bite.

## Alternatives considered

Brief: what else we looked at and why we didn't pick it. Two or three
bullets is fine.
```

## Existing ADRs

- [0001 — Single SQLite warehouse](0001-single-sqlite-warehouse.md)
- [0002 — Streaming writes](0002-streaming-writes.md)
- [0003 — Selective 4xx-skip retry](0003-selective-4xx-skip-retry.md)
- [0004 — Incremental hydration with staleness](0004-incremental-hydration-with-staleness.md)
- [0005 — Transactional boundary on competitions](0005-transactional-boundary-on-competitions.md)
- [0006 — Scope `pull-new` to ongoing containers only](0006-ongoing-only-pull-new.md)
- [0007 — Per-round ingestion: rounds, stages, ascents](0007-per-round-ingestion.md)
