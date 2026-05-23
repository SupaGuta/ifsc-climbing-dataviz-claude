# Exports & filtering

The `export` command dumps pre-joined SQL views to timestamped CSVs in
`data/exports/`. The CLI doesn't ship filtering flags — recipes below filter
in **pandas** (for notebook/Python users) or **sqlite3 CLI** (for shell
users). Pick whichever fits your workflow; both yield equivalent results.

## Export everything

```bash
python -m ifsc_data export
```

Writes six CSVs to `data/exports/`:

```
seasons_2026-05-23T140530Z.csv
leagues_2026-05-23T140530Z.csv
events_2026-05-23T140530Z.csv
competitions_2026-05-23T140530Z.csv
athletes_2026-05-23T140530Z.csv
results_2026-05-23T140530Z.csv
```

The `results_*.csv` is the big one — fully denormalized with event name,
city, country, date, discipline, category, gender, athlete name, athlete
country, and rank. Most analyses can work off this single file.

## Export one view

```bash
python -m ifsc_data export results
python -m ifsc_data export athletes --output-dir /tmp/csv
```

Choices: `seasons`, `leagues`, `events`, `competitions`, `athletes`,
`results`. View definitions live in [`src/ifsc_data/exporter.py`](../../src/ifsc_data/exporter.py).

---

## Recipe: women's lead results only

> **Heads up — paraclimbing categories bundled.** The filters below match
> *any* category whose name contains "Women" (the gender field gets `1`
> from the regex in `season_leagues.hydrate`). That includes paraclimbing
> classes like `Women RP2`, `Women AU2`, `Women B3`. For Olympic-style
> open women only, add `e.is_paraclimbing = 0` to the SQL filter (or
> `results["event_country"]`-style exclusion in pandas after joining
> through events).

**Pandas:**

```python
import pandas as pd

results = pd.read_csv("data/exports/results_2026-05-23T140530Z.csv")
events = pd.read_csv("data/exports/events_2026-05-23T140530Z.csv")
results = results.merge(
    events[["event_ifsc_id", "is_paraclimbing"]],
    on="event_ifsc_id", how="left",
)
wl = results[
    (results["gender"] == "female")
    & (results["discipline"] == "lead")
    & (results["is_paraclimbing"] == 0)        # drop this line to include paraclimbing women
]
wl.to_csv("data/exports/results_women_lead.csv", index=False)
```

**sqlite3 CLI:**

```bash
sqlite3 data/ifsc.sqlite <<'SQL' > data/exports/results_women_lead.csv
.headers on
.mode csv
SELECT e.name AS event_name, s.year, e.city, e.country,
       cat.name AS category, a.firstname || ' ' || a.lastname AS athlete,
       a.country AS athlete_country, r.rank
FROM results r
JOIN competitions c ON r.competition_id = c.id
JOIN events e ON c.event_id = e.id
LEFT JOIN seasons s ON e.season_id = s.id
JOIN disciplines d ON c.discipline_id = d.id
JOIN categories cat ON c.category_id = cat.id
JOIN athletes a ON r.athlete_id = a.id
WHERE cat.gender = 1
  AND d.name = 'lead'
  AND e.is_paraclimbing = 0     -- drop this line to include paraclimbing women
ORDER BY e.date_start DESC, r.rank;
SQL
```

Notes: discipline names are lowercased in the warehouse (see
[reference-tables.md](../data-dictionary/reference-tables.md)).
`cat.gender = 1` means women (see
[athletes.md](../data-dictionary/athletes.md) for the gender encoding).
`e.is_paraclimbing` is authoritative — the per-athlete flag is heuristic.

---

## Recipe: all events in Chamonix

**Pandas:**

```python
events = pd.read_csv("data/exports/events_2026-05-23T140530Z.csv")
chamonix = events[events["city"] == "Chamonix"]
chamonix.sort_values("date_start", ascending=False)
```

**sqlite3 CLI:**

```bash
sqlite3 data/ifsc.sqlite "SELECT name, date_start FROM events WHERE city = 'Chamonix' ORDER BY date_start DESC;"
```

---

## Recipe: top-10 finishes per country in 2024

**Pandas:**

```python
results = pd.read_csv("data/exports/results_2026-05-23T140530Z.csv")
top10 = results[(results["season_year"] == 2024) & (results["rank"] <= 10)]
by_country = top10.groupby("athlete_country").size().sort_values(ascending=False)
by_country.head(20)
```

**sqlite3 CLI:**

```bash
sqlite3 data/ifsc.sqlite <<'SQL'
.headers on
.mode column
SELECT a.country, COUNT(*) AS top10_count
FROM results r
JOIN competitions c ON r.competition_id = c.id
JOIN events e ON c.event_id = e.id
JOIN seasons s ON e.season_id = s.id
JOIN athletes a ON r.athlete_id = a.id
WHERE s.year = 2024 AND r.rank <= 10
GROUP BY a.country
ORDER BY top10_count DESC
LIMIT 20;
SQL
```

---

## Recipe: paraclimbing events only

The authoritative flag is `events.is_paraclimbing` (NOT the heuristic field
on `athletes`).

**Pandas:**

```python
events = pd.read_csv("data/exports/events_2026-05-23T140530Z.csv")
para = events[events["is_paraclimbing"] == 1]
```

**sqlite3 CLI:**

```bash
sqlite3 data/ifsc.sqlite "SELECT name, city, country, date_start FROM events WHERE is_paraclimbing = 1 ORDER BY date_start DESC LIMIT 50;"
```

For full paraclimbing *results*, join through `competitions` → `events`:

```sql
SELECT a.firstname, a.lastname, cat.name AS category, r.rank
FROM results r
JOIN competitions c ON r.competition_id = c.id
JOIN events e ON c.event_id = e.id
JOIN categories cat ON c.category_id = cat.id
JOIN athletes a ON r.athlete_id = a.id
WHERE e.is_paraclimbing = 1;
```

---

## Tips

- **Filename pattern is `<view>_<UTC>.csv`.** Re-running `export` never
  overwrites a prior dump. If you want a stable filename, write your own
  with pandas after reading.
- **Exports are point-in-time.** They don't update if you re-run `pull-new`
  afterward — re-export to refresh.
- **For one-off questions, skip the export.** Just run a SQL query straight
  against `data/ifsc.sqlite` — see [custom-queries.md](custom-queries.md).
- **CSV gender values are `"male"` / `"female"`**, not 0/1, because the
  export views use a `CASE` to translate. Internal queries against the
  SQLite use the integer encoding (`cat.gender = 0` for men).
