"""Tests der Weboberfläche. compute_and_store wird durch einen Dummy ersetzt."""

from __future__ import annotations

import sqlite3
import sys
import types
from datetime import date

import pytest
from fastapi.testclient import TestClient

from heizoel import db
from heizoel.config import settings

TODAY = date.today().isoformat()
PROFILE = {
    "plz": "80331", "tank_liter": "3000", "fill_percent": "40", "fill_date": TODAY,
    "annual": "2500", "min_order": "1000", "lead_days": "10", "notify": "mail",
}


@pytest.fixture()
def stub_decision(monkeypatch):
    calls = []
    service = types.ModuleType("heizoel.decision.service")
    service.compute_and_store = lambda user_id, today: calls.append(user_id)
    service.compute_all = lambda today=None: calls.append("all")
    monkeypatch.setitem(sys.modules, "heizoel.decision.service", service)
    return calls


@pytest.fixture()
def client(tmp_db, stub_decision):
    from heizoel.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def register(c, name="Anna", email="anna@example.org", password="geheim123"):
    return c.post("/register", data={"name": name, "email": email, "password": password,
                                     "invite": settings.invite_code})


def onboard(c, **over):
    return c.post("/onboarding", data={**PROFILE, **over})


def make_rec(user_id=1):
    db.execute(
        "INSERT INTO recommendations (user_id, date, verdict, rule, reason, personal_ct, "
        "percentile_365, range_days, latest_order_date, need_liter, inputs_json) "
        "VALUES (?, ?, 'buy_now', 'r', 'Preis ist niedrig.', 95.5, 20, 60, ?, 2000, '{}')",
        (user_id, TODAY, TODAY),
    )


def test_register_first_user_admin_and_redirect_to_onboarding(client):
    r = register(client)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    register(client.__class__(client.app, follow_redirects=False), "Bob", "bob@example.org")
    roles = [u["role"] for u in db.query("SELECT role FROM users ORDER BY id")]
    assert roles == ["admin", "user"]
    assert client.get("/").headers["location"] == "/onboarding"


def test_register_wrong_invite_and_duplicate(client):
    r = client.post("/register", data={"name": "A", "email": "a@x.de", "password": "geheim123",
                                       "invite": "falsch"})
    assert r.status_code == 400
    register(client)
    r = register(client)
    assert r.status_code == 400 and "bereits" in r.text


