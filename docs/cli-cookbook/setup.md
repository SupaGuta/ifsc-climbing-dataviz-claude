# Setup

From an empty clone to a fully-populated warehouse.

## 1. Install

Python 3.12+ required.

```bash
git clone <repo>
cd ifsc-climbing-dataviz-claude
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

For notebook work add the optional extras:

```bash
pip install -e ".[notebook]"        # jupyterlab + pandas
```

## 2. Create the `.env`

```bash
cp .env.example .env
```

The file ships with sensible defaults for everything except the two
credential lines:

```
IFSC_CSRF_TOKEN=
IFSC_SESSION_COOKIE=
```

Leave those blank and let the `auth` command fill them in.

## 3. Fetch credentials

```bash
python -m ifsc_data auth
```

This makes a single plain GET to `https://ifsc.results.info`, parses the
CSRF meta tag and session cookie out of the response, and writes them to
`.env` while preserving every other line.

Want to inspect before writing? Pass `--dry-run`:

```bash
python -m ifsc_data auth --dry-run
```

For deeper details (cadence, what failures look like, alternate `.env`
paths) see [`../operations/auth.md`](../operations/auth.md).

## 4. Create the database

```bash
python -m ifsc_data init
```

Creates `data/ifsc.sqlite` with the schema from
[`src/ifsc_data/db/schema.py`](https://github.com/SupaGuta/ifsc-climbing-dataviz-claude/blob/main/src/ifsc_data/db/schema.py).
Idempotent — running it on an existing DB verifies tables/indexes exist but
never deletes data.

## 5. Populate

```bash
python -m ifsc_data pull-new
```

Walks the entity graph (seasons → season_leagues → events → competitions →
athletes), inserting and hydrating as it goes. **Takes 3–5 minutes** from
an empty database; subsequent runs only catch what's new.

Watch the console: each phase prints its row count and progress. Logs at
WARNING level are hidden from console by default; add `-v` to see them:

```bash
python -m ifsc_data -v pull-new
```

Either way, all WARNINGs go to `logs/ifsc-data.log` for post-mortem.

## 6. Verify

```bash
python -m ifsc_data status
```

Should print row counts close to the data-dictionary's "typical size"
numbers:

```
DB: .../data/ifsc.sqlite
table                      rows   hydrated
seasons                      38         38
leagues                      15          -
season_leagues              450        450
disciplines                   5          -
categories                   70          -
events                     1401       1401
competitions               5825       5825
athletes                  14923      14922
results                  148139          -
```

Off by a row or two on athletes is normal (one known permanent 404).
Significantly fewer hydrated rows than total rows on a hydratable table →
the run was killed early; just re-run `pull-new` or `refresh`.

## What's next

- [daily-use.md](daily-use.md) — the everyday commands
- [exports.md](exports.md) — get CSVs for downstream tools
- [../python-api/README.md](../python-api/README.md) — call the package
  programmatically instead of via CLI
