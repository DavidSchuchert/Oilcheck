"""Orchestrierung der Referenzpreise: TECSON, Brent, EUR/USD, Fallback. Schreibt reference_prices."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from heizoel import co2, db
from heizoel.config import settings
from heizoel.fetchers import fallback
from heizoel.fetchers.ecb import fetch_eurusd
from heizoel.fetchers.eia import fetch_brent
from heizoel.fetchers.tecson import ParseError, fetch_tecson_price

log = logging.getLogger(__name__)

SOURCE_PRIORITY = {"manual": 4, "tecson": 3, "import": 2, "fallback": 1}
LOOKBACK_DAYS = 10  # Vorlauf für Forward-Fill an Wochenenden/Feiertagen und verzögerte EIA-Daten


def upsert_price(day: date, source: str, ct: float, brent_usd: float | None = None,
                 eurusd: float | None = None) -> bool:
    """Schreibt nur, wenn kein Eintrag existiert oder die Quelle mindestens gleichwertig ist."""
    if source not in SOURCE_PRIORITY:
        raise ValueError(f"Unbekannte Quelle: {source}")
    key = day.isoformat()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction() as conn:
        row = conn.execute("SELECT source FROM reference_prices WHERE date = ?", (key,)).fetchone()
        if row is not None and SOURCE_PRIORITY[source] < SOURCE_PRIORITY[row["source"]]:
            conn.execute(
                "UPDATE reference_prices SET brent_usd = COALESCE(brent_usd, ?), "
                "eurusd = COALESCE(eurusd, ?) WHERE date = ?",
                (brent_usd, eurusd, key),
            )
            return False
        conn.execute(
            "INSERT INTO reference_prices (date, source, ct_per_liter, brent_usd, eurusd, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET source = excluded.source, "
            "ct_per_liter = excluded.ct_per_liter, "
            "brent_usd = COALESCE(excluded.brent_usd, brent_usd), "
            "eurusd = COALESCE(excluded.eurusd, eurusd), fetched_at = excluded.fetched_at",
            (key, source, ct, brent_usd, eurusd, now),
        )
    return True


def forward_fill(d: dict[date, float], start: date, end: date) -> dict[date, float]:
    """Füllt jeden Tag von start bis end mit dem letzten bekannten Wert (auch aus der Zeit vor start).

    Tage vor dem ersten bekannten Wert bleiben leer.
    """
    result: dict[date, float] = {}
    known = sorted(k for k in d if k <= end)
    idx = 0
    last: float | None = None
    day = known[0] if known and known[0] < start else start
    while day <= end:
        while idx < len(known) and known[idx] <= day:
            last = d[known[idx]]
            idx += 1
        if last is not None and day >= start:
            result[day] = last
        day += timedelta(days=1)
    return result


def fetch_market_data(start: date, end: date,
                      client: httpx.Client | None = None) -> tuple[dict[date, float], dict[date, float]]:
    """Brent (USD/bbl) und EUR/USD je Kalendertag zwischen start und end, lückenlos per Forward-Fill."""
    early = start - timedelta(days=LOOKBACK_DAYS)
    brent = fetch_brent(early, end, settings.eia_api_key, client=client)
    eurusd = fetch_eurusd(early, end, client=client)
    return forward_fill(brent, start, end), forward_fill(eurusd, start, end)


def set_manual_price(day: date, ct: float) -> None:
    """Speichert einen manuellen Preis; fehlende Marktdaten werden für den Tag nachgeladen (Margen-Anker)."""
    row = db.query_one("SELECT brent_usd, eurusd FROM reference_prices WHERE date = ?", (day.isoformat(),))
    brent_usd = row["brent_usd"] if row else None
    eurusd = row["eurusd"] if row else None
    if brent_usd is None or eurusd is None:
        try:
            brent, fx = fetch_market_data(day, day)
            brent_usd = brent_usd if brent_usd is not None else brent.get(day)
            eurusd = eurusd if eurusd is not None else fx.get(day)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Marktdaten für %s nicht abrufbar, speichere ohne: %s", day, exc)
    upsert_price(day, "manual", ct, brent_usd, eurusd)


def _last_known_market() -> tuple[float | None, float | None]:
    row = db.query_one(
        "SELECT brent_usd, eurusd FROM reference_prices "
        "WHERE brent_usd IS NOT NULL AND eurusd IS NOT NULL ORDER BY date DESC LIMIT 1"
    )
    return (row["brent_usd"], row["eurusd"]) if row else (None, None)


def _log_job(started: datetime, ok: bool, message: str) -> None:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    db.execute(
        "INSERT INTO job_runs (job, started_at, finished_at, ok, message) VALUES (?, ?, ?, ?, ?)",
        ("fetch_reference", started.strftime(fmt), datetime.now(UTC).strftime(fmt), int(ok), message),
    )


def fetch_today(today: date, client: httpx.Client | None = None) -> dict:
    started = datetime.now(UTC)
    brent_usd: float | None = None
    eurusd: float | None = None
    errors: list[str] = []
    try:
        brent, fx = fetch_market_data(today, today, client=client)
        brent_usd, eurusd = brent.get(today), fx.get(today)
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"Marktdaten: {exc}")
        log.warning("Marktdaten nicht abrufbar: %s", exc)

    source = "tecson"
    ct: float | None = None
    try:
        ct = fetch_tecson_price(today, client=client)
    except (ParseError, httpx.HTTPError) as exc:
        errors.append(f"TECSON: {exc}")
        log.warning("TECSON nicht verfügbar, nutze Fallback: %s", exc)
        source = "fallback"
        if brent_usd is None or eurusd is None:
            last_brent, last_fx = _last_known_market()
            brent_usd = brent_usd if brent_usd is not None else last_brent
            eurusd = eurusd if eurusd is not None else last_fx
        if brent_usd is not None and eurusd is not None:
            margin = fallback.calibrate_margin(today=today)
            ct = fallback.fallback_price_ct(
                brent_usd, eurusd, fallback.co2_net_ct(today), margin, settings.vat
            )

    if ct is None:
        result = {"date": today, "source": None, "ct_per_liter": None, "brent_usd": brent_usd,
                  "eurusd": eurusd, "error": "; ".join(errors) or "kein Preis ermittelbar"}
        _log_job(started, False, result["error"])
        return result

    upsert_price(today, source, ct, brent_usd, eurusd)
    error = "; ".join(errors) or None
    result = {"date": today, "source": source, "ct_per_liter": ct, "brent_usd": brent_usd,
              "eurusd": eurusd, "error": error}
    _log_job(started, source == "tecson", f"{source} {ct:.2f} ct/L" + (f" ({error})" if error else ""))
    return result


def backfill(start: date, end: date) -> int:
    """Rechnet Fallback-Preise für start..end aus Brent und EUR/USD zurück (Historie ohne TECSON)."""
    brent, fx = fetch_market_data(start, end)
    steps = co2.load_steps()
    anchors = fallback.margin_anchors(today=end)
    if anchors:
        margins = [m for _, m in anchors]
        log.info("Backfill: %d Margen-Anker, Marge %.1f bis %.1f ct/L netto", len(anchors), min(margins), max(margins))
    else:
        log.info("Backfill: 0 Margen-Anker, Standardmarge %.1f ct/L netto", fallback.DEFAULT_MARGIN_CT)
    written = 0
    day = start
    while day <= end:
        b, r = brent.get(day), fx.get(day)
        if b is not None and r is not None:
            ct = fallback.fallback_price_ct(b, r, fallback.co2_net_ct(day, steps),
                                           fallback.margin_on(day, anchors), settings.vat)
            if upsert_price(day, "fallback", ct, b, r):
                written += 1
        day += timedelta(days=1)
    return written
