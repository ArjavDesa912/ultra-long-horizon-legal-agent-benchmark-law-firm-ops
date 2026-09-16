#!/usr/bin/env python3
"""Gold solution for 023_firmwide_temporal_anomaly_sweep (v2). Run against a
FRESH container. Idempotent: this batch's ops_reports rows are deleted and
rewritten, so a second run leaves identical state.

The sweep's rules are read from the firm_controls registry (enabled temporal
controls only) and evaluated with the comparison each rule text states -- three
strictly-before, one on-or-before (CTL-TMP-04)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("temporal_control_finding", "temporal_anomalies", "temporal_anomaly_severity")


def strictly_before(a, b):
    da, db = glib.Gold.dp(a), glib.Gold.dp(b)
    return bool(da and db and da < db)


def on_or_before(a, b):
    da, db = glib.Gold.dp(a), glib.Gold.dp(b)
    return bool(da and db and da <= db)


def main():
    g = glib.Gold()
    batch = g.nonce()
    severity_critical = int(g.nonce(field="severity_critical_count"))

    controls = [c for c in g.all("firm_controls")
                if c.get("enabled") and str(c.get("control_id", "")).startswith("CTL-TMP")]

    tables = {
        "invoices": g.all("invoices"),
        "matters": g.all("matters"),
        "ediscovery_documents": g.all("ediscovery_documents"),
        "grant_applications": g.all("grant_applications"),
    }

    def evaluate(rule_text, rows):
        text = str(rule_text or "").lower()
        if "due_date is strictly before issued_date" in text:
            return sum(1 for r in rows if strictly_before(r.get("due_date"), r.get("issued_date")))
        if "date_closed strictly before date_opened" in text:
            return sum(1 for r in rows if r.get("date_closed")
                       and strictly_before(r.get("date_closed"), r.get("date_opened")))
        if "reviewed_at strictly before date_created" in text:
            return sum(1 for r in rows if r.get("reviewed_at")
                       and strictly_before(r.get("reviewed_at"), r.get("date_created")))
        if "decision_date on or before submitted_date" in text:
            return sum(1 for r in rows if r.get("decision_date") and r.get("submitted_date")
                       and on_or_before(r.get("decision_date"), r.get("submitted_date")))
        raise RuntimeError(f"unrecognized temporal control rule: {rule_text}")

    findings = []
    for ctl in controls:
        scope = ctl.get("scope_collection")
        findings.append({
            "control_id": ctl.get("control_id"),
            "rule_key": str(ctl.get("rule", "")).split(":", 1)[0].strip(),
            "scope_collection": scope,
            "violation_count": evaluate(ctl.get("rule"), tables.get(scope) or g.all(scope)),
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

    # ---- STAGE 2: summary, reusing this batch's finding rows ---------------
    live_findings = [r for r in g.all("ops_reports")
                     if r.get("report") == "temporal_control_finding" and r.get("batch_code") == batch]
    by_rule = {f["rule_key"]: f["violation_count"] for f in findings}
    total = sum(int(r.get("violation_count") or 0) for r in live_findings)
    g.push("ops_reports", {
        "report": "temporal_anomalies", "batch_code": batch,
        "total_anomalies": total, "by_rule": by_rule, "clean": total == 0,
    })

    # ---- STAGE 3: severity, reusing the summary row's own values -----------
    summary_row = next(r for r in g.all("ops_reports")
                       if r.get("report") == "temporal_anomalies" and r.get("batch_code") == batch)
    live_by_rule = summary_row.get("by_rule") or {}
    live_total = summary_row.get("total_anomalies") or 0
    # sorted() first: ties break alphabetically regardless of JSONB key order
    worst_rule = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    g.push("ops_reports", {
        "report": "temporal_anomaly_severity", "batch_code": batch,
        "worst_rule": worst_rule,
        "worst_rule_count": live_by_rule.get(worst_rule, 0),
        "overall_severity": "critical" if live_total > severity_critical else "low",
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
