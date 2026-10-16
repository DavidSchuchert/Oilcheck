"""Jahresverbrauch aus echten Füllständen lernen.

Aus je zwei Messungen (mind. 14 Tage Abstand) ergibt sich der Verbrauch im Intervall
(Liter alt - Liter neu + Käufe im Intervall). Geteilt durch den Saisonanteil des Intervalls
(MONTH_WEIGHTS) folgt ein implizierter Jahresverbrauch; gemittelt wird nach Intervalllänge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from heizoel import db
from heizoel.decision import consumption

MIN_GAP_DAYS = 14
MIN_SHARE = 0.02


@dataclass
class LearnResult:
    annual_liter: float | None
    days: int
    n_readings: int
    confidence: float
    method: str
    note: str


def _compute(tank: int, levels: list[tuple[date, float]],
             purchases: list[tuple[date, int]]) -> LearnResult:
    """levels: (Datum, Liter) aufsteigend. Rein rechnerisch, ohne DB."""
    weighted = 0.0
    total_days = 0
    n = 0
    anchor = 0
    for i in range(1, len(levels)):
        old_d, old_l = levels[anchor]
        new_d, new_l = levels[i]
        gap = (new_d - old_d).days
        if gap < MIN_GAP_DAYS:
            continue
        bought = sum(q for d, q in purchases if old_d < d <= new_d)
        used = old_l - new_l + bought
        share = consumption.consumed_between(1, old_d, new_d)
        anchor = i  # nächstes Intervall startet an dieser Messung
        if used < 0 or share < MIN_SHARE:
            continue
        weighted += used / share * gap
        total_days += gap
        n += 1
    if n < 1 or total_days == 0:
        return LearnResult(None, 0, len(levels), 0.0, "none",
                           "Noch zu wenige Füllstandsmessungen, um deinen Verbrauch zu lernen.")
    annual = weighted / total_days
    conf = min(1.0, total_days / 365) * min(1.0, n / 3)
    return LearnResult(annual, total_days, n, conf, "fill_levels",
                       f"Aus {n + 1} Messungen über {total_days} Tage")


def learn_consumption(user_id: int, today: date) -> LearnResult:
    prof = db.query_one("SELECT tank_liter FROM profiles WHERE user_id = ?", (user_id,))
    if prof is None:
        return LearnResult(None, 0, 0, 0.0, "none", "Kein Profil vorhanden.")
    tank = prof["tank_liter"]
    rows = db.query("SELECT date, fill_percent FROM fill_levels WHERE user_id = ? ORDER BY date",
                    (user_id,))
    levels = [(date.fromisoformat(r["date"]), r["fill_percent"] / 100 * tank) for r in rows]
    buys = [(date.fromisoformat(r["date"]), r["liter"]) for r in db.query(
        "SELECT date, liter FROM purchases WHERE user_id = ?", (user_id,))]
    res = _compute(tank, levels, buys)
    if res.annual_liter is None:
        db.execute(
            "UPDATE profiles SET learned_annual_liter = NULL, learned_days = NULL, "
            "learned_n = NULL, learned_confidence = NULL, learned_at = NULL WHERE user_id = ?",
            (user_id,))
    else:
        db.execute(
            "UPDATE profiles SET learned_annual_liter = ?, learned_days = ?, learned_n = ?, "
            "learned_confidence = ?, learned_at = ? WHERE user_id = ?",
            (res.annual_liter, res.days, res.n_readings, res.confidence,
             today.isoformat(), user_id))
    return res


def effective_annual_liter(stated: int, learned: float | None, confidence: float) -> int:
    """Mischung aus angegebenem und gelerntem Wert, begrenzt auf 0,5x bis 2x des angegebenen."""
    if learned is None:
        return stated
    c = max(0.0, min(1.0, confidence))
    mixed = stated * (1 - c) + learned * c
    return round(max(0.5 * stated, min(2.0 * stated, mixed)))


def learn_all(today: date) -> int:
    """Alle Profile neu lernen; gibt die Zahl der Nutzer mit gelerntem Wert zurück."""
    count = 0
    for r in db.query("SELECT user_id FROM profiles ORDER BY user_id"):
        if learn_consumption(r["user_id"], today).annual_liter is not None:
            count += 1
    return count
