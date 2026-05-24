# Logs

Logging setup lives in
[`src/wcl_data/logging_setup.py`](https://github.com/SupaGuta/world-climbing-lab/blob/main/src/wcl_data/logging_setup.py).
Two sinks, configured once per process by `logging_setup.configure()`:

## What goes where

| Sink | Level | Format | Where |
|---|---|---|---|
| Console | INFO + above, with WARNING **hidden** unless `-v` | Colored (`HH:MM:SS LEVEL logger: msg`) | stdout |
| File | WARNING + above | Plain (`asctime levelname name: msg`) | `logs/wcl-data.log` |

The console deliberately suppresses WARNING by default so the run output
stays readable. Pass `-v` / `--verbose` *before* the subcommand to keep
warnings on screen:

```bash
python -m wcl_data -v pull-new
```

The file log catches them regardless, so a post-mortem is always possible.

## When to read the file log

- After a `pull-new` / `refresh` finishes with `failed` counts > 0.
- After a credentials rotation (look for 4xx WARNINGs around the boundary).
- When `status` shows hydrated counts well below total — find the warning
  about which rows were dropped.

```bash
# Last 20 warnings
tail -n 20 logs/wcl-data.log

# Just 4xx drops (likely auth issues)
grep '4[0-9][0-9]' logs/wcl-data.log | tail
```

## No automatic rotation

The file handler is a plain `logging.FileHandler` — **no size cap, no time
rotation**. The file grows monotonically. In practice this is not a real
problem (a year of weekly `pull-new` runs produces a few MB), but if you
run a lot of `refresh --stale-days 0` or hit a retry storm, the file can
grow into the tens of MB.

### Manual cleanup recipe

When `logs/wcl-data.log` gets uncomfortably large, delete or rotate it
manually. The next CLI invocation will recreate it:

**PowerShell (Windows):**

```powershell
Remove-Item logs\wcl-data.log
# Or: rotate, keep the last entry
Move-Item logs\wcl-data.log logs\wcl-data.$(Get-Date -Format 'yyyyMMdd').log
```

**bash / zsh (macOS / Linux):**

```bash
rm logs/wcl-data.log
# Or: rotate
mv logs/wcl-data.log logs/wcl-data.$(date +%Y%m%d).log
```

**Periodic via Task Scheduler (Windows) / cron (Unix):**

Schedule one of the above commands to run weekly or monthly. There's no
in-app helper for this — it's a deliberate "we'd rather not silently lose
log data."

### If you want true rotation

The standard library provides `logging.handlers.RotatingFileHandler`
(size-based) and `TimedRotatingFileHandler` (time-based). Swapping the
plain `FileHandler` in `src/wcl_data/logging_setup.py:configure()` for
one of those would add rotation. This isn't done by default because:

1. The volume is small enough in practice to not warrant the moving
   parts.
2. Rotation behavior is a deployment-specific call (size threshold?
   retention?).

If you decide you want it, the change is a few lines and could be
exposed via an env var (e.g. `WCL_LOG_ROTATE=size:10MB:5`). Open an issue
to discuss before patching.

## Log levels in the codebase

A rough map of what each level means:

- **INFO** — phase-level progress: "Hydrating 50 athletes." Always
  emitted, always on console.
- **WARNING** — recoverable single-item failure: 4xx drops, retry
  attempts. File-only by default; `-v` to surface.
- **ERROR** — give-up after `max_retries`: "Giving up on 3 items after
  2 retries: [/athletes/X, ...]." Always on console.
- **CRITICAL** — not currently used by the package.

`log.exception(...)` calls inside fetchers (caught parse errors) log
with full traceback at ERROR level — these end up on console *and* in
the file.

## Programmatic configuration

If you're calling the package from a notebook or your own script and want
to control logging yourself, **call `configure()` before any package
function**:

```python
import logging
from wcl_data import logging_setup

logging_setup.configure(level=logging.DEBUG, verbose=True)
```

It's idempotent — re-calling on an already-configured root logger is a
no-op.

To skip the package's logging entirely (e.g. you want your own root
handler), just don't call `configure()`. The package modules each use
`log = logging.getLogger(__name__)`, so standard logging propagation
applies.
