"""Tägliches Nachrichten-Lagebild: Feeds -> Vorfilter -> Sprachmodell -> Prüfung -> DB.

Das Modell ordnet nur ein. Es liefert nie Zahlen für die Kaufregel (siehe PLAN.md 5.1).
"""

from __future__ import annotations

import json
import logging
from datetime import date

from heizoel import db
from heizoel.config import settings
from heizoel.llm.base import ModelError, TextModel, get_model, validate_against_schema
from heizoel.news.feeds import fetch_all, prefilter, store_items
from heizoel.news.prompts import (
    CATEGORIES,
    EMPTY_SITUATION,
    ITEM_SCHEMA,
    ITEM_SYSTEM_PROMPT,
    SITUATION_SCHEMA,
    SITUATION_SYSTEM_PROMPT,
)

__all__ = ["CATEGORIES", "EMPTY_SITUATION", "load_digest", "run_digest"]

log = logging.getLogger(__name__)

FALLBACK_SITUATION = "Lagebild konnte nicht regelkonform erzeugt werden."
FORBIDDEN_PHRASES = ["prognose", "wird steigen", "wird fallen", "kaufen sie"]
NOTE_MAX = 160


def build_item_prompt(item: dict) -> str:
    return "\n".join([
        f"Titel: {item['title']}",
        f"Quelle: {item['feed']}",
        f"Datum: {item.get('published') or 'unbekannt'}",
        f"Zusammenfassung: {item.get('summary') or ''}",
    ])  # fmt: skip


def build_prompt(items: list[dict]) -> str:
    """Prompt für Stufe 2: relevante Meldungen mit Titel, Quelle und note."""
    lines = ["Relevante Meldungen der letzten 24 Stunden:", ""]
    for n, item in enumerate(items, 1):
        lines.append(f"{n}. Titel: {item['title']}")
        lines.append(f"   Quelle: {item.get('source') or item.get('feed') or ''}")
        lines.append(f"   Einordnung: {item.get('note') or ''}")
        lines.append("")
    return "\n".join(lines)


def _save_digest(
    day: date,
    situation: str,
    relevant: list[dict],
    hints: list[dict],
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    duration_ms: int,
) -> None:
    iso = day.isoformat()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO news_digests "
            "(date, summary, items_json, model, tokens_in, tokens_out, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso,
                situation,
                json.dumps(relevant, ensure_ascii=False),
                model,
                tokens_in,
                tokens_out,
                duration_ms,
            ),
        )
        for hint in hints:
            conn.execute(
                "INSERT OR IGNORE INTO rule_change_hints (date, title, quote, url, status) "
                "VALUES (?, ?, ?, ?, 'open')",
                (iso, hint["title"], hint["quote"], hint["url"]),
            )


