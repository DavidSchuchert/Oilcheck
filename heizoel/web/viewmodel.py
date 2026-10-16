"""Alltagssprache für die Weboberfläche: Formatierer und die Texte der Antwort-Karte."""

from __future__ import annotations

from datetime import date, timedelta

WOCHENTAGE = ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def to_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def datum_lang(value) -> str:
    """'Fr., 06.12.2026'"""
    d = to_date(value)
    if d is None:
        return "–"
    return f"{WOCHENTAGE[d.weekday()]}, {d.day:02d}.{d.month:02d}.{d.year}"


def datum_kurz(value) -> str:
    """'6. Dezember'"""
    d = to_date(value)
    if d is None:
        return "–"
    return f"{d.day}. {MONATE[d.month - 1]}"


def _de(number: float, digits: int) -> str:
    return f"{number:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def eur100(ct) -> str:
    """Cent je Liter entspricht Euro je 100 Liter: '164,43 €'."""
    return "–" if ct is None else f"{_de(float(ct), 2)} €"


def eur(value) -> str:
    """Euro ganz: '4.440 €'."""
    return "–" if value is None else f"{_de(round(float(value)), 0)} €"


def ct1(value) -> str:
    """Cent mit einer Nachkommastelle: '95,5'."""
    return "–" if value is None else _de(float(value), 1)


def price_level_sentence(percentile_365: float | None) -> str:
    p = percentile_365
    if p is None:
        return "Für einen Preisvergleich fehlen noch Daten."
    if p <= 20:
        return "Heizöl ist gerade so günstig wie selten im letzten Jahr."
    if p <= 40:
        return "Heizöl ist gerade eher günstig."
    if p <= 60:
        return "Heizöl kostet gerade etwa so viel wie im Durchschnitt des letzten Jahres."
    if p <= 80:
        return "Heizöl ist gerade eher teuer."
    return "Heizöl ist gerade so teuer wie an fast keinem Tag im letzten Jahr."


def range_sentence(range_days: float | None, today: date) -> str | None:
    if range_days is None:
        return None
    days = max(0.0, float(range_days))
    until = datum_lang(today + timedelta(days=round(days)))
    weeks = days / 7
    if weeks < 10:
        n = max(1, round(weeks))
        unit = "Woche" if n == 1 else "Wochen"
    else:
        n = max(1, round(days / 30.4))
        unit = "Monat" if n == 1 else "Monate"
    return f"Dein Tank reicht noch etwa {n} {unit} (bis ungefähr {until})."


def answer_title(verdict: str, step_date) -> str:
    if verdict == "buy_now":
        return "Ja, jetzt kaufen"
    if verdict == "buy_before_step":
        d = to_date(step_date)
        return f"Ja, vor dem {datum_lang(d)} kaufen" if d else "Ja, bald kaufen"
    if verdict == "wait":
        return "Nein, noch warten"
    if verdict == "watch":
        return "Noch nicht, aber im Blick behalten"
    return "Keine Antwort"


def answer_short(verdict: str) -> str:
    return {
        "buy_now": "Ja, jetzt kaufen", "buy_before_step": "Ja, vor dem Termin kaufen",
        "wait": "Nein, noch warten", "watch": "Noch nicht, im Blick behalten",
    }.get(verdict, verdict)


def build_answer(rec, today: date, plan: dict | None, fill_percent: float) -> dict:
    """Texte der Antwort-Karte aus einer gespeicherten Empfehlung (sqlite3.Row oder dict)."""
    verdict = rec["verdict"]
    step_date = to_date(rec["next_step_date"])
    step_ct = rec["next_step_ct"]
    sentences = [price_level_sentence(rec["percentile_365"])]

    extra = None
    if verdict == "buy_now" and rec["rule"] == "range_critical":
        extra = (f"Dein Tank wird knapp, bestelle spätestens am "
                 f"{datum_lang(rec['latest_order_date'])}.")
    elif step_date and step_ct is not None and step_ct >= 0.5:
        extra = (f"Am {datum_lang(step_date)} wird Heizöl durch die CO2-Abgabe etwa "
                 f"{ct1(step_ct)} Cent je Liter teurer.")
    else:
        extra = range_sentence(rec["range_days"], today)
    if extra:
        sentences.append(extra)

    need = _de(round(rec["need_liter"] / 10) * 10, 0)
    why = [f"Preis heute: {ct1(rec['personal_ct'])} Cent je Liter (dein Preis für {need} Liter)"]
    if rec["percentile_365"] is not None:
        why.append(f"Günstiger als an {round(100 - rec['percentile_365'])} von 100 Tagen "
                   f"im letzten Jahr")
    dev = rec["season_deviation_ct"]
    if dev is not None:
        month = MONATE[today.month - 1]
        if abs(dev) < 0.05:
            why.append(f"Jahreszeit: zurzeit so viel wie üblich für {month}")
        else:
            why.append(f"Jahreszeit: zurzeit {ct1(abs(dev))} Cent "
                       f"{'über' if dev > 0 else 'unter'} dem Üblichen für {month}")

    tip = None
    if plan and plan.get("possible") and plan.get("text") and verdict in ("buy_now", "buy_before_step"):
        text = str(plan["text"]).strip()
        tip = text if text.lower().startswith("tipp") else f"Tipp: {text}"

    return {
        "title": answer_title(verdict, step_date), "sentences": sentences, "why": why,
        "reason": rec["reason"], "tip": tip, "verdict": verdict,
    }
