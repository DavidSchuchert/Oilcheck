import sqlite3
from datetime import date

import pytest

from heizoel import db
from heizoel.decision import learning, service

TODAY = date(2026, 10, 16)


def _user(annual=2500, tank=3000):
    db.execute("INSERT INTO users (id, email, name, password_hash) VALUES (1, 'a@b.de', 'A', 'x')")
    db.execute(
        "INSERT INTO profiles (user_id, plz, region, tank_liter, fill_percent, fill_date, "
        "annual_consumption_liter) VALUES (1, '80331', '80', ?, 50, '2026-01-01', ?)",
        (tank, annual))


def _fill(d, pct):
    db.execute("INSERT INTO fill_levels (user_id, date, fill_percent) VALUES (1, ?, ?)", (d, pct))


def test_simple_interval(tmp_db):
    _user()
    _fill("2026-01-01", 100)
    _fill("2026-03-01", 80)
    res = learning.learn_consumption(1, TODAY)
    assert res.annual_liter == pytest.approx(600 / 0.31, rel=0.01)
    assert res.days == 59 and res.confidence == pytest.approx(59 / 365 / 3)
    assert "Messungen" in res.note
    row = db.query_one("SELECT * FROM profiles WHERE user_id = 1")
    assert row["learned_annual_liter"] == pytest.approx(res.annual_liter)
    assert row["learned_at"] == TODAY.isoformat() and row["learned_n"] == 1


def test_purchase_in_interval(tmp_db):
    _user()
    _fill("2026-01-01", 20)
    _fill("2026-03-01", 80)
    db.execute("INSERT INTO purchases (user_id, date, liter, ct_per_liter) "
               "VALUES (1, '2026-02-01', 2400, 90)")
    res = learning.learn_consumption(1, TODAY)
    # 600 - 2400 + 2400 = 600 verbraucht
    assert res.annual_liter == pytest.approx(600 / 0.31, rel=0.01)


def test_measurement_error_discarded(tmp_db):
    _user()
    _fill("2026-01-01", 50)
    _fill("2026-03-01", 80)  # Anstieg ohne Kauf
    res = learning.learn_consumption(1, TODAY)
    assert res.annual_liter is None and res.confidence == 0
    row = db.query_one("SELECT * FROM profiles WHERE user_id = 1")
    assert row["learned_annual_liter"] is None


def test_too_close_and_summer_share_discarded(tmp_db):
    _user()
    _fill("2026-01-01", 100)
    _fill("2026-01-10", 95)  # < 14 Tage
    assert learning.learn_consumption(1, TODAY).annual_liter is None
    _fill("2026-06-20", 90)  # Intervall ab 1.1.: ok
    assert learning.learn_consumption(1, TODAY).annual_liter is not None


def test_effective_annual_liter():
    assert learning.effective_annual_liter(2500, None, 0.8) == 2500
    assert learning.effective_annual_liter(2500, 2000.0, 0.5) == 2250
    assert learning.effective_annual_liter(2500, 2000.0, 0.0) == 2500
    assert learning.effective_annual_liter(2500, 9000.0, 1.0) == 5000
    assert learning.effective_annual_liter(2500, 100.0, 1.0) == 1250


def test_load_profile_uses_mix(tmp_db):
    _user()
    assert service.load_profile(1).annual_consumption_liter == 2500
    db.execute("UPDATE profiles SET learned_annual_liter = 2000, learned_confidence = 0.5 "
               "WHERE user_id = 1")
    assert service.load_profile(1).annual_consumption_liter == 2250
    row = db.query_one("SELECT annual_consumption_liter FROM profiles")
    assert row[0] == 2500


def test_learn_all(tmp_db):
    _user()
    _fill("2026-01-01", 100)
    _fill("2026-03-01", 80)
    assert learning.learn_all(TODAY) == 1


def test_migration_adds_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.executescript("DROP TABLE profiles; CREATE TABLE profiles (user_id INTEGER PRIMARY KEY, "
                       "plz TEXT, region TEXT, tank_liter INTEGER, fill_percent REAL, "
                       "fill_date TEXT, annual_consumption_liter INTEGER);")
    conn.commit()
    conn.close()
    with db.transaction() as c:
        db._migrate(c)
    with db.transaction() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(profiles)")}
    assert {"learned_annual_liter", "learned_days", "learned_n", "learned_at",
            "learned_confidence"} <= cols
