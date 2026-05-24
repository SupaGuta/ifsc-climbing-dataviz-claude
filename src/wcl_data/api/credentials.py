"""Auto-fetch World Climbing API credentials (CSRF token + session cookie).

A single plain GET to the World Climbing landing page surfaces both pieces:
  - the Rails CSRF token in a `<meta name="csrf-token" content="...">` tag
  - the session cookie in the `Set-Cookie` response header

No JS execution, no login flow. Useful when the manually-pasted credentials
in `.env` age out.
"""
from __future__ import annotations

import logging
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
    - If it exists: replace the existing WCL_CSRF_TOKEN / WCL_SESSION_COOKIE
      lines in place; preserve every other line, comment, and ordering. Append
      either key if it's missing.
    """
    if not env_path.exists():
        env_path.write_text(
            f"WCL_CSRF_TOKEN={csrf}\n"
            f"WCL_SESSION_COOKIE={cookie}\n",
            encoding="utf-8",
        )
        return

    original = env_path.read_text(encoding="utf-8").splitlines()
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

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
