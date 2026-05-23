# Operations

Procedures for keeping the system healthy: credential rotation, log
management, recovery from failures, backups.

This is the **what-to-do-periodically** folder. For per-command "I ran X
and got Y, what now" → see
[`../cli-cookbook/troubleshooting.md`](../cli-cookbook/troubleshooting.md)
instead. The split:

- **cookbook/troubleshooting:** triggered by a specific failure ("this run
  errored, fix it")
- **operations/:** procedures and reference ("how does auth work, when do
  I refresh, how do I back up")

## Pages

- [auth.md](auth.md) — IFSC credential rotation, the `auth` command, what
  401/403 mean
- [logs.md](logs.md) — where logs go, what's at each level, manual cleanup
  (no rotation built in)
- [recovery.md](recovery.md) — recovering from killed runs, partial
  ingest, schema concerns
- [backup.md](backup.md) — snapshotting the SQLite warehouse, `.env`
  hygiene, CSV exports as portable backup

## Quick reference

| Situation | Page |
|---|---|
| API starts 401-ing | [auth.md](auth.md) |
| `logs/ifsc-data.log` is getting big | [logs.md](logs.md) |
| Run killed mid-`pull-new` | [recovery.md](recovery.md) |
| Need to test a risky change | [backup.md](backup.md) |
