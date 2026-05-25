# Custom queries

Drop-in SQL recipes for ad-hoc questions. Run them with either:

```bash
sqlite3 data/wcl.sqlite "<query>"
```

or in Python:

```python
import sqlite3
conn = sqlite3.connect("data/wcl.sqlite")
for row in conn.execute("<query>"):
    print(row)
```

For schema reference see [`../data-dictionary/`](../data-dictionary/README.md).
For pre-joined CSV exports of common views see [exports.md](exports.md).

## Row counts and coverage

```sql
-- Counts per table
SELECT 'seasons' AS t, COUNT(*) FROM seasons
UNION ALL SELECT 'leagues', COUNT(*) FROM leagues
UNION ALL SELECT 'season_leagues', COUNT(*) FROM season_leagues
UNION ALL SELECT 'events', COUNT(*) FROM events
UNION ALL SELECT 'competitions', COUNT(*) FROM competitions
UNION ALL SELECT 'athletes', COUNT(*) FROM athletes
UNION ALL SELECT 'results', COUNT(*) FROM results;

-- Athlete profile coverage
SELECT
  COUNT(*)                                          AS total,
  COUNT(birthday)                                    AS with_birthday,
  COUNT(height)                                      AS with_height,
  COUNT(photo_url)                                   AS with_photo,
  ROUND(100.0 * COUNT(birthday) / COUNT(*), 1)      AS pct_birthday
FROM athletes WHERE last_fetched_at IS NOT NULL;
```

## Recent events

```sql
-- Most recent 20 events
SELECT name, city, country, date_start
FROM events
WHERE date_start IS NOT NULL
ORDER BY date_start DESC
LIMIT 20;

-- Events in the last 90 days (note the upper bound: events table contains
-- future-seeded entries like Paralympic Games LA28, which leak in without it)
SELECT name, city, country, date_start
FROM events
WHERE date_start >= date('now', '-90 days')
  AND date_start <= date('now')
ORDER BY date_start DESC;
```

## Athlete leaderboards

```sql
-- Athletes with the most top-3 finishes (all-time, all disciplines)
SELECT a.firstname || ' ' || a.lastname AS athlete, a.country,
       COUNT(*) AS top3_count
FROM results r
JOIN athletes a ON r.athlete_id = a.id
WHERE r.rank BETWEEN 1 AND 3
GROUP BY a.id
ORDER BY top3_count DESC
LIMIT 20;

-- Most wins (rank = 1) per discipline
SELECT d.name AS discipline,
       a.firstname || ' ' || a.lastname AS athlete,
       a.country,
       COUNT(*) AS wins
FROM results r
JOIN competitions c ON r.competition_id = c.id
JOIN disciplines d ON c.discipline_id = d.id
JOIN athletes a ON r.athlete_id = a.id
WHERE r.rank = 1
GROUP BY d.id, a.id
HAVING wins >= 5
ORDER BY d.name, wins DESC;
```

## Country breakdowns

Both `events` and `athletes` carry **two** country columns: `country` (raw
federation code — mixes ISO3 with IFSC variants like `GER`, `SUI`, `INA`,
`IRI`, `MAS`, `SIN`) and `country_iso3` (canonical ISO 3166-1 alpha-3).
For aggregations, **prefer `country_iso3`** — otherwise Germany splits
across `GER` (IFSC) and `DEU` (ISO3), Indonesia across `INA` and `IDN`,
etc. See [ADR 0008](../decisions/0008-country-iso3-sibling-column.md).

```sql
-- Number of athletes per country (top 20) — ISO3-clean
SELECT country_iso3, COUNT(*) AS n
FROM athletes
WHERE country_iso3 IS NOT NULL
GROUP BY country_iso3
ORDER BY n DESC
LIMIT 20;

-- Number of events hosted per country — ISO3-clean
SELECT country_iso3, COUNT(*) AS event_count
FROM events
WHERE country_iso3 IS NOT NULL
GROUP BY country_iso3
ORDER BY event_count DESC
LIMIT 20;

-- Federation-of-record view (useful for IFSC-style podium summaries) —
-- preserves GER/SUI/INA/...
SELECT country, COUNT(*) AS n
FROM athletes
WHERE country IS NOT NULL
GROUP BY country
ORDER BY n DESC
LIMIT 20;
```

## Season summaries

```sql
-- Events per season
SELECT s.year, COUNT(*) AS event_count
FROM seasons s
LEFT JOIN events e ON e.season_id = s.id
GROUP BY s.id
ORDER BY s.year DESC;

-- Competitions per discipline per season (current year)
SELECT d.name AS discipline, COUNT(*) AS comp_count
FROM seasons s
JOIN events e ON e.season_id = s.id
JOIN competitions c ON c.event_id = e.id
JOIN disciplines d ON c.discipline_id = d.id
WHERE s.year = (SELECT MAX(year) FROM seasons)
GROUP BY d.id
ORDER BY comp_count DESC;
```

## Staleness / freshness checks

```sql
-- Oldest hydrated rows per table (find stale data)
SELECT 'events' AS t, MIN(last_fetched_at) AS oldest FROM events WHERE last_fetched_at IS NOT NULL
UNION ALL SELECT 'competitions', MIN(last_fetched_at) FROM competitions WHERE last_fetched_at IS NOT NULL
UNION ALL SELECT 'athletes', MIN(last_fetched_at) FROM athletes WHERE last_fetched_at IS NOT NULL;

-- How many rows are stale by more than 30 days
SELECT COUNT(*) FROM athletes
WHERE last_fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days');
```

## Paraclimbing slices

```sql
-- All paraclimbing events
SELECT name, city, country, date_start
FROM events
WHERE is_paraclimbing = 1
ORDER BY date_start DESC;

-- Paraclimbing results by category
SELECT cat.name AS category, COUNT(*) AS result_count
FROM results r
JOIN competitions c ON r.competition_id = c.id
JOIN events e ON c.event_id = e.id
JOIN categories cat ON c.category_id = cat.id
WHERE e.is_paraclimbing = 1
GROUP BY cat.id
ORDER BY result_count DESC;
```

**Note:** for paraclimbing, `events.is_paraclimbing` is the authoritative
flag (use it for any per-competition or per-result filter). The
athletes-level proxy is `athletes.paraclimbing_sport_class IS NOT NULL`
— still heuristic, but the only signal on the athlete row itself. See
[athletes.md](../data-dictionary/athletes.md) and
[ADR 0009](../decisions/0009-athletes-payload-expansion.md).

## Tips

- **Always join via local `id`, not `ifsc_id`.** The latter isn't unique for
  competitions (see [competitions.md](../data-dictionary/competitions.md)).
- **`date_start` is TEXT in `YYYY-MM-DD`** — comparisons (`>`, `<`, `BETWEEN`)
  work correctly because the format is lexicographically sortable.
- **`last_fetched_at` is TEXT in ISO-8601 UTC** — same trick, same caveat.
- **Reference data lookups are cheap** — disciplines (5 rows), categories
  (~70 rows), leagues (~15 rows) — don't worry about JOIN cost.
