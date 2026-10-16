"""Prompts und Schemas der zweistufigen Nachrichten-Einordnung."""

from __future__ import annotations

CATEGORIES = ["rohoel", "raffinerie", "geopolitik", "co2_gesetz", "heizoel_markt", "sonstiges"]

EMPTY_SITUATION = "Keine für den Heizölmarkt relevanten Meldungen in den letzten 24 Stunden."

# Reihenfolge zählt: das Modell formuliert erst die note, dann urteilt es.
ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "relevant": {"type": "boolean"},
        "category": {"type": "string", "enum": CATEGORIES},
        "rule_change": {"type": "boolean"},
        "quote": {"type": "string"},
    },
    "required": ["note", "relevant", "category", "rule_change", "quote"],
}

SITUATION_SCHEMA: dict = {
    "type": "object",
    "properties": {"situation": {"type": "string"}},
    "required": ["situation"],
}

ITEM_SYSTEM_PROMPT = f"""Du bewertest genau eine Nachrichtenmeldung für private Heizölkäufer \
in Deutschland. Antworte nur mit JSON.

Felder:
- note: ein Satz auf Deutsch (höchstens 160 Zeichen), der sagt, worum es in dieser Meldung geht \
und was das für Heizöl- oder Dieselpreise bedeutet. Nur Inhalte der Meldung, keine Prognose.
- relevant: true, wenn die Meldung den Preis von Heizöl, Diesel oder Gasöl in Deutschland in \
den nächsten Wochen beeinflussen kann. Das sind: Rohölpreis, OPEC, Krieg oder Sanktionen in \
Förderregionen (Iran, Russland, Nahost, Hormus), Raffinerien, Dieselknappheit, Kraftstoff- \
und Heizölpreise (auch Spritpreise, Dieselkosten), Steuern und Abgaben auf Kraftstoff oder \
Heizöl (Energiesteuer, Tankrabatt, Steuersenkung, CO2-Preis, BEHG, ETS2, Mehrwertsteuer, \
Steuern auf Gewinne von Ölkonzernen), Wechselkurs Euro/US-Dollar, Ölkonzerne im Zusammenhang \
mit Krieg oder Ölpreis. Nicht relevant: E-Autos, CO2-Zertifikate für Unternehmen, Klimaziele \
allgemein, Wasserstoff, Schifffahrt, Strompreise, Gaspreise ohne Ölbezug.
  Faustregel: Kommen Sprit, Diesel, Benzin, Heizöl, Tankrabatt, Ölpreis, Ölkonzern oder \
Steuern auf Kraftstoff im Titel vor, ist relevant true.
- category: eine aus {CATEGORIES}; bei relevant false "sonstiges".
- rule_change: true nur bei einer konkreten Gesetzes- oder Steueränderung, die Heizöl oder \
Kraftstoff in Deutschland betrifft (Beschluss, Entwurf, Ankündigung mit Datum). Steuern in anderen \
Ländern (z. B. Polen, Ungarn) sind IMMER rule_change false: keine Regeländerung für Deutschland, aber trotzdem relevant. Sonst false.
- quote: wörtlicher Ausschnitt aus Titel oder Zusammenfassung als Beleg, Wort für Wort kopiert; leer, wenn \
rule_change false.

Beispiele (Eingabe -> Antwort):
Titel: Opec+ kürzt Fördermenge um eine Million Barrel pro Tag
-> {{"note": "Die Opec+ senkt die Fördermenge, das betrifft das Rohölangebot.", \
"relevant": true, "category": "rohoel", "rule_change": false, "quote": ""}}
Titel: Spritpreise: Diesel erreicht Jahreshöchststand an deutschen Tankstellen
-> {{"note": "Der Dieselpreis in Deutschland steigt auf ein Jahreshoch.", \
"relevant": true, "category": "heizoel_markt", "rule_change": false, "quote": ""}}
Titel: Bundestag beschließt Senkung der Energiesteuer auf Diesel ab 1. Januar
-> {{"note": "Die Energiesteuer auf Diesel soll ab Januar sinken.", \
"relevant": true, "category": "co2_gesetz", "rule_change": true, \
"quote": "Senkung der Energiesteuer auf Diesel ab 1. Januar"}}
Titel: Spritpreise: Wirkung der Spritsteuer-Senkung bleibt hinter Erwartungen zurück
-> {{"note": "Die Steuersenkung auf Sprit wirkt sich nur teilweise auf die Preise aus.", \
"relevant": true, "category": "co2_gesetz", "rule_change": true, \
"quote": "Spritsteuer-Senkung"}}
Titel: Tanken wird teurer: Kostenwelle durch hohe Dieselpreise erfasst Spediteure
-> {{"note": "Hohe Dieselpreise erhöhen die Kosten in der Wirtschaft.", \
"relevant": true, "category": "heizoel_markt", "rule_change": false, "quote": ""}}
Titel: Ölpreise: Ungarn plant Sonderabgabe für Mineralölkonzerne
-> {{"note": "Ungarn plant eine Abgabe auf Ölkonzerne, das kann Ölpreise beeinflussen.", \
"relevant": true, "category": "rohoel", "rule_change": false, "quote": ""}}
Titel: E-Auto-Absatz steigt auf Rekord
-> {{"note": "Es geht um den Absatz von Elektroautos.", \
"relevant": false, "category": "sonstiges", "rule_change": false, "quote": ""}}
Titel: EU-Emissionshandel: Zertifikate für Stahlwerke werden knapper
-> {{"note": "Es geht um CO2-Zertifikate für die Industrie, nicht um Kraftstoff.", \
"relevant": false, "category": "sonstiges", "rule_change": false, "quote": ""}}
"""

SITUATION_SYSTEM_PROMPT = """Du fasst für private Heizölkäufer in Deutschland ein Lagebild \
aus bereits ausgewählten, relevanten Meldungen zusammen.

Regeln:
- 2 bis 4 Sätze, ausschließlich auf Deutsch (keine englischen Wörter), sachlich. Gib wieder, \
was gemeldet wurde; keine Vermutungen darüber, was es bewirken könnte.
- Verwende ausschließlich Inhalte der gelieferten Meldungen. Erfinde nichts, nutze kein anderes Wissen.
- Keine Preisprognose, keine Richtungsaussage wie "wird steigen" oder "wird fallen", \
keine Kaufempfehlung.
- Verwende das Wort "Prognose" nicht.
- Behaupte nie, es gäbe keine relevanten Meldungen: es gibt welche.
"""
