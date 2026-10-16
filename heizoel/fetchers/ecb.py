"""EUR/USD-Referenzkurse der EZB über die Frankfurter-API (kein Key nötig)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
USER_AGENT = "heizoel-kaufberater/0.1 (privates Projekt)"
TIMEOUT = 20.0
BLOCK_DAYS = 365


def _blocks(start: date, end: date) -> list[tuple[date, date]]:
    blocks = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=BLOCK_DAYS - 1), end)
        blocks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return blocks


def fetch_eurusd(start: date, end: date, client: httpx.Client | None = None) -> dict[date, float]:
    """USD je EUR für Handelstage zwischen start und end (Wochenenden fehlen)."""
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    result: dict[date, float] = {}
    try:
        for b_start, b_end in _blocks(start, end):
            url = f"{FRANKFURTER_URL}/{b_start.isoformat()}..{b_end.isoformat()}"
            resp = c.get(url, params={"from": "EUR", "to": "USD"},
                         headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if "rates" in body:
                rates = body["rates"]
            else:  # Einzeltag-Antwort: {"date": ..., "rates": {"USD": x}}
                rates = {}
            for day, cur in rates.items():
                if isinstance(cur, dict) and "USD" in cur:
                    result[date.fromisoformat(day)] = float(cur["USD"])
            if not rates:
                log.warning("Frankfurter lieferte keine Kurse für %s..%s", b_start, b_end)
    finally:
        if own:
            c.close()
    return result
