"""Täglicher Lauf: Referenzpreis holen, Regionaloffsets, Empfehlungen, Benachrichtigungen."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from heizoel import db
from heizoel.config import settings

log = logging.getLogger(__name__)


def _run_step(name: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = db.execute("INSERT INTO job_runs (job, started_at) VALUES (?, ?)", (name, started))
    try:
        value = fn()
        ok, message = True, _short(value)
    except Exception as exc:
        log.exception("Schritt %s fehlgeschlagen", name)
        ok, message, value = False, f"{type(exc).__name__}: {exc}", None
    db.execute(
        "UPDATE job_runs SET finished_at = ?, ok = ?, message = ? WHERE id = ?",
        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), int(ok), message[:500], run_id),
    )
    return ok, value


def _short(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in list(value.items())[:8])
    return str(value)


def run_daily(today: date | None = None, notify: bool = True) -> dict[str, Any]:
    """Führt alle Schritte aus. Ein fehlgeschlagener Schritt bricht die folgenden nicht ab."""
    from heizoel.decision.service import compute_all
    from heizoel.fetchers.reference import fetch_today
    from heizoel.jobs.notify import fill_level_reminders, notify_users
    from heizoel.jobs.offsets import compute_region_offsets

    today = today or date.today()
    summary: dict[str, Any] = {"date": today.isoformat()}

    ok, fetched = _run_step("fetch_reference", lambda: fetch_today(today))
    summary["fetch"] = fetched if ok else "fehlgeschlagen"

    ok, offsets = _run_step("region_offsets", lambda: compute_region_offsets(today))
    summary["offsets"] = len(offsets) if ok else "fehlgeschlagen"

    from heizoel.fetchers.offers import refresh_offers

    ok, portal = _run_step("portal_offers", lambda: refresh_offers(today))
    summary["portal_offers"] = portal if ok else "fehlgeschlagen"

    if settings.llm_driver != "none":
        from heizoel.news.digest import run_digest

        ok, digest = _run_step("news_digest", lambda: run_digest(today))
        summary["news"] = digest if ok else "fehlgeschlagen"

    try:
        from heizoel.decision.learning import learn_all
    except ImportError:  # Modul noch nicht vorhanden
        learn_all = None
    if learn_all is not None:
        ok, learned = _run_step("consumption_learning", lambda: learn_all(today))
        summary["learned"] = learned if ok else "fehlgeschlagen"

    ok, results = _run_step("recommendations", lambda: compute_all(today))
    summary["recommendations"] = len(results) if ok else "fehlgeschlagen"

    if notify and ok and results:
        ok2, sent = _run_step("notify", lambda: notify_users(results, today))
        summary["notified"] = sent if ok2 else "fehlgeschlagen"

    if notify:
        ok, reminded = _run_step("fill_reminders", lambda: fill_level_reminders(today))
        summary["fill_reminders"] = reminded if ok else "fehlgeschlagen"

    log.info("Tageslauf: %s", summary)
    return summary
