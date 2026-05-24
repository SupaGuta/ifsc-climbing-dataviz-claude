"""Shared pytest fixtures."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
