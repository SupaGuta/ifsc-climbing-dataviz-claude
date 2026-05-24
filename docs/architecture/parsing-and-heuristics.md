# Parsing & heuristics

The World Climbing API exposes some data cleanly (athlete `firstname`, event
`local_start_date`) and some data only via free-text event names, ambiguous
field semantics, or fields whose meaning has drifted over years of API
evolution. This file is the list of places where the package guesses, and
the rules it follows when guessing.

The guiding rule is **"return NULL rather than wrong."** Downstream consumers
can detect NULL and decide what to do; they can't detect a quietly-misparsed
value.

## Event city + country (`src/wcl_data/parsers/event_location.py`)

Many older events don't carry city/country fields — the location lives in
the event *name*, e.g. `"IFSC Climbing World Cup - Chamonix (FRA) 2019"`.
`parse_city_country(event_name) -> (city, iso3)` extracts both with
conservative regex.

The four anchors it looks for, in priority order:

1. **`(XXX)` ISO3 in parentheses** — `(FRA)`, `(USA)`, `(JPN)`. Highest
   confidence.
2. **`XXX)` broken parenthesis** — `" CHN)"` (missing opening paren). The
   ISO3 is still distinctive enough to anchor.
3. **`(Country Name)`** — `"(France)"`, `"(New Caledonia)"`. Validated via
   `pycountry.countries.lookup` with a small override map for known aliases
   (`"china"` → `"CHN"`, `"hong kong"` → `"HKG"`, etc.).
4. **Country token before a year** — `"..., USA 1997"`. Last resort.

If no anchor matches, the function returns `(None, None)`. The caller
(`fetchers/events.py`) then falls back to the API's own `location` and
`country` fields if present.

The city half is a *segment-based* extraction: once an anchor is found, take
the text to the left, split on `,` / ` - `, and walk segments right-to-left
looking for the first non-empty, non-stopword, non-event-keyword chunk. Then
`postprocess_city` strips event-keyword noise (`"world cup"`, `"masters"`,
`"climbing"`), discipline blocks (`"(L)"`, `"(B)"`), discipline modifiers
(`"Speed Rock "`), US state suffixes (`"Boulder, Colorado"` → `"Boulder"`),
and Chinese province suffixes.

**Why so conservative:** the API has 1,400+ events with extremely varied
naming conventions across two decades. A more aggressive parser would
manufacture false cities (`"Boulder"` confused with the discipline, `"Rock
Junior"` as a city, …). Returning NULL on uncertain matches lets the
sibling-backfill pass (next section) recover most of them.

### Sibling backfill

Many events have a city but no country. `events.hydrate` accumulates a
`city_to_country` map from the rows in the current batch, then back-fills
country onto NULL-country rows whose city matches. There's also a
cross-batch backfill (`repo.backfill_event_country_from_siblings`) that does
the same lookup against existing DB rows after the batch completes. This
recovers most events whose city was parsed from the name but whose country
anchor was missing.

## Athlete paraclimbing status (`src/wcl_data/fetchers/athletes.py`)

The `is_paraclimbing` flag on athletes is a heuristic:

```python
is_paraclimbing=1 if data.get("paraclimbing_sport_class") is not None else 0,
```

I.e. an athlete is "paraclimbing" iff they have a sport-class field. This
matches the API's modelling but it's lossy: a paraclimbing athlete who
hasn't had a sport class assigned (rare, but happens) will be flagged as
non-paraclimbing.

If your downstream code needs authoritative paraclimbing status, cross-check
against `events.is_paraclimbing` (which comes from the API's
`is_paraclimbing_event` field on the *event*, not the athlete). The README
documents this caveat under "Notes / known limits."

## Athlete gender (`src/wcl_data/fetchers/athletes.py`)

The API returns `"male"` / `"female"` as a string. The repository stores it
as INTEGER for consistency with the categories table:

```python
gender_str = (data.get("gender") or "").lower()
gender = 0 if gender_str == "male" else (1 if gender_str == "female" else None)
```

Anything else (empty, unknown, future values) stores NULL. There is no third
category in the current data.

## Category gender (`src/wcl_data/fetchers/season_leagues.py`)

The API only labels categories as `"Lead Men"`, `"Boulder Women"`,
`"Youth A Male"`, etc. — gender is embedded in the name. The fetcher
regex-extracts it:

```python
_GENDER_RE = re.compile(r"\b(?P<g>men|male|women|female)\b", re.IGNORECASE)
```

For non-matching categories (paraclimbing classes like `"AL1"`, age-group
combinations) `gender` is NULL — same NULL-over-wrong principle.

## Discipline normalization

Discipline names are lowercased before insert (`discipline_name = parts[0].lower()`
in `season_leagues._ingest_d_cat`, `(d_cat.get("discipline_kind") or "").lower()`
in `events.hydrate`). The UNIQUE constraint on `disciplines.name` then
collapses `"Lead"` / `"lead"` / `"LEAD"` into one row.

## What's deliberately *not* a heuristic

- **Names, dates, IDs, ranks** — taken straight from the API. If the API
  returns garbage, we store garbage. Don't add cleanup that could mask an
  upstream change.
- **Athlete country** — the API has a clean country field (~99.99% coverage).
  No heuristic needed.
- **Event dates** — `local_start_date` / `local_end_date` are clean.

## Where the data is genuinely lossy (upstream side)

Recorded in the project notes for context, not bugs:

| Field             | Coverage | Why                                                      |
|-------------------|----------|----------------------------------------------------------|
| `birthday`        | ~51%     | Older athletes / privacy. API just doesn't have it.      |
| `photo_url`       | ~15%     | Many athletes never had a photo uploaded.                |
| `height`          | ~9%      | Self-reported field, mostly empty.                       |
| `arm_span`        | very low | Same as height.                                          |
| `events.country`  | ~96%     | Older events store location only in the name; backfilled from sibling rows where possible. |

If you find a `NULL` in production, it's almost always real, not a parse bug.
A regression test that asserts on coverage rates would be brittle for the
same reason — the API state changes.

## When to add new heuristics

The bar is: **there's a deterministic rule that's correct >99% of the time,
and the wrong-1% case fails closed (NULL or unchanged) rather than producing
plausible-but-wrong values.** If you can't satisfy both, leave the field
NULL and let downstream code handle it. The event-location parser is at the
edge of what's defensible; anything fuzzier belongs in a downstream
enrichment layer, not the ingestion package.
