"""Kaufregel: reine Funktion ohne DB-Zugriff. Spezifikation in PLAN.md, Abschnitt 4."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from heizoel import co2
from heizoel.decision import consumption
from heizoel.models import DecisionInputs, DecisionResult

FILL_TARGET = 0.95
MIN_VALUES_PERCENTILE = 60


def quantity_surcharge_ct(need_liter: int) -> float:
    if need_liter < 1500:
        return 6.0
    if need_liter < 2000:
        return 4.0
    if need_liter < 3000:
        return 2.0
    return 0.0


def compute_need_liter(tank_liter: int, current_liter: float, min_order_liter: int) -> int:
    """Bestellmenge: freier Platz bis 95 % Füllung, auf 100 L abgerundet; 0 wenn Mindestmenge nicht passt."""
    space = tank_liter * FILL_TARGET - current_liter
    if space < min_order_liter:
        return 0
    return max(min_order_liter, math.floor(space / 100.0) * 100)


def percentile(history: list[tuple[date, float]], today: date, days: int,
               value: float) -> float | None:
    start = today - timedelta(days=days)
    window = [v for d, v in history if start <= d < today]
    if len(window) < MIN_VALUES_PERCENTILE:
        return None
    return 100.0 * sum(1 for v in window if v <= value) / len(window)


def season_deviation(history: list[tuple[date, float]], today: date,
                     reference_ct: float) -> float | None:
    if len(history) < 500:
        return None
    by_year: dict[int, list[tuple[int, float]]] = {}
    for d, v in history:
        if today.year - 5 <= d.year < today.year:
            by_year.setdefault(d.year, []).append((d.month, v))
    deviations = []
    for vals in by_year.values():
        if len(vals) < 300:  # nicht vollständig
            continue
        month_vals = [v for m, v in vals if m == today.month]
        if not month_vals:
            continue
        year_mean = sum(v for _, v in vals) / len(vals)
        deviations.append(sum(month_vals) / len(month_vals) - year_mean)
    if not deviations:
        return None
    typical = sum(deviations) / len(deviations)
    start = today - timedelta(days=365)
    last = [v for d, v in history if start <= d < today]
    if not last:
        return None
    return (reference_ct - sum(last) / len(last)) - typical


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x, n)


def recommend(inputs: DecisionInputs) -> DecisionResult:
    p = inputs.profile
    today = inputs.today

    # 1. Bestand, Verbrauch, Reichweite
    current_liter = consumption.estimate_current_liter(
        p.tank_liter, p.fill_percent, p.fill_date, p.annual_consumption_liter, today)
    daily = consumption.daily_liter(p.annual_consumption_liter, today)
    range_d = consumption.range_days(
        current_liter, p.annual_consumption_liter, today, 0.10, p.tank_liter)
    fill_now = current_liter / p.tank_liter * 100 if p.tank_liter else 0.0

    # 2./3. Menge und Preis
    need = compute_need_liter(p.tank_liter, current_liter, p.min_order_liter)
    surcharge = quantity_surcharge_ct(need)
    personal = inputs.reference_ct + inputs.region_offset_ct + surcharge
    eur_per_ct = need / 100

    # 4./5. Historie
    pct365 = percentile(inputs.history, today, 365, inputs.reference_ct)
    pct730 = percentile(inputs.history, today, 730, inputs.reference_ct)
    season_dev = season_deviation(inputs.history, today, inputs.reference_ct)

    # 6. CO2
    cur = co2.current_step(inputs.co2_steps, today)
    nxt = co2.next_step(inputs.co2_steps, today)
    next_ct = next_days = next_date = next_eur = None
    if nxt is not None:
        nxt_ct = co2.step_ct(nxt, inputs.co2_kg_per_liter, inputs.vat)
        cur_ct = co2.step_ct(cur, inputs.co2_kg_per_liter, inputs.vat) if cur else 0.0
        next_ct = nxt_ct - cur_ct
        next_days = (nxt.valid_from - today).days
        next_date = nxt.valid_from
        next_eur = next_ct * need / 100
    voraussichtlich = "voraussichtlich " if nxt is not None and nxt.status == "draft" else ""

    # 7. spätester Bestelltag
    lead = p.lead_days
    latest = today + timedelta(days=int(max(0.0, range_d - lead - 14)))

    price = f"{personal:.1f} ct/L"
    eur_txt = f"1 ct/L entspricht {eur_per_ct:.0f} Euro bei {need} L"
    step_txt = ""
    if next_ct is not None and next_days is not None and next_eur is not None and abs(next_ct) >= 0.5:
        step_txt = (f" Die nächste CO2-Stufe bringt {voraussichtlich}{next_ct:+.1f} ct/L "
                    f"in {next_days} Tagen ({next_eur:+.0f} Euro auf {need} L).")

    # 8. Regeln
    verdict, rule, reason = "watch", "watch", ""
    if need == 0:
        verdict, rule = "wait", "tank_full"
        reason = f"Tank ist zu {fill_now:.0f} % voll, Mindestmenge passt nicht hinein."
    elif range_d < lead + 14:
        verdict, rule = "buy_now", "range_critical"
        reason = (f"Reichweite nur noch {range_d:.0f} Tage bei {lead} Tagen Lieferzeit: "
                  f"jetzt {need} L bestellen zu {price} ({eur_txt}).")
    elif (pct365 is not None and pct365 <= p.threshold_percentile and fill_now < 60
          and (pct730 is None or pct730 <= p.threshold_percentile)):
        # Backtest über 12 Startmonate (10/2026): nur mit dem 2-Jahres-Filter und Schwelle 20
        # schlägt die Regel den Notkauf robust; ohne Filter kauft sie in fallenden Märkten zu früh.
        verdict, rule = "buy_now", "cheap_and_space"
        reason = (f"Der Preis von {price} liegt im günstigsten Teil der letzten ein und zwei Jahre "
                  f"(Perzentil {pct365:.0f}, Schwelle {p.threshold_percentile}), "
                  f"der Tank ist zu {fill_now:.0f} % voll, Reichweite {range_d:.0f} Tage "
                  f"({eur_txt}).")
    elif (next_days is not None and next_days < 45 and next_ct is not None
          and next_ct >= 2.0 and fill_now < 70):
        verdict, rule = "buy_before_step", "before_step"
        reason = (f"Die nächste CO2-Stufe kommt in {next_days} Tagen und erhöht den Preis "
                  f"{voraussichtlich}um {next_ct:.1f} ct/L, das sind {next_eur:.0f} Euro "
                  f"auf {need} L; der Tank ist zu {fill_now:.0f} % voll, Preis jetzt {price}.")
    elif pct365 is not None and pct365 >= 70 and range_d > 90:
        verdict, rule = "wait", "expensive_and_time"
        reason = (f"Der Preis von {price} ist teuer (Perzentil {pct365:.0f}), die Reichweite "
                  f"von {range_d:.0f} Tagen erlaubt Warten ({eur_txt}).")
    elif pct365 is None:
        verdict, rule = "watch", "no_history"
        reason = (f"Noch zu wenig Preishistorie für eine Einordnung (Preis {price}); "
                  f"bewertet wird nur die Reichweite von {range_d:.0f} Tagen, "
                  f"Kauf spätestens ab Reichweite unter {lead + 14} Tagen.")
    else:
        verdict, rule = "watch", "watch"
        reason = (f"Preis {price} bei Perzentil {pct365:.0f}, Reichweite {range_d:.0f} Tage: "
                  f"Bei Perzentil ≤ {p.threshold_percentile} oder Reichweite unter "
                  f"{lead + 14} Tagen wird Kauf empfohlen ({eur_txt}).")
    if rule != "before_step" and rule != "tank_full" and next_days is not None and next_days <= 90:
        reason += step_txt

    # 9. Teilkauf
    split = None
    if need > 0 and fill_now < 50 and pct365 is not None and 30 <= pct365 <= 60:
        half = max(p.min_order_liter, round(need / 2 / 100.0) * 100)
        split = f"Alternative: jetzt {half} Liter, den Rest später"

    details = {
        "today": today.isoformat(),
        "current_liter": _r(current_liter, 1),
        "fill_percent_now": _r(fill_now, 1),
        "daily_liter": _r(daily, 2),
        "range_days": _r(range_d, 1),
        "need_liter": need,
        "quantity_surcharge_ct": surcharge,
        "personal_ct": _r(personal),
        "eur_per_ct": _r(eur_per_ct),
        "percentile_365": _r(pct365, 1),
        "percentile_730": _r(pct730, 1),
        "season_deviation_ct": _r(season_dev),
        "latest_order_date": latest.isoformat(),
        "next_step_date": next_date.isoformat() if next_date else None,
        "next_step_days": next_days,
        "next_step_ct": _r(next_ct),
        "next_step_eur_for_need": _r(next_eur),
        "next_step_status": nxt.status if nxt else None,
        "current_step_eur_per_t": cur.eur_per_t if cur else None,
        "history_n": len(inputs.history),
        "rule": rule,
    }

    return DecisionResult(
        verdict=verdict,  # type: ignore[arg-type]
        rule=rule,
        reason=reason,
        personal_ct=personal,
        quantity_surcharge_ct=surcharge,
        percentile_365=pct365,
        percentile_730=pct730,
        season_deviation_ct=season_dev,
        current_liter=current_liter,
        daily_liter=daily,
        range_days=range_d,
        latest_order_date=latest,
        need_liter=need,
        eur_per_ct=eur_per_ct,
        next_step_date=next_date,
        next_step_days=next_days,
        next_step_ct=next_ct,
        next_step_eur_for_need=next_eur,
        split_suggestion=split,
        details=details,
    )


def result_to_inputs_json(inputs: DecisionInputs, result: DecisionResult) -> str:
    p = inputs.profile
    payload = {
        "today": inputs.today.isoformat(),
        "reference_ct": inputs.reference_ct,
        "region_offset_ct": inputs.region_offset_ct,
        "region_offset_n": inputs.region_offset_n,
        "history_n": len(inputs.history),
        "co2_kg_per_liter": inputs.co2_kg_per_liter,
        "vat": inputs.vat,
        "profile": {
            "plz": p.plz,
            "region": p.region,
            "tank_liter": p.tank_liter,
            "fill_percent": p.fill_percent,
            "fill_date": p.fill_date.isoformat(),
            "annual_consumption_liter": p.annual_consumption_liter,
            "min_order_liter": p.min_order_liter,
            "lead_days": p.lead_days,
            "threshold_percentile": p.threshold_percentile,
        },
        "verdict": result.verdict,
        "rule": result.rule,
        "split_suggestion": result.split_suggestion,
        "details": result.details,
    }
    return json.dumps(payload, ensure_ascii=False)
