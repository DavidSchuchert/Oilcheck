"""Tests für Benachrichtigungen und Erinnerungen (ohne SMTP: send_mail loggt nur)."""

from __future__ import annotations

from datetime import date, timedelta

from heizoel import db
from heizoel.jobs import notify
from heizoel.models import DecisionResult


def _user(email="a@b.de", fill_days_ago=45, channel="mail") -> int:
    uid = db.execute(
        "INSERT INTO users (email, name, password_hash) VALUES (?, 'Anna', 'x')", (email,)
    )
    fill_date = (date.today() - timedelta(days=fill_days_ago)).isoformat()
    db.execute(
        "INSERT INTO profiles (user_id, plz, region, tank_liter, fill_percent, fill_date, "
        "annual_consumption_liter, notify_channel) VALUES (?, '80331', '80', 3000, 40, ?, 2500, ?)",
        (uid, fill_date, channel),
    )
    return uid


def test_fill_reminder_once_per_month(tmp_db):
    _user()
    assert notify.fill_level_reminders() == 1
    assert notify.fill_level_reminders() == 0
    assert db.query_one("SELECT COUNT(*) c FROM reminders_sent")["c"] == 1


def test_fill_reminder_skips_fresh_and_silent(tmp_db):
    _user("fresh@b.de", fill_days_ago=5)
    _user("silent@b.de", fill_days_ago=90, channel="none")
    assert notify.fill_level_reminders() == 0


def test_mail_body_plain_language():
    result = DecisionResult(
        verdict="wait", rule="expensive_and_time", reason="Perzentil 97",
        personal_ct=176.1, quantity_surcharge_ct=2.0, percentile_365=97.0, percentile_730=95.0,
        season_deviation_ct=27.7, current_liter=1580, daily_liter=12.0, range_days=103,
        latest_order_date=date(2027, 1, 3), need_liter=2700, eur_per_ct=27.0,
        next_step_date=date(2027, 1, 1), next_step_days=77, next_step_ct=0.0,
        next_step_eur_for_need=0.0, split_suggestion=None,
    )
    body = notify._body("Anna", result)
    assert "Nein, noch warten." in body
    assert "so teuer wie an fast keinem Tag" in body
    assert "etwa 3 Monate" in body
    assert "Perzentil" not in body
    assert "CO2-Abgabe" not in body  # Stufe ohne Preisänderung wird nicht erwähnt
