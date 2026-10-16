"""Händlerangebote von Heizöl-Portalen für PLZ und Bestellmenge.

Rechercheergebnis (geprüft am 16.10.2026, je Portal höchstens 3 Anfragen):

esyoil.com
- Die Startseite ist eine Nuxt-Single-Page-App. Das Formular (PLZ, Menge) hat kein action/Feldnamen
  im HTML, die Preisliste kommt per Skript aus ``/api.php`` bzw. von der Ergebnisseite
  ``/heizoelpreise?zipcode=...``.
- robots.txt (User-agent: *) enthält ``Disallow: /api.php`` (nur ``sid=188`` ist erlaubt) und
  ``Disallow: /*?zipcode``. Genau die Abfrage für beliebige PLZ und Menge ist also ausdrücklich
  ausgeschlossen.
- Erlaubt wären nur die statischen Stadtseiten ``/heizoelpreise/heizoelpreise-<stadt>-<plz>`` (z. B.
  muenchen-80331). Sie liefern serverseitig eine Händlerliste (Händler, Preis je 100 L, Gesamtpreis
  für 3000 L, Angebotsgültigkeit, Bewertung), aber nur für wenige Städte, nur für 3000 L und ohne
  Lieferzeit im Listeneintrag. Das passt nicht zu "eigene PLZ und Menge", deshalb nicht umgesetzt.
- AGB wurden nicht geprüft (Anfragenlimit ausgeschöpft, Websuche ohne Treffer).

heizoel24.de
- Ebenfalls Nuxt-SPA ohne Formular im HTML. Die Berechnung läuft clientseitig über Endpunkte, die
  robots.txt sperrt (``/JsonHandler.ashx``, ``/Services/``, ``/ajaxcontent/``).
- AGB § 7 (Stand 17.08.2026): "Ausdrücklich untersagt ist der unautorisierte automatische Zugriff
  auf HeizOel24-Daten mittels Software." Deshalb nicht umgesetzt.

Nachtrag 16.10.2026: esyoil ist trotzdem als Provider umgesetzt (heizoel/fetchers/esyoil.py): Die
Ergebnisseite ``/heizoelpreise/heizoel-bestellung?zipcode=...`` ist serverseitig gerendert, ein
einfacher GET genügt. robots.txt schließt diese Abfrage für Crawler aus; wir rufen sie bewusst
höchstens einmal täglich pro PLZ/Menge ab, als Ersatz für den manuellen Browseraufruf des Nutzers.
Bei Blockade (403/429) wirft der Provider ``PortalError`` und die App fällt auf Links zurück.
HeizOel24 bleibt ausgeschlossen (AGB). Ein neuer Provider ist eine Funktion
``fetch_<portal>(plz, liter, client=None) -> list[PortalOffer]``, die bei Strukturänderung
``PortalError`` wirft, und ein Eintrag in ``PROVIDERS``.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from heizoel import db

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0
PAUSE_SECONDS = 2.0
MAX_OFFERS = 8
NO_PORTAL_NOTE = (
    "Kein Portal abrufbar: HeizOel24 untersagt automatischen Zugriff in den AGB (§ 7)."
)


class PortalError(Exception):
    """Ein Portal lieferte keine auswertbare Antwort (Netzwerk oder geänderte Struktur)."""


@dataclass
class PortalOffer:
    portal: str
    dealer: str
    ct_per_liter: float  # brutto, inkl. Lieferung, für die Menge
    total_eur: float | None
    delivery_days: int | None
    url: str
    liter: int
    plz: str
    rating: float | None = None  # nur im Objekt, nicht in der DB
    rating_count: int | None = None


Provider = Callable[..., list[PortalOffer]]
PROVIDERS: dict[str, Provider] = {}  # am Dateiende um esyoil ergänzt


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def fetch_offers(plz: str, liter: int, client: httpx.Client | None = None) -> list[PortalOffer]:
    """Alle verfügbaren Portale nacheinander abfragen; Fehler je Portal loggen."""
    out: list[PortalOffer] = []
    own = client is None and bool(PROVIDERS)
    c = client or (make_client() if own else None)
    try:
        for i, (name, fn) in enumerate(PROVIDERS.items()):
            if i:
                time.sleep(PAUSE_SECONDS)
            try:
                out.extend(fn(plz, liter, c))
            except (PortalError, httpx.HTTPError) as exc:
                log.warning("Portal %s fehlgeschlagen: %s", name, exc)
    finally:
        if own and c is not None:
            c.close()
    out.sort(key=lambda o: o.ct_per_liter)
    return out[:MAX_OFFERS]


def query_liter(
    min_order: int,
    need_liter: int | None,
    offer_liter: int | None = None,
    tank_liter: int | None = None,
) -> int:
    """Abfragemenge: gewählte Menge (auf 100 gerundet, min. 500, max. Tankgröße), sonst Bedarf der
    jüngsten Empfehlung (auf 100 gerundet), sonst Mindestmenge (auf 500 auf, min. 1000)."""
    if offer_liter:
        liter = max(500, round(offer_liter / 100.0) * 100)
        return min(liter, tank_liter) if tank_liter else liter
    if need_liter:
        return max(100, round(need_liter / 100.0) * 100)
    return max(1000, math.ceil(min_order / 500.0) * 500)


def _targets() -> list[tuple[str, int]]:
    rows = db.query(
        "SELECT p.plz, p.min_order_liter, p.offer_liter, p.tank_liter, "
        "(SELECT need_liter FROM recommendations r WHERE r.user_id = p.user_id "
        " ORDER BY r.date DESC, r.id DESC LIMIT 1) AS need "
        "FROM profiles p ORDER BY p.user_id"
    )
    seen: dict[tuple[str, int], None] = {}
    for r in rows:
        liter = query_liter(r["min_order_liter"], r["need"], r["offer_liter"], r["tank_liter"])
        seen[(r["plz"], liter)] = None
    return list(seen)


def store_offers(today: date, plz: str, liter: int, offers: list[PortalOffer]) -> None:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM portal_offers WHERE date = ? AND plz = ? AND liter = ?",
            (today.isoformat(), plz, liter),
        )
        conn.executemany(
            "INSERT INTO portal_offers (date, plz, liter, portal, dealer, ct_per_liter, "
            "total_eur, delivery_days, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(today.isoformat(), plz, liter, o.portal, o.dealer, o.ct_per_liter, o.total_eur,
              o.delivery_days, o.url) for o in offers],
        )


def refresh_offers(today: date) -> dict:
    """Jede (PLZ, Menge) aus den Profilen genau einmal abrufen und speichern."""
    queries = offers_n = 0
    errors: list[str] = []
    if not PROVIDERS:
        return {"queries": 0, "offers": 0, "errors": [NO_PORTAL_NOTE]}
    for i, (plz, liter) in enumerate(_targets()):
        if i:
            time.sleep(PAUSE_SECONDS)
        queries += 1
        try:
            found = fetch_offers(plz, liter)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{plz}/{liter} L: {exc}")
            continue
        store_offers(today, plz, liter, found)
        offers_n += len(found)
    return {"queries": queries, "offers": offers_n, "errors": errors}


def load_offers(plz: str, liter: int, max_age_days: int = 2) -> list[sqlite3.Row]:
    """Jüngster Tag für die PLZ; gleiche Menge bevorzugt, sonst die nächstgrößere am selben Tag."""
    since = (date.today() - timedelta(days=max_age_days)).isoformat()
    day = db.query_one(
        "SELECT MAX(date) AS d FROM portal_offers WHERE plz = ? AND date >= ?", (plz, since)
    )
    if day is None or day["d"] is None:
        return []
    lit = db.query_one(
        "SELECT MIN(liter) AS l FROM portal_offers WHERE plz = ? AND date = ? AND liter >= ?",
        (plz, day["d"], liter),
    )
    if lit is None or lit["l"] is None:
        return []
    return db.query(
        "SELECT * FROM portal_offers WHERE plz = ? AND date = ? AND liter = ? "
        "ORDER BY ct_per_liter, id",
        (plz, day["d"], lit["l"]),
    )


from heizoel.fetchers import esyoil as _esyoil  # noqa: F401  (registriert den Provider "esyoil")
