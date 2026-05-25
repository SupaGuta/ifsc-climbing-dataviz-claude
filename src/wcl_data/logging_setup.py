"""Logging setup.

- File log at logs/wcl-data.log captures everything WARNING and above (plain format).
- Console captures INFO/ERROR/CRITICAL but DROPS warnings — the file is the place to inspect those.
- Console output is colored by level (green INFO, red ERROR, …) via colorama,
  which falls back gracefully when stdout is piped or redirected.
"""
from __future__ import annotations

import logging

from colorama import Fore, Style, init as colorama_init

from .config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "wcl-data.log"

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
        if short_name.startswith("wcl_data."):
            short_name = short_name[len("wcl_data."):]
        elif short_name == "wcl_data":
            short_name = "wcl_data"
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


def configure(level: int = logging.INFO, *, verbose: bool = False, quiet: bool = False) -> None:
    """Configure root logger. Idempotent.

    Verbosity matrix (console; the WARN-and-above file log at logs/wcl-data.log
    is unaffected):

      default      -> INFO + ERROR + CRITICAL    (WARN hidden, file-only)
      verbose=True -> INFO + WARN + ERROR + CRIT (everything)
      quiet=True   -> ERROR + CRITICAL only      (no INFO chatter)

    `verbose` and `quiet` are mutually exclusive at the CLI layer; if both
    arrive here `quiet` wins (errors-only is the more conservative choice).
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
    if quiet:
        ch.setLevel(logging.ERROR)
    else:
        ch.setLevel(level)
    ch.setFormatter(_ColorFormatter())
    if not verbose and not quiet:
        # Quiet already silences WARN by raising the level; the filter is only
        # needed for the default path (level=INFO, WARN should not appear).
        ch.addFilter(_NoWarning())

    root.setLevel(min(level, logging.WARNING))
    root.addHandler(fh)
    root.addHandler(ch)
