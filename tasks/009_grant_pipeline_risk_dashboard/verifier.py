#!/usr/bin/env python3
"""Verifier for 009_grant_pipeline_risk_dashboard (v2).

Dual-path (Python vs SQL) on every window-driven figure; hazard coverage for
inclusive window edges, per-collection status semantics, and the stage-2
derives-from-stage-1 contract."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def dp(value):
    return vlib.dp(value)


def dt_of(value):
    s = dp(value)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def in_window(value, window_days, ep_dt):
    d0 = dt_of(value)
    return d0 is not None and 0 <= (d0 - ep_dt).days <= window_days


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    opp_w = int(vlib.get_nonce(v.token, field="opp_window_days"))
    award_w = int(vlib.get_nonce(v.token, field="award_end_window_days"))
    ep = dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    opps = vlib.fetch_all(v.token, "grant_opportunities")
    reports = vlib.fetch_all(v.token, "grant_reports")
    awards = vlib.fetch_all(v.token, "grant_awards")

    at_risk = [
        o for o in opps
        if in_window(o.get("deadline"), opp_w, ep_dt)
        and o.get("status") not in ("awarded", "declined", "withdrawn")
    ]
    at_risk_value = sum(int(o.get("max_award") or 0) for o in at_risk)
    reports_due = [
        r for r in reports
        if in_window(r.get("due_date"), opp_w, ep_dt)
        and r.get("status") not in ("submitted", "accepted")
    ]
    nearing = [
        a for a in awards
        if in_window(a.get("end_date"), award_w, ep_dt)
    ]

    # SQL dual-path: opportunities (count + value).
    sql_opp = vlib.sql(
        v.token,
        "SELECT COUNT(*) AS n, COALESCE(SUM(max_award), 0) AS v FROM grant_opportunities "
        "WHERE status NOT IN ('awarded', 'declined', 'withdrawn') "
        f"AND deadline::date >= DATE '{ep}' AND deadline::date <= DATE '{ep}' + INTERVAL '{opp_w} days'",
    )
    v.expect_equal(int(sql_opp[0]["n"]), len(at_risk), "at-risk count: SQL vs Python")
    v.expect_equal(int(sql_opp[0]["v"]), at_risk_value, "at-risk value: SQL vs Python")

    # SQL dual-path: reports due soon and awards nearing end.
    sql_reports = vlib.sql(
        v.token,
        "SELECT COUNT(*) AS n FROM grant_reports WHERE status NOT IN ('submitted', 'accepted') "
        f"AND due_date::date >= DATE '{ep}' AND due_date::date <= DATE '{ep}' + INTERVAL '{opp_w} days'",
    )
    v.expect_equal(int(sql_reports[0]["n"]), len(reports_due), "reports due soon: SQL vs Python")
    sql_awards = vlib.sql(
        v.token,
        "SELECT COUNT(*) AS n FROM grant_awards "
        f"WHERE end_date::date >= DATE '{ep}' AND end_date::date <= DATE '{ep}' + INTERVAL '{award_w} days'",
    )
    v.expect_equal(int(sql_awards[0]["n"]), len(nearing), "awards nearing end: SQL vs Python")

    all_reports = vlib.fetch_all(v.token, "ops_reports")
    dash = [r for r in all_reports if r.get("report") == "grant_pipeline_risk" and r.get("batch_code") == batch]
    detail = [r for r in all_reports if r.get("report") == "grant_pipeline_risk_detail" and r.get("batch_code") == batch]

    v.expect_equal(len(dash), 1, "grant_pipeline_risk row count")
    d0 = dash[0]
    v.expect_equal(int(d0.get("opportunities_at_risk")), len(at_risk), "opportunities_at_risk")
    v.expect_equal(int(d0.get("opportunities_at_risk_value")), at_risk_value, "opportunities_at_risk_value")
    v.expect_equal(int(d0.get("reports_due_soon")), len(reports_due), "reports_due_soon")
    v.expect_equal(int(d0.get("awards_nearing_end")), len(nearing), "awards_nearing_end")

    # Stage-2 contract: detail rows == the dashboard's counted set.
    got_ids = sorted(str(r.get("opportunity_id")) for r in detail)
    want_ids = sorted(str(o.get("opportunity_id")) for o in at_risk)
    v.expect_equal(got_ids, want_ids, "detail set == dashboard counted set")
    for o in at_risk:
        row = next((r for r in detail if str(r.get("opportunity_id")) == str(o.get("opportunity_id"))), None)
        v.expect(row is not None, f"missing detail row for {o.get('opportunity_id')}")
        v.expect_equal(dp(row.get("deadline")), dp(o.get("deadline")), f"detail deadline for {o.get('opportunity_id')}")
        v.expect_equal(int(row.get("max_award") or 0), int(o.get("max_award") or 0), f"detail max_award for {o.get('opportunity_id')}")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_applications", "grant_awards", "grant_expenses",
        "firm_policies", "staff_roster", "firm_controls", "hold_reminders",
        "grant_opportunities",
        "grant_reports",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
