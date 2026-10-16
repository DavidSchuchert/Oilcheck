"""Alle Seiten der Weboberfläche."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from heizoel import co2, db
from heizoel.config import settings
from heizoel.decision.engine import quantity_surcharge_ct
from heizoel.fetchers import offers as offers_mod
from heizoel.web import auth, viewmodel
from heizoel.web.auth import require_admin, require_profile, require_user
from heizoel.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

OUTLIER_CT = 15.0
DEP_user = Depends(require_user)
DEP_profile = Depends(require_profile)
DEP_admin = Depends(require_admin)


# --------------------------------------------------------------------------- Helfer


def _go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": message, "kind": kind}


def _render(request: Request, name: str, user=None, status_code: int = 200, **ctx):
    flash = request.session.pop("flash", None)
    ctx.setdefault("flash", flash)
    ctx["user"] = user if user is not None else auth.get_current_user(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _parse_date(value: str | None, default: date | None = None) -> date:
    if not value:
        if default is None:
            raise ValueError("Datum fehlt.")
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Ungültiges Datum.") from exc


def _num(value: str, label: str, lo: float, hi: float) -> float:
    try:
        number = float(str(value).replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{label}: bitte eine Zahl angeben.") from exc
    if not lo <= number <= hi:
        raise ValueError(f"{label}: Wert muss zwischen {lo:g} und {hi:g} liegen.")
    return number


def _fill_percent(liter: str, percent: str, tank_liter: float) -> float:
    """Füllstand in Prozent aus Liter (hat Vorrang) oder Prozent; mindestens eins ist nötig."""
    if str(liter).strip():
        value = _num(liter, "Liter im Tank", 0, float("inf"))
        if not tank_liter:
            raise ValueError("Tankgröße fehlt.")
        return round(min(100.0, max(0.0, value / tank_liter * 100)), 1)
    if str(percent).strip():
        return _num(percent, "Füllstand", 0, 100)
    raise ValueError("Bitte Liter im Tank oder Füllstand in Prozent angeben.")


def _compute(user_id: int) -> None:
    """Empfehlung neu berechnen; Fehler werden geloggt, nie weitergereicht."""
    try:
        from heizoel.decision.service import compute_and_store

        compute_and_store(user_id, date.today())
    except Exception:
        log.exception("compute_and_store fehlgeschlagen für Nutzer %s", user_id)


def _learn(user_id: int) -> None:
    """Verbrauch aus den Angaben lernen; fehlt das Modul oder schlägt es fehl, passiert nichts."""
    try:
        from heizoel.decision.learning import learn_consumption

        learn_consumption(user_id, date.today())
    except ImportError:
        pass
    except Exception:
        log.exception("learn_consumption fehlgeschlagen für Nutzer %s", user_id)


def _learned(user_id: int) -> tuple[float | None, float | None]:
    try:
        row = db.query_one(
            "SELECT learned_annual_liter, learned_confidence FROM profiles WHERE user_id = ?",
            (user_id,),
        )
    except sqlite3.OperationalError:
        return None, None
    if row is None:
        return None, None
    return row["learned_annual_liter"], row["learned_confidence"]


def _split_plan(rec, profile) -> dict | None:
    try:
        from heizoel.decision.planner import split_plan

        plan = split_plan(rec["need_liter"], profile["min_order_liter"], rec["personal_ct"],
                          profile["tank_liter"])
        return plan if isinstance(plan, dict) else None
    except ImportError:
        return None
    except Exception:
        log.exception("split_plan fehlgeschlagen")
        return None


CATEGORY_LABELS = {
    "crude": "Rohöl", "refinery": "Raffinerie", "geopolitics": "Geopolitik",
    "co2_law": "CO2/Gesetz", "heating_oil": "Heizölmarkt", "other": "Sonstiges",
    "rohoel": "Rohöl", "raffinerie": "Raffinerie", "geopolitik": "Geopolitik",
    "co2": "CO2/Gesetz", "co2_gesetz": "CO2/Gesetz", "heizoel": "Heizölmarkt", "heizoel_markt": "Heizölmarkt", "sonstiges": "Sonstiges",
}


def _category_label(value: str | None) -> str:
    key = (value or "").strip()
    return CATEGORY_LABELS.get(key.lower(), key or "Sonstiges")


def _digest_from_sql(day: str) -> dict | None:
    row = db.query_one("SELECT * FROM news_digests WHERE date = ?", (day,))
    if row is None:
        return None
    try:
        items = json.loads(row["items_json"] or "[]")
    except (TypeError, ValueError):
        items = []
    return {
        "date": row["date"], "situation": row["summary"],
        "items": items if isinstance(items, list) else [],
        "model": row["model"], "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"], "duration_ms": row["duration_ms"],
    }


def _latest_digest(today: date, max_age_days: int = 3) -> dict | None:
    """Jüngster Digest, höchstens max_age_days alt. Fällt ohne news-Modul auf SQL zurück."""
    row = db.query_one(
        "SELECT date FROM news_digests WHERE date >= ? ORDER BY date DESC LIMIT 1",
        ((today - timedelta(days=max_age_days)).isoformat(),),
    )
    if row is None:
        return None
    digest = None
    try:
        from heizoel.news.digest import load_digest

        digest = load_digest(date.fromisoformat(row["date"]))
    except Exception:  # noqa: BLE001
        digest = None
    if not digest:
        try:
            digest = _digest_from_sql(row["date"])
        except Exception:
            log.exception("Digest konnte nicht gelesen werden")
            return None
    if not digest:
        return None
    digest["items"] = [
        {**it, "category_label": _category_label(it.get("category"))}
        for it in (digest.get("items") or []) if isinstance(it, dict)
    ]
    return digest


def _open_hints_count() -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM rule_change_hints WHERE status IN ('open', 'confirmed')"
    )["n"]


def _latest_reference() -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM reference_prices ORDER BY date DESC LIMIT 1")


def _reference_at_or_before(day: str) -> float | None:
    row = db.query_one(
        "SELECT ct_per_liter FROM reference_prices WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (day,),
    )
    return row["ct_per_liter"] if row else None


def _profile_form(
    plz: str, tank_liter: str, annual: str, min_order: str, lead_days: str, notify: str
) -> dict:
    plz = plz.strip()
    if not re.fullmatch(r"\d{5}", plz):
        raise ValueError("Die PLZ muss aus genau 5 Ziffern bestehen.")
    if notify not in ("mail", "none"):
        raise ValueError("Ungültiger Benachrichtigungskanal.")
    return {
        "plz": plz,
        "region": plz[:2],
        "tank_liter": int(_num(tank_liter, "Tankgröße", 100, 100000)),
        "annual": int(_num(annual, "Jahresverbrauch", 100, 100000)),
        "min_order": int(_num(min_order, "Mindestbestellmenge", 100, 50000)),
        "lead_days": int(_num(lead_days, "Lieferzeit", 0, 90)),
        "notify": notify,
    }


# --------------------------------------------------------------------------- Auth


@router.get("/register")
def register_form(request: Request):
    return _render(request, "register.html", form={}, error=None)


@router.post("/register")
def register(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    invite: str = Form(""),
):
    form = {"name": name, "email": email}
    if invite.strip() != settings.invite_code:
        return _render(request, "register.html", status_code=400, form=form,
                       error="Der Einladungscode ist falsch.")
    first = db.query_one("SELECT COUNT(*) AS n FROM users")["n"] == 0
    try:
        user_id = auth.create_user(email, name, password, role="admin" if first else "user")
    except ValueError as exc:
        return _render(request, "register.html", status_code=400, form=form, error=str(exc))
    request.session.clear()
    request.session["user_id"] = user_id
    return _go("/onboarding")


@router.get("/login")
def login_form(request: Request):
    if auth.get_current_user(request):
        return _go("/")
    return _render(request, "login.html", form={}, error=None)


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form("")):
    user = auth.authenticate(email, password)
    if user is None:
        return _render(request, "login.html", status_code=400, form={"email": email},
                       error="E-Mail oder Passwort stimmt nicht.")
    request.session.clear()
    request.session["user_id"] = user["id"]
    return _go("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _go("/login")


# --------------------------------------------------------------------------- Onboarding


@router.get("/onboarding")
def onboarding_form(request: Request, user=DEP_user):
    if db.query_one("SELECT 1 FROM profiles WHERE user_id = ?", (user["id"],)):
        return _go("/")
    return _render(request, "onboarding.html", user=user, error=None,
                   form={"liter": "", "fill_percent": "", "fill_date": date.today().isoformat(),
                         "min_order": 1000, "lead_days": 10, "notify": "mail", "half": ""})


@router.post("/onboarding")
def onboarding(
    request: Request,
    plz: str = Form(""),
    tank_liter: str = Form(""),
    liter: str = Form(""),
    fill_percent: str = Form(""),
    fill_date: str = Form(""),
    annual: str = Form(""),
    min_order: str = Form("1000"),
    lead_days: str = Form("10"),
    notify: str = Form("mail"),
    half: str = Form(""),
    user=DEP_user,
):
    form = {"plz": plz, "tank_liter": tank_liter, "liter": liter, "fill_percent": fill_percent,
            "fill_date": fill_date, "annual": annual, "min_order": min_order,
            "lead_days": lead_days, "notify": notify, "half": half}
    if half and not liter.strip() and not fill_percent.strip():
        fill_percent = "50"  # "ungefähr halb voll"
    try:
        values = _profile_form(plz, tank_liter, annual, min_order, lead_days, notify)
        percent = _fill_percent(liter, fill_percent, values["tank_liter"])
        day = _parse_date(fill_date, date.today())
    except ValueError as exc:
        return _render(request, "onboarding.html", user=user, status_code=400, form=form,
                       error=str(exc))
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profiles (user_id, plz, region, tank_liter, fill_percent, "
            "fill_date, annual_consumption_liter, min_order_liter, lead_days, notify_channel) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], values["plz"], values["region"], values["tank_liter"], percent,
             day.isoformat(), values["annual"], values["min_order"], values["lead_days"],
             values["notify"]),
        )
        conn.execute(
            "INSERT INTO fill_levels (user_id, date, fill_percent, source) "
            "VALUES (?, ?, ?, 'onboarding') "
            "ON CONFLICT(user_id, date, source) DO UPDATE SET fill_percent = excluded.fill_percent",
            (user["id"], day.isoformat(), percent),
        )
    _compute(user["id"])
    return _go("/")


# --------------------------------------------------------------------------- Dashboard


def _chart_data(user_id: int, today: date) -> dict:
    start = (today - timedelta(days=730)).isoformat()
    prices = db.query(
        "SELECT date, ct_per_liter FROM reference_prices WHERE date >= ? ORDER BY date", (start,)
    )
    labels = [r["date"] for r in prices]
    values = [round(r["ct_per_liter"], 2) for r in prices]
    purchases = db.query(
        "SELECT date, ct_per_liter FROM purchases WHERE user_id = ? AND date >= ? ORDER BY date",
        (user_id, start),
    )
    points: list[float | None] = [None] * len(labels)
    for p in purchases:
        idx = None
        for i, label in enumerate(labels):
            if label <= p["date"]:
                idx = i
            else:
                break
        if idx is not None:
            points[idx] = round(p["ct_per_liter"], 2)
    return {"labels": labels, "prices": values, "purchases": points}


def _offers_info(rows: list[sqlite3.Row], rec, ref_info, liter: int) -> dict | None:
    """Stand, Menge und Vergleich des besten Portal-Angebots mit Referenz- und Schätzwert."""
    if not rows:
        return None
    stand = max(r["fetched_at"] for r in rows)
    try:
        local = datetime.strptime(stand, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone()
        stand_text = local.strftime("%d.%m.%Y, %H:%M Uhr")
    except ValueError:
        stand_text = stand
    best = rows[0]["ct_per_liter"]
    personal = None
    if rec and rec["personal_ct"] is not None:
        # personal_ct gilt für need_liter; Mengenzuschlag auf die gewählte Menge umrechnen.
        personal = (
            rec["personal_ct"]
            - quantity_surcharge_ct(rec["need_liter"])
            + quantity_surcharge_ct(liter)
        )
    return {
        "stand": stand_text, "liter": rows[0]["liter"], "best": best,
        "ref_ct": ref_info["ct"] if ref_info else None, "personal_ct": personal,
        "diff": best - personal if personal is not None else None,
    }


@router.get("/")
def dashboard(request: Request, user=DEP_profile):
    uid = user["id"]
    today = date.today()
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (uid,))
    rec = db.query_one(
        "SELECT * FROM recommendations WHERE user_id = ? ORDER BY date DESC LIMIT 1", (uid,)
    )
    ref = _latest_reference()
    ref_info = None
    if ref:
        ref_day = date.fromisoformat(ref["date"])
        p7 = _reference_at_or_before((ref_day - timedelta(days=7)).isoformat())
        p30 = _reference_at_or_before((ref_day - timedelta(days=30)).isoformat())
        ref_info = {
            "date": ref["date"],
            "ct": ref["ct_per_liter"],
            "d7": ref["ct_per_liter"] - p7 if p7 is not None else None,
            "d30": ref["ct_per_liter"] - p30 if p30 is not None else None,
        }

    step_info = None
    need = rec["need_liter"] if rec else profile["min_order_liter"]
    steps = co2.load_steps()
    step = co2.next_step(steps, today)
    if step:
        step_ct = rec["next_step_ct"] if rec and rec["next_step_ct"] is not None else None
        if step_ct is None:
            prev = co2.current_step(steps, today)
            step_ct = co2.step_ct(step) - (co2.step_ct(prev) if prev else 0.0)
        step_info = {
            "date": step.valid_from.isoformat(),
            "days": (step.valid_from - today).days,
            "ct": step_ct,
            "eur": step_ct * need / 100,
            "draft": step.status == "draft",
            "status": step.status,
            "note": step.note,
        }

    tank = profile["tank_liter"]
    fill_liter = profile["fill_percent"] / 100 * tank
    est_liter = None
    if rec:
        try:
            cur = json.loads(rec["inputs_json"] or "{}").get("details", {}).get("current_liter")
            est_liter = float(cur) if cur is not None else None
        except (TypeError, ValueError, AttributeError):
            est_liter = None

    cur_liter = est_liter if est_liter is not None else fill_liter
    cur_percent = min(100.0, max(0.0, cur_liter / tank * 100)) if tank else 0.0
    bar_color = ("bg-green-500" if cur_percent > 40 else "bg-amber-400" if cur_percent > 20
                 else "bg-red-500")
    fill_day = viewmodel.to_date(profile["fill_date"])
    stale_days = (today - fill_day).days if fill_day else 0
    reach_date = None
    answer = None
    if rec:
        reach_date = today + timedelta(days=round(rec["range_days"]))
        answer = viewmodel.build_answer(rec, today, _split_plan(rec, profile), cur_percent)
    learned_liter, learned_conf = _learned(uid)
    learned_ok = learned_liter is not None and (learned_conf or 0) >= 0.3

    digest = _latest_digest(today)
    chart = _chart_data(uid, today)
    auto_liter = offers_mod.query_liter(
        profile["min_order_liter"], rec["need_liter"] if rec else None, None, tank
    )
    offer_liter = offers_mod.query_liter(
        profile["min_order_liter"], rec["need_liter"] if rec else None,
        profile["offer_liter"], tank,
    )
    portal_offers = offers_mod.load_offers(profile["plz"], offer_liter)
    offers_info = _offers_info(portal_offers, rec, ref_info, offer_liter)
    return _render(
        request, "dashboard.html", user=user, profile=profile, rec=rec, ref=ref_info,
        step=step_info, need=need, fill_liter=fill_liter, est_liter=est_liter,
        tank_liter=tank, eur_per_ct=need / 100, digest=digest, answer=answer,
        cur_liter=cur_liter, cur_percent=cur_percent, bar_color=bar_color,
        stale_days=stale_days, reach_date=reach_date,
        learned_liter=learned_liter if learned_ok else None,
        llm_driver=settings.llm_driver, hints_open=_open_hints_count(),
        chart_json=json.dumps(chart), today=today.isoformat(),
        portal_offers=portal_offers, offers_info=offers_info,
        offer_liter=offer_liter, auto_liter=auto_liter,
        offer_liter_custom=profile["offer_liter"] is not None,
        offer_min=min(500, tank),
        no_portal=not offers_mod.PROVIDERS, no_portal_note=offers_mod.NO_PORTAL_NOTE,
        range_days=round(rec["range_days"]) if rec else None,
        recent_offers=db.query(
            "SELECT * FROM offers WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 5", (uid,)
        ),
    )


OFFERS_REFRESH_SECONDS = 600


@router.post("/offers/refresh")
def offers_refresh(request: Request, liter: str = Form(""), user=DEP_profile):
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))
    rec = db.query_one(
        "SELECT need_liter FROM recommendations WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (user["id"],),
    )
    need_liter = rec["need_liter"] if rec else None
    choice = liter.strip().lower()
    if choice in ("", "auto"):
        db.execute("UPDATE profiles SET offer_liter = NULL WHERE user_id = ?", (user["id"],))
        liter_n = offers_mod.query_liter(profile["min_order_liter"], need_liter, None, profile["tank_liter"])
    else:
        try:
            wanted = int(float(choice.replace(",", ".")))
        except ValueError:
            _flash(request, "Menge: bitte eine Zahl angeben.", "error")
            return _go("/")
        liter_n = offers_mod.query_liter(
            profile["min_order_liter"], need_liter, wanted, profile["tank_liter"]
        )
        db.execute("UPDATE profiles SET offer_liter = ? WHERE user_id = ?", (liter_n, user["id"]))
    liter = liter_n
    key = f"offers_refresh_at:{liter}"
    last = request.session.get(key)
    now = time.time()
    if isinstance(last, (int, float)) and now - last < OFFERS_REFRESH_SECONDS:
        minutes = max(1, int((now - last) // 60))
        _flash(request, f"Zuletzt vor {minutes} Minuten abgerufen, bitte kurz warten.", "warn")
        return _go("/")
    try:
        found = offers_mod.fetch_offers(profile["plz"], liter)
    except Exception as exc:
        log.exception("fetch_offers fehlgeschlagen")
        _flash(request, f"Abruf fehlgeschlagen: {exc}", "error")
        return _go("/")
    if not found and not offers_mod.PROVIDERS:
        _flash(request, offers_mod.NO_PORTAL_NOTE, "warn")
        return _go("/")
    request.session[key] = now
    offers_mod.store_offers(date.today(), profile["plz"], liter, found)
    if found:
        _flash(request, f"{len(found)} Angebote für PLZ {profile['plz']}, {liter} L abgerufen.")
    else:
        _flash(request, "Abruf ohne Angebote: kein Portal hat Preise geliefert.", "warn")
    return _go("/")


@router.post("/recompute")
def recompute(request: Request, user=DEP_profile):
    _compute(user["id"])
    _flash(request, "Empfehlung wurde neu berechnet.")
    return _go("/")


def _update_fill(uid: int, percent: float, day: date, source: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE profiles SET fill_percent = ?, fill_date = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE user_id = ?",
            (percent, day.isoformat(), uid),
        )
        conn.execute(
            "INSERT INTO fill_levels (user_id, date, fill_percent, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, date, source) DO UPDATE SET fill_percent = excluded.fill_percent",
            (uid, day.isoformat(), percent, source),
        )


@router.post("/fill-level")
def fill_level(
    request: Request,
    liter: str = Form(""),
    fill_percent: str = Form(""),
    date_: str = Form("", alias="date"),
    user=DEP_profile,
):
    profile = db.query_one("SELECT tank_liter FROM profiles WHERE user_id = ?", (user["id"],))
    try:
        percent = _fill_percent(liter, fill_percent, profile["tank_liter"])
        day = _parse_date(date_, date.today())
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return _go("/")
    _update_fill(user["id"], percent, day, "manual")
    _learn(user["id"])
    _compute(user["id"])
    _flash(request, "Füllstand gespeichert.")
    return _go("/")


@router.post("/offer")
def offer(
    request: Request,
    ct_per_liter: str = Form(""),
    liter: str = Form(""),
    dealer: str = Form(""),
    portal: str = Form(""),
    date_: str = Form("", alias="date"),
    user=DEP_profile,
):
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))
    try:
        ct = _num(ct_per_liter, "Preis", 20, 500)
        qty = int(_num(liter, "Menge", 100, 50000))
        day = _parse_date(date_, date.today())
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return _go("/")
    ref = _latest_reference()
    outlier = 1 if ref and abs(ct - ref["ct_per_liter"]) > OUTLIER_CT else 0
    db.execute(
        "INSERT INTO offers (user_id, region, date, ct_per_liter, liter, dealer, portal, "
        "is_outlier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], profile["region"], day.isoformat(), ct, qty, dealer.strip() or None,
         portal.strip() or None, outlier),
    )
    if outlier:
        _flash(request, "Angebot gespeichert, aber als Ausreißer markiert (weicht stark vom "
                        "üblichen Marktpreis ab).", "warn")
    else:
        _flash(request, "Angebot gespeichert.")
    return _go("/")


@router.post("/purchase")
def purchase(
    request: Request,
    date_: str = Form("", alias="date"),
    liter: str = Form(""),
    ct_per_liter: str = Form(""),
    dealer: str = Form(""),
    user=DEP_profile,
):
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))
    try:
        qty = int(_num(liter, "Menge", 1, 50000))
        ct = _num(ct_per_liter, "Preis", 20, 500)
        day = _parse_date(date_, date.today())
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return _go("/")
    new_percent = min(100.0, profile["fill_percent"] + qty / profile["tank_liter"] * 100)
    db.execute(
        "INSERT INTO purchases (user_id, date, liter, ct_per_liter, dealer) VALUES (?, ?, ?, ?, ?)",
        (user["id"], day.isoformat(), qty, ct, dealer.strip() or None),
    )
    _update_fill(user["id"], new_percent, day, "purchase")
    _learn(user["id"])
    _compute(user["id"])
    _flash(request, f"Kauf eingetragen, Füllstand jetzt {new_percent:.0f} %.")
    return _go("/")


# --------------------------------------------------------------------------- Verlauf


def _history_stats(uid: int) -> tuple[dict | None, dict | None]:
    stats = backtest = None
    try:
        from heizoel.decision.stats import purchase_stats

        stats = purchase_stats(uid)
    except ImportError:
        pass
    except Exception:
        log.exception("purchase_stats fehlgeschlagen")
    try:
        from heizoel.decision.stats import user_backtest

        backtest = user_backtest(uid, date.today())
    except ImportError:
        pass
    except Exception:
        log.exception("user_backtest fehlgeschlagen")
    if not isinstance(stats, dict):
        stats = None
    if not isinstance(backtest, dict) or not backtest.get("text"):
        backtest = None
    return stats, backtest


# Schlüssel, unter denen purchase_stats den Vergleich zum Tagespreis je Kauf liefern kann
# (ct je Liter; negativ = günstiger gekauft als der Tagespreis).
_DELTA_KEYS = ("delta_ct", "delta_ct_per_liter", "vs_ref_ct", "delta")


def _purchase_rows(purchases, stats: dict | None) -> list[dict]:
    items = (stats or {}).get("purchases") or []
    by_key = {}
    for it in items:
        if isinstance(it, dict):
            by_key[(str(it.get("date")), it.get("liter"))] = it
    rows = []
    for p in purchases:
        it = by_key.get((p["date"], p["liter"]), {})
        delta = next((it[k] for k in _DELTA_KEYS if it.get(k) is not None), None)
        rows.append({"date": p["date"], "liter": p["liter"], "ct": p["ct_per_liter"],
                     "dealer": p["dealer"], "delta_ct": delta})
    return rows


def _purchase_totals(purchases, stats: dict | None) -> dict | None:
    if not purchases:
        return None
    stats = stats or {}
    liter = stats.get("total_liter")
    total = stats.get("total_eur")
    if liter is None:
        liter = sum(p["liter"] for p in purchases)
    if total is None:
        total = sum(p["liter"] * p["ct_per_liter"] / 100 for p in purchases)
    return {"liter": liter, "eur": total, "delta_eur": stats.get("total_delta_eur")}


@router.get("/history")
def history(request: Request, user=DEP_profile):
    uid = user["id"]
    profile = db.query_one("SELECT tank_liter FROM profiles WHERE user_id = ?", (uid,))
    purchases = db.query(
        "SELECT * FROM purchases WHERE user_id = ? ORDER BY date DESC, id DESC", (uid,)
    )
    stats, backtest = _history_stats(uid)
    return _render(
        request, "history.html", user=user, tank_liter=profile["tank_liter"],
        stats=stats, backtest=backtest, rows=_purchase_rows(purchases, stats),
        totals=_purchase_totals(purchases, stats),
        fills=db.query(
            "SELECT * FROM fill_levels WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 100",
            (uid,),
        ),
        recs=db.query(
            "SELECT * FROM recommendations WHERE user_id = ? ORDER BY date DESC LIMIT 365", (uid,)
        ),
    )


# --------------------------------------------------------------------------- Einstellungen


@router.get("/settings")
def settings_form(request: Request, user=DEP_profile):
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))
    return _render(request, "settings.html", user=user, p=profile, error=None)


@router.post("/settings")
def settings_save(
    request: Request,
    plz: str = Form(""),
    tank_liter: str = Form(""),
    annual: str = Form(""),
    min_order: str = Form(""),
    lead_days: str = Form(""),
    notify: str = Form("mail"),
    threshold_percentile: str = Form("20"),
    user=DEP_profile,
):
    try:
        v = _profile_form(plz, tank_liter, annual, min_order, lead_days, notify)
        threshold = int(_num(threshold_percentile, "Schwelle", 10, 50))
    except ValueError as exc:
        profile = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))
        return _render(request, "settings.html", user=user, p=profile, status_code=400,
                       error=str(exc))
    db.execute(
        "UPDATE profiles SET plz = ?, region = ?, tank_liter = ?, annual_consumption_liter = ?, "
        "min_order_liter = ?, lead_days = ?, notify_channel = ?, threshold_percentile = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE user_id = ?",
        (v["plz"], v["region"], v["tank_liter"], v["annual"], v["min_order"], v["lead_days"],
         v["notify"], threshold, user["id"]),
    )
    _compute(user["id"])
    _flash(request, "Einstellungen gespeichert.")
    return _go("/settings")


@router.post("/settings/password")
def change_password(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    user=DEP_profile,
):
    if not auth.verify_password(current, user["password_hash"]):
        _flash(request, "Das aktuelle Passwort stimmt nicht.", "error")
    elif len(new) < 8:
        _flash(request, "Das neue Passwort muss mindestens 8 Zeichen lang sein.", "error")
    else:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (auth.hash_password(new), user["id"]))
        _flash(request, "Passwort geändert.")
    return _go("/settings")


@router.post("/settings/delete")
def delete_account(request: Request, password: str = Form(""), user=DEP_user):
    if not auth.verify_password(password, user["password_hash"]):
        _flash(request, "Passwort falsch, Konto wurde nicht gelöscht.", "error")
        return _go("/settings")
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    request.session.clear()
    return _go("/login")


# --------------------------------------------------------------------------- Admin


@router.get("/admin")
def admin(request: Request, user=DEP_admin):
    return _render(
        request, "admin.html", user=user,
        sources=db.query(
            "SELECT r.source, r.date, r.ct_per_liter FROM reference_prices r "
            "JOIN (SELECT source, MAX(date) AS d FROM reference_prices GROUP BY source) m "
            "ON r.source = m.source AND r.date = m.d ORDER BY r.source"
        ),
        price_count=db.query_one("SELECT COUNT(*) AS n FROM reference_prices")["n"],
        jobs=db.query("SELECT * FROM job_runs ORDER BY id DESC LIMIT 20"),
        steps=db.query("SELECT * FROM co2_steps ORDER BY valid_from"),
        offsets=db.query(
            "SELECT r.* FROM region_offsets r "
            "JOIN (SELECT region, MAX(date) AS d FROM region_offsets GROUP BY region) m "
            "ON r.region = m.region AND r.date = m.d ORDER BY r.region"
        ),
        offer_stats=db.query(
            "SELECT region, COUNT(*) AS n, COALESCE(SUM(is_outlier), 0) AS outliers "
            "FROM offers GROUP BY region ORDER BY region"
        ),
        users=db.query(
            "SELECT u.id, u.name, u.email, u.role, p.plz, "
            "(SELECT MAX(date) FROM recommendations WHERE user_id = u.id) AS last_rec "
            "FROM users u LEFT JOIN profiles p ON p.user_id = u.id ORDER BY u.id"
        ),
        digest_log=_digest_log(),
        portal_today=db.query(
            "SELECT portal, COUNT(*) AS n FROM portal_offers WHERE date = ? GROUP BY portal "
            "ORDER BY portal", (date.today().isoformat(),)),
        portal_today_n=db.query_one(
            "SELECT COUNT(*) AS n FROM portal_offers WHERE date = ?",
            (date.today().isoformat(),))["n"],
        portal_jobs=db.query(
            "SELECT * FROM job_runs WHERE job = 'portal_offers' ORDER BY id DESC LIMIT 5"),
        portal_names=sorted(offers_mod.PROVIDERS), no_portal_note=offers_mod.NO_PORTAL_NOTE,
        hints=db.query(
            "SELECT * FROM rule_change_hints ORDER BY "
            "CASE status WHEN 'open' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, "
            "date DESC, id DESC LIMIT 30"
        ),
        news_total=db.query_one("SELECT COUNT(*) AS n FROM news_items")["n"],
        news_feeds=db.query(
            "SELECT feed, COUNT(*) AS total, "
            "COALESCE(SUM(fetched_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')), 0) "
            "AS last7 FROM news_items GROUP BY feed ORDER BY feed"
        ),
        news_last7=db.query_one(
            "SELECT COUNT(*) AS n FROM news_items "
            "WHERE fetched_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')"
        )["n"],
        llm_driver=settings.llm_driver, llm_model=settings.llm_model,
        today=date.today().isoformat(),
    )


def _digest_log() -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM news_digests ORDER BY date DESC LIMIT 14"):
        try:
            n_items = len(json.loads(r["items_json"] or "[]"))
        except (TypeError, ValueError):
            n_items = 0
        text = r["summary"] or ""
        out.append({
            "date": r["date"], "model": r["model"], "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"], "duration_ms": r["duration_ms"],
            "n_items": n_items, "short": text if len(text) <= 140 else text[:137] + "...",
        })
    return out


@router.post("/admin/digest")
def admin_digest(request: Request, user=DEP_admin):
    try:
        from heizoel.news.digest import run_digest

        result = run_digest(date.today(), force=True)
        if isinstance(result, dict) and result.get("skipped"):
            _flash(request, f"Lagebild übersprungen: {result['skipped']}", "warn")
        else:
            r = result if isinstance(result, dict) else {}
            _flash(request, f"Lagebild erzeugt: {r.get('n_items', '?')} Meldungen, "
                            f"{r.get('n_relevant', '?')} relevant, {r.get('n_hints', 0)} Hinweise, "
                            f"Modell {r.get('model', '?')}, {r.get('duration_ms', '?')} ms.")
    except Exception as exc:
        log.exception("run_digest fehlgeschlagen")
        _flash(request, f"Lagebild fehlgeschlagen: {exc}", "error")
    return _go("/admin")


@router.post("/admin/offers/refresh")
def admin_offers_refresh(request: Request, user=DEP_admin):
    try:
        result = offers_mod.refresh_offers(date.today())
    except Exception as exc:
        log.exception("refresh_offers fehlgeschlagen")
        _flash(request, f"Abruf fehlgeschlagen: {exc}", "error")
        return _go("/admin")
    errors = result.get("errors") or []
    msg = f"{result['queries']} Abfragen, {result['offers']} Angebote."
    if errors:
        _flash(request, f"{msg} Hinweise: {'; '.join(errors)}", "warn")
    else:
        _flash(request, msg)
    return _go("/admin")


@router.post("/admin/hints/{hint_id}/confirm")
def admin_hint_confirm(hint_id: int, request: Request, user=DEP_admin):
    db.execute("UPDATE rule_change_hints SET status = 'confirmed' WHERE id = ?", (hint_id,))
    _flash(request, "Hinweis bestätigt. Trage die neue Stufe oben bei CO2-Stufen ein.")
    return _go("/admin#hinweise")


@router.post("/admin/hints/{hint_id}/dismiss")
def admin_hint_dismiss(hint_id: int, request: Request, user=DEP_admin):
    db.execute("UPDATE rule_change_hints SET status = 'dismissed' WHERE id = ?", (hint_id,))
    _flash(request, "Hinweis verworfen.")
    return _go("/admin#hinweise")


@router.post("/admin/fetch")
def admin_fetch(request: Request, user=DEP_admin):
    try:
        from heizoel.fetchers.reference import fetch_today

        result = fetch_today(date.today())
        _flash(request, f"Preis geholt: {result}")
    except Exception as exc:
        log.exception("fetch_today fehlgeschlagen")
        _flash(request, f"Abruf fehlgeschlagen: {exc}", "error")
    return _go("/admin")


@router.post("/admin/recompute-all")
def admin_recompute_all(request: Request, user=DEP_admin):
    try:
        from heizoel.decision.service import compute_all

        try:
            compute_all(date.today())
        except TypeError:
            compute_all()
        _flash(request, "Empfehlungen für alle Nutzer neu berechnet.")
    except Exception as exc:
        log.exception("compute_all fehlgeschlagen")
        _flash(request, f"Berechnung fehlgeschlagen: {exc}", "error")
    return _go("/admin")


@router.post("/admin/co2")
def admin_co2(
    request: Request,
    valid_from: str = Form(""),
    eur_per_t: str = Form(""),
    eur_per_t_max: str = Form(""),
    status: str = Form("fixed"),
    note: str = Form(""),
    source_url: str = Form(""),
    user=DEP_admin,
):
    try:
        day = _parse_date(valid_from)
        low = _num(eur_per_t, "EUR/t", 0, 1000)
        high = _num(eur_per_t_max, "EUR/t max", 0, 1000) if eur_per_t_max.strip() else None
        if status not in ("fixed", "draft", "market"):
            raise ValueError("Ungültiger Status.")
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return _go("/admin")
    db.execute(
        "INSERT INTO co2_steps (valid_from, eur_per_t, eur_per_t_max, status, note, source_url) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(valid_from) DO UPDATE SET "
        "eur_per_t = excluded.eur_per_t, eur_per_t_max = excluded.eur_per_t_max, "
        "status = excluded.status, note = excluded.note, source_url = excluded.source_url",
        (day.isoformat(), low, high, status, note.strip(), source_url.strip()),
    )
    _flash(request, "CO2-Stufe gespeichert.")
    return _go("/admin")


@router.post("/admin/co2/delete")
def admin_co2_delete(request: Request, valid_from: str = Form(""), user=DEP_admin):
    db.execute("DELETE FROM co2_steps WHERE valid_from = ?", (valid_from,))
    _flash(request, "CO2-Stufe gelöscht.")
    return _go("/admin")


@router.post("/admin/manual-price")
def admin_manual_price(
    request: Request,
    date_: str = Form("", alias="date"),
    ct: str = Form(""),
    user=DEP_admin,
):
    try:
        day = _parse_date(date_, date.today())
        price = _num(ct, "Preis", 20, 500)
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return _go("/admin")
    from heizoel.fetchers.reference import set_manual_price

    set_manual_price(day, price)  # lädt Brent/EURUSD für den Tag nach, damit der Wert als Margen-Anker wirkt
    _flash(request, "Referenzpreis gespeichert. Für eine neue Historie danach 'heizoel backfill' ausführen.")
    return _go("/admin")
