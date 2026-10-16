"""Gemeinsame Fixtures. Jeder Test bekommt eine frische SQLite-Datei im tmp_path."""

from __future__ import annotations

import pytest

from heizoel import db
from heizoel.config import settings


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", path)
    db.init_db()
    try:
        from heizoel.decision import stats

        stats._BACKTEST_CACHE.clear()
    except ImportError:
        pass
    return path
