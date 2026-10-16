"""Mehrjahres-Statistik: eigene Käufe gegen den Referenzpreis und Regel-Backtest je Nutzer."""

from __future__ import annotations

from datetime import date, timedelta

from heizoel import co2, db
from heizoel.decision import service
from heizoel.decision.backtest import run_backtest

_BACKTEST_CACHE: dict[tuple[int, date], dict | None] = {}


def _reference_on(day: date) -> float | None:
    r = db.query_one(
        "SELECT ct_per_liter FROM reference_prices WHERE date <= ? AND date >= ? "
        "ORDER BY date DESC LIMIT 1",
        (day.isoformat(), (day - timedelta(days=7)).isoformat()))
    return None if r is None else r["ct_per_liter"]


def purchase_stats(user_id: int) -> dict:
    rows = db.query("SELECT date, liter, ct_per_liter, dealer FROM purchases "
                    "WHERE user_id = ? ORDER BY date, id", (user_id,))
    items: list[dict] = []
    total_liter = 0
    total_eur = 0.0
    total_delta = 0.0
    for r in rows:
        ref = _reference_on(date.fromisoformat(r["date"]))
        delta_ct = None if ref is None else round(r["ct_per_liter"] - ref, 2)
        delta_eur = None if delta_ct is None else round(delta_ct * r["liter"] / 100, 2)
        items.append({"date": r["date"], "liter": r["liter"], "ct_per_liter": r["ct_per_liter"],
                      "reference_ct": ref, "delta_ct": delta_ct, "delta_eur": delta_eur,
                      "dealer": r["dealer"]})
        total_liter += r["liter"]
        total_eur += r["ct_per_liter"] * r["liter"] / 100
        if delta_eur is not None:
            total_delta += delta_eur
    return {
        "purchases": items,
        "total_liter": total_liter,
        "total_eur": round(total_eur, 2),
        "avg_ct": round(total_eur * 100 / total_liter, 2) if total_liter else 0.0,
        "total_delta_eur": round(total_delta, 2),
        "n": len(items),
    }


def user_backtest(user_id: int, today: date) -> dict | None:
    profile = service.load_profile(user_id)
    if profile is None:
        return None
    n_hist = db.query_one("SELECT COUNT(*) AS c FROM reference_prices")["c"]
    key = (user_id, today, n_hist, profile.tank_liter, profile.annual_consumption_liter,
           profile.min_order_liter, profile.threshold_percentile)
    if key in _BACKTEST_CACHE:
        return _BACKTEST_CACHE[key]
    result = _compute_backtest(profile, today)
    for k in [k for k in _BACKTEST_CACHE if k[0] == user_id and k != key]:
        del _BACKTEST_CACHE[k]
    _BACKTEST_CACHE[key] = result
    return result


N_STARTS = 12  # Startmonate, über die gemittelt wird: ein einzelner Pfad ist reines Glück


def _compute_backtest(profile, today: date) -> dict | None:
    history = service.load_history(today, 5)
    if not history or (history[-1][0] - history[0][0]).days < 400:
        return None
    first = history[0][0] + timedelta(days=365)
    starts = []
    y, m = first.year, first.month
    for _ in range(N_STARTS):
        s = date(y, m, 1)
        if s >= history[-1][0] - timedelta(days=365):
            break
        starts.append(s)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    runs = []
    for s in starts or [None]:
        runs.append(run_backtest(history, profile.tank_liter, profile.annual_consumption_liter,
                                 profile.min_order_liter, profile.lead_days, co2.load_steps(),
                                 profile.threshold_percentile, start=s))
    n = len(runs)
    out = {
        "rule": {"avg_ct": round(sum(r["rule"]["avg_ct"] for r in runs) / n, 1),
                 "purchases": round(sum(r["rule"]["purchases"] for r in runs) / n, 1)},
        "emergency": {"avg_ct": round(sum(r["emergency"]["avg_ct"] for r in runs) / n, 1),
                      "purchases": round(sum(r["emergency"]["purchases"] for r in runs) / n, 1)},
        "saving_eur": round(sum(r["saving_eur"] for r in runs) / n),
        "saving_pct": round(sum(r["saving_pct"] for r in runs) / n, 1),
        "wins": sum(1 for r in runs if r["saving_eur"] > 0),
        "n_starts": n,
        "years": round((history[-1][0] - history[0][0]).days / 365.25, 1),
    }

    def de(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    base = (f"Wir haben die Regel {n}-mal über die letzten {de(out['years'])} Jahre mit deinen "
            f"Tankwerten durchgespielt, jedes Mal mit einem anderen Startmonat. ")
    if out["saving_eur"] > 0:
        out["text"] = base + (
            f"Im Schnitt hätte sie {de(out['rule']['avg_ct'])} Cent je Liter gezahlt statt "
            f"{de(out['emergency']['avg_ct'])} Cent beim Kauf auf den letzten Drücker, also etwa "
            f"{out['saving_eur']} € pro Durchlauf gespart. Besser war sie in {out['wins']} von {n} Fällen."
        )
    else:
        out["text"] = base + (
            f"Ehrlich gesagt hätte sie im Schnitt nichts gebracht: {de(out['rule']['avg_ct'])} Cent "
            f"je Liter gegenüber {de(out['emergency']['avg_ct'])} Cent beim Kauf auf den letzten "
            f"Drücker, besser nur in {out['wins']} von {n} Fällen. Die Preishistorie ist zurzeit noch "
            f"eine Näherung; mit echten Preispunkten aus der Vergangenheit wird das Bild genauer."
        )
    return out
