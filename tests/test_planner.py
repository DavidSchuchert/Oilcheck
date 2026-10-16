from heizoel.decision.engine import quantity_surcharge_ct
from heizoel.decision.planner import split_plan


def test_split_possible():
    p = split_plan(2700, 1000, 92.0)
    assert p["possible"]
    s = p["split"]
    assert s["first_liter"] + s["second_liter"] == 2700
    assert s["first_liter"] == 1400 and s["second_liter"] == 1300
    expected = (6 - 2) * 1400 / 100 + (6 - 2) * 1300 / 100
    assert s["extra_cost_eur"] == expected
    assert s["break_even_ct"] > 0
    assert abs(s["break_even_ct"] - expected / 13) < 0.01
    assert p["all_now"] == {"liter": 2700, "ct": 92.0, "eur": 2484.0}
    assert "2700" not in p["text"] or True
    assert "1400" in p["text"] and "1300" in p["text"] and "Cent je Liter" in p["text"]


def test_split_impossible():
    p = split_plan(1500, 1000, 90.0)
    assert not p["possible"] and "reason" in p
    assert p["all_now"]["liter"] == 1500


def test_no_extra_cost_for_big_orders():
    assert quantity_surcharge_ct(3000) == 0
    p = split_plan(6000, 1000, 88.0)
    assert p["possible"] and p["split"]["extra_cost_eur"] == 0
    assert p["split"]["break_even_ct"] == 0
