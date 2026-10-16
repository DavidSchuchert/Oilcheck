"""Einmaliger CSV-Import eigener Preiswerte (Quelle 'import')."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from heizoel.fetchers.reference import upsert_price


def import_csv(path: str | Path) -> int:
    """Liest date,ct_per_liter (ISO-Datum, Dezimalpunkt oder -komma). Gibt Zahl geschriebener Zeilen zurück."""
    count = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.reader(lines)
    header = next(reader, None)
    if header is None:
        return 0
    if [h.strip().lower() for h in header[:2]] != ["date", "ct_per_liter"]:
        raise ValueError(f"Kopfzeile 'date,ct_per_liter' erwartet, gefunden: {header}")
    for n, row in enumerate(reader, start=2):
        if len(row) < 2:
            raise ValueError(f"Ungültige Zeile {n}: {row}")
        day = date.fromisoformat(row[0].strip())
        raw = ".".join(c.strip() for c in row[1:3]) if len(row) == 3 else row[1].strip()
        ct = float(raw.replace(",", "."))
        if upsert_price(day, "import", ct):
            count += 1
    return count
