#!/usr/bin/env python3
"""Verifier for 017_grant_budget_burn_forecast (v2).

Dual-path on every derived number: per-award recomputed spend is aggregated
from the seed-snapshot expenses in Python AND via SQL SUM GROUP BY over the
live expenses; both paths must agree with each other and with the forecast
rows the agent wrote. Hazard coverage: trusting the cached total_spent (stale
on 69 of 70 in-scope awards), joining expenses on the award row id (reports
zero spend), and applying one burn branch everywhere all FAIL here. The
stage-2 tier rows must reuse the stage-1 rows' own values (SEVERITY-01).
Expectations are recomputed from the seed snapshot + live episode row, so the
verifier is idempotent across repeated gold runs and fails closed via
vlib.run.
"""
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

SCOPE_STATUSES = ("active", "reporting_due")


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    seed_awards = [a for a in vlib.seed_rows("grant_awards") if a.get("status") in SCOPE_STATUSES]
    seed_expenses = vlib.seed_rows("grant_expenses")
    live_awards = {str(a["id"]): a for a in vlib.fetch_all(v.token, "grant_awards")}

    # ------------------------------------------------------ path 1: Python
    spend = {str(a["award_id"]): 0.0 for a in seed_awards}
    window_spend = {str(a["award_id"]): 0.0 for a in seed_awards}
    window_start = (ep_dt - timedelta(days=90)).isoformat()
    for e in seed_expenses:
        aid = str(e.get("award_id"))
        if aid in spend:
            amt = e.get("amount", 0) or 0
            spend[aid] += amt
            d = vlib.dp(e.get("expense_date"))
            if d is not None and window_start < d <= ep:
                window_spend[aid] += amt

    expected = {}
    for a in seed_awards:
        aid = str(a["award_id"])
        recomputed = spend[aid]
        start_dt = datetime.strptime(vlib.dp(a.get("start_date")), "%Y-%m-%d").date()
        age = (ep_dt - start_dt).days
        daily = (window_spend[aid] / 90.0) if age > 180 else (recomputed / max(1, age))
        remaining = (a.get("amount", 0) or 0) - recomputed
        days_until = int(math.floor(remaining / daily)) if daily > 0 else 999999
        projected = (ep_dt + timedelta(days=days_until)).isoformat()
        expected[aid] = {
            "recomputed": recomputed, "cached": a.get("total_spent"),
            "matches": recomputed == a.get("total_spent"),
            "daily": round(daily, 2), "days": days_until, "projected": projected,
            "exhaust": projected < vlib.dp(a.get("end_date")),
        }

    # ------------------------------------------------------ path 2: SQL
    ids = sorted(expected.keys())
    id_list = ",".join(f"'{i}'" for i in ids)
    sql_spend = {str(r["award_id"]): float(r["val"] or 0) for r in vlib.sql(
        v.token,
        f"SELECT award_id, COALESCE(SUM(amount), 0) AS val FROM grant_expenses "
        f"WHERE award_id IN ({id_list}) GROUP BY award_id")}
    sql_window = {str(r["award_id"]): float(r["val"] or 0) for r in vlib.sql(
        v.token,
        f"SELECT award_id, COALESCE(SUM(amount), 0) AS val FROM grant_expenses "
        f"WHERE award_id IN ({id_list}) AND expense_date::date > DATE '{window_start}' "
        f"AND expense_date::date <= DATE '{ep}' GROUP BY award_id")}
    for aid, exp in expected.items():
        v.expect_cents(sql_spend.get(aid, 0.0), exp["recomputed"],
                       f"dual-path: award {aid} recomputed spend (SQL vs Python)")
        # Both paths must compute the SAME window predicate (BURN-01's
        # trailing-90d intermediate is accumulated for every in-scope award;
        # the age>180 branch only decides which value feeds the daily rate).
        v.expect_cents(sql_window.get(aid, 0.0), window_spend[aid],
                       f"dual-path: award {aid} window spend (SQL vs Python)")

    # --------------------------------------------------- stage 1: forecasts
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "grant_burn_forecast" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(expected), "grant_burn_forecast row count")
    by_award = {str(r.get("award_id")): r for r in rows}
    for aid, exp in expected.items():
        row = by_award.get(aid)
        v.expect(row is not None, f"missing grant_burn_forecast row for award {aid}")
        v.expect_cents(row.get("recomputed_total_spent"), exp["recomputed"], f"award {aid} recomputed_total_spent")
        v.expect_cents(row.get("cached_total_spent"), exp["cached"], f"award {aid} cached_total_spent")
        v.expect_equal(row.get("spent_field_matches"), exp["matches"], f"award {aid} spent_field_matches")
        actual_daily = row.get("daily_burn_rate")
        v.expect(isinstance(actual_daily, (int, float)) and abs(float(actual_daily) - exp["daily"]) <= 0.05,
                 f"award {aid} daily_burn_rate")
        v.expect_equal(row.get("days_until_exhausted"), exp["days"], f"award {aid} days_until_exhausted")
        v.expect_equal(vlib.dp(row.get("projected_exhaustion_date")), exp["projected"],
                       f"award {aid} projected_exhaustion_date")
        v.expect_equal(row.get("will_exhaust_before_end"), exp["exhaust"], f"award {aid} will_exhaust_before_end")

    # --------------------------------------------- stage 2: tiers + summary
    tier_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "grant_burn_risk_tier" and r.get("batch_code") == batch]
    v.expect_equal(len(tier_rows), len(expected), "grant_burn_risk_tier row count")
    tier_by_award = {str(r.get("award_id")): r for r in tier_rows}
    total_remaining = 0.0
    total_daily = 0.0
    critical = 0
    for aid, exp in expected.items():
        award = next(a for a in seed_awards if str(a["award_id"]) == aid)
        remaining = (award.get("amount", 0) or 0) - exp["recomputed"]
        months = round(remaining / (exp["daily"] * 30.0), 1) if exp["daily"] > 0 else 999.0
        row = tier_by_award.get(aid)
        v.expect(row is not None, f"missing grant_burn_risk_tier row for award {aid}")
        v.expect_equal(row.get("risk_tier"), "critical" if exp["exhaust"] else "normal",
                       f"award {aid} risk_tier")
        actual_months = row.get("months_of_runway")
        v.expect(isinstance(actual_months, (int, float)) and abs(float(actual_months) - months) <= 0.15,
                 f"award {aid} months_of_runway")
        # SEVERITY-01: the tier row must reuse the forecast row's own values
        stage1 = by_award.get(aid)
        v.expect(stage1 is not None, f"award {aid} tier row has no matching forecast row")
        v.expect_equal(row.get("risk_tier"),
                       "critical" if stage1.get("will_exhaust_before_end") else "normal",
                       f"award {aid} risk_tier reuses stage-1 verdict")
        total_remaining += remaining
        total_daily += exp["daily"]
        if exp["exhaust"]:
            critical += 1
    firmwide = round(total_remaining / (total_daily * 30.0), 1) if total_daily > 0 else 999.0
    summary = [r for r in vlib.fetch_all(v.token, "ops_reports")
               if r.get("report") == "grant_runway_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary), 1, "grant_runway_summary row count")
    srow = summary[0]
    v.expect_equal(srow.get("awards_forecasted"), len(expected), "awards_forecasted")
    v.expect_equal(srow.get("awards_critical"), critical, "awards_critical")
    actual_firmwide = srow.get("firmwide_months_of_runway")
    v.expect(isinstance(actual_firmwide, (int, float)) and abs(float(actual_firmwide) - firmwide) <= 0.15,
             "firmwide_months_of_runway")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
