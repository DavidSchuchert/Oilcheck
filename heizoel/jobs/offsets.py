"""Regionale Aufschläge aus Nutzerangeboten berechnen.

Für jede Region (erste zwei PLZ-Stellen) wird der Median der Differenz
"Angebotspreis (auf 3000 L normiert) minus Referenzpreis am Angebotstag"
über die letzten WINDOW_DAYS Tage gebildet und in region_offsets geschrieben.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta

from heizoel import db

log = logging.getLogger(__name__)

WINDOW_DAYS = 60
MIN_OFFERS = 3


def _surcharge(liter: int) -> float:
    from heizoel.decision.engine import quantity_surcharge_ct

    return quantity_surcharge_ct(liter)


def _reference_on(day: date) -> float | None:
    row = db.query_one(
        "SELECT ct_per_liter FROM reference_prices WHERE date <= ? AND date >= ? "
        "ORDER BY date DESC LIMIT 1",
        (day.isoformat(), (day - timedelta(days=7)).isoformat()),
    )
    return row["ct_per_liter"] if row else None


def compute_region_offsets(today: date | None = None) -> dict[str, tuple[float, int]]:
    today = today or date.today()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = db.query(
        "SELECT region, date, ct_per_liter, liter FROM offers "
        "WHERE is_outlier = 0 AND date >= ? AND date <= ?",
        (since, today.isoformat()),
    )
    diffs: dict[str, list[float]] = {}
    for r in rows:
        ref = _reference_on(date.fromisoformat(r["date"]))
        if ref is None:
            continue
        normalized = r["ct_per_liter"] - _surcharge(int(r["liter"]))
        diffs.setdefault(r["region"], []).append(normalized - ref)

    result: dict[str, tuple[float, int]] = {}
    out_rows: list[tuple] = []
    for region, values in diffs.items():
        n = len(values)
        offset = statistics.median(values) if n >= MIN_OFFERS else 0.0
        result[region] = (round(offset, 2), n)
        out_rows.append((region, today.isoformat(), round(offset, 2), n))

    if out_rows:
        db.executemany(
            "INSERT INTO region_offsets (region, date, offset_ct, n_offers) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(region, date) DO UPDATE SET offset_ct = excluded.offset_ct, "
            "n_offers = excluded.n_offers",
            out_rows,
        )
    log.info("Regionaloffsets berechnet: %s", result)
    return result
