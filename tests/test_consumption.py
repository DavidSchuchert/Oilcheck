from datetime import date

from heizoel.decision import consumption as c


def test_weights_sum_to_100():
    assert sum(c.MONTH_WEIGHTS.values()) == 100


def test_daily_liter_january():
    assert abs(c.daily_liter(2500, date(2026, 1, 15)) - 2500 * 0.17 / 31) < 1e-9


def test_consumed_between_full_year_equals_annual():
    assert abs(c.consumed_between(2500, date(2026, 1, 1), date(2027, 1, 1)) - 2500) < 1e-6


def test_consumed_between_empty_interval():
    assert c.consumed_between(2500, date(2026, 5, 1), date(2026, 5, 1)) == 0


def test_estimate_current_never_negative():
    cur = c.estimate_current_liter(3000, 10, date(2026, 1, 1), 2500, date(2026, 6, 1))
    assert cur == 0.0


def test_estimate_current_same_day():
    assert c.estimate_current_liter(3000, 50, date(2026, 10, 16), 2500, date(2026, 10, 16)) == 1500


def test_range_days_full_tank_october_plausible():
    d = c.range_days(3000, 2500, date(2026, 10, 1), 0.10, 3000)
    assert 250 < d < 500


def test_range_days_empty_and_cap():
    assert c.range_days(100, 2500, date(2026, 1, 1), 0.10, 3000) == 0.0
    assert c.range_days(1e9, 2500, date(2026, 1, 1)) == 730


def test_range_days_without_tank_goes_to_zero():
    d = c.range_days(100, 2500, date(2026, 1, 1))
    assert 0 < d < 20
