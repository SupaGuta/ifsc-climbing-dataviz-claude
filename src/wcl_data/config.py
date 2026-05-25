"""Runtime configuration, loaded from environment / `.env`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    csrf_token: str
    session_cookie: str
    referer: str
    max_workers: int
    connect_timeout: float
    read_timeout: float
    db_path: Path
    stale_days: int
    grace_days: int

    @property
    def api_headers(self) -> dict[str, str]:
        return {
            "X-Csrf-Token": self.csrf_token,
            "Referer": self.referer,
            "Cookie": self.session_cookie,
        }


def load_settings(*, require_credentials: bool = True) -> Settings:
    """Read settings from environment.

    With `require_credentials=True` (default) missing CSRF/cookie raises RuntimeError.
    With `require_credentials=False` the credentials default to empty strings,
    which is fine for commands that don't hit the API (init / status / export).

    `WCL_CONNECT_TIMEOUT` / `WCL_READ_TIMEOUT` are split so a slow DNS / TLS
    handshake fails fast (5s default) while a legitimately large per-result
    payload still has time to stream (120s default). The two map to the
    `(connect, read)` tuple `requests` accepts directly.
    """
    csrf = os.getenv("WCL_CSRF_TOKEN", "").strip()
    cookie = os.getenv("WCL_SESSION_COOKIE", "").strip()
    if require_credentials and (not csrf or not cookie):
        raise RuntimeError(
            "Missing WCL_CSRF_TOKEN or WCL_SESSION_COOKIE. "
            "Copy .env.example to .env and fill in credentials from DevTools on ifsc.results.info."
        )

    db_path = Path(os.getenv("WCL_DB_PATH", "data/wcl.sqlite"))
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    return Settings(
        csrf_token=csrf,
        session_cookie=cookie,
        referer=os.getenv("WCL_REFERER", "https://ifsc.results.info"),
        max_workers=int(os.getenv("WCL_MAX_WORKERS", "50")),
        connect_timeout=float(os.getenv("WCL_CONNECT_TIMEOUT", "5")),
        read_timeout=float(os.getenv("WCL_READ_TIMEOUT", "120")),
        db_path=db_path,
        stale_days=int(os.getenv("WCL_STALE_DAYS", "30")),
        grace_days=int(os.getenv("WCL_GRACE_DAYS", "15")),
    )
