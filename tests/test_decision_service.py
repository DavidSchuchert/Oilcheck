from datetime import date, timedelta

from heizoel import db
from heizoel.decision import service

TODAY = date(2026, 10, 16)


def _setup(n_days=400, with_profile=True):
    db.execute("INSERT INTO users (id, email, name, password_hash) VALUES (1, 'a@b.de', 'A', 'x')")
    if with_profile:
        db.execute(
            "INSERT INTO profiles (user_id, plz, region, tank_liter, fill_percent, fill_date, "
            "annual_consumption_liter) VALUES (1, '80331', '80', 3000, 40, ?, 2500)",
            (TODAY.isoformat(),))
    rows = [((TODAY - timedelta(days=i)).isoformat(), 80.0 + (i % 30)) for i in range(n_days)]
    db.executemany(
        "INSERT INTO reference_prices (date, source, ct_per_liter) VALUES (?, 'import', ?)", rows)


def test_compute_and_store_upserts(tmp_db):
    _setup()
    db.execute("INSERT INTO region_offsets (region, date, offset_ct, n_offers) "
               "VALUES ('80', ?, 2.0, 5)", (TODAY.isoformat(),))
    res = service.compute_and_store(1, TODAY)
    assert res is not None
    rows = db.query("SELECT * FROM recommendations")
    assert len(rows) == 1
    assert rows[0]["verdict"] == res.verdict and rows[0]["rule"] == res.rule
    assert rows[0]["date"] == TODAY.isoformat()
    assert rows[0]["need_liter"] == res.need_liter
    assert '"region_offset_ct": 2.0' in rows[0]["inputs_json"]

    db.execute("UPDATE profiles SET fill_percent = 95 WHERE user_id = 1")
    res2 = service.compute_and_store(1, TODAY)
    rows = db.query("SELECT * FROM recommendations")
    assert len(rows) == 1
    assert rows[0]["rule"] == res2.rule == "tank_full"
    assert service.load_latest_recommendation(1)["rule"] == "tank_full"


def test_no_profile_or_no_price(tmp_db):
    _setup(with_profile=False)
    assert service.build_inputs(1, TODAY) is None
    assert service.compute_and_store(1, TODAY) is None
    assert service.load_latest_recommendation(1) is None
    assert service.compute_and_store(1, TODAY + timedelta(days=30)) is None


def test_helpers(tmp_db):
    _setup()
    assert service.latest_reference(TODAY + timedelta(days=3))[0] == TODAY
    assert service.latest_reference(TODAY + timedelta(days=9)) is None
    assert service.latest_region_offset("80", TODAY) == (0.0, 0)
    hist = service.load_history(TODAY)
    assert hist == sorted(hist) and len(hist) == 400
    assert service.load_profile(1).tank_liter == 3000


def test_compute_all(tmp_db):
    _setup()
    out = service.compute_all(TODAY)
    assert list(out) == [1]
