# Data dictionary

Per-table column reference for the SQLite warehouse at `data/ifsc.sqlite`. The
schema's source of truth is [`src/ifsc_data/db/schema.py`](../../src/ifsc_data/db/schema.py);
this folder explains what each column *means*, what's in it, and what's
missing.

For the *why* of the schema design (single SQLite file, staleness model,
transactional boundary on competitions) see
[`../architecture/database-and-schema.md`](../architecture/database-and-schema.md)
and the relevant ADRs.

## Tables

- [seasons](seasons.md) — top of the entity tree
- [season-leagues](season-leagues.md) — (season × league) junction
- [events](events.md) — competition events with city/country/date
- [competitions](competitions.md) — (event × discipline × category) triples
- [athletes](athletes.md) — athlete profiles
- [results](results.md) — (competition × athlete × rank)
- [reference-tables](reference-tables.md) — `leagues`, `disciplines`, `categories`

## Conventions

### Identity columns

Every entity carries two IDs:

- **`id` INTEGER PRIMARY KEY** — local row PK assigned by SQLite. Used by
  foreign keys.
- **`ifsc_id` INTEGER UNIQUE** — the IFSC API's ID, used to build URLs like
  `/seasons/{ifsc_id}`. Always populated.

Foreign keys reference `id`, never `ifsc_id`. The one place this rule bends:
`competitions.ifsc_id` is *not* globally unique across events, which is why
that table uses `UNIQUE (event_id, ifsc_id)` instead of a unique constraint
on `ifsc_id` alone.

### Units

- **`height`, `arm_span`** — centimetres (INTEGER). Self-reported on IFSC; see
  coverage caveats per table.
- **`gender`** — INTEGER. `0` = male, `1` = female, NULL = unknown / not in
  the data.
- **`birthday`** — TEXT in `YYYY-MM-DD` (ISO date), or NULL.
- **`date_start`, `date_end`** — TEXT in `YYYY-MM-DD`. No time component.
- **`last_fetched_at`** — TEXT in `YYYY-MM-DDTHH:MM:SSZ` (ISO-8601 UTC with
  literal `Z`). Lexicographically sortable.
- **Booleans (`is_paraclimbing`)** — INTEGER `0` / `1`. SQLite has no native
  bool.
- **Country** — TEXT, ISO 3166-1 alpha-3 code (`"FRA"`, `"USA"`, `"JPN"`),
  uppercase. NULL when undetermined.

### NULL semantics

NULL means *we don't know*, not *zero* or *false*. For hydratable tables,
`last_fetched_at IS NULL` specifically means the row is a discovery skeleton —
inserted by a parent fetcher but never filled in. The next `hydrate` /
`refresh` / `pull-new` will pick it up.

For non-identity fields (height, birthday, photo_url, city, country): NULL
almost always means *the IFSC API doesn't have it either*, not that the
parser dropped it. See the per-table pages for coverage stats.

### Recomputing coverage

The coverage percentages cited in each table page are accurate as of
2026-05-23 but will drift. To recompute for any column:

```bash
python -c "import sqlite3; c = sqlite3.connect('data/ifsc.sqlite'); \
  total = c.execute('SELECT COUNT(*) FROM athletes WHERE last_fetched_at IS NOT NULL').fetchone()[0]; \
  populated = c.execute('SELECT COUNT(*) FROM athletes WHERE last_fetched_at IS NOT NULL AND birthday IS NOT NULL').fetchone()[0]; \
  print(f'{populated}/{total} = {100*populated/total:.1f}%')"
```

Adapt by swapping the table and column names. Always condition on
`last_fetched_at IS NOT NULL` so unhydrated skeletons don't deflate the
denominator.

## What's *not* in the warehouse

- **Round-by-round attempts** (qualification routes, top boulders, semi-final
  ascents). The API exposes these per-competition but they're not ingested.
- **Judge / route-setter data.** Not consumed.
- **Live ranking deltas.** Each competition stores its final ranking only.
- **Anything older than IFSC's API coverage.** The earliest seasons date from
  the late 1990s; pre-API archive results aren't here.
