# Authentication

The IFSC public API requires a CSRF token and a session cookie. Both are
stored in `.env` (gitignored) as `IFSC_CSRF_TOKEN` and
`IFSC_SESSION_COOKIE`.

**They rotate.** Expect to refresh every few months — sooner if the IFSC
changes its session policy.

## When to refresh

Whenever `refresh` / `pull-new` / `hydrate` starts failing with
`HTTP 401 Unauthorized` or `HTTP 403 Forbidden`. Look in
`logs/ifsc-data.log` for lines like:

```
WARNING ifsc_data.api.client: Fetch failed for /athletes/1234: HTTP 401 Unauthorized
```

Because the API client treats 4xx as permanent (see
[ADR 0003](../decisions/0003-selective-4xx-skip-retry.md)), a credentials
expiry shows up as a **silent storm of dropped rows**, not a hard error.
`status` will reveal the symptom: hydrated counts plateau at far below
total counts.

## How to refresh

```bash
python -m ifsc_data auth
```

What this does, from
[`src/ifsc_data/api/credentials.py`](https://github.com/SupaGuta/ifsc-climbing-dataviz-claude/blob/main/src/ifsc_data/api/credentials.py):

1. Plain GET to `https://ifsc.results.info`.
2. Regex-extracts the `<meta name="csrf-token" content="...">` value.
3. Picks up the session cookie from the response's `Set-Cookie` header
   (any cookie whose name contains `session`).
4. Rewrites `IFSC_CSRF_TOKEN=` and `IFSC_SESSION_COOKIE=` in `.env`,
   preserving every other line, comment, and ordering. Appends either
   key if missing.

No JS execution, no login flow. The IFSC landing page exposes everything
needed to authenticated subsequent API calls.

## Useful flags

### `--dry-run`

Print what would be written, don't touch `.env`:

```bash
python -m ifsc_data auth --dry-run
```

Output shape:

```
Fetched fresh credentials from https://ifsc.results.info
  CSRF token:     a1b2c3d4e5f6g7h8... (88 chars)
  Session cookie: _ifsc_results_session=... (123 chars)

--dry-run: not writing to .env. Lines that would be written:
  IFSC_CSRF_TOKEN=<full token>
  IFSC_SESSION_COOKIE=<full cookie>
```

Useful for verifying that fetch works before committing to a rewrite, or
when you want to paste credentials into a different file.

### `--env-file PATH`

Target a non-default `.env`:

```bash
python -m ifsc_data auth --env-file /tmp/alt.env
```

Useful when running against multiple deployments or when scripting auth
refresh outside the repo.

## After refresh

Re-run whatever was failing:

```bash
python -m ifsc_data pull-new
```

The new credentials are picked up on the next `load_settings()` call
(every CLI invocation). No daemon to restart.

## What if `auth` itself fails

Two failure modes:

### `RuntimeError: Could not find <meta name="csrf-token">`

The IFSC site layout has changed. The CSRF meta tag is no longer at the
expected location.

**Workaround:** paste credentials manually. Open
`https://ifsc.results.info` in a browser, open DevTools → Network, refresh
the page, find the request to `/api/v1/...`, copy:

- `X-Csrf-Token` request header → `IFSC_CSRF_TOKEN`
- `Cookie` request header value → `IFSC_SESSION_COOKIE`

Paste into `.env` (overwrite the two lines). Then open an issue so the
`_CSRF_META_RE` regex in `src/ifsc_data/api/credentials.py` can be updated.

### `RuntimeError: No session-like cookie returned`

The IFSC stopped naming its session cookie with `session` in the name.

**Workaround:** same DevTools paste. Then open an issue to update the
cookie-name match.

## Operational notes

- **Credentials are sensitive enough to keep out of git** — `.env` is
  gitignored and should stay that way. See
  [backup.md](backup.md) for `.env` hygiene.
- **The `--dry-run` output prints the full token to stdout**, so don't
  pipe it to a shared log when debugging.
- **No multi-user model.** The CSRF token is tied to a single anonymous
  session; there's no per-user auth on the IFSC public API. Multiple
  developers can use independent `.env` files without conflict.
