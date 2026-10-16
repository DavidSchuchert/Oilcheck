"""Teilkauf-Planer: lohnt es sich, die Bestellung zu teilen? Reine Funktionen."""

from __future__ import annotations

from heizoel.decision.engine import quantity_surcharge_ct


def _de(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def split_plan(need_liter: int, min_order_liter: int, price_ct: float,
               tank_liter: int | None = None) -> dict:
    """Vergleicht "alles jetzt" mit "halb jetzt, halb später".

    price_ct ist der Preis in ct/L fuer die gesamte need_liter-Menge, enthaelt also den
    Mengenaufschlag fuer need_liter. Die kleineren Teilmengen kosten wegen hoeherer Aufschlaege mehr.
    tank_liter ist optional; ist es gesetzt und kleiner als need_liter, ist der Plan unmoeglich.
    """
    all_now = {"liter": need_liter, "ct": round(price_ct, 2),
               "eur": round(price_ct * need_liter / 100, 2)}
    if tank_liter is not None and need_liter > tank_liter:
        return {"possible": False, "all_now": all_now,
                "reason": "Die Menge passt nicht in den Tank.", "text": "Eine Teilung ist nicht möglich."}
    first = max(min_order_liter, int(need_liter / 2 / 100 + 0.5) * 100)
    second = need_liter - first
    if second < min_order_liter:
        reason = (f"Die Menge von {need_liter} L ist zu klein, um sie in zwei Lieferungen "
                  f"von je mindestens {min_order_liter} L zu teilen.")
        return {"possible": False, "all_now": all_now, "reason": reason, "text": reason}

    s_all = quantity_surcharge_ct(need_liter)
    extra_ct_l = quantity_surcharge_ct(first) - s_all
    extra = (extra_ct_l * first + (quantity_surcharge_ct(second) - s_all) * second) / 100
    break_even = extra / (second / 100)
    first_ct = price_ct + extra_ct_l
    if extra <= 0:
        text = (f"Wenn du jetzt {first} L kaufst und {second} L später, kostet dich das heute "
                f"nichts extra.")
    else:
        text = (f"Wenn du jetzt nur {first} L kaufst und {second} L später, zahlst du heute etwa "
                f"{round(extra)} € mehr. Das lohnt sich nur, wenn der Preis später um mehr als "
                f"{_de(break_even)} Cent je Liter fällt.")
    return {
        "possible": True,
        "all_now": all_now,
        "split": {"first_liter": first, "second_liter": second, "first_ct": round(first_ct, 2),
                  "extra_cost_eur": round(extra, 2), "break_even_ct": round(break_even, 2)},
        "text": text,
    }