def test_login_logout(client):
    register(client)
    client.post("/logout")
    assert client.get("/").headers["location"] == "/login"
    assert client.post("/login", data={"email": "anna@example.org", "password": "x"}).status_code == 400
    r = client.post("/login", data={"email": "ANNA@example.org", "password": "geheim123"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_onboarding_creates_profile_and_fill_level(client, stub_decision):
    register(client)
    assert onboard(client, plz="12").status_code == 400
    r = onboard(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    p = db.query_one("SELECT * FROM profiles WHERE user_id = 1")
    assert p["region"] == "80" and p["tank_liter"] == 3000
    f = db.query_one("SELECT * FROM fill_levels WHERE user_id = 1")
    assert f["source"] == "onboarding" and f["fill_percent"] == 40
    assert stub_decision == [1]


def test_dashboard_with_and_without_recommendation(client):
    register(client)
    onboard(client)
    r = client.get("/")
    assert r.status_code == 200 and "Noch keine Empfehlung" in r.text
    make_rec()
    r = client.get("/")
    assert r.status_code == 200
    assert "Ja, jetzt kaufen" in r.text and "Preis ist niedrig." in r.text  # im Warum-Bereich
    assert "Günstiger als an 80 von 100 Tagen" in r.text
    assert client.get("/history").status_code == 200


def test_fill_level_purchase_offer(client):
    register(client)
    onboard(client)
    db.execute("INSERT INTO reference_prices (date, source, ct_per_liter) VALUES (?, 'import', 100)",
               (TODAY,))
    client.post("/fill-level", data={"fill_percent": "25", "date": TODAY})
    client.post("/fill-level", data={"fill_percent": "30", "date": TODAY})
    rows = db.query("SELECT * FROM fill_levels WHERE source = 'manual'")
    assert len(rows) == 1 and rows[0]["fill_percent"] == 30
    assert db.query_one("SELECT fill_percent FROM profiles")["fill_percent"] == 30

    client.post("/purchase", data={"date": TODAY, "liter": "1500", "ct_per_liter": "98.5",
                                   "dealer": "Müller"})
    pu = db.query_one("SELECT * FROM purchases")
    assert pu["liter"] == 1500 and pu["ct_per_liter"] == 98.5 and pu["dealer"] == "Müller"
    assert db.query_one("SELECT fill_percent FROM profiles")["fill_percent"] == 80
    assert db.query_one("SELECT * FROM fill_levels WHERE source = 'purchase'")["fill_percent"] == 80
    client.post("/purchase", data={"date": TODAY, "liter": "3000", "ct_per_liter": "98"})
    assert db.query_one("SELECT fill_percent FROM profiles")["fill_percent"] == 100

    client.post("/offer", data={"ct_per_liter": "105", "liter": "2000", "dealer": "A"})
    client.post("/offer", data={"ct_per_liter": "140", "liter": "2000", "portal": "esyoil"})
    offers = db.query("SELECT * FROM offers ORDER BY id")
    assert [o["is_outlier"] for o in offers] == [0, 1]
    assert offers[0]["region"] == "80" and offers[1]["portal"] == "esyoil"
    assert client.get("/").status_code == 200


def test_admin_access_and_co2(client):
    register(client)
    onboard(client)
    assert client.get("/admin").status_code == 200
    r = client.post("/admin/co2", data={"valid_from": "2030-01-01", "eur_per_t": "99",
                                        "eur_per_t_max": "", "status": "draft", "note": "x"})
    assert r.status_code == 303
    s = db.query_one("SELECT * FROM co2_steps WHERE valid_from = '2030-01-01'")
    assert s["eur_per_t"] == 99 and s["status"] == "draft"
    client.post("/admin/co2", data={"valid_from": "2030-01-01", "eur_per_t": "100",
                                    "status": "fixed"})
    assert db.query_one("SELECT eur_per_t FROM co2_steps WHERE valid_from = '2030-01-01'")[0] == 100
    client.post("/admin/co2/delete", data={"valid_from": "2030-01-01"})
    assert db.query_one("SELECT 1 FROM co2_steps WHERE valid_from = '2030-01-01'") is None
    client.post("/admin/manual-price", data={"date": TODAY, "ct": "101.5"})
    assert db.query_one("SELECT source FROM reference_prices")["source"] == "manual"
    assert client.post("/admin/recompute-all").status_code == 303


def test_admin_forbidden_for_normal_user(client):
    register(client)
    with TestClient(client.app, follow_redirects=False) as other:
        register(other, "Bob", "bob@example.org")
        assert other.get("/admin").status_code == 403
        assert other.post("/admin/co2", data={"valid_from": "2031-01-01",
                                              "eur_per_t": "1"}).status_code == 403


def test_settings_and_delete_account(client):
    register(client)
    onboard(client)
    r = client.post("/settings", data={**PROFILE, "threshold_percentile": "20", "plz": "10115"})
    assert r.status_code == 303
    p = db.query_one("SELECT * FROM profiles")
    assert p["region"] == "10" and p["threshold_percentile"] == 20
    assert client.get("/settings").status_code == 200

    client.post("/settings/password", data={"current": "geheim123", "new": "neuespasswort"})
    client.post("/logout")
    assert client.post("/login", data={"email": "anna@example.org",
                                       "password": "neuespasswort"}).status_code == 303

    client.post("/settings/delete", data={"password": "falsch"})
    assert db.query_one("SELECT 1 FROM profiles") is not None
    client.post("/settings/delete", data={"password": "neuespasswort"})
    assert db.query_one("SELECT 1 FROM profiles") is None
    assert db.query_one("SELECT 1 FROM users") is None
    assert client.get("/").headers["location"] == "/login"


def _insert_digest(day=TODAY, items=None, model="m1"):
    import json

    items = items if items is not None else [
        {"url": "https://example.org/a", "title": "Raffinerie steht still", "source": "Quelle",
         "category": "refinery", "note": "Kurz erklärt"}]
    db.execute(
        "INSERT INTO news_digests (date, summary, items_json, model, tokens_in, tokens_out, "
        "duration_ms) VALUES (?, 'Der Markt ist ruhig.', ?, ?, 100, 50, 1234)",
        (day, json.dumps(items), model),
    )


def test_dashboard_shows_digest(client):
    register(client)
    onboard(client)
    _insert_digest()
    r = client.get("/")
    assert r.status_code == 200
    assert "Der Markt ist ruhig." in r.text and "Raffinerie steht still" in r.text
    assert "Einordnung, keine Vorhersage." in r.text and "Raffinerie" in r.text


def test_dashboard_digest_via_dummy_module(client, monkeypatch):
    register(client)
    onboard(client)
    _insert_digest()
    mod = types.ModuleType("heizoel.news.digest")
    mod.load_digest = lambda day: {
        "date": day.isoformat(), "situation": "Dummy-Lage",
        "items": [{"url": "https://x.de", "title": "Dummy-Titel", "source": "s",
                   "category": "crude", "note": "n"}]}
    monkeypatch.setitem(sys.modules, "heizoel.news.digest", mod)
    r = client.get("/")
    assert "Dummy-Lage" in r.text and "Dummy-Titel" in r.text and "Rohöl" in r.text


def test_dashboard_without_digest_driver_none(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_driver", "none")
    register(client)
    onboard(client)
    assert "Lagebild ist nicht aktiviert" in client.get("/").text
    monkeypatch.setattr(settings, "llm_driver", "ollama")
    assert "Noch kein Lagebild" in client.get("/").text


def test_dashboard_hint_notice(client):
    register(client)
    onboard(client)
    assert "möglichen Änderungen" not in client.get("/").text
    db.execute("INSERT INTO rule_change_hints (date, title, quote, url) VALUES (?, 't', 'q', 'u')",
               (TODAY,))
    r = client.get("/")
    assert "möglichen Änderungen" in r.text and "/admin#hinweise" in r.text


def test_admin_hints_confirm_dismiss(client):
    register(client)
    onboard(client)
    for u in ("u1", "u2"):
        db.execute("INSERT INTO rule_change_hints (date, title, quote, url) VALUES (?, 'Titel', "
                   "'Zitat', ?)", (TODAY, u))
    r = client.post("/admin/hints/1/confirm")
    assert r.status_code == 303
    assert db.query_one("SELECT status FROM rule_change_hints WHERE id = 1")["status"] == "confirmed"
    assert "bei CO2-Stufen ein" in client.get("/admin").text
    client.post("/admin/hints/2/dismiss")
    assert db.query_one("SELECT status FROM rule_change_hints WHERE id = 2")["status"] == "dismissed"


def test_admin_digest_flash_and_model_log(client, monkeypatch):
    register(client)
    onboard(client)
    mod = types.ModuleType("heizoel.news.digest")
    mod.run_digest = lambda today, force=False: {
        "date": today.isoformat(), "n_items": 7, "n_relevant": 3, "n_hints": 1,
        "model": "m1", "tokens_in": 1, "tokens_out": 2, "duration_ms": 3}
    monkeypatch.setitem(sys.modules, "heizoel.news.digest", mod)
    assert client.post("/admin/digest").status_code == 303
    page = client.get("/admin").text
    assert "Lagebild erzeugt" in page and "7 Meldungen" in page
    _insert_digest(model="modell-xyz")
    page = client.get("/admin").text
    assert "modell-xyz" in page and "1234" in page and "Der Markt ist ruhig." in page
    assert "LLM-Treiber:" in page


def _fill_state():
    return (db.query_one("SELECT fill_percent FROM profiles")["fill_percent"],
            db.query("SELECT * FROM fill_levels WHERE source = 'manual'"))


def test_fill_level_liter_percent_and_errors(client):
    register(client)
    onboard(client)
    client.post("/fill-level", data={"liter": "1200", "date": TODAY})
    assert _fill_state()[0] == 40.0
    client.post("/fill-level", data={"fill_percent": "25", "date": TODAY})
    assert _fill_state()[0] == 25.0
    client.post("/fill-level", data={"liter": "600", "fill_percent": "90", "date": TODAY})
    assert _fill_state()[0] == 20.0
    client.post("/fill-level", data={"liter": "9999", "date": TODAY})
    assert _fill_state()[0] == 100.0
    before = _fill_state()
    client.post("/fill-level", data={"date": TODAY})
    assert _fill_state()[0] == before[0] and len(_fill_state()[1]) == len(before[1])
    assert "Liter im Tank oder Füllstand" in client.get("/").text


def test_onboarding_with_liter(client):
    register(client)
    r = onboard(client, liter="750", fill_percent="")
    assert r.status_code == 303
    assert db.query_one("SELECT fill_percent FROM profiles")["fill_percent"] == 25.0
    assert db.query_one("SELECT fill_percent FROM fill_levels")["fill_percent"] == 25.0


def test_onboarding_requires_fill(client):
    register(client)
    assert onboard(client, liter="", fill_percent="").status_code == 400


def test_dashboard_shows_liter(client):
    register(client)
    onboard(client)
    t = client.get("/").text
    assert "1.200 Liter</strong> im Tank (von 3.000)" in t
    db.execute(
        "INSERT INTO recommendations (user_id, date, verdict, rule, reason, personal_ct, "
        "range_days, latest_order_date, need_liter, inputs_json) "
        "VALUES (1, ?, 'wait', 'r', 'x', 95.5, 60, ?, 2000, ?)",
        (TODAY, TODAY, '{"details": {"current_liter": 1043.0}}'),
    )
    assert "1.040 Liter</strong> im Tank" in client.get("/").text
    assert "1.200 Liter (40 %)" in client.get("/history").text


def _portal_row(dealer, ct, liter=1000):
    db.execute(
        "INSERT INTO portal_offers (date, plz, liter, portal, dealer, ct_per_liter, total_eur, "
        "delivery_days, url) VALUES (?, '80331', ?, 'testportal', ?, ?, ?, 5, 'https://example.org/o')",
        (TODAY, liter, dealer, ct, ct * liter / 100),
    )


def test_dashboard_shows_portal_offers(client):
    register(client)
    onboard(client)
    make_rec()
    _portal_row("Billig GmbH", 98.5, 2000)
    _portal_row("Teuer AG", 105.0, 2000)
    html = client.get("/").text
    assert "Angebote für PLZ 80331" in html and "Alle 2 Angebote" in html
    assert "Billig GmbH" in html and "Teuer AG" in html and "zum Portal" in html
    assert "Als mein Angebot merken" in html
    assert "3,0 Cent je Liter darüber" in html


def test_dashboard_without_offers_shows_hint_and_links(client):
    register(client)
    onboard(client)
    html = client.get("/").text
    assert "Noch keine Angebote geladen" in html and "heizoel24.de" in html


def test_offers_refresh_and_rate_limit(client, monkeypatch):
    from heizoel.fetchers import offers as om

    register(client)
    onboard(client)
    calls = []

    def fake(plz, liter, client=None):
        calls.append((plz, liter))
        return [om.PortalOffer("t", "Händler", 99.0, 990.0, 3, "https://example.org", liter, plz)]

    monkeypatch.setattr(om, "fetch_offers", fake)
    r = client.post("/offers/refresh")
    assert r.status_code == 303 and calls == [("80331", 1000)]
    assert db.query_one("SELECT COUNT(*) AS n FROM portal_offers")["n"] == 1
    assert "1 Angebote" in client.get("/").text
    client.post("/offers/refresh")
    assert len(calls) == 1  # Rate-Limit
    assert "Zuletzt vor" in client.get("/").text


def _fake_fetch(monkeypatch):
    from heizoel.fetchers import offers as om

    calls = []

    def fake(plz, liter, client=None):
        calls.append((plz, liter))
        return [om.PortalOffer("t", "Händler", 99.0, 990.0, 3, "https://example.org", liter, plz)]

    monkeypatch.setattr(om, "fetch_offers", fake)
    return calls


def test_offers_refresh_custom_liter_auto_and_rate_limit_per_liter(client, monkeypatch):
    register(client)
    onboard(client)
    calls = _fake_fetch(monkeypatch)
    assert client.post("/offers/refresh", data={"liter": "1500"}).status_code == 303
    assert calls == [("80331", 1500)]
    assert db.query_one("SELECT offer_liter FROM profiles")["offer_liter"] == 1500
    html = client.get("/").text
    assert "Angebote für PLZ 80331, 1.500 Liter" in html and "zurück auf automatisch" in html
    client.post("/offers/refresh", data={"liter": "1500"})
    assert len(calls) == 1  # gleiche Menge gedrosselt
    client.post("/offers/refresh", data={"liter": "2000"})
    assert calls[-1] == ("80331", 2000)  # andere Menge nicht gedrosselt
    client.post("/offers/refresh", data={"liter": "auto"})
    assert db.query_one("SELECT offer_liter FROM profiles")["offer_liter"] is None
    assert calls[-1] == ("80331", 1000)


def test_offers_diff_uses_surcharge_of_chosen_liter(client):
    register(client)
    onboard(client)
    make_rec()  # need_liter 2000 -> Zuschlag 2.0
    db.execute("UPDATE profiles SET offer_liter = 3000")
    _portal_row("Billig GmbH", 95.5, 3000)
    html = client.get("/").text
    # Schätzwert 95.5 - 2.0 + 0.0 = 93.5
    assert "Angebote für PLZ 80331, 3.000 Liter" in html
    assert "2,0 Cent je Liter darüber" in html


def test_offers_table_sortable_markup(client):
    register(client)
    onboard(client)
    make_rec()
    _portal_row("Billig GmbH", 98.5, 2000)
    html = client.get("/").text
    assert 'data-sort="98.5"' in html and 'data-sort="billig gmbh"' in html
    assert 'id="offer-sort"' in html and "Sortieren nach" in html


def _make_rec(verdict="wait", percentile=97, rule="r", reason="Technischer Satz p97.",
              next_step_date=None, next_step_ct=None, season=1.2):
    db.execute(
        "INSERT INTO recommendations (user_id, date, verdict, rule, reason, personal_ct, "
        "percentile_365, season_deviation_ct, range_days, latest_order_date, next_step_date, "
        "next_step_ct, need_liter, inputs_json) "
        "VALUES (1, ?, ?, ?, ?, 95.5, ?, ?, 60, ?, ?, ?, 2000, '{}')",
        (TODAY, verdict, rule, reason, percentile, season, TODAY, next_step_date, next_step_ct),
    )


def test_answer_card_wait_and_everyday_sentence(client):
    register(client)
    onboard(client)
    _make_rec("wait", 97)
    html = client.get("/").text
    assert "Soll ich jetzt Heizöl kaufen?" in html and "Nein, noch warten" in html
    assert "so teuer wie an fast keinem Tag im letzten Jahr" in html
    assert "Dein Tank reicht noch etwa" in html


def test_answer_card_variants(client):
    register(client)
    onboard(client)
    _make_rec("buy_before_step", 10, next_step_date="2030-12-06", next_step_ct=3.0)
    html = client.get("/").text
    assert "Ja, vor dem Fr., 06.12.2030 kaufen" in html
    assert "durch die CO2-Abgabe etwa 3,0 Cent je Liter teurer" in html
    assert "so günstig wie selten" in html
    db.execute("DELETE FROM recommendations")
    _make_rec("buy_now", 50, rule="range_critical")
    assert "Dein Tank wird knapp, bestelle spätestens am" in client.get("/").text
    db.execute("DELETE FROM recommendations")
    _make_rec("watch", None)
    html = client.get("/").text
    assert "Noch nicht, aber im Blick behalten" in html
    assert "Für einen Preisvergleich fehlen noch Daten." in html


def test_why_section_contains_technical_sentence(client):
    register(client)
    onboard(client)
    _make_rec("wait", 97)
    html = client.get("/").text
    assert "<summary" in html and "Warum?" in html
    assert "Technischer Satz p97." in html
    assert "Preis heute: 95,5 Cent je Liter (dein Preis für 2.000 Liter)" in html
    assert "Günstiger als an 3 von 100 Tagen im letzten Jahr" in html
    assert "1,2 Cent über dem Üblichen für " in html


def test_split_plan_tip_from_dummy_module(client, monkeypatch):
    register(client)
    onboard(client)
    _make_rec("buy_now", 10)
    mod = types.ModuleType("heizoel.decision.planner")
    mod.split_plan = lambda need, min_order, price, tank=None: {
        "possible": True, "text": "Kaufe zuerst 1.000 Liter.", "split": {}}
    monkeypatch.setitem(sys.modules, "heizoel.decision.planner", mod)
    assert "Tipp: Kaufe zuerst 1.000 Liter." in client.get("/").text


def test_best_offer_block_prices_per_100_liter(client):
    register(client)
    onboard(client)
    make_rec()
    _portal_row("Billig GmbH", 164.43, 2700)
    html = client.get("/").text
    assert "Dein günstigstes Angebot heute" in html
    assert "164,43 € je 100 Liter" in html and "4.440 € für 2.700 Liter" in html
    assert "Lieferung in etwa 5 Tagen" in html and "Zum Händler" in html
    assert "Für andere Menge prüfen" in html


def test_tank_block_and_stale_hint(client):
    from datetime import timedelta

    register(client)
    onboard(client)
    assert "Dein letzter Stand ist" not in client.get("/").text
    old = (date.today() - timedelta(days=40)).isoformat()
    db.execute("UPDATE profiles SET fill_date = ?", (old,))
    html = client.get("/").text
    assert "Dein letzter Stand ist 40 Tage alt. Wie viel ist heute im Tank?" in html
    assert "Aktualisieren" in html and "lieber in Prozent" in html
    assert "Angenommener Verbrauch: 2.500 Liter im Jahr" in html


def test_tank_learned_consumption(client):
    register(client)
    onboard(client)
    for col in ("learned_annual_liter", "learned_confidence"):
        try:  # je nach Schemastand schon vorhanden
            db.execute(f"ALTER TABLE profiles ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass
    db.execute("UPDATE profiles SET learned_annual_liter = 2204, learned_confidence = 0.5")
    assert "Dein tatsächlicher Verbrauch: etwa 2.200 Liter im Jahr" in client.get("/").text
    db.execute("UPDATE profiles SET learned_confidence = 0.1")
    assert "Angenommener Verbrauch" in client.get("/").text


def test_learn_consumption_called_after_fill_and_purchase(client, monkeypatch):
    register(client)
    onboard(client)
    calls = []
    mod = types.ModuleType("heizoel.decision.learning")

    def boom(user_id, today):
        calls.append(user_id)
        raise RuntimeError("kaputt")

    mod.learn_consumption = boom
    monkeypatch.setitem(sys.modules, "heizoel.decision.learning", mod)
    assert client.post("/fill-level", data={"liter": "900"}).status_code == 303
    assert client.post("/purchase", data={"liter": "500", "ct_per_liter": "99"}).status_code == 303
    assert calls == [1, 1]  # Fehler werden geschluckt


def test_onboarding_half_full_checkbox(client):
    register(client)
    html = client.get("/onboarding").text
    assert "Kurz drei Dinge zu deinem Haus" in html and "ungefähr halb voll" in html
    assert "Weitere Einstellungen (kannst du später ändern)" in html
    r = onboard(client, liter="", fill_percent="", half="1")
    assert r.status_code == 303
    assert db.query_one("SELECT fill_percent FROM profiles")["fill_percent"] == 50.0


def test_history_with_mocked_stats(client, monkeypatch):
    register(client)
    onboard(client)
    _make_rec("wait", 50)
    client.post("/purchase", data={"date": TODAY, "liter": "1500", "ct_per_liter": "98.5"})
    mod = types.ModuleType("heizoel.decision.stats")
    mod.purchase_stats = lambda uid: {
        "purchases": [{"date": TODAY, "liter": 1500, "delta_ct": -2.3}], "total_liter": 1500,
        "total_eur": 1477.5, "avg_ct": 98.5, "total_delta_eur": -34.5, "n": 1}
    mod.user_backtest = lambda uid, today: {
        "text": "Mit der Regel hättest du 120 Euro gespart.", "saving_eur": 120,
        "saving_pct": 3.0, "years": 2}
    monkeypatch.setitem(sys.modules, "heizoel.decision.stats", mod)
    html = client.get("/history").text
    assert "Deine Käufe" in html and "2,30 € je 100 L günstiger" in html
    assert "Insgesamt 1.500 Liter für 1.478 €" in html
    assert "Was hätte die Regel gebracht?" in html and "120 Euro gespart" in html
    assert "Deine Füllstände" in html and "Frühere Einschätzungen" in html
    assert "Nein, noch warten" in html


def test_history_without_stats_module(client):
    register(client)
    onboard(client)
    html = client.get("/history").text
    assert "Dafür brauchen wir mehr Preisdaten" in html


def test_settings_threshold_select(client):
    register(client)
    onboard(client)
    html = client.get("/settings").text
    assert "Ab wann gilt für dich günstig?" in html
    assert "sehr günstig (unter den günstigsten 20 %)" in html
