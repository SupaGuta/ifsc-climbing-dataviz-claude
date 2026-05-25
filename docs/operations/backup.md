# Backup

The warehouse is **one file** — `data/wcl.sqlite`. Backing it up is a
copy. Restoring is a copy back.

## Snapshot before risky operations

Before any of:

- `refresh --stale-days 0` (~45-90 min, touches everything)
- Schema-level changes (editing `src/wcl_data/db/schema.py`)
- Bulk SQL operations against the warehouse
- Testing a new fetcher against the live API

…snapshot:

**PowerShell (Windows):**

```powershell
Copy-Item data\wcl.sqlite data\wcl.$(Get-Date -Format 'yyyyMMdd').sqlite
```

**bash / zsh:**

```bash
cp data/wcl.sqlite data/wcl.$(date +%Y%m%d).sqlite
```

Restore by copying back:

```bash
cp data/wcl.20260523.sqlite data/wcl.sqlite
```

Snapshots named `data/wcl.*` (the recommended pattern above) **are gitignored**
by `.gitignore`'s `data/wcl.*` rule — they won't be accidentally committed.
If you store a snapshot elsewhere or under a different name, verify with
`git check-ignore -v <path>` before assuming it's safe.

## `.env` hygiene

`.env` contains live API credentials and **must not be committed**. The
project's `.gitignore` excludes it. Things that are easy to get wrong:

- Don't commit `.env` from a different branch (`git checkout` won't
  touch a gitignored file, but `git stash --include-untracked` will).
- Don't paste `.env` contents into PR descriptions, issue comments, or
  chat. Tokens are full-length in the file; treat them as secrets.
- If you need to share a credential snapshot with a collaborator, use a
  proper secrets channel — not the repo.

If credentials are accidentally exposed (committed by mistake, pasted
publicly): rotate them by re-running `python -m wcl_data auth`. The new
session cookie invalidates the old one.

## CSV exports as a portable secondary backup

If you want a backup that's readable without SQLite (or that survives
SQLite-version changes), export to CSV:

```bash
python -m wcl_data export
```

Writes eight CSVs to `data/exports/` with timestamped filenames. The
`results_*.csv` is the big one — it's fully denormalized and can
reconstruct most analytical questions on its own.

Trade-offs vs a SQLite snapshot:

| Snapshot (`.sqlite`) | CSV exports |
|---|---|
| Bit-perfect, every row preserved | Pre-joined view — some redundancy, some columns omitted |
| Restored with a file copy | Restored by re-ingesting (slow) |
| Binary format | Plain text, greppable, opens anywhere |
| ~600 MB (post-ADR 0007 per-round tables) | ~40 MB for `results`; per-round views (`round_results`, `ascents` opt-in) are the bulk |
| One file | Eight files |

For most "I want yesterday's data back" cases, the `.sqlite` snapshot is
the right answer. For "I want this data to outlive the project," CSVs are
worth keeping alongside.

## What's *not* backed up by `data/`

- **Notebooks** are git-tracked; commits are your backup.
- **`logs/`** are gitignored. They're regenerated on every run, so loss
  is fine.
- **`.env`** is gitignored and irreplaceable in the sense that the
  cookie can't be re-issued exactly — but `auth` will get you a fresh
  one whenever you need it.

## Restore-from-scratch story

If everything's lost (machine wipe, repo re-clone, etc.):

1. `git clone <repo>`
2. `pip install -e ".[dev]"`
3. `cp .env.example .env`
4. `python -m wcl_data auth` → fills in fresh credentials
5. `python -m wcl_data init` → recreates schema
6. `python -m wcl_data refresh` → repopulates from the live API (~45-90 min for a full backfill, incl. per-round tables)

Total time from zero: ~45-90 minutes (most of it the refresh). `pull-new`
alone won't work here — it only touches ongoing containers and would leave
historical seasons and per-round tables empty. The World Climbing API is
the source of truth; the local warehouse is reproducible.

Snapshots and CSV exports are conveniences, not load-bearing.
