import json
import math
from dataclasses import replace
from datetime import date, timedelta

from heizoel import co2
from heizoel.decision.engine import quantity_surcharge_ct, recommend, result_to_inputs_json
from heizoel.models import Co2Step, DecisionInputs, Profile

TODAY = date(2026, 10, 16)
NO_STEPS: list[Co2Step] = []


def make_history(start: date, days: int, base: float = 90, amplitude: float = 6,
                trend_per_year: float = 3) -> list[tuple[date, float]]:
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        # Hoch im Dezember/Januar: Cosinus mit Maximum um den 1. Januar
        doy = d.timetuple().tm_yday
        season = amplitude * math.cos(2 * math.pi * (doy - 1) / 365.25)
        out.append((d, base + season + trend_per_year * i / 365.25))
    return out


def profile(**kw) -> Profile:
    base = {"user_id": 1, "plz": "80331", "region": "80", "tank_liter": 3000, "fill_percent": 40.0,
                "fill_date": TODAY, "annual_consumption_liter": 2500, "min_order_liter": 1000,
                "lead_days": 10, "notify_channel": "none", "threshold_percentile": 30}
    base.update(kw)
    return Profile(**base)


def inputs(history=None, ref=90.0, prof=None, steps=NO_STEPS, today=TODAY) -> DecisionInputs:
    return DecisionInputs(
        today=today, reference_ct=ref, region_offset_ct=1.5, region_offset_n=4,
        history=history if history is not None else [], profile=prof or profile(),
        co2_steps=steps)


def hist_with_percentile(target_ref: float, n=400):
    """Historie, in der target_ref bei ca. Perzentil 50 liegt."""
    return [(TODAY - timedelta(days=n - i), 80.0 + (i % 21)) for i in range(n)]


def uniform_hist(n=400):
    # Werte 50..149 gleichmäßig
    return [(TODAY - timedelta(days=n - i), 50.0 + (i % 100)) for i in range(n)]


def test_tank_full():
    r = recommend(inputs(prof=profile(fill_percent=90)))
    assert r.rule == "tank_full" and r.verdict == "wait"
    assert r.need_liter == 0 and r.eur_per_ct == 0
    assert "90 %" in r.reason


def test_range_critical():
    r = recommend(inputs(history=uniform_hist(), prof=profile(fill_percent=12)))
    assert r.rule == "range_critical" and r.verdict == "buy_now"
    assert r.need_liter >= 1000
    assert r.latest_order_date == TODAY


def test_cheap_and_space():
    r = recommend(inputs(history=uniform_hist(), ref=55.0, prof=profile(fill_percent=40)))
    assert r.percentile_365 is not None and r.percentile_365 <= 30
    assert r.rule == "cheap_and_space" and r.verdict == "buy_now"
    assert "ct/L" in r.reason


def test_before_step():
    steps = [Co2Step(date(2026, 1, 1), 55, None, "fixed"),
             Co2Step(TODAY + timedelta(days=30), 85, None, "fixed")]
    r = recommend(inputs(history=uniform_hist(), ref=100.0, steps=steps,
                         prof=profile(fill_percent=50)))
    assert r.next_step_days == 30
    assert r.next_step_ct is not None and r.next_step_ct > 2.0
    assert r.rule == "before_step" and r.verdict == "buy_before_step"
    assert r.next_step_eur_for_need == r.next_step_ct * r.need_liter / 100


def test_before_step_draft_wording():
    steps = [Co2Step(date(2026, 1, 1), 55, None, "fixed"),
             Co2Step(TODAY + timedelta(days=30), 85, None, "draft")]
    r = recommend(inputs(history=uniform_hist(), ref=100.0, steps=steps,
                         prof=profile(fill_percent=50)))
    assert "voraussichtlich" in r.reason


def test_before_step_not_when_tank_full_enough():
    steps = [Co2Step(date(2026, 1, 1), 55, None, "fixed"),
             Co2Step(TODAY + timedelta(days=30), 85, None, "fixed")]
    r = recommend(inputs(history=uniform_hist(), ref=100.0, steps=steps,
                         prof=profile(fill_percent=75)))
    assert r.rule != "before_step"


