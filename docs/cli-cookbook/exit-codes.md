# Exit codes

`python -m wcl_data` (and the `wcl-data` shell command) returns a
distinct exit code per failure category, so wrapper scripts and CI
jobs can branch on the failure type without parsing stderr.

| Code | Meaning | Typical cause |
|------|---------|---------------|
| `0` | Success | Command completed without an unhandled exception. |
| `1` | Generic error | Unhandled exception (parser bug, IO failure not covered by codes 3-5). Look at the traceback. |
| `2` | Usage error | Bad command-line input — unknown subcommand, unknown view passed to `export <view>`, unknown entity passed to `hydrate`. Surfaced via `argparse` exits (`SystemExit(2)`) or explicit returns. |
| `3` | DB lock / IO | `sqlite3.OperationalError`. Most often `database is locked` (another `wcl-data` process or a notebook holds an open write transaction); occasionally disk full or read-only filesystem. |
| `4` | Missing / expired credentials | `WCL_CSRF_TOKEN` or `WCL_SESSION_COOKIE` is empty in `.env`. Fix: `python -m wcl_data auth`. |
| `5` | Upstream API failure | The run aborted mid-batch — typically `AuthFailureAbort` after the consecutive-401/403 threshold tripped (credentials valid at startup but rejected during the run; rotate with `python -m wcl_data auth`). |

## Examples

### CI: redrive once on a DB-lock contention, fail loudly on auth

```bash
python -m wcl_data pull-new
code=$?
if [ "$code" -eq 3 ]; then
  sleep 60
  python -m wcl_data pull-new
elif [ "$code" -eq 4 ] || [ "$code" -eq 5 ]; then
  echo "auth failure — manual rotation needed"
  exit 1
fi
```

### PowerShell: branch on the four user-actionable codes

```powershell
python -m wcl_data refresh
switch ($LASTEXITCODE) {
    0 { Write-Host "OK" }
    3 { Write-Host "Database locked — close other writers and retry" }
    4 { Write-Host "Run: python -m wcl_data auth" ; & python -m wcl_data auth }
    5 { Write-Host "Mid-run auth failure — credentials rotated under us" }
    default { Write-Host "Unhandled error (code $LASTEXITCODE) — see logs/wcl-data.log" }
}
```

## See also

- [troubleshooting.md](troubleshooting.md) — symptom → diagnosis for each code's
  underlying cause.
- [`../operations/auth.md`](../operations/auth.md) — refreshing credentials.
- [`../decisions/`](../decisions/README.md) — ADRs covering the retry policy
  and silent-fail-mode background.
