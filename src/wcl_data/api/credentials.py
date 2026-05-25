"""Auto-fetch World Climbing API credentials (CSRF token + session cookie).

A single plain GET to the World Climbing landing page surfaces both pieces:
  - the Rails CSRF token in a `<meta name="csrf-token" content="...">` tag
  - the session cookie in the `Set-Cookie` response header

No JS execution, no login flow. Useful when the manually-pasted credentials
in `.env` age out.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

REFERER_URL = "https://ifsc.results.info"

_CSRF_META_RE = re.compile(
    r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchedCredentials:
    csrf_token: str
    session_cookie: str  # `"<name>=<value>"`, ready for the Cookie header


def fetch_credentials(url: str = REFERER_URL, *, timeout: int = 30) -> FetchedCredentials:
    """Fetch a fresh CSRF token + session cookie from the World Climbing landing page.

    Raises RuntimeError if either piece can't be parsed out of the response.
    """
    log.info("Fetching credentials from %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    csrf_match = _CSRF_META_RE.search(resp.text)
    if not csrf_match:
        raise RuntimeError(
            f"Could not find <meta name=\"csrf-token\"> in response from {url}. "
            "The site layout may have changed."
        )
    csrf_token = csrf_match.group(1)

    session = next(
        (c for c in resp.cookies if "session" in c.name.lower()),
        None,
    )
    if session is None:
        available = [c.name for c in resp.cookies] or "[]"
        raise RuntimeError(
            f"No session-like cookie returned by {url}. Got: {available}"
        )

    return FetchedCredentials(
        csrf_token=csrf_token,
        session_cookie=f"{session.name}={session.value}",
    )


def update_env_file(env_path: Path, csrf: str, cookie: str) -> None:
    """Update `WCL_CSRF_TOKEN` and `WCL_SESSION_COOKIE` in `env_path`.

    - If the file doesn't exist: create with just the two lines.
    - If it exists: copy the current bytes to `<env_path>.bak` (recovery file
      for a fat-fingered re-auth), then replace the existing WCL_CSRF_TOKEN /
      WCL_SESSION_COOKIE lines in place. Every other line, comment, and
      ordering is preserved; missing keys are appended.

    On POSIX the written file is `chmod 0o600` so a leaked dotfile can't be
    world-read; `os.chmod` is a no-op on Windows (NTFS perms model differs),
    so the call is gated by `os.name`.
    """
    if not env_path.exists():
        env_path.write_text(
            f"WCL_CSRF_TOKEN={csrf}\n"
            f"WCL_SESSION_COOKIE={cookie}\n",
            encoding="utf-8",
        )
        _restrict_env_perms(env_path)
        return

    # Byte-for-byte backup so the recovery file matches whatever the user
    # had — line endings, trailing newline, encoding. `.env.bak` is a
    # rolling one-deep history: a second re-auth overwrites the previous
    # backup with the now-stale token. Good enough for "I just clobbered
    # my creds, restore the previous file".
    bak_path = env_path.with_name(env_path.name + ".bak")
    bak_path.write_bytes(env_path.read_bytes())
    # Chmod the bak too — it carries the previous (still-valid until the
    # token actually expires) credentials, so leaving it world-readable
    # would defeat the 0o600 hardening on .env itself.
    _restrict_env_perms(bak_path)

    raw = env_path.read_text(encoding="utf-8")
    # Preserve the file's existing line ending (CRLF on Windows-checked-out
    # .env files, LF on POSIX) so this command doesn't silently normalize it.
    newline = "\r\n" if "\r\n" in raw else "\n"
    original = raw.splitlines()
    new_lines: list[str] = []
    seen = {"WCL_CSRF_TOKEN": False, "WCL_SESSION_COOKIE": False}

    for line in original:
        if line.startswith("WCL_CSRF_TOKEN="):
            new_lines.append(f"WCL_CSRF_TOKEN={csrf}")
            seen["WCL_CSRF_TOKEN"] = True
        elif line.startswith("WCL_SESSION_COOKIE="):
            new_lines.append(f"WCL_SESSION_COOKIE={cookie}")
            seen["WCL_SESSION_COOKIE"] = True
        else:
            new_lines.append(line)

    if not seen["WCL_CSRF_TOKEN"]:
        new_lines.append(f"WCL_CSRF_TOKEN={csrf}")
    if not seen["WCL_SESSION_COOKIE"]:
        new_lines.append(f"WCL_SESSION_COOKIE={cookie}")

    env_path.write_text(newline.join(new_lines) + newline, encoding="utf-8")
    _restrict_env_perms(env_path)


def _restrict_env_perms(env_path: Path) -> None:
    """`chmod 0o600` on POSIX; no-op on Windows.

    Split out so the gating logic is in one place and the create / overwrite
    paths in `update_env_file` stay focused on content. On NTFS, `os.chmod`
    only toggles the read-only bit — it does not map to a POSIX-style
    user-only access control, so calling it on Windows would be misleading
    rather than protective.
    """
    if os.name != "nt":
        os.chmod(env_path, 0o600)
