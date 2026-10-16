"""Tests für Portal-Angebote (esyoil-Parser gegen gespeicherte Fixtures, übrige Tests mit Test-Providern)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from heizoel import db
from heizoel.fetchers import offers as om
from heizoel.fetchers.esyoil import fetch_esyoil, parse_esyoil
from heizoel.fetchers.offers import PortalError, PortalOffer


def mk(portal, dealer, ct, plz="80331", liter=2000):
    return PortalOffer(portal, dealer, ct, ct * liter / 100, 7, "https://example.org/x", liter, plz)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(om.time, "sleep", lambda s: None)


FIX = Path(__file__).parent / "fixtures"


def test_esyoil_registered():
    assert "esyoil" in om.PROVIDERS


@pytest.fixture()
def no_providers(monkeypatch):
    monkeypatch.setattr(om, "PROVIDERS", {})


def test_fetch_offers_sorts_limits_and_survives_errors(monkeypatch):
    def good(plz, liter, quality="normal", client=None):
        r = client.get("https://example.org/a")
        assert r.status_code == 200
        return [mk("a", f"H{i}", 100 + i) for i in range(10)]

    def bad(plz, liter, quality="normal", client=None):
        raise PortalError("Struktur geändert")

    def other(plz, liter, quality="normal", client=None):
        return [mk("c", "Billig", 90.0)]

    monkeypatch.setattr(om, "PROVIDERS", {"a": good, "b": bad, "c": other})
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ok")))
    res = om.fetch_offers("80331", 2000, "normal", client)
    assert len(res) == 8 and res[0].dealer == "Billig"
    assert [o.ct_per_liter for o in res] == sorted(o.ct_per_liter for o in res)


def test_query_liter():
    assert om.query_liter(1000, None) == 1000
    assert om.query_liter(1200, None) == 1500
    assert om.query_liter(300, None) == 1000
    assert om.query_liter(1000, 2347) == 2300


def _user(uid, plz, min_order):
    db.execute("INSERT INTO users (id, email, name, password_hash) VALUES (?, ?, 'n', 'x')",
               (uid, f"u{uid}@x.de"))
    db.execute(
        "INSERT INTO profiles (user_id, plz, region, tank_liter, fill_percent, fill_date, "
        "annual_consumption_liter, min_order_liter) VALUES (?, ?, ?, 3000, 50, '2026-01-01', 2000, ?)",
        (uid, plz, plz[:2], min_order),
    )


def test_refresh_without_provider(tmp_db, no_providers):
    _user(1, "80331", 1000)
    res = om.refresh_offers(date.today())
    assert res["queries"] == 0 and res["errors"]


def test_refresh_and_load(tmp_db, monkeypatch):
    _user(1, "80331", 1000)
    _user(2, "80331", 1000)  # gleiche Abfrage, nur einmal
    _user(3, "10115", 1200)
    calls = []

    def fake(plz, liter, quality="normal", client=None):
        calls.append((plz, liter, quality))
        return [mk("t", "B", 120, plz, liter), mk("t", "A", 110, plz, liter)]

    monkeypatch.setattr(om, "PROVIDERS", {"t": fake})
    today = date.today()
    res = om.refresh_offers(today)
    assert res == {"queries": 4, "offers": 8, "errors": [], "by_quality": {"normal": 4, "premium": 4}}
    assert sorted(calls) == [("10115", 1500, "normal"), ("10115", 1500, "premium"),
                             ("80331", 1000, "normal"), ("80331", 1000, "premium")]
    om.refresh_offers(today)  # idempotent am selben Tag
    assert db.query_one("SELECT COUNT(*) AS n FROM portal_offers")["n"] == 8
    rows = om.load_offers("80331", 1000)
    assert [r["dealer"] for r in rows] == ["A", "B"]
    assert om.load_offers("80331", 800)[0]["liter"] == 1000  # nächstgrößere Menge
    assert om.load_offers("80331", 1500) == []
    assert om.load_offers("99999", 1000) == []


def test_load_offers_max_age(tmp_db):
    old = (date.today() - timedelta(days=5)).isoformat()
    om.store_offers(date.fromisoformat(old), "80331", 1000, [mk("t", "A", 100, liter=1000)])
    assert om.load_offers("80331", 1000) == []
    assert len(om.load_offers("80331", 1000, max_age_days=10)) == 1


def _html(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_esyoil_parse_munich():
    res = parse_esyoil(_html("esyoil_80331_2700.html"), "80331", 2700)
    assert len(res) > 10
    first = res[0]
    assert (first.dealer, first.ct_per_liter, first.total_eur, first.delivery_days) == (
        "Heizguenstig.de", 164.43, 4439.72, 20)
    assert first.rating == 4.92 and first.rating_count == 416
    assert first.url.startswith("https://www.esyoil.com/heizoelhaendler/")
    assert first.portal == "esyoil" and first.plz == "80331" and first.liter == 2700
    cts = [o.ct_per_liter for o in res]
    assert cts == sorted(cts)
    unrated = [o for o in res if o.dealer == "DS EMOVA GmbH"]
    assert len(unrated) == 1 and unrated[0].rating is None and unrated[0].rating_count is None


def test_esyoil_parse_other_plz():
    res = parse_esyoil(_html("esyoil_36251_1000.html"), "36251", 1000)
    assert len(res) > 10
    assert [o.ct_per_liter for o in res] == sorted(o.ct_per_liter for o in res)
    assert all(30 <= o.ct_per_liter <= 400 and o.total_eur for o in res)
    assert all(abs(o.total_eur - o.ct_per_liter * 10) <= o.total_eur * 0.02 for o in res)


def test_esyoil_plausibility_filter():
    html = _html("esyoil_80331_2700.html")
    bad = html.replace("4.439,72\u00a0€", "1.439,72\u00a0€")
    res = parse_esyoil(bad, "80331", 2700)
    assert "Heizguenstig.de" not in [o.dealer for o in res]
    assert len(res) == len(parse_esyoil(html, "80331", 2700)) - 1
    cheap = html.replace("164,43\u00a0€", "12,00\u00a0€")
    assert "Heizguenstig.de" not in [o.dealer for o in parse_esyoil(cheap, "80331", 2700)]


def test_esyoil_empty_page_raises():
    with pytest.raises(PortalError, match="Struktur"):
        parse_esyoil("<html><body><p>Hallo</p></body></html>", "80331", 2700)


def test_esyoil_no_offers_message_is_empty_list():
    assert parse_esyoil("<html><body><p>Leider keine Angebote.</p></body></html>", "99999", 1000) == []


def test_fetch_esyoil_params_and_ok():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, text=_html("esyoil_80331_2700.html"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = fetch_esyoil("80331", 2700, client=client)
    assert res[0].dealer == "Heizguenstig.de"
    assert seen["zipcode"] == "80331" and seen["amount"] == "2700" and seen["hose"] == "fortyMetre"
    assert seen["prod"] == "normal"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_fetch_esyoil_blocked(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, text="no")))
    with pytest.raises(PortalError, match=str(status)):
        fetch_esyoil("80331", 2700, client=client)


def test_query_liter_with_offer_liter():
    assert om.query_liter(1000, 2300, 1540) == 1500
    assert om.query_liter(1000, 2300, 200) == 500
    assert om.query_liter(1000, 2300, 5000, 3000) == 3000
    assert om.query_liter(1000, 2300, None, 3000) == 2300


def test_refresh_uses_profile_offer_liter(tmp_db, monkeypatch):
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    _user(1, "80331", 1000)
    db.execute("UPDATE profiles SET offer_liter = 1500 WHERE user_id = 1")
    seen = []
    monkeypatch.setattr(om, "fetch_offers", lambda plz, liter, quality="normal", client=None: seen.append(liter) or [])
    om.refresh_offers(date.today())
    assert seen == [1500, 1500]  # Standard und Premium


def test_migration_adds_offer_liter_column(tmp_path, monkeypatch):
    import sqlite3

    from heizoel.config import settings

    path = tmp_path / "old.db"
    monkeypatch.setattr(settings, "db_path", path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE profiles (user_id INTEGER PRIMARY KEY, plz TEXT NOT NULL)")
    conn.execute("INSERT INTO profiles VALUES (1, '80331')")
    conn.commit()
    conn.close()
    db.init_db()
    row = db.query_one("SELECT plz, offer_liter FROM profiles WHERE user_id = 1")
    assert row["plz"] == "80331" and row["offer_liter"] is None
    db.init_db()  # idempotent


def test_fetch_esyoil_premium_param_and_invalid_quality():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, text=_html("esyoil_36251_1000_premium.html"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = fetch_esyoil("36251", 1000, "premium", client)
    assert seen["prod"] == "premium" and all(o.quality == "premium" for o in res)
    with pytest.raises(ValueError):
        fetch_esyoil("36251", 1000, "super", client)


def test_esyoil_parse_premium_fixture():
    prem = parse_esyoil(_html("esyoil_36251_1000_premium.html"), "36251", 1000, quality="premium")
    std = parse_esyoil(_html("esyoil_36251_1000.html"), "36251", 1000)
    assert len(prem) > 5
    assert all(o.quality == "premium" and 100 < o.ct_per_liter < 300 for o in prem)
    assert all(o.quality == "normal" for o in std)
    assert any(o.ct_per_liter > std[0].ct_per_liter for o in prem)


def test_load_offers_separates_qualities(tmp_db):
    today = date.today()
    om.store_offers(today, "80331", 1000, [mk("t", "Std", 100, liter=1000)], "normal")
    om.store_offers(today, "80331", 1000, [mk("t", "Prem", 110, liter=1000)], "premium")
    om.store_offers(today, "80331", 1000, [mk("t", "Std2", 101, liter=1000)], "normal")  # ersetzt nur normal
    assert [r["dealer"] for r in om.load_offers("80331", 1000)] == ["Std2"]
    assert [r["dealer"] for r in om.load_offers("80331", 1000, "premium")] == ["Prem"]
    assert om.load_offers("99999", 1000, "premium") == []


def test_refresh_survives_one_quality_failing(tmp_db, monkeypatch):
    _user(1, "80331", 1000)

    def fake(plz, liter, quality="normal", client=None):
        if quality == "premium":
            raise RuntimeError("kaputt")
        return [mk("t", "A", 100, plz, liter)]

    monkeypatch.setattr(om, "PROVIDERS", {"t": fake})
    monkeypatch.setattr(om, "fetch_offers", lambda plz, liter, q="normal", client=None: fake(plz, liter, q))
    res = om.refresh_offers(date.today())
    assert res["by_quality"] == {"normal": 1, "premium": 0} and len(res["errors"]) == 1


def test_migration_adds_quality_columns(tmp_path, monkeypatch):
    import sqlite3

    from heizoel.config import settings

    path = tmp_path / "old2.db"
    monkeypatch.setattr(settings, "db_path", path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE profiles (user_id INTEGER PRIMARY KEY, plz TEXT NOT NULL)")
    conn.execute("INSERT INTO profiles VALUES (1, '80331')")
    conn.execute("CREATE TABLE portal_offers (id INTEGER PRIMARY KEY, date TEXT, plz TEXT, "
                 "liter INTEGER, portal TEXT, ct_per_liter REAL)")
    conn.execute("INSERT INTO portal_offers VALUES (1, '2026-10-16', '80331', 1000, 't', 100)")
    conn.commit()
    conn.close()
    db.init_db()
    assert db.query_one("SELECT offer_quality FROM profiles")["offer_quality"] == "normal"
    assert db.query_one("SELECT quality FROM portal_offers")["quality"] == "normal"
    db.init_db()
