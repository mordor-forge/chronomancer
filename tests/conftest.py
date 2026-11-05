"""Make the plugin's core package importable without Hermes."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from change_windows.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "data.db"

    def connect():
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    return Store(connect)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data.db"
