"""Brent-Spotpreis (USD/Barrel) über die EIA Open Data API v2, Serie RBRTE."""

from __future__ import annotations

import logging
from datetime import date

import httpx

log = logging.getLogger(__name__)

EIA_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
USER_AGENT = "heizoel-kaufberater/0.1 (privates Projekt)"
TIMEOUT = 20.0
PAGE_SIZE = 5000


def fetch_brent(start: date, end: date, api_key: str,
                client: httpx.Client | None = None) -> dict[date, float]:
    if not api_key:
        log.warning("Kein EIA-API-Key gesetzt (HEIZOEL_EIA_API_KEY), Brent-Daten werden übersprungen")
        return {}
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    result: dict[date, float] = {}
    offset = 0
    try:
        while True:
            params = {
                "api_key": api_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": "RBRTE",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": PAGE_SIZE,
            }
            resp = c.get(EIA_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            body = resp.json().get("response", {})
            rows = body.get("data", [])
            for row in rows:
                value = row.get("value")
                if value in (None, ""):
                    continue
                try:
                    result[date.fromisoformat(row["period"])] = float(value)
                except (ValueError, KeyError):
                    log.warning("EIA-Zeile ignoriert: %r", row)
            total = int(body.get("total", len(rows)) or 0)
            offset += len(rows)
            if not rows or offset >= total:
                break
    finally:
        if own:
            c.close()
    return result
