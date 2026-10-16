"""Verbrauchsmodell: Tagesverbrauch nach Gradtagzahl-Gewichten, Restbestand und Reichweite.

Reine Funktionen ohne Zustand.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

# Anteil des Jahresverbrauchs je Kalendermonat in Prozent (grob nach Gradtagzahlen).
MONTH_WEIGHTS: dict[int, float] = {
    1: 17, 2: 14, 3: 12, 4: 8, 5: 4, 6: 2,
    7: 2, 8: 2, 9: 4, 10: 9, 11: 12, 12: 14,
}

MAX_RANGE_DAYS = 730


def daily_liter(annual_liter: int, on: date) -> float:
    """Erwarteter Tagesverbrauch in Litern an diesem Tag."""
    days = calendar.monthrange(on.year, on.month)[1]
    return annual_liter * MONTH_WEIGHTS[on.month] / 100 / days


def consumed_between(annual_liter: int, start: date, end: date) -> float:
    """Verbrauch in Litern im Intervall [start, end)."""
    total = 0.0
    day = start
    while day < end:
        total += daily_liter(annual_liter, day)
        day += timedelta(days=1)
    return total


def estimate_current_liter(tank_liter: int, fill_percent: float, fill_date: date,
                           annual_liter: int, today: date) -> float:
    """Bestand am fill_date minus Verbrauch bis heute, nie unter 0 (und nie über dem Tank)."""
    start = max(0.0, min(tank_liter, tank_liter * fill_percent / 100))
    return max(0.0, start - consumed_between(annual_liter, fill_date, today))


def range_days(current_liter: float, annual_liter: int, today: date,
               reserve_fraction: float = 0.10, tank_liter: int | None = None) -> float:
    """Tage, bis der Bestand auf die Reserve fällt. Tagesweise vorwärts simuliert, max. 730.

    Reserve = reserve_fraction * tank_liter (0, wenn tank_liter None). Der letzte Tag
    wird anteilig gerechnet.
    """
    reserve = reserve_fraction * tank_liter if tank_liter is not None else 0.0
    stock = current_liter
    if stock <= reserve:
        return 0.0
    day = today
    for n in range(MAX_RANGE_DAYS):
        use = daily_liter(annual_liter, day)
        if use <= 0:
            day += timedelta(days=1)
            continue
        if stock - use <= reserve:
            return n + (stock - reserve) / use
        stock -= use
        day += timedelta(days=1)
    return float(MAX_RANGE_DAYS)
