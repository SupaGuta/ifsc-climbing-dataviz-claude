"""Logging setup.

- File log at logs/ifsc-data.log captures everything WARNING and above (plain format).
- Console captures INFO/ERROR/CRITICAL but DROPS warnings — the file is the place to inspect those.
- Console output is colored by level (green INFO, red ERROR, …) via colorama,
  which falls back gracefully when stdout is piped or redirected.
"""
from __future__ import annotations

import logging

from colorama import Fore, Style, init as colorama_init

from .config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "ifsc-data.log"

colorama_init()


_LEVEL_COLOR = {
    logging.DEBUG: Fore.BLUE,
    logging.INFO: Fore.GREEN,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
}


class _ColorFormatter(logging.Formatter):
    """`HH:MM:SS  LEVEL  logger.short.name: message` with the level coloured."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        short_name = record.name
        if short_name.startswith("ifsc_data."):
            short_name = short_name[len("ifsc_data."):]
        elif short_name == "ifsc_data":
            short_name = "ifsc_data"
        color = _LEVEL_COLOR.get(record.levelno, "")
        return (
            f"{Style.DIM}{ts}{Style.RESET_ALL}  "
            f"{color}{record.levelname:<5}{Style.RESET_ALL}  "
            f"{Style.DIM}{short_name}{Style.RESET_ALL}: "
            f"{record.getMessage()}"
        )


class _NoWarning(logging.Filter):
    """Drop WARNING-level records (they still go to the file log)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno != logging.WARNING


def configure(level: int = logging.INFO, *, verbose: bool = False) -> None:
    """Configure root logger. Idempotent.

    `verbose=True` keeps warnings on the console (useful for debugging).
    Default behaviour hides them from stdout; they always go to the file log.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(file_formatter)

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(_ColorFormatter())
    if not verbose:
        ch.addFilter(_NoWarning())

    root.setLevel(min(level, logging.WARNING))
    root.addHandler(fh)
    root.addHandler(ch)
