#!/usr/bin/env python3
"""gold_alt for 009 (v2): SQL-first window filtering and aggregation."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def dp(v):
    return glib.Gold.dp(v)


def main():
    g = glib.Gold()
    batch = g.nonce()
    opp_w = int(g.nonce(field="opp_window_days"))
    award_w = int(g.nonce(field="award_end_window_days"))
    ep = dp(g.nonce(field="episode_date"))

    at_risk = g.sql(
        "SELECT opportunity_id, title, deadline, max_award FROM grant_opportunities "
        "WHERE status NOT IN ('awarded','declined','withdrawn') "
        f"AND deadline::date >= DATE '{ep}' AND deadline::date <= DATE '{ep}' + INTERVAL '{opp_w} days' "
        "ORDER BY opportunity_id"
    )
    reports_due = g.sql(
        "SELECT COUNT(*) AS n FROM grant_reports "
        "WHERE status NOT IN ('submitted','accepted') "
        f"AND due_date::date >= DATE '{ep}' AND due_date::date <= DATE '{ep}' + INTERVAL '{opp_w} days'"
    )
    awards_near = g.sql(
        "SELECT COUNT(*) AS n FROM grant_awards "
        f"WHERE end_date::date >= DATE '{ep}' AND end_date::date <= DATE '{ep}' + INTERVAL '{award_w} days'"
    )

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("grant_pipeline_risk", "grant_pipeline_risk_detail"):
            g.delete("ops_reports", r["id"])

    at_risk_value = sum(int(r["max_award"] or 0) for r in at_risk)
    g.push("ops_reports", {
        "report": "grant_pipeline_risk", "batch_code": batch,
        "opportunities_at_risk": len(at_risk),
        "opportunities_at_risk_value": at_risk_value,
        "reports_due_soon": int(reports_due[0]["n"]) if reports_due else 0,
        "awards_nearing_end": int(awards_near[0]["n"]) if awards_near else 0,
    })
    for o in at_risk:
        g.push("ops_reports", {
            "report": "grant_pipeline_risk_detail", "batch_code": batch,
            "opportunity_id": o.get("opportunity_id"), "name": o.get("title"),
            "deadline": dp(o.get("deadline")), "max_award": o.get("max_award"),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
