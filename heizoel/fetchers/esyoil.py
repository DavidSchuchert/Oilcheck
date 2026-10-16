"""esyoil.com: Händlerangebote für PLZ und Menge (serverseitig gerenderte Nuxt-Seite).

Die Ergebnisseite ``/heizoelpreise/heizoel-bestellung?zipcode=...&amount=...`` liefert per einfachem
GET (Browser-User-Agent) rund 370 KB HTML mit allen Angeboten als ``<li>``-Einträgen; ein separater
API-Aufruf ist nicht nötig. Beispiel: tests/fixtures/esyoil_80331_2700.html.

Hinweis zu robots.txt: esyoil schließt diese Abfrage (``Disallow: /*?zipcode``) für Crawler aus.
Wir rufen sie bewusst höchstens einmal täglich pro PLZ/Menge ab, als Ersatz für den manuellen
Browseraufruf des Nutzers. Bei Blockade (403/429) wird ``PortalError`` geworfen, die App fällt
dann auf reine Links zurück.

Jedes Angebot steht zweimal im HTML (Desktop- und Mobil-Layout); es wird nach (Händler, Preis,
Gesamt) dedupliziert. Preise: "164,43 €" je 100 Liter entspricht numerisch ct/L.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from heizoel.fetchers.offers import (
    PROVIDERS,
    TIMEOUT,
    USER_AGENT,
    PortalError,
    PortalOffer,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.esyoil.com"
SEARCH_URL = f"{BASE_URL}/heizoelpreise/heizoel-bestellung"
FIXED_PARAMS = {
    "unloading_points": "1",
    "deliveryTimes": "normal",
    "payment_type": "all",
    "short_vehicle": "withTrailer",
    "hose": "fortyMetre",
}
QUALITIES = ("normal", "premium")
PLAUSIBLE_CT = (30.0, 400.0)
TOTAL_TOLERANCE = 0.02

_EUR = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")
_DAYS = re.compile(r"\(\s*in\s+(\d+)\s+Tagen?\s*\)")
_RATING = re.compile(r"(\d(?:[.,]\d+)?)\s+von\s+5\s+Sternen\s*\(\s*(\d{1,3}(?:\.\d{3})*|\d+)\s*\)")


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def _parse_li(li, plz: str, liter: int, fallback_url: str, quality: str = "normal") -> PortalOffer | None:
    text = li.get_text(" ", strip=True)
    link = li.find("a", href=re.compile(r"^(?:https?://[^/]+)?/heizoelhaendler/"))
    prices = _EUR.findall(text)
    if link is None or len(prices) < 2:
        return None
    dealer = link.get_text(" ", strip=True)
    ct = _num(prices[0])
    total = _num(prices[1])
    if not dealer:
        return None
    if not (PLAUSIBLE_CT[0] <= ct <= PLAUSIBLE_CT[1]):
        log.warning("esyoil: %s verworfen, unplausibler Preis %s ct/L", dealer, ct)
        return None
    expected = ct * liter / 100.0
    if abs(total - expected) > expected * TOTAL_TOLERANCE:
        log.warning("esyoil: %s verworfen, Gesamt %.2f passt nicht zu %.2f", dealer, total, expected)
        return None
    m = _DAYS.search(text)
    days = int(m.group(1)) if m else None
    rating = count = None
    mr = _RATING.search(text)
    if mr:
        rating = float(mr.group(1).replace(",", "."))
        count = int(mr.group(2).replace(".", ""))
    href = str(link.get("href", ""))
    url = href if href.startswith("http") else BASE_URL + href
    return PortalOffer("esyoil", dealer, ct, total, days, url or fallback_url, liter, plz, rating, count, quality)


def parse_esyoil(
    html: str, plz: str, liter: int, fallback_url: str = SEARCH_URL, quality: str = "normal"
) -> list[PortalOffer]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[tuple[str, float, float | None]] = set()
    out: list[PortalOffer] = []
    for li in soup.find_all("li"):
        if li.find("li") is not None or "Jetzt Bestellen" not in li.get_text():
            continue
        offer = _parse_li(li, plz, liter, fallback_url, quality)
        if offer is None:
            continue
        key = (offer.dealer, offer.ct_per_liter, offer.total_eur)
        if key in seen:
            continue
        seen.add(key)
        out.append(offer)
    out.sort(key=lambda o: o.ct_per_liter)
    if not out:
        text = soup.get_text(" ", strip=True)
        if "Jetzt Bestellen" not in text and re.search(r"keine\s+(?:\w+\s+)?Angebote|Leider", text):
            return []  # defensiv: Seite meldet für PLZ/Menge keine Angebote
        raise PortalError(f"esyoil: keine Angebote erkannt, Struktur geändert? Auszug: {text[:200]!r}")
    return out


def fetch_esyoil(
    plz: str, liter: int, quality: str = "normal", client: httpx.Client | None = None
) -> list[PortalOffer]:
    if quality not in QUALITIES:
        raise ValueError(f"Unbekannte Heizölqualität: {quality!r}")
    params = {"zipcode": plz, "amount": str(liter), **FIXED_PARAMS, "prod": quality}
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        resp = c.get(
            SEARCH_URL,
            params=params,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    finally:
        if own:
            c.close()
    if resp.status_code != 200:
        raise PortalError(f"esyoil: HTTP {resp.status_code}")
    return parse_esyoil(resp.text, plz, liter, str(resp.url), quality)


PROVIDERS["esyoil"] = fetch_esyoil
