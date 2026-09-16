#!/usr/bin/env python3
"""Alternate gold for 017_grant_budget_burn_forecast (v2).

Same end-state as gold.py, materially different path: the per-award spend
aggregates come from SQL GROUP BY queries (g.sql) — one for lifetime spend,
one for the trailing window — instead of REST reads + Python accumulation.
Idempotent.
"""
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

SCOPE_STATUSES = ("active", "reporting_due")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    awards = {str(a["award_id"]): a for a in g.all("grant_awards") if a.get("status") in SCOPE_STATUSES}
    ids = sorted(awards.keys())
    id_list = ",".join(f"'{i}'" for i in ids)

    # SQL-first: spend per award_id, lifetime and trailing-window.
    spend = {r["award_id"]: float(r["val"] or 0) for r in g.sql(
        f"SELECT award_id, COALESCE(SUM(amount), 0) AS val FROM grant_expenses "
        f"WHERE award_id IN ({id_list}) GROUP BY award_id")}
    window_start = (ep_dt - timedelta(days=90)).isoformat()
    window = {r["award_id"]: float(r["val"]) for r in g.sql(
        f"SELECT award_id, COALESCE(SUM(amount), 0) AS val FROM grant_expenses "
        f"WHERE award_id IN ({id_list}) AND expense_date::date > DATE '{window_start}' "
        f"AND expense_date::date <= DATE '{ep}' GROUP BY award_id")}

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "grant_burn_forecast", "grant_burn_risk_tier", "grant_runway_summary"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
    for aid in sorted(awards):
        a = awards[aid]
        recomputed = spend.get(aid, 0.0)
        cached = a.get("total_spent")
        start_dt = datetime.strptime(glib.Gold.dp(a.get("start_date")), "%Y-%m-%d").date()
        age = (ep_dt - start_dt).days
        daily = (window.get(aid, 0.0) / 90.0) if age > 180 else (recomputed / max(1, age))
        remaining = (a.get("amount", 0) or 0) - recomputed
        days_until = int(math.floor(remaining / daily)) if daily > 0 else 999999
        projected = (ep_dt + timedelta(days=days_until)).isoformat()
        will_exhaust = projected < glib.Gold.dp(a.get("end_date"))
        g.push("ops_reports", {
            "report": "grant_burn_forecast", "batch_code": batch, "award_id": aid,
            "recomputed_total_spent": recomputed, "cached_total_spent": cached,
            "spent_field_matches": recomputed == cached,
            "daily_burn_rate": round(daily, 2), "days_until_exhausted": days_until,
            "projected_exhaustion_date": projected, "will_exhaust_before_end": will_exhaust,
        })

    # ---- STAGE 2 (read back) --------------------------------------------
    live = [r for r in g.all("ops_reports")
            if r.get("report") == "grant_burn_forecast" and r.get("batch_code") == batch]
    total_remaining = 0.0
    total_daily = 0.0
    critical = 0
    for row in live:
        daily = float(row.get("daily_burn_rate") or 0)
        remaining = (awards[str(row.get("award_id"))].get("amount", 0) or 0) \
            - float(row.get("recomputed_total_spent") or 0)
        months = round(remaining / (daily * 30.0), 1) if daily > 0 else 999.0
        g.push("ops_reports", {
            "report": "grant_burn_risk_tier", "batch_code": batch,
            "award_id": row.get("award_id"),
            "risk_tier": "critical" if row.get("will_exhaust_before_end") else "normal",
            "months_of_runway": months,
        })
        total_remaining += remaining
        total_daily += daily
        if row.get("will_exhaust_before_end"):
            critical += 1
    firmwide = round(total_remaining / (total_daily * 30.0), 1) if total_daily > 0 else 999.0
    g.push("ops_reports", {
        "report": "grant_runway_summary", "batch_code": batch,
        "awards_forecasted": len(live), "awards_critical": critical,
        "firmwide_months_of_runway": firmwide,
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
