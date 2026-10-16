"""Datenklassen, die als Vertrag zwischen Modulen dienen.

Diese Signaturen sind fix. Wer sie ändert, muss alle Verwender anpassen (decision, web, jobs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Verdict = Literal["buy_now", "buy_before_step", "wait", "watch"]

VERDICT_LABELS: dict[str, str] = {
    "buy_now": "Jetzt kaufen",
    "buy_before_step": "Vor dem Stichtag kaufen",
    "wait": "Warten",
    "watch": "Beobachten",
}


@dataclass(frozen=True)
class Co2Step:
    valid_from: date
    eur_per_t: float            # Festpreis bzw. Korridor-Untergrenze
    eur_per_t_max: float | None  # Korridor-Obergrenze, sonst None
    status: Literal["fixed", "draft", "market"]
    note: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class Profile:
    user_id: int
    plz: str
    region: str
    tank_liter: int
    fill_percent: float
    fill_date: date
    annual_consumption_liter: int
    min_order_liter: int
    lead_days: int
    notify_channel: str
    threshold_percentile: int


@dataclass(frozen=True)
class DecisionInputs:
    """Alles, was die Kaufregel braucht. Reine Daten, keine DB-Zugriffe."""

    today: date
    reference_ct: float                 # nationaler Referenzpreis heute, ct/L brutto, 3000 L
    region_offset_ct: float             # 0.0 wenn unbekannt
    region_offset_n: int                # Anzahl Angebote hinter dem Offset
    history: list[tuple[date, float]]   # Referenzpreise aufsteigend nach Datum, bis zu 5 Jahre
    profile: Profile
    co2_steps: list[Co2Step]            # alle bekannten Stufen, auch vergangene
    co2_kg_per_liter: float = 2.65
    vat: float = 0.19


@dataclass(frozen=True)
class DecisionResult:
    verdict: Verdict
    rule: str                           # Maschinenschlüssel der Regel, die gegriffen hat
    reason: str                         # Ein deutscher Satz, mit Zahlen
    personal_ct: float                  # Referenz + Regionaloffset + Mengenaufschlag
    quantity_surcharge_ct: float
    percentile_365: float | None        # 0..100, None wenn zu wenig Historie
    percentile_730: float | None
    season_deviation_ct: float | None   # heute minus trendbereinigtes Monatsmittel, None wenn zu wenig Historie
    current_liter: float                # geschätzter Restbestand heute
    daily_liter: float                  # geschätzter Tagesverbrauch heute
    range_days: float
    latest_order_date: date             # spätester Bestelltag, bevor es knapp wird
    need_liter: int                     # zu bestellende Menge (auf Mindestmenge gerundet)
    eur_per_ct: float                   # need_liter / 100: was 1 ct/L in Euro bedeutet
    next_step_date: date | None
    next_step_days: int | None
    next_step_ct: float | None          # Preissprung der nächsten Stufe in ct/L brutto
    next_step_eur_for_need: float | None
    split_suggestion: str | None        # Teilkauf-Vorschlag als Text oder None
    details: dict = field(default_factory=dict)  # weitere Zwischenwerte für inputs_json
