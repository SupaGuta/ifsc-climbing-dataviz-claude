# CLI cookbook

Task-oriented recipes for the `python -m ifsc_data` CLI. Grouped by intent —
find the section that matches what you're trying to do.

For the CLI reference (all flags, all options) see the root
[`README.md`](https://github.com/SupaGuta/world-climbing-lab/blob/main/README.md). For the *why* behind each command's
behavior, see [`../architecture/`](../architecture/overview.md) and the
[ADRs](../decisions/README.md).

## Sections

- [setup.md](setup.md) — first-time install, credentials, initial populate
- [daily-use.md](daily-use.md) — `pull-new`, `refresh`, `hydrate`, smoke
  testing, tuning concurrency
- [exports.md](exports.md) — `export` to CSV, then filter with **pandas** or
  **sqlite3 CLI** ("women's lead only," "Chamonix 2019," "top-10 by country")
- [custom-queries.md](custom-queries.md) — SQL one-liners against the
  warehouse for ad-hoc questions
- [troubleshooting.md](troubleshooting.md) — "I ran X and got Y, what now"
  for common failure modes

## Decision matrix

| Want to … | See |
|-----------|-----|
| Get started from a fresh clone | [setup.md](setup.md) |
| Catch new IFSC content | [daily-use.md](daily-use.md) (`pull-new`) |
| Refresh stale rows on the 30-day cadence | [daily-use.md](daily-use.md) (`refresh`) |
| Force-refresh everything | [daily-use.md](daily-use.md) (`refresh --stale-days 0`) |
| Export data to CSV | [exports.md](exports.md) |
| Filter exports without modifying the package | [exports.md](exports.md) |
| Answer "how many X are in Y?" | [custom-queries.md](custom-queries.md) |
| Diagnose 401/403/slow runs/dropped rows | [troubleshooting.md](troubleshooting.md) |
| Recover from a killed run | [../operations/recovery.md](../operations/recovery.md) |
| Refresh credentials | [../operations/auth.md](../operations/auth.md) |
