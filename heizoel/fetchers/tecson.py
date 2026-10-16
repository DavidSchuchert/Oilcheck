"""TECSON-Referenzpreis (bundesweiter Heizöl-Durchschnitt, 3000 L, inkl. MwSt.).

Stand der Seitenstruktur (geprüft am 16.10.2026, siehe tests/fixtures/tecson_sample.html):
- https://www.tecson.de/heizoelpreise.html liefert nur eine Meta-Refresh-Weiterleitung auf
  https://www.tecson.de/de/heizoelpreise.html; deshalb wird direkt die /de/-URL abgerufen.
- Der Tagespreis steht an drei Stellen: (1) im Hidden-Input ``dailyprice`` (z. B. "174.10"),
  (2) in der Tabelle "Referenzpreis aktuell" als "Ø 174,1 ct/l", (3) im Marktbericht als
  "Durchschnittspreis bei 174,1 Cent pro Liter". Preisbezug laut Seite: Liefermenge 3000 L, brutto.
- EUR je 100 L entspricht numerisch ct/L, es ist keine Umrechnung nötig.

Annahme: Die Seite bleibt serverseitig gerendert. Der Parser probiert die drei Fundstellen in
dieser Reihenfolge und prüft den Wert auf Plausibilität (30 bis 400 ct/L).
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

TECSON_URL = "https://www.tecson.de/de/heizoelpreise.html"
USER_AGENT = "heizoel-kaufberater/0.1 (privates Projekt)"
TIMEOUT = 20.0
PLAUSIBLE_RANGE = (30.0, 400.0)


class ParseError(Exception):
    """Der TECSON-Preis konnte nicht aus der Seite gelesen werden."""


def _to_float(text: str) -> float:
    return float(text.strip().replace(".", "").replace(",", ".")) if "," in text else float(text.strip())


def _plausible(value: float) -> bool:
    return PLAUSIBLE_RANGE[0] <= value <= PLAUSIBLE_RANGE[1]


def _from_hidden_input(soup: BeautifulSoup) -> float | None:
    el = soup.find("input", attrs={"name": "dailyprice"})
    if el is None:
        return None
    try:
        return _to_float(str(el.get("value", "")))
    except ValueError:
        return None


def _from_table(soup: BeautifulSoup) -> float | None:
    for td in soup.find_all("td"):
        if "Referenzpreis aktuell" in td.get_text(" ", strip=True):
            nxt = td.find_next_sibling("td")
            if nxt is None:
                continue
            m = re.search(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*ct\s*/\s*l", nxt.get_text(" ", strip=True), re.IGNORECASE)
            if m:
                return _to_float(m.group(1))
    return None


_REGEXES = [
    re.compile(r"Durchschnittspreis\s+bei\s+(\d{2,3}(?:,\d{1,2})?)\s*Cent", re.IGNORECASE),
    re.compile(r"Ø\s*(\d{2,3}(?:,\d{1,2})?)\s*ct\s*/\s*l", re.IGNORECASE),
    re.compile(r'name="dailyprice"[^>]*value="(\d{2,3}(?:\.\d{1,2})?)"', re.IGNORECASE),
]


def _from_regex(html: str, text: str) -> float | None:
    for rx in _REGEXES:
        for source in (text, html):
            m = rx.search(source)
            if m:
                try:
                    value = _to_float(m.group(1))
                except ValueError:
                    continue
                if _plausible(value):
                    return value
    return None


def parse_tecson_price(html: str) -> float:
    """Liest den Referenzpreis in ct/L (brutto, 3000 L) aus dem HTML. Wirft ParseError."""
    soup = BeautifulSoup(html, "lxml")
    for name, finder in (("hidden input dailyprice", _from_hidden_input), ("Preistabelle", _from_table)):
        value = finder(soup)
        if value is not None and _plausible(value):
            return value
        if value is not None:
            log.warning("TECSON %s lieferte unplausiblen Wert %s", name, value)
    text = soup.get_text(" ", strip=True).replace("\xad", "").replace("\xa0", " ")
    value = _from_regex(html, text)
    if value is not None:
        return value
    raise ParseError(
        "TECSON-Preis nicht gefunden: weder Hidden-Input 'dailyprice' noch Tabelle "
        "'Referenzpreis aktuell' noch Regex-Fallback passten. Seitenstruktur geändert?"
    )


def fetch_tecson_price(today: date, client: httpx.Client | None = None) -> float:
    """Holt den heutigen TECSON-Preis in ct/L brutto. Wirft ParseError oder httpx.HTTPError."""
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        resp = c.get(TECSON_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        price = parse_tecson_price(resp.text)
    finally:
        if own:
            c.close()
    log.info("TECSON-Preis %s: %.2f ct/L", today.isoformat(), price)
    return price
