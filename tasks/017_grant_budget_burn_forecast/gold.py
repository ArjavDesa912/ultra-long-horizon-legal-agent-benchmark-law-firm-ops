#!/usr/bin/env python3
"""Gold solution for 017_grant_budget_burn_forecast (v2). Run against a FRESH
container. Idempotent: safe to run twice.

Scope: every grant_awards row with status in (active, reporting_due).
Spend is recomputed from grant_expenses joined on the award's award_id string
(the only resolving join — the bulk expense rows reference AWD-BULK-* ids that
resolve to no award row). The burn branch (trailing 90-day window vs lifetime
spend over elapsed days) comes from firm policy BURN-01, applied per award.
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

    awards = [a for a in g.all("grant_awards") if a.get("status") in SCOPE_STATUSES]
    expenses = g.all("grant_expenses")
    awards_by_id = {str(a["award_id"]): a for a in awards}

    # Recomputed spend per award: expenses whose award_id equals the award's
    # award_id string. (Joining on the award row id resolves nothing: the bulk
    # expense rows carry AWD-BULK-* ids.)
    spend = {str(a["award_id"]): 0.0 for a in awards}
    window_spend = {str(a["award_id"]): 0.0 for a in awards}
    window_start = (ep_dt - timedelta(days=90)).isoformat()
    for e in expenses:
        aid = str(e.get("award_id"))
        if aid in spend:
            amt = e.get("amount", 0) or 0
            spend[aid] += amt
            expense_date = glib.Gold.dp(e.get("expense_date"))
            if expense_date is not None and window_start < expense_date <= ep:
                window_spend[aid] += amt

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "grant_burn_forecast", "grant_burn_risk_tier", "grant_runway_summary"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: one forecast row per in-scope award --------------------
    for a in awards:
        aid = str(a["award_id"])
        recomputed = spend[aid]
        cached = a.get("total_spent")
        start = glib.Gold.dp(a.get("start_date"))
        start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        age = (ep_dt - start_dt).days
        if age > 180:
            daily = window_spend[aid] / 90.0  # trailing 90-day window (BURN-01)
        else:
            daily = recomputed / max(1, age)  # lifetime spend over elapsed days
        remaining = (a.get("amount", 0) or 0) - recomputed
        if daily > 0:
            days_until = int(math.floor(remaining / daily))
        else:
            days_until = 999999
        projected = (ep_dt + timedelta(days=days_until)).isoformat()
        end = glib.Gold.dp(a.get("end_date"))
        will_exhaust = projected < end
        g.push("ops_reports", {
            "report": "grant_burn_forecast", "batch_code": batch, "award_id": aid,
            "recomputed_total_spent": recomputed, "cached_total_spent": cached,
            "spent_field_matches": recomputed == cached,
            "daily_burn_rate": round(daily, 2), "days_until_exhausted": days_until,
            "projected_exhaustion_date": projected, "will_exhaust_before_end": will_exhaust,
        })

    # ---- STAGE 2: tier rows read back from the forecast rows -------------
    live = [r for r in g.all("ops_reports")
            if r.get("report") == "grant_burn_forecast" and r.get("batch_code") == batch]
    total_remaining = 0.0
    total_daily = 0.0
    critical = 0
    for row in live:
        daily = float(row.get("daily_burn_rate") or 0)
        remaining = (awards_by_id[str(row.get("award_id"))].get("amount", 0) or 0) \
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
