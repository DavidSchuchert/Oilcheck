from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from heizoel import db
from heizoel.config import settings
from heizoel.fetchers import fallback, reference
from heizoel.fetchers.history_import import import_csv
from heizoel.fetchers.tecson import ParseError, fetch_tecson_price, parse_tecson_price

FIXTURE = Path(__file__).parent / "fixtures" / "tecson_sample.html"
TODAY = date(2026, 10, 16)


def _row(day: date):
    return db.query_one("SELECT * FROM reference_prices WHERE date = ?", (day.isoformat(),))


def test_parser_sample():
    assert parse_tecson_price(FIXTURE.read_text(encoding="utf-8")) == pytest.approx(174.1)


def test_parser_table_fallback():
    html = FIXTURE.read_text(encoding="utf-8").replace('name="dailyprice"', 'name="x"')
    assert parse_tecson_price(html) == pytest.approx(174.1)


def test_parser_regex_fallback():
    assert parse_tecson_price("<p>bundesweite Durchschnittspreis bei 171,5 Cent pro Liter</p>") == 171.5


def test_parser_error():
    with pytest.raises(ParseError):
        parse_tecson_price("<html><body>nichts</body></html>")


def test_fallback_price_range():
    co2_net = 55 * settings.co2_kg_per_liter / 1000 * 100
    value = fallback.fallback_price_ct(75, 1.10, co2_net, fallback.DEFAULT_MARGIN_CT, 0.19)
    assert 80 < value < 120


def test_calibrate_default_and_median(tmp_db):
    assert fallback.calibrate_margin(today=TODAY) == fallback.DEFAULT_MARGIN_CT
    co2_net = fallback.co2_net_ct(date(2026, 10, 1))
    price = fallback.fallback_price_ct(80, 1.1, co2_net, 30.0, settings.vat)
    for i in range(1, 6):
        reference.upsert_price(date(2026, 10, i), "tecson", price, 80, 1.1)
    assert fallback.calibrate_margin(today=TODAY) == pytest.approx(30.0)


def test_upsert_priority(tmp_db):
    d = date(2026, 5, 1)
    assert reference.upsert_price(d, "tecson", 150.0, 70, 1.1)
    assert not reference.upsert_price(d, "fallback", 99.0)
    assert _row(d)["ct_per_liter"] == 150.0 and _row(d)["source"] == "tecson"
    assert reference.upsert_price(d, "manual", 140.0)
    assert not reference.upsert_price(d, "tecson", 150.0)
    row = _row(d)
    assert row["source"] == "manual" and row["ct_per_liter"] == 140.0 and row["brent_usd"] == 70


def test_forward_fill():
    d = {date(2026, 1, 2): 10.0, date(2026, 1, 5): 12.0}
    filled = reference.forward_fill(d, date(2026, 1, 3), date(2026, 1, 7))
    assert filled == {
        date(2026, 1, 3): 10.0, date(2026, 1, 4): 10.0, date(2026, 1, 5): 12.0,
        date(2026, 1, 6): 12.0, date(2026, 1, 7): 12.0,
    }