def test_expensive_and_time():
    r = recommend(inputs(history=uniform_hist(), ref=140.0, prof=profile(fill_percent=60)))
    assert r.percentile_365 >= 70
    assert r.rule == "expensive_and_time" and r.verdict == "wait"


def test_no_history():
    r = recommend(inputs(history=[], prof=profile(fill_percent=60)))
    assert r.rule == "no_history" and r.verdict == "watch"
    assert r.percentile_365 is None and r.season_deviation_ct is None
    assert "Preishistorie" in r.reason


def test_watch_names_next_condition():
    r = recommend(inputs(history=uniform_hist(), ref=100.0, prof=profile(fill_percent=60)))
    assert r.rule == "watch" and r.verdict == "watch"
    assert "Perzentil ≤ 30" in r.reason


def test_split_suggestion():
    r = recommend(inputs(history=uniform_hist(), ref=95.0, prof=profile(fill_percent=40)))
    assert 30 <= r.percentile_365 <= 60
    assert r.split_suggestion is not None
    assert r.split_suggestion.startswith("Alternative: jetzt ")
    r2 = recommend(inputs(history=uniform_hist(), ref=95.0, prof=profile(fill_percent=60)))
    assert r2.split_suggestion is None


def test_surcharge_and_need():
    r = recommend(inputs(prof=profile(fill_percent=40, tank_liter=3000)))
    # Bestand ~1200, Platz 2850-1200 = 1650 -> 1600 L -> +4 ct
    assert r.need_liter == 1600
    assert r.quantity_surcharge_ct == 4.0
    assert r.personal_ct == 90.0 + 1.5 + 4.0
    assert r.eur_per_ct == 16.0


def test_season_deviation_with_history():
    hist = make_history(TODAY - timedelta(days=6 * 365), 6 * 365 - 1)
    r = recommend(inputs(history=hist, ref=hist[-1][1]))
    assert r.season_deviation_ct is not None
    short = recommend(inputs(history=hist[-300:], ref=90))
    assert short.season_deviation_ct is None


def test_percentile_windows():
    hist = make_history(TODAY - timedelta(days=6 * 365), 6 * 365 - 1)
    r = recommend(inputs(history=hist, ref=hist[-1][1]))
    assert r.percentile_365 is not None and r.percentile_730 is not None
    # steigender Trend: aktueller Preis ist am oberen Ende
    assert r.percentile_365 > 60


def test_json_serializable():
    hist = make_history(TODAY - timedelta(days=3 * 365), 3 * 365 - 1)
    steps = co2.DEFAULT_STEPS
    inp = inputs(history=hist, ref=95.0, steps=steps)
    r = recommend(inp)
    json.dumps(r.details)
    data = json.loads(result_to_inputs_json(inp, r))
    assert data["history_n"] == len(hist)
    assert "history" not in data
    assert data["reference_ct"] == 95.0
    assert data["profile"]["tank_liter"] == 3000
    assert data["details"]["rule"] == r.rule


def test_next_step_none_without_steps():
    r = recommend(inputs(steps=NO_STEPS))
    assert r.next_step_date is None and r.next_step_ct is None


def test_replace_profile_works():
    # Absicherung: frozen dataclass, replace funktioniert für Backtest-Nutzung
    p = replace(profile(), fill_percent=10)
    assert p.fill_percent == 10


def test_quantity_surcharge_ct_table():
    assert quantity_surcharge_ct(1000) == 6.0
    assert quantity_surcharge_ct(1499) == 6.0
    assert quantity_surcharge_ct(1500) == 4.0
    assert quantity_surcharge_ct(1999) == 4.0
    assert quantity_surcharge_ct(2000) == 2.0
    assert quantity_surcharge_ct(2999) == 2.0
    assert quantity_surcharge_ct(3000) == 0.0
