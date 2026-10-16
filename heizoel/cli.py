"""Kommandozeile: `uv run heizoel <befehl>`."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import date, timedelta

from heizoel import db


def _date(s: str) -> date:
    return date.fromisoformat(s)


def cmd_init(_: argparse.Namespace) -> None:
    db.init_db()
    print("Datenbank bereit.")


def cmd_create_user(a: argparse.Namespace) -> None:
    from heizoel.web.auth import create_user

    password = a.password or getpass.getpass("Passwort: ")
    uid = create_user(a.email, a.name, password, role="admin" if a.admin else "user")
    print(f"Nutzer {uid} angelegt ({'admin' if a.admin else 'user'}).")


def cmd_fetch(a: argparse.Namespace) -> None:
    from heizoel.fetchers.reference import fetch_today

    print(json.dumps(fetch_today(a.date or date.today()), ensure_ascii=False, default=str, indent=2))


def cmd_backfill(a: argparse.Namespace) -> None:
    from heizoel.fetchers.reference import backfill

    start = a.start or date.today() - timedelta(days=365 * 5)
    end = a.end or date.today()
    n = backfill(start, end)
    print(f"{n} Tage als Fallback geschrieben ({start} bis {end}).")


def cmd_import_history(a: argparse.Namespace) -> None:
    from heizoel.fetchers.history_import import import_csv

    print(f"{import_csv(a.path)} Zeilen importiert.")


def cmd_manual_price(a: argparse.Namespace) -> None:
    from heizoel.fetchers.reference import set_manual_price

    set_manual_price(a.date or date.today(), a.ct)
    print("Gespeichert.")


def cmd_offsets(a: argparse.Namespace) -> None:
    from heizoel.jobs.offsets import compute_region_offsets

    print(compute_region_offsets(a.date or date.today()))


def cmd_recommend(a: argparse.Namespace) -> None:
    from heizoel.decision.service import compute_all
    from heizoel.models import VERDICT_LABELS

    results = compute_all(a.date or date.today())
    if not results:
        print("Keine Empfehlungen (kein Profil oder kein Referenzpreis).")
    for uid, r in results.items():
        print(f"Nutzer {uid}: {VERDICT_LABELS[r.verdict]} [{r.rule}] {r.personal_ct:.1f} ct/L")
        print(f"  {r.reason}")


def cmd_daily(a: argparse.Namespace) -> None:
    from heizoel.jobs.daily import run_daily

    print(json.dumps(run_daily(a.date, notify=not a.no_notify), ensure_ascii=False, default=str, indent=2))


def cmd_news(a: argparse.Namespace) -> None:
    from heizoel.news.digest import run_digest

    res = run_digest(a.date or date.today(), force=a.force)
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))


def cmd_backtest(a: argparse.Namespace) -> None:
    from heizoel.co2 import load_steps
    from heizoel.decision.backtest import run_backtest
    from heizoel.decision.service import load_history

    history = load_history(date.today(), years=a.years)
    if len(history) < 400:
        print(f"Zu wenig Historie ({len(history)} Tage). Erst `heizoel backfill` ausführen.")
        return
    res = run_backtest(
        history, tank_liter=a.tank, annual_liter=a.annual, min_order_liter=a.min_order,
        lead_days=a.lead, co2_steps=load_steps(), threshold_percentile=a.threshold,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    db.init_db()
    if not a.no_scheduler:
        from heizoel.jobs.scheduler import start_scheduler

        start_scheduler()
    uvicorn.run("heizoel.web.app:app", host=a.host, port=a.port, reload=a.reload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="heizoel", description="Heizöl-Kaufberater")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Datenbank anlegen").set_defaults(fn=cmd_init)

    s = sub.add_parser("create-user", help="Nutzer anlegen")
    s.add_argument("--email", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--password")
    s.add_argument("--admin", action="store_true")
    s.set_defaults(fn=cmd_create_user)

    s = sub.add_parser("fetch", help="Referenzpreis für heute holen")
    s.add_argument("--date", type=_date)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("backfill", help="Historie aus Brent/EURUSD als Fallback berechnen")
    s.add_argument("--start", type=_date)
    s.add_argument("--end", type=_date)
    s.set_defaults(fn=cmd_backfill)

    s = sub.add_parser("import-history", help="CSV date,ct_per_liter importieren")
    s.add_argument("path")
    s.set_defaults(fn=cmd_import_history)

    s = sub.add_parser("manual-price", help="Referenzpreis von Hand setzen")
    s.add_argument("ct", type=float)
    s.add_argument("--date", type=_date)
    s.set_defaults(fn=cmd_manual_price)

    s = sub.add_parser("offsets", help="Regionaloffsets berechnen")
    s.add_argument("--date", type=_date)
    s.set_defaults(fn=cmd_offsets)

    s = sub.add_parser("recommend", help="Empfehlungen für alle Nutzer berechnen")
    s.add_argument("--date", type=_date)
    s.set_defaults(fn=cmd_recommend)

    s = sub.add_parser("daily", help="Kompletter Tageslauf")
    s.add_argument("--date", type=_date)
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_daily)

    s = sub.add_parser("news", help="Nachrichten-Lagebild erzeugen (braucht HEIZOEL_LLM_DRIVER)")
    s.add_argument("--date", type=_date)
    s.add_argument("--force", action="store_true", help="vorhandenes Lagebild des Tages ersetzen")
    s.set_defaults(fn=cmd_news)

    s = sub.add_parser("backtest", help="Kaufregel gegen Historie testen")
    s.add_argument("--tank", type=int, default=3000)
    s.add_argument("--annual", type=int, default=2500)
    s.add_argument("--min-order", type=int, default=1000)
    s.add_argument("--lead", type=int, default=10)
    s.add_argument("--threshold", type=int, default=30)
    s.add_argument("--years", type=int, default=5)
    s.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("serve", help="Webserver mit Scheduler starten")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.add_argument("--no-scheduler", action="store_true")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
