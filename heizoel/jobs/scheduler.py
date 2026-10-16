"""Zeitplan im selben Prozess wie der Webserver. Alternative: System-Cron ruft `heizoel daily` auf."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from heizoel.config import settings

log = logging.getLogger(__name__)


def start_scheduler() -> BackgroundScheduler:
    from heizoel.jobs.daily import run_daily

    hour, minute = (int(x) for x in settings.daily_run_at.split(":"))
    scheduler = BackgroundScheduler(timezone="Europe/Berlin")
    scheduler.add_job(
        run_daily,
        CronTrigger(hour=hour, minute=minute),
        id="daily",
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()
    log.info("Scheduler gestartet, Tageslauf um %02d:%02d Europe/Berlin", hour, minute)
    return scheduler
