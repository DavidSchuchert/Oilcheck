from datetime import date, timedelta

from heizoel import db
from heizoel.decision import stats
from tests.test_engine import make_history

TODAY = date(2026, 10, 16)


def _user():
    db.execute("INSERT INTO users (id, email, name, password_hash) VALUES (1, 'a@b.de', 'A', 'x')")
    db.execute(
        "INSERT INTO profiles (user_id, plz, region, tank_liter, fill_percent, fill_date, "
        "annual_consumption_liter) VALUES (1, '80331', '80', 3000, 40, ?, 2500)",
        (TODAY.isoformat(),))


def _prices(rows):
    db.executemany("INSERT INTO reference_prices (date, source, ct_per_liter) "
                   "VALUES (?, 'import', ?)", rows)


def test_purchase_stats(tmp_db):
    _user()
    assert stats.purchase_stats(1)["n"] == 0
    _prices([("2026-03-01", 90.0), ("2026-06-01", 80.0)])
    db.execute("INSERT INTO purchases (user_id, date, liter, ct_per_liter, dealer) "
               "VALUES (1, '2026-03-03', 2000, 92.0, 'A')")  # Referenz vom 1.3.
    db.execute("INSERT INTO purchases (user_id, date, liter, ct_per_liter, dealer) "
               "VALUES (1, '2026-06-01', 1000, 78.0, 'B')")
    s = stats.purchase_stats(1)
    assert s["n"] == 2 and s["total_liter"] == 3000
    assert s["purchases"][0]["delta_ct"] == 2.0 and s["purchases"][0]["delta_eur"] == 40.0
    assert s["purchases"][1]["delta_eur"] == -20.0
    assert s["total_delta_eur"] == 20.0
    assert s["total_eur"] == 2620.0 and s["avg_ct"] == round(2620 * 100 / 3000, 2)


def test_purchase_stats_no_reference(tmp_db):
    _user()
    db.execute("INSERT INTO purchases (user_id, date, liter, ct_per_liter) "
               "VALUES (1, '2026-03-03', 1000, 92.0)")
    p = stats.purchase_stats(1)["purchases"][0]
    assert p["reference_ct"] is None and p["delta_ct"] is None


def test_user_backtest(tmp_db):
    _user()
    assert stats.user_backtest(1, TODAY) is None
    hist = make_history(TODAY - timedelta(days=3 * 365), 3 * 365 + 1)
    _prices([(d.isoformat(), p) for d, p in hist])
    stats._BACKTEST_CACHE.clear()
    bt = stats.user_backtest(1, TODAY)
    assert bt is not None
    assert 2.9 <= bt["years"] <= 3.1
    assert "Cent je Liter" in bt["text"] and "letzten Drücker" in bt["text"]
    assert stats.user_backtest(1, TODAY) is bt
