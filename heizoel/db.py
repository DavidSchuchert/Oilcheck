"""SQLite-Zugriff. Bewusst dünn: eine Verbindung pro Aufruf, Row-Factory, Schema-Anwendung.

Verwendung:
    from heizoel import db
    rows = db.query("SELECT * FROM users WHERE id = ?", (1,))
    db.execute("INSERT INTO ...", (...))
    with db.transaction() as conn: conn.execute(...)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from heizoel.config import PROJECT_ROOT, settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _db_path() -> Path:
    path = settings.db_path
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# (Tabelle, Spalte, ALTER-Statement) für Spalten, die CREATE TABLE IF NOT EXISTS in alten DBs nicht nachträgt.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("profiles", "offer_liter", "ALTER TABLE profiles ADD COLUMN offer_liter INTEGER"),
    ("profiles", "learned_annual_liter", "ALTER TABLE profiles ADD COLUMN learned_annual_liter REAL"),
    ("profiles", "learned_days", "ALTER TABLE profiles ADD COLUMN learned_days INTEGER"),
    ("profiles", "learned_n", "ALTER TABLE profiles ADD COLUMN learned_n INTEGER"),
    ("profiles", "learned_at", "ALTER TABLE profiles ADD COLUMN learned_at TEXT"),
    ("profiles", "learned_confidence", "ALTER TABLE profiles ADD COLUMN learned_confidence REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Fehlende Spalten ergänzen, ohne Daten zu verlieren."""
    for table, column, ddl in MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def init_db() -> None:
    """Schema idempotent anwenden und Stammdaten (CO2-Stufen) einspielen, falls leer."""
    with transaction() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
    from heizoel.co2 import seed_default_steps

    seed_default_steps()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Führt ein Statement aus und gibt lastrowid zurück."""
    with transaction() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


def executemany(sql: str, seq: list[tuple] | list[dict]) -> None:
    with transaction() as conn:
        conn.executemany(sql, seq)
