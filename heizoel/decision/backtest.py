"""Backtest: Regel-Strategie gegen Notkauf-Strategie auf historischen Referenzpreisen.

Performance: Das Percentile-/Saison-Rechnen in engine.recommend() ist O(n) pro Aufruf.
recommend() wird deshalb nicht täglich, sondern alle RECOMMEND_EVERY_DAYS Tage aufgerufen
(Entscheidungen fallen realistisch ohnehin nicht täglich), sofern nicht kritisch: ist die
Reichweite knapp (< lead_days + 14), wird sofort entschieden. Alle Käufe passieren
sofort (keine Lieferzeit), Preis = Referenz + Mengenaufschlag, Regionaloffset 0.
"""

from __future__ import annotations

from datetime import date, timedelta

from heizoel.decision import consumption
from heizoel.decision.engine import compute_need_liter, quantity_surcharge_ct, recommend
from heizoel.models import Co2Step, DecisionInputs, Profile

RECOMMEND_EVERY_DAYS = 3


def _summary(liter: float, cost_ct: float, purchases: int) -> dict:
    return {
        "liter": round(liter),
        "eur": round(cost_ct / 100, 2),
        "avg_ct": round(cost_ct / liter, 2) if liter else 0.0,
        "purchases": purchases,
    }


def run_backtest(history: list[tuple[date, float]], tank_liter: int, annual_liter: int,
                 min_order_liter: int, lead_days: int, co2_steps: list[Co2Step],
                 threshold_percentile: int = 30, start: date | None = None) -> dict:
    empty = {"liter": 0, "eur": 0.0, "avg_ct": 0.0, "purchases": 0}
    if not history:
        return {"rule": dict(empty), "emergency": dict(empty),
                "saving_eur": 0.0, "saving_pct": 0.0, "days": 0}
    history = sorted(history)
    if start is None:
        start = history[0][0] + timedelta(days=365)
    end = history[-1][0]

    stock = {"rule": tank_liter * 0.5, "emergency": tank_liter * 0.5}
    liter = {"rule": 0.0, "emergency": 0.0}
    cost = {"rule": 0.0, "emergency": 0.0}
    count = {"rule": 0, "emergency": 0}

    price_by_day = dict(history)
    last_price = None
    days = 0
    day = start
    n_hist = 0
    while day <= end:
        # letzter bekannter Preis (Lücken überbrücken)
        if day in price_by_day:
            last_price = price_by_day[day]
        if last_price is None:
            day += timedelta(days=1)
            continue
        while n_hist < len(history) and history[n_hist][0] < day:
            n_hist += 1
        known = history[:n_hist]

        def profile_for(s: float, d: date = day) -> Profile:
            return Profile(
                user_id=0, plz="00000", region="00", tank_liter=tank_liter,
                fill_percent=s / tank_liter * 100, fill_date=d,
                annual_consumption_liter=annual_liter, min_order_liter=min_order_liter,
                lead_days=lead_days, notify_channel="none",
                threshold_percentile=threshold_percentile)

        # Strategie A: Regel
        rng = consumption.range_days(stock["rule"], annual_liter, day, 0.10, tank_liter)
        critical = rng < lead_days + 14
        if critical or (days % RECOMMEND_EVERY_DAYS == 0):
            res = recommend(DecisionInputs(
                today=day, reference_ct=last_price, region_offset_ct=0.0, region_offset_n=0,
                history=known, profile=profile_for(stock["rule"]), co2_steps=co2_steps))
            if res.verdict in ("buy_now", "buy_before_step") and res.need_liter > 0:
                q = res.need_liter
                liter["rule"] += q
                cost["rule"] += q * (last_price + quantity_surcharge_ct(q))
                count["rule"] += 1
                stock["rule"] += q

        # Strategie B: Notkauf
        rng_e = consumption.range_days(stock["emergency"], annual_liter, day, 0.10, tank_liter)
        if rng_e < lead_days + 3:
            q = compute_need_liter(tank_liter, stock["emergency"], min_order_liter)
            if q > 0:
                liter["emergency"] += q
                cost["emergency"] += q * (last_price + quantity_surcharge_ct(q))
                count["emergency"] += 1
                stock["emergency"] += q

        use = consumption.daily_liter(annual_liter, day)
        for k, v in stock.items():
            stock[k] = max(0.0, v - use)
        days += 1
        day += timedelta(days=1)

    rule = _summary(liter["rule"], cost["rule"], count["rule"])
    emerg = _summary(liter["emergency"], cost["emergency"], count["emergency"])
    saving_ct = emerg["avg_ct"] - rule["avg_ct"] if rule["liter"] and emerg["liter"] else 0.0
    saving_eur = saving_ct * rule["liter"] / 100
    saving_pct = saving_ct / emerg["avg_ct"] * 100 if emerg["avg_ct"] else 0.0
    return {"rule": rule, "emergency": emerg, "saving_eur": round(saving_eur, 2),
            "saving_pct": round(saving_pct, 2), "days": days}
