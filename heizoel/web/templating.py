"""Gemeinsame Jinja2-Umgebung."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from heizoel.config import settings
from heizoel.models import VERDICT_LABELS
from heizoel.web import viewmodel

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["VERDICT_LABELS"] = VERDICT_LABELS
templates.env.globals["app_url"] = settings.app_url


def _liter(value, step: int = 10) -> str:
    """Liter mit deutschem Tausenderpunkt, gerundet auf `step`."""
    if value is None:
        return "–"
    rounded = int(round(float(value) / step) * step)
    return f"{rounded:,}".replace(",", ".")


templates.env.filters["liter"] = _liter
templates.env.filters["eur100"] = viewmodel.eur100
templates.env.filters["eur"] = viewmodel.eur
templates.env.filters["ct1"] = viewmodel.ct1
templates.env.filters["datum_lang"] = viewmodel.datum_lang
templates.env.filters["datum_kurz"] = viewmodel.datum_kurz
templates.env.filters["antwort"] = viewmodel.answer_short