def run_digest(today: date, force: bool = False, model: TextModel | None = None) -> dict:
    iso = today.isoformat()
    if not force and db.query_one("SELECT 1 FROM news_digests WHERE date = ?", (iso,)):
        return {"skipped": "exists"}
    model = model or get_model()
    if model is None:
        return {"skipped": "llm_driver=none"}

    items = prefilter(fetch_all())
    store_items(items)

    if not items:
        _save_digest(today, EMPTY_SITUATION, [], [], None, 0, 0, 0)
        return {
            "date": iso, "n_items": 0, "n_relevant": 0, "n_hints": 0,
            "model": None, "tokens_in": 0, "tokens_out": 0, "duration_ms": 0, "n_calls": 0,
        }  # fmt: skip

    totals = {"tokens_in": 0, "tokens_out": 0, "duration_ms": 0, "n_calls": 0}
    model_name = model.name

    def call(system: str, prompt: str, schema: dict) -> dict:
        nonlocal model_name
        totals["n_calls"] += 1
        result = model.complete(system, prompt, schema)
        validate_against_schema(result.data, schema)
        totals["tokens_in"] += result.tokens_in
        totals["tokens_out"] += result.tokens_out
        totals["duration_ms"] += result.duration_ms
        model_name = result.model or model_name
        return result.data

    # Stufe 1: je Meldung ein eigener Aufruf
    relevant: list[dict] = []
    hints: list[dict] = []
    for item in items:
        try:
            data = call(ITEM_SYSTEM_PROMPT, build_item_prompt(item), ITEM_SCHEMA)
        except ModelError as exc:
            log.warning("Meldung übersprungen (%s): %s", item["url"], exc)
            continue
        if not data["relevant"]:
            continue
        relevant.append({
            "url": item["url"],
            "title": item["title"],
            "source": item["feed"],
            "category": data["category"],
            "note": data["note"].strip()[:NOTE_MAX],
        })  # fmt: skip
        quote = data["quote"].strip()
        if data["rule_change"] and quote:
            haystack = f"{item['title']} {item.get('summary') or ''}".lower()
            if quote.lower() not in haystack:
                log.warning("Zitat nicht wörtlich, Titel als Zitat genommen: %r", quote)
                quote = item["title"]
            hints.append({"title": item["title"], "url": item["url"], "quote": quote})

    # Stufe 2: Lagebild nur bei mindestens einer relevanten Meldung
    if not relevant:
        situation = EMPTY_SITUATION
    elif settings.news_summary_mode != "model":
        # Deterministisch aus den Einzelbewertungen: kleine Modelle bewerten einzelne Meldungen
        # zuverlässig, dichten aber beim freien Zusammenfassen dazu.
        situation = compose_situation(relevant)
    else:
        situation = None
        try:
            situation = (
                call(SITUATION_SYSTEM_PROMPT, build_prompt(relevant), SITUATION_SCHEMA)["situation"]
                .strip()
                .strip("{} \n")
            )
        except ModelError as exc:
            log.warning("Lagebild-Aufruf fehlgeschlagen: %s", exc)
        if not situation or EMPTY_SITUATION in situation:
            situation = "Relevante Meldungen heute: " + "; ".join(r["title"] for r in relevant)
        elif any(p in situation.lower() for p in FORBIDDEN_PHRASES):
            log.warning("Lagebild enthält verbotene Formulierung, ersetzt: %r", situation)
            situation = FALLBACK_SITUATION

    _save_digest(
        today, situation, relevant, hints,
        model_name, totals["tokens_in"], totals["tokens_out"], totals["duration_ms"],
    )  # fmt: skip
    return {
        "date": iso,
        "n_items": len(items),
        "n_relevant": len(relevant),
        "n_hints": len(hints),
        "model": model_name,
        "tokens_in": totals["tokens_in"],
        "tokens_out": totals["tokens_out"],
        "duration_ms": totals["duration_ms"],
        "n_calls": totals["n_calls"],
    }


CATEGORY_WORDS = {
    "rohoel": "Rohöl", "raffinerie": "Raffinerien", "geopolitik": "Geopolitik",
    "co2_gesetz": "Steuern und CO2-Preis", "heizoel_markt": "Heizöl- und Dieselmarkt",
    "sonstiges": "Sonstiges",
}


def compose_situation(relevant: list[dict]) -> str:
    """Lagebild ohne freies Formulieren: Anzahl, Themen und die Einzelsätze des Modells."""
    n = len(relevant)
    cats = []
    for r in relevant:
        word = CATEGORY_WORDS.get(r.get("category", ""), "Sonstiges")
        if word not in cats:
            cats.append(word)
    head = (f"{n} relevante Meldung" if n == 1 else f"{n} relevante Meldungen") + " in den letzten 24 Stunden"
    head += f", Themen: {', '.join(cats)}." if cats else "."
    notes = []
    for r in relevant:
        note = (r.get("note") or "").strip().rstrip(".")
        if note:
            notes.append(note + ".")
    return head + (" " + " ".join(notes[:4]) if notes else "")


def load_digest(day: date) -> dict | None:
    row = db.query_one("SELECT * FROM news_digests WHERE date = ?", (day.isoformat(),))
    if row is None:
        return None
    return {
        "date": row["date"],
        "situation": row["summary"],
        "items": json.loads(row["items_json"]),
        "model": row["model"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"],
    }
