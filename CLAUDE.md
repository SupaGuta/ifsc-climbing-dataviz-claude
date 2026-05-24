# CLAUDE.md — `world-climbing-lab`

A Layer 0 ingestion package that pulls the World Climbing public competition
API (`ifsc.results.info`) into a single local SQLite warehouse at
`data/wcl.sqlite`. Long-term direction is machine learning on top of the
warehouse; sibling packages `wcl_analytics` / `wcl_ml` may land later.

## Read these first

Before re-deriving understanding from `src/`, check **[`docs/README.md`](docs/README.md)** —
a 36-file reference tree that already documents this codebase end-to-end:

- `docs/contributing.md` — dev setup, tests, code conventions, add-a-fetcher walkthrough
- `docs/architecture/` — system snapshot (edit in place as code changes)
- `docs/decisions/` — numbered ADRs 0001-0007, append-only
- `docs/data-dictionary/` — per-table column reference with coverage stats
- `docs/cli-cookbook/`, `docs/python-api/`, `docs/operations/` — task-oriented recipes

If a doc contradicts the code, trust the code and fix the doc.

## Entry points

| Action | Command |
|--------|---------|
| Run the CLI | `python -m wcl_data <cmd>` (or the `wcl-data` shell command) |
| Available `<cmd>` | `init`, `auth`, `refresh`, `pull-new`, `hydrate`, `status`, `export` |
| Run tests | `pytest -q` (~1s; HTTP fully mocked, in-memory SQLite) |
| Inspect the warehouse | `python -m wcl_data status` — never edit `data/wcl.sqlite` by hand |

First-time bootstrap: `auth` → `init` → `refresh` (~45-90 min for a full backfill).
Everyday: `pull-new` (~30-60s on a steady-state warehouse).

## Naming convention

The package was renamed from `ifsc_data` → `wcl_data` on 2026-05-24 (the
federation rebranded IFSC → World Climbing). Sweep rules:

- **Code/config identifiers:** `wcl_data` package, `WCL_*` env vars, `data/wcl.sqlite`
- **Prose:** "World Climbing"
- **Two preserved exceptions** (do *not* sweep): the API URL `ifsc.results.info`
  and the column name `ifsc_id` (mirrors the upstream API's own field name)

## Guardrails

- **Never write `.env` or commit credentials without explicit user authorization.**
  Credentials can be auto-refreshed via `python -m wcl_data auth` when needed.
- **Never edit `data/wcl.sqlite` by hand.** Use the CLI; the schema is owned
  by `src/wcl_data/db/schema.py` and applied idempotently.

## Windows tooling notes

This repo's primary dev environment is Windows / PowerShell 5.1.

- PowerShell has no `&&` chaining (use `; if ($?) { … }`), no ternary, and
  `2>&1` on native exes can falsely flag `$?` — see the system prompt's
  PowerShell section for the full list.
- `python -c "…"` snippets containing `\"` escapes are Bash-safe but break
  the PowerShell parser. Use the **Bash** tool for those.
- `print()` of upstream payloads with diacritics crashes on `cp1252` consoles.
  Set `PYTHONIOENCODING=utf-8` or run `chcp 65001` first.

## ADR convention

`docs/decisions/` is append-only and numbered (currently 0001-0007). If a
design decision changes, add a new ADR rather than rewriting an old one.
Small dated footnote-style notes on an existing ADR are fine when the
context shifted but the core decision still stands — see the 2026-05-24
notes on ADR 0001 and ADR 0004 for the established pattern.
