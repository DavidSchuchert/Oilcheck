"""Benachrichtigungen. Zunächst nur Mail; ohne SMTP-Konfiguration wird geloggt statt gesendet."""

from __future__ import annotations

import logging
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage

from heizoel import db
from heizoel.config import settings
from heizoel.models import VERDICT_LABELS, DecisionResult

log = logging.getLogger(__name__)

REMIND_AFTER_DAYS = 7  # Kaufempfehlung erneut senden, wenn sie so lange unverändert besteht


def send_mail(to: str, subject: str, body: str) -> bool:
    if not settings.smtp_host:
        log.info("Mail (nicht gesendet, kein SMTP): an=%s betreff=%s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    return True


def _should_notify(user_id: int, today: date, result: DecisionResult) -> bool:
    previous = db.query_one(
        "SELECT verdict FROM recommendations WHERE user_id = ? AND date < ? "
        "ORDER BY date DESC LIMIT 1",
        (user_id, today.isoformat()),
    )
    changed = previous is None or previous["verdict"] != result.verdict
    if changed and result.verdict in ("buy_now", "buy_before_step"):
        return True
    if changed and previous is not None and previous["verdict"] in ("buy_now", "buy_before_step"):
        return True  # Entwarnung
    if result.verdict in ("buy_now", "buy_before_step"):
        last = db.query_one(
            "SELECT date FROM notifications_sent WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (user_id,),
        )
        if last is None:
            return True
        return date.fromisoformat(last["date"]) <= today - timedelta(days=REMIND_AFTER_DAYS)
    return False


ANSWER = {
    "buy_now": "Ja, jetzt kaufen.",
    "buy_before_step": "Ja, vor dem Stichtag kaufen.",
    "wait": "Nein, noch warten.",
    "watch": "Noch nicht, aber im Blick behalten.",
}


def _price_level(p: float | None) -> str:
    if p is None:
        return "Für einen Preisvergleich fehlen noch Daten."
    if p <= 20:
        return "Heizöl ist gerade so günstig wie selten im letzten Jahr."
    if p <= 40:
        return "Heizöl ist gerade eher günstig."
    if p <= 60:
        return "Heizöl kostet gerade etwa so viel wie im Durchschnitt des letzten Jahres."
    if p <= 80:
        return "Heizöl ist gerade eher teuer."
    return "Heizöl ist gerade so teuer wie an fast keinem Tag im letzten Jahr."


def _range_text(days: float, latest: date) -> str:
    if days < 70:
        span = f"{max(1, round(days / 7))} Wochen"
    else:
        span = f"{max(1, round(days / 30))} Monate"
    return f"Dein Tank reicht noch etwa {span}. Spätestens bestellen am {latest.strftime('%d.%m.%Y')}."


def _body(name: str, result: DecisionResult) -> str:
    lines = [
        f"Hallo {name},",
        "",
        "Soll ich jetzt Heizöl kaufen?",
        ANSWER[result.verdict],
        "",
        _price_level(result.percentile_365),
        _range_text(result.range_days, result.latest_order_date),
    ]
    if result.next_step_days is not None and result.next_step_ct and result.next_step_ct >= 0.5:
        lines.append(
            f"In {result.next_step_days} Tagen wird Heizöl durch die CO2-Abgabe etwa "
            f"{result.next_step_ct:.0f} Cent je Liter teurer."
        )
    lines += [
        "",
        f"Dein Preis heute: etwa {result.personal_ct:.0f} Cent je Liter für {result.need_liter} Liter.",
        f"Jeder Cent je Liter macht bei dir {result.eur_per_ct:.0f} Euro aus.",
    ]
    if result.split_suggestion:
        lines += ["", result.split_suggestion]
    lines += ["", f"Alles im Detail: {settings.app_url}/", "",
              "Das ist eine Einordnung des heutigen Preises, keine Vorhersage."]
    return "\n".join(lines)


def notify_users(results: dict[int, DecisionResult], today: date | None = None) -> int:
    today = today or date.today()
    sent = 0
    for user_id, result in results.items():
        row = db.query_one(
            "SELECT u.email, u.name, p.notify_channel FROM users u "
            "JOIN profiles p ON p.user_id = u.id WHERE u.id = ?",
            (user_id,),
        )
        if row is None or row["notify_channel"] == "none":
            continue
        if not _should_notify(user_id, today, result):
            continue
        subject = f"Heizöl: {VERDICT_LABELS[result.verdict]} ({result.personal_ct:.1f} ct/L)"
        try:
            send_mail(row["email"], subject, _body(row["name"], result))
        except Exception as exc:  # noqa: BLE001
            log.warning("Mail an %s fehlgeschlagen: %s", row["email"], exc)
            continue
        db.execute(
            "INSERT OR REPLACE INTO notifications_sent (user_id, date, verdict, channel) "
            "VALUES (?, ?, ?, 'mail')",
            (user_id, today.isoformat(), result.verdict),
        )
        sent += 1
    return sent


FILL_REMINDER_DAYS = 30


def fill_level_reminders(today: date | None = None) -> int:
    """Erinnert per Mail, wenn der letzte Füllstand älter als FILL_REMINDER_DAYS ist, höchstens alle 30 Tage."""
    today = today or date.today()
    cutoff = (today - timedelta(days=FILL_REMINDER_DAYS)).isoformat()
    rows = db.query(
        "SELECT u.id, u.email, u.name, p.fill_date, p.tank_liter, p.fill_percent FROM users u "
        "JOIN profiles p ON p.user_id = u.id "
        "WHERE p.notify_channel = 'mail' AND p.fill_date <= ? "
        "AND NOT EXISTS (SELECT 1 FROM reminders_sent r WHERE r.user_id = u.id "
        "                AND r.kind = 'fill_level' AND r.date > ?)",
        (cutoff, cutoff),
    )
    sent = 0
    for r in rows:
        age = (today - date.fromisoformat(r["fill_date"])).days
        liter = round(r["tank_liter"] * r["fill_percent"] / 100 / 10) * 10
        body = "\n".join([
            f"Hallo {r['name']},",
            "",
            f"dein letzter Tankstand ist {age} Tage alt (damals etwa {liter} Liter).",
            "Wie viel ist heute im Tank? Ein Blick auf die Anzeige reicht, dann stimmt die Einschätzung wieder.",
            "",
            f"Hier eintragen: {settings.app_url}/",
        ])
        try:
            send_mail(r["email"], "Heizöl: Wie voll ist dein Tank?", body)
        except Exception as exc:  # noqa: BLE001
            log.warning("Erinnerung an %s fehlgeschlagen: %s", r["email"], exc)
            continue
        db.execute(
            "INSERT OR IGNORE INTO reminders_sent (user_id, kind, date) VALUES (?, 'fill_level', ?)",
            (r["id"], today.isoformat()),
        )
        sent += 1
    return sent
