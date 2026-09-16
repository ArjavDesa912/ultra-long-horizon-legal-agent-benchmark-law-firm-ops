#!/usr/bin/env python3
"""Gold for 009_grant_pipeline_risk_dashboard (v2).

Windows come from the episode row (opp_window_days, award_end_window_days);
status semantics from GRANT-REPORT-01/SEVERITY-01 as seeded. Detail rows reuse
the SAME counted at-risk set as the dashboard row (read back after push)."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def dp(v):
    return glib.Gold.dp(v)


def main():
    g = glib.Gold()
    batch = g.nonce()
    opp_win = int(g.nonce(field="opp_window_days"))
    award_win = int(g.nonce(field="award_end_window_days"))
    ep = dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    opps = g.all("grant_opportunities")
    reports = g.all("grant_reports")
    awards = g.all("grant_awards")

    at_risk = [
        o for o in opps
        if o.get("deadline") and 0 <= (datetime.strptime(dp(o["deadline"]), "%Y-%m-%d").date() - ep_dt).days <= opp_win
        and o.get("status") not in ("awarded", "declined", "withdrawn")
    ]
    at_risk_value = sum(int(o.get("max_award") or 0) for o in at_risk)

    reports_due = [
        r for r in reports
        if r.get("due_date") and 0 <= (datetime.strptime(dp(r["due_date"]), "%Y-%m-%d").date() - ep_dt).days <= opp_win
        and r.get("status") not in ("submitted", "accepted")
    ]
    nearing_end = [
        a for a in awards
        if a.get("end_date") and 0 <= (datetime.strptime(dp(a["end_date"]), "%Y-%m-%d").date() - ep_dt).days <= award_win
    ]

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("grant_pipeline_risk", "grant_pipeline_risk_detail"):
            g.delete("ops_reports", r["id"])

    g.push("ops_reports", {
        "report": "grant_pipeline_risk", "batch_code": batch,
        "opportunities_at_risk": len(at_risk),
        "opportunities_at_risk_value": at_risk_value,
        "reports_due_soon": len(reports_due),
        "awards_nearing_end": len(nearing_end),
    })
    for o in sorted(at_risk, key=lambda x: str(x.get("opportunity_id"))):
        g.push("ops_reports", {
            "report": "grant_pipeline_risk_detail", "batch_code": batch,
            "opportunity_id": o.get("opportunity_id"), "name": o.get("title"),
            "deadline": dp(o.get("deadline")), "max_award": o.get("max_award"),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
