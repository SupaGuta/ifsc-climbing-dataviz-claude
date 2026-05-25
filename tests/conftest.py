"""Shared pytest fixtures."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def _redirect_log_file(tmp_path_factory):
    """Send `logs/wcl-data.log` to a per-session tmp dir.

    Without this, every test that calls `cli.main(...)` triggers
    `logging_setup.configure()` which registers a FileHandler at
    `REPO_ROOT/logs/wcl-data.log` — creating a real artefact in the
    project tree and leaking that handler across all subsequent tests
    (because `configure()` short-circuits once root has handlers).
    """
    from wcl_data import logging_setup

    mp = pytest.MonkeyPatch()
    tmp_log_dir = tmp_path_factory.mktemp("wcl_logs")
    mp.setattr(logging_setup, "LOG_DIR", tmp_log_dir)
    mp.setattr(logging_setup, "LOG_FILE", tmp_log_dir / "wcl-data.log")
    # Drop any handlers a prior test session left on the root logger so
    # `configure()` actually runs and binds to the patched LOG_FILE.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    yield
    mp.undo()


@pytest.fixture
def fixture(request) -> Any:
    """Load a JSON fixture by its stem name, e.g. fixture('athletes-id')."""
    def _load(name: str) -> Any:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return _load


@pytest.fixture
def memory_db():
    """Yield a connection to an in-memory SQLite DB with the schema applied."""
    from wcl_data.db.schema import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    yield conn
    conn.close()
