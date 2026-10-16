"""CO2-Preis-Stufen: Stammdaten, Umrechnung in ct/L, Zugriff.

Umrechnung: 1 L Heizöl EL erzeugt ca. 2,65 kg CO2.
10 EUR/t entsprechen damit 2,65 ct/L netto, mit 19 % MwSt rund 3,15 ct/L brutto.
"""

from __future__ import annotations

from datetime import date

from heizoel import db
from heizoel.config import settings
from heizoel.models import Co2Step

# Stand Oktober 2026. Quelle: BEHG (nationaler Brennstoffemissionshandel) und EU-ETS2-Verordnung.
DEFAULT_STEPS: list[Co2Step] = [
    Co2Step(date(2021, 1, 1), 25, None, "fixed", "BEHG Einstieg"),
    Co2Step(date(2022, 1, 1), 30, None, "fixed", "BEHG"),
    Co2Step(date(2023, 1, 1), 30, None, "fixed", "BEHG, Erhöhung ausgesetzt"),
    Co2Step(date(2024, 1, 1), 45, None, "fixed", "BEHG"),
    Co2Step(date(2025, 1, 1), 55, None, "fixed", "BEHG"),
    Co2Step(date(2026, 1, 1), 55, 65, "fixed", "BEHG Preiskorridor"),
    Co2Step(
        date(2027, 1, 1), 55, 65, "draft",
        "Verlängerung des Korridors, Gesetzentwurf Kabinett 12.08.2026, Bundestag ausstehend",
    ),
    Co2Step(
        date(2028, 1, 1), 65, None, "market",
        "EU-ETS2 Marktpreis, Start 2028 per EU-Verordnung (in Kraft seit 07.04.2026). "
        "65 EUR/t ist ein Platzhalter, kein beschlossener Wert.",
    ),
]


def eur_per_t_to_ct_per_liter(eur_per_t: float, kg_per_liter: float | None = None,
                              vat: float | None = None) -> float:
    kg = settings.co2_kg_per_liter if kg_per_liter is None else kg_per_liter
    v = settings.vat if vat is None else vat
    return eur_per_t * kg / 1000 * 100 * (1 + v)


def step_ct(step: Co2Step, kg_per_liter: float | None = None, vat: float | None = None) -> float:
    """CO2-Anteil dieser Stufe in ct/L brutto. Bei Korridor wird die Untergrenze genommen."""
    return eur_per_t_to_ct_per_liter(step.eur_per_t, kg_per_liter, vat)


def seed_default_steps() -> None:
    if db.query_one("SELECT 1 FROM co2_steps LIMIT 1"):
        return
    db.executemany(
        "INSERT INTO co2_steps (valid_from, eur_per_t, eur_per_t_max, status, note, source_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (s.valid_from.isoformat(), s.eur_per_t, s.eur_per_t_max, s.status, s.note, s.source_url)
            for s in DEFAULT_STEPS
        ],
    )


def load_steps() -> list[Co2Step]:
    rows = db.query("SELECT * FROM co2_steps ORDER BY valid_from")
    return [
        Co2Step(
            valid_from=date.fromisoformat(r["valid_from"]),
            eur_per_t=r["eur_per_t"],
            eur_per_t_max=r["eur_per_t_max"],
            status=r["status"],
            note=r["note"] or "",
            source_url=r["source_url"] or "",
        )
        for r in rows
    ]


def current_step(steps: list[Co2Step], on: date) -> Co2Step | None:
    current = None
    for s in steps:
        if s.valid_from <= on:
            current = s
    return current


def next_step(steps: list[Co2Step], on: date) -> Co2Step | None:
    for s in steps:
        if s.valid_from > on:
            return s
    return None
