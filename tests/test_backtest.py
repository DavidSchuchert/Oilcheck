import time
from datetime import date

from heizoel import co2
from heizoel.decision.backtest import run_backtest
from tests.test_engine import make_history


def test_backtest_three_years():
    hist = make_history(date(2023, 1, 1), 3 * 365)
    res = run_backtest(hist, 3000, 2500, 1000, 10, co2.DEFAULT_STEPS)
    for k in ("rule", "emergency", "saving_eur", "saving_pct", "days"):
        assert k in res
    for s in ("rule", "emergency"):
        assert set(res[s]) == {"liter", "eur", "avg_ct", "purchases"}
        assert res[s]["purchases"] >= 1
    assert res["days"] > 300


def test_backtest_speed_five_years():
    hist = make_history(date(2021, 1, 1), 5 * 365)
    t = time.time()
    run_backtest(hist, 3000, 2500, 1000, 10, co2.DEFAULT_STEPS)
    assert time.time() - t < 10
