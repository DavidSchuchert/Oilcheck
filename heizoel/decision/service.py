"""DB-Zugriff für die Kaufregel: Eingaben laden, Ergebnis speichern."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from heizoel import co2, db
from heizoel.config import settings
from heizoel.decision.engine import recommend, result_to_inputs_json
from heizoel.decision.learning import effective_annual_liter
from heizoel.models import DecisionInputs, DecisionResult, Profile


def load_profile(user_id: int) -> Profile | None:
    r = db.query_one("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
    if r is None:
        return None
    return Profile(
        user_id=r["user_id"],
        plz=r["plz"],
        region=r["region"],
        tank_liter=r["tank_liter"],
        fill_percent=r["fill_percent"],
        fill_date=date.fromisoformat(r["fill_date"]),
        annual_consumption_liter=effective_annual_liter(
            r["annual_consumption_liter"], r["learned_annual_liter"], r["learned_confidence"] or 0.0),
        min_order_liter=r["min_order_liter"],
        lead_days=r["lead_days"],
        notify_channel=r["notify_channel"],
        threshold_percentile=r["threshold_percentile"],
    )


def latest_reference(today: date) -> tuple[date, float] | None:
    r = db.query_one(
        "SELECT date, ct_per_liter FROM reference_prices WHERE date <= ? AND date >= ? "
        "ORDER BY date DESC LIMIT 1",
        (today.isoformat(), (today - timedelta(days=7)).isoformat()),
    )
    return None if r is None else (date.fromisoformat(r["date"]), r["ct_per_liter"])


def load_history(today: date, years: int = 5) -> list[tuple[date, float]]:
    start = today - timedelta(days=365 * years + 1)
    rows = db.query(
        "SELECT date, ct_per_liter FROM reference_prices WHERE date >= ? AND date <= ? "
        "ORDER BY date",
        (start.isoformat(), today.isoformat()),
    )
    return [(date.fromisoformat(r["date"]), r["ct_per_liter"]) for r in rows]


def latest_region_offset(region: str, today: date) -> tuple[float, int]:
    r = db.query_one(
        "SELECT offset_ct, n_offers FROM region_offsets WHERE region = ? AND date <= ? "
        "AND date >= ? ORDER BY date DESC LIMIT 1",
        (region, today.isoformat(), (today - timedelta(days=7)).isoformat()),
    )
    return (0.0, 0) if r is None else (r["offset_ct"], r["n_offers"])


def build_inputs(user_id: int, today: date) -> DecisionInputs | None:
    profile = load_profile(user_id)
    if profile is None:
        return None
    ref = latest_reference(today)
    if ref is None:
        return None
    offset, n = latest_region_offset(profile.region, today)
    return DecisionInputs(
        today=today,
        reference_ct=ref[1],
        region_offset_ct=offset,
        region_offset_n=n,
        history=load_history(today, settings.history_years),
        profile=profile,
        co2_steps=co2.load_steps(),
        co2_kg_per_liter=settings.co2_kg_per_liter,
        vat=settings.vat,
    )


_UPSERT = """
INSERT INTO recommendations (
    user_id, date, verdict, rule, reason, personal_ct, percentile_365, percentile_730,
    season_deviation_ct, range_days, latest_order_date, next_step_date, next_step_ct,
    need_liter, inputs_json
) VALUES (
    :user_id, :date, :verdict, :rule, :reason, :personal_ct, :percentile_365, :percentile_730,
    :season_deviation_ct, :range_days, :latest_order_date, :next_step_date, :next_step_ct,
    :need_liter, :inputs_json
)
ON CONFLICT(user_id, date) DO UPDATE SET
    verdict = excluded.verdict,
    rule = excluded.rule,
    reason = excluded.reason,
    personal_ct = excluded.personal_ct,
    percentile_365 = excluded.percentile_365,
    percentile_730 = excluded.percentile_730,
    season_deviation_ct = excluded.season_deviation_ct,
    range_days = excluded.range_days,
    latest_order_date = excluded.latest_order_date,
    next_step_date = excluded.next_step_date,
    next_step_ct = excluded.next_step_ct,
    need_liter = excluded.need_liter,
    inputs_json = excluded.inputs_json,
    created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
"""


def compute_and_store(user_id: int, today: date) -> DecisionResult | None:
    inputs = build_inputs(user_id, today)
    if inputs is None:
        return None
    result = recommend(inputs)
    db.execute(_UPSERT, {
        "user_id": user_id,
        "date": today.isoformat(),
        "verdict": result.verdict,
        "rule": result.rule,
        "reason": result.reason,
        "personal_ct": result.personal_ct,
        "percentile_365": result.percentile_365,
        "percentile_730": result.percentile_730,
        "season_deviation_ct": result.season_deviation_ct,
        "range_days": result.range_days,
        "latest_order_date": result.latest_order_date.isoformat(),
        "next_step_date": result.next_step_date.isoformat() if result.next_step_date else None,
        "next_step_ct": result.next_step_ct,
        "need_liter": result.need_liter,
        "inputs_json": result_to_inputs_json(inputs, result),
    })
    return result


def compute_all(today: date) -> dict[int, DecisionResult]:
    out: dict[int, DecisionResult] = {}
    for r in db.query("SELECT user_id FROM profiles ORDER BY user_id"):
        res = compute_and_store(r["user_id"], today)
        if res is not None:
            out[r["user_id"]] = res
    return out


def load_latest_recommendation(user_id: int) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM recommendations WHERE user_id = ? ORDER BY date DESC LIMIT 1",
        (user_id,),
    )
