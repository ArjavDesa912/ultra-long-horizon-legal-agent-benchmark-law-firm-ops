#!/usr/bin/env python3
"""Independent second gold for 023_firmwide_temporal_anomaly_sweep (v2).

Materially different path from gold.py: every per-control violation count is a
single SQL statement (date casts + comparisons in the database) instead of a
Python filter over fetched rows, and the summary/severity values are read back
via SQL as well. Writes still go through the REST API (SQL is read-only here).
Run against a FRESH container."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("temporal_control_finding", "temporal_anomalies", "temporal_anomaly_severity")

# control_id -> (scope table, SQL predicate). The predicates mirror the
# registry's rule text; CTL-TMP-04 is the on-or-before variant.
SQL_RULES = {
    "CTL-TMP-01": ("invoices",
                   "due_date IS NOT NULL AND issued_date IS NOT NULL "
                   "AND due_date::date < issued_date::date"),
    "CTL-TMP-02": ("matters",
                   "date_closed IS NOT NULL AND date_opened IS NOT NULL "
                   "AND date_closed::date < date_opened::date"),
    "CTL-TMP-03": ("ediscovery_documents",
                   "reviewed_at IS NOT NULL AND date_created IS NOT NULL "
                   "AND reviewed_at::date < date_created::date"),
    "CTL-TMP-04": ("grant_applications",
                   "decision_date IS NOT NULL AND submitted_date IS NOT NULL "
                   "AND decision_date::date <= submitted_date::date"),
}


def main():
    g = glib.Gold()
    batch = g.nonce()
    severity_critical = int(g.nonce(field="severity_critical_count"))

    controls = [c for c in g.all("firm_controls")
                if c.get("enabled") and str(c.get("control_id", "")).startswith("CTL-TMP")]

    findings = []
    for ctl in controls:
        cid = ctl.get("control_id")
        table, predicate = SQL_RULES[cid]
        rows = g.sql(f"SELECT COUNT(*) AS n FROM {table} WHERE {predicate}")
        findings.append({
            "control_id": cid,
            "rule_key": str(ctl.get("rule", "")).split(":", 1)[0].strip(),
            "scope_collection": ctl.get("scope_collection"),
            "violation_count": int(rows[0]["n"]),
        })

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    for f in findings:
        g.push("ops_reports", {"report": "temporal_control_finding", "batch_code": batch, **f})

    # ---- STAGE 2: summary, reusing this batch's finding rows (SQL read) ----
    live = g.sql(
        "SELECT rule_key, violation_count FROM ops_reports "
        "WHERE report = 'temporal_control_finding' AND batch_code = '" + batch + "'"
    )
    by_rule = {f["rule_key"]: f["violation_count"] for f in findings}
    total = sum(int(r["violation_count"] or 0) for r in live)
    g.push("ops_reports", {
        "report": "temporal_anomalies", "batch_code": batch,
        "total_anomalies": total, "by_rule": by_rule, "clean": total == 0,
    })

    # ---- STAGE 3: severity, reusing the summary row's own values -----------
    summary = g.sql(
        "SELECT total_anomalies, by_rule FROM ops_reports "
        "WHERE report = 'temporal_anomalies' AND batch_code = '" + batch + "'"
    )[0]
    live_by_rule = summary.get("by_rule") or {}
    live_total = summary.get("total_anomalies") or 0
    worst_rule = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    g.push("ops_reports", {
        "report": "temporal_anomaly_severity", "batch_code": batch,
        "worst_rule": worst_rule,
        "worst_rule_count": live_by_rule.get(worst_rule, 0),
        "overall_severity": "critical" if live_total > severity_critical else "low",
    })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