def _transport(tecson_html: str | None):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "www.tecson.de":
            if tecson_html is None:
                return httpx.Response(500)
            return httpx.Response(200, text=tecson_html)
        if host == "api.eia.gov":
            return httpx.Response(200, json={"response": {"total": 1, "data": [
                {"period": TODAY.isoformat(), "series": "RBRTE", "value": "75.0"}]}})
        if host == "api.frankfurter.dev":
            return httpx.Response(200, json={"rates": {TODAY.isoformat(): {"USD": 1.10}}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_fetch_tecson_price_mock():
    client = httpx.Client(transport=_transport(FIXTURE.read_text(encoding="utf-8")))
    assert fetch_tecson_price(TODAY, client=client) == pytest.approx(174.1)


def test_fetch_today_success(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "eia_api_key", "k")
    client = httpx.Client(transport=_transport(FIXTURE.read_text(encoding="utf-8")))
    res = reference.fetch_today(TODAY, client=client)
    assert res["source"] == "tecson" and res["ct_per_liter"] == pytest.approx(174.1)
    assert res["brent_usd"] == 75.0 and res["eurusd"] == 1.10 and res["error"] is None
    row = _row(TODAY)
    assert row["source"] == "tecson" and row["brent_usd"] == 75.0
    job = db.query_one("SELECT * FROM job_runs WHERE job = 'fetch_reference'")
    assert job["ok"] == 1


def test_fetch_today_parse_error_uses_fallback(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "eia_api_key", "k")
    client = httpx.Client(transport=_transport("<html>kaputt</html>"))
    res = reference.fetch_today(TODAY, client=client)
    assert res["source"] == "fallback" and res["error"]
    assert 80 < res["ct_per_liter"] < 300
    assert _row(TODAY)["source"] == "fallback"
    assert db.query_one("SELECT ok FROM job_runs")["ok"] == 0


def test_fetch_today_http_error_without_market_data(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "eia_api_key", "")
    client = httpx.Client(transport=_transport(None))
    res = reference.fetch_today(TODAY, client=client)
    assert res["ct_per_liter"] is None and res["error"]


def test_backfill(tmp_db, monkeypatch):
    start, end = date(2026, 3, 1), date(2026, 3, 10)
    days = (end - start).days + 1

    def fake(s, e, client=None):
        return (reference.forward_fill({start: 75.0}, s, e), reference.forward_fill({start: 1.1}, s, e))

    monkeypatch.setattr(reference, "fetch_market_data", fake)
    reference.upsert_price(date(2026, 3, 5), "tecson", 150.0)
    assert reference.backfill(start, end) == days - 1
    assert _row(date(2026, 3, 5))["source"] == "tecson"
    assert _row(date(2026, 3, 6))["source"] == "fallback"


def test_import_csv(tmp_db, tmp_path):
    f = tmp_path / "h.csv"
    f.write_text(
        "date,ct_per_liter\n# Kommentar\n2021-03-01,70.5\n2021-04-01,\"72,25\"\n\n2021-05-01,73,5\n",
        encoding="utf-8",
    )
    assert import_csv(f) == 3
    assert _row(date(2021, 4, 1))["ct_per_liter"] == 72.25
    assert _row(date(2021, 5, 1))["ct_per_liter"] == 73.5
    assert _row(date(2021, 3, 1))["source"] == "import"
    assert import_csv(Path("data/history_seed.csv")) == 0


def _anchor_price(day: date, margin: float, brent=80.0, fx=1.1) -> float:
    return fallback.fallback_price_ct(brent, fx, fallback.co2_net_ct(day), margin, settings.vat)


def test_margin_on_interpolation_edges_empty():
    a = [(date(2026, 1, 1), 20.0), (date(2026, 1, 11), 40.0)]
    assert fallback.margin_on(date(2026, 1, 6), a) == pytest.approx(30.0)
    assert fallback.margin_on(date(2026, 1, 1), a) == pytest.approx(20.0)
    assert fallback.margin_on(date(2025, 6, 1), a) == pytest.approx(20.0)
    assert fallback.margin_on(date(2027, 1, 1), a) == pytest.approx(40.0)
    assert fallback.margin_on(date(2026, 1, 6), []) == fallback.DEFAULT_MARGIN_CT


def test_margin_anchors_clusters_and_median(tmp_db):
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 5), date(2026, 6, 1)
    reference.upsert_price(d1, "manual", _anchor_price(d1, 20.0), 80, 1.1)
    reference.upsert_price(d2, "tecson", _anchor_price(d2, 30.0), 80, 1.1)
    reference.upsert_price(d3, "import", _anchor_price(d3, 50.0), 80, 1.1)
    reference.upsert_price(date(2026, 7, 1), "fallback", 100.0, 80, 1.1)  # kein Anker
    anchors = fallback.margin_anchors(today=TODAY)
    assert len(anchors) == 2
    assert anchors[0][1] == pytest.approx(25.0)
    assert d1 <= anchors[0][0] <= d2
    assert anchors[1] == (d3, pytest.approx(50.0))
    assert fallback.calibrate_margin(today=TODAY) == pytest.approx(50.0)


def test_backfill_interpolates_margin(tmp_db, monkeypatch):
    a0, a1 = date(2026, 3, 1), date(2026, 4, 30)
    reference.upsert_price(a0, "manual", _anchor_price(a0, 20.0), 80.0, 1.1)
    reference.upsert_price(a1, "manual", _anchor_price(a1, 40.0), 80.0, 1.1)

    def fake(s, e, client=None):
        return (reference.forward_fill({a0: 80.0}, s, e), reference.forward_fill({a0: 1.1}, s, e))

    monkeypatch.setattr(reference, "fetch_market_data", fake)
    reference.backfill(a0, a1)
    mid = date(2026, 3, 31)  # 30 von 60 Tagen
    expected = _anchor_price(mid, 30.0)
    assert _row(mid)["source"] == "fallback"
    assert _row(mid)["ct_per_liter"] == pytest.approx(expected)
    assert _row(date(2026, 3, 10))["ct_per_liter"] < _row(date(2026, 4, 20))["ct_per_liter"]


def test_set_manual_price_fetches_market_data(tmp_db, monkeypatch):
    day = date(2024, 2, 1)
    monkeypatch.setattr(reference, "fetch_market_data", lambda s, e, client=None: ({day: 82.0}, {day: 1.08}))
    reference.set_manual_price(day, 120.0)
    row = _row(day)
    assert (row["brent_usd"], row["eurusd"], row["source"]) == (82.0, 1.08, "manual")


def test_set_manual_price_market_error(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(reference, "fetch_market_data", boom)
    reference.set_manual_price(date(2024, 2, 1), 120.0)
    row = _row(date(2024, 2, 1))
    assert row["brent_usd"] is None and row["ct_per_liter"] == 120.0
