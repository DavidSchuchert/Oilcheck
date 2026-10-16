"""Fallback-Preismodell: Brent, EUR/USD, Energiesteuer, Marge und CO2 zu einem Brutto-Preis."""

from __future__ import annotations

import itertools
import statistics
from datetime import date

from heizoel import co2, db
from heizoel.config import settings

ENERGY_TAX_CT = 6.135      # Energiesteuer Heizöl EL, ct/L netto
DEFAULT_MARGIN_CT = 20.0   # Raffinerie, Logistik, Händler, ct/L netto; Platzhalter, wird kalibriert
BARREL_LITERS = 158.987


def co2_net_ct(on: date, steps: list | None = None) -> float:
    """CO2-Anteil in ct/L netto am Tag `on` (Korridor: Untergrenze). 0, wenn keine Stufe gilt."""
    step = co2.current_step(steps if steps is not None else co2.load_steps(), on)
    if step is None:
        return 0.0
    return step.eur_per_t * settings.co2_kg_per_liter / 1000 * 100


def fallback_price_ct(brent_usd: float, eurusd: float, co2_ct_net: float,
                      margin_ct: float, vat: float) -> float:
    crude_ct = brent_usd / BARREL_LITERS / eurusd * 100
    return (crude_ct + ENERGY_TAX_CT + margin_ct + co2_ct_net) * (1 + vat)


def margin_from_price(price_ct: float, brent_usd: float, eurusd: float,
                      co2_ct_net: float, vat: float) -> float:
    crude_ct = brent_usd / BARREL_LITERS / eurusd * 100
    return price_ct / (1 + vat) - crude_ct - ENERGY_TAX_CT - co2_ct_net


ANCHOR_CLUSTER_DAYS = 14   # Ankerpunkte, die näher beieinander liegen, werden zum Median zusammengefasst


def margin_anchors(today: date | None = None) -> list[tuple[date, float]]:
    """Implizite Marge (ct/L netto) an Tagen mit echtem Preis und gespeicherten Marktdaten, aufsteigend.

    Punkte innerhalb von ANCHOR_CLUSTER_DAYS (ab dem ersten Punkt des Clusters) werden zu Median-Datum
    und Median-Marge zusammengefasst.
    """
    until = (today or date.today()).isoformat()
    rows = db.query(
        "SELECT date, ct_per_liter, brent_usd, eurusd FROM reference_prices "
        "WHERE source IN ('tecson', 'manual', 'import') AND date <= ? "
        "AND brent_usd IS NOT NULL AND eurusd IS NOT NULL ORDER BY date",
        (until,),
    )
    steps = co2.load_steps()
    points = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        m = margin_from_price(r["ct_per_liter"], r["brent_usd"], r["eurusd"],
                              co2_net_ct(d, steps), settings.vat)
        points.append((d, m))
    clusters: list[list[tuple[date, float]]] = []
    for point in points:
        if clusters and (point[0] - clusters[-1][0][0]).days <= ANCHOR_CLUSTER_DAYS:
            clusters[-1].append(point)
        else:
            clusters.append([point])
    anchors = []
    for cluster in clusters:
        ordinals = [d.toordinal() for d, _ in cluster]
        anchors.append((date.fromordinal(round(statistics.median(ordinals))),
                        statistics.median(m for _, m in cluster)))
    return anchors


def margin_on(day: date, anchors: list[tuple[date, float]]) -> float:
    """Marge am Tag `day`: linear zwischen den umgebenden Ankern, außerhalb der Randwert."""
    if not anchors:
        return DEFAULT_MARGIN_CT
    if day <= anchors[0][0]:
        return anchors[0][1]
    if day >= anchors[-1][0]:
        return anchors[-1][1]
    for (d0, m0), (d1, m1) in itertools.pairwise(anchors):
        if d0 <= day <= d1:
            span = (d1 - d0).days
            return m0 if span == 0 else m0 + (m1 - m0) * (day - d0).days / span
    return anchors[-1][1]


def calibrate_margin(today: date | None = None) -> float:
    """Jüngste bekannte Marge (für den Tagesabruf); DEFAULT_MARGIN_CT ohne Anker."""
    anchors = margin_anchors(today)
    return anchors[-1][1] if anchors else DEFAULT_MARGIN_CT
