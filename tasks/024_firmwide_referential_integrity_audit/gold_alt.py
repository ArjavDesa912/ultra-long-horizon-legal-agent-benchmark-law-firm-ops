#!/usr/bin/env python3
"""Independent second gold for 024_firmwide_referential_integrity_audit (v2).

Materially different path from gold.py: every per-control violation count is a
single SQL statement (anti-join against the referenced table, with the
PLACEHOLDER- exemption and the roster-active window expressed in SQL) instead
of a Python pass over fetched rows. Writes still go through the REST API.
Run against a FRESH container."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("ref_integrity_control_finding", "referential_integrity",
           "referential_integrity_severity")

# control_id -> SQL count of violating rows. The anti-join implements
# REF-INTEG-01: a reference is a violation when it does not resolve to a real
# row of the referenced collection, except PLACEHOLDER- scaffolding; the
# employee-style rule resolves against staff_roster rows active at the episode
# date.
# NB: the *_id reference columns are JSONB; `col #>> '{}'` unwraps to plain
# text (and maps JSON null to SQL NULL) so the PLACEHOLDER- exemption and the
# anti-join behave exactly like the Python path.
def control_sql(cid, episode_date):
    if cid == "CTL-REF-01":
        return ("SELECT COUNT(*) AS n FROM deadlines d LEFT JOIN matters m "
                "ON d.matter_id #>> '{}' = m.id::text "
                "WHERE m.id IS NULL AND d.matter_id #>> '{}' IS NOT NULL "
                "AND d.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'")
    if cid == "CTL-REF-02":
        return ("SELECT COUNT(*) AS n FROM tasks t LEFT JOIN matters m "
                "ON t.matter_id #>> '{}' = m.id::text "
                "WHERE m.id IS NULL AND t.matter_id #>> '{}' IS NOT NULL "
                "AND t.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'")
    if cid == "CTL-REF-03":
        return ("SELECT COUNT(*) AS n FROM time_entries e LEFT JOIN matters m "
                "ON e.matter_id #>> '{}' = m.id::text "
                "WHERE m.id IS NULL AND e.matter_id #>> '{}' IS NOT NULL "
                "AND e.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'")
    if cid == "CTL-REF-04":
        return ("SELECT COUNT(*) AS n FROM invoices i LEFT JOIN clients c "
                "ON i.client_id #>> '{}' = c.id::text "
                "WHERE c.id IS NULL AND i.client_id #>> '{}' IS NOT NULL "
                "AND i.client_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'")
    if cid == "CTL-REF-05":
        return ("SELECT COUNT(*) AS n FROM trust_transactions x LEFT JOIN clients c "
                "ON x.client_id #>> '{}' = c.id::text "
                "WHERE c.id IS NULL AND x.client_id #>> '{}' IS NOT NULL "
                "AND x.client_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'")
    if cid == "CTL-REF-06":
        return ("SELECT COUNT(*) AS n FROM time_entries e LEFT JOIN staff_roster r "
                "ON e.employee_id = r.employee_id "
                "AND r.active_from::date <= '" + episode_date + "'::date "
                "AND (r.active_to IS NULL OR r.active_to::date >= '" + episode_date + "'::date) "
                "WHERE r.employee_id IS NULL AND e.employee_id IS NOT NULL "
                "AND e.employee_id::text NOT LIKE 'PLACEHOLDER-%'")
    raise RuntimeError(f"no SQL path for control {cid}")


def main():
    g = glib.Gold()
    batch = g.nonce()
    severity_critical = int(g.nonce(field="severity_critical_count"))
    episode_date = glib.Gold.dp(g.nonce(field="episode_date"))

    controls = [c for c in g.all("firm_controls")
                if c.get("enabled") and str(c.get("control_id", "")).startswith("CTL-REF")]

    findings = []
    for ctl in controls:
        cid = ctl.get("control_id")
        rows = g.sql(control_sql(cid, episode_date))
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
        g.push("ops_reports", {"report": "ref_integrity_control_finding", "batch_code": batch, **f})

    # ---- STAGE 2: summary, reusing this batch's finding rows (SQL read) ----
    live = g.sql(
        "SELECT violation_count FROM ops_reports "
        "WHERE report = 'ref_integrity_control_finding' AND batch_code = '" + batch + "'"
    )
    by_rule = {f["rule_key"]: f["violation_count"] for f in findings}
    total = sum(int(r["violation_count"] or 0) for r in live)
    g.push("ops_reports", {
        "report": "referential_integrity", "batch_code": batch,
        "total_violations": total, "by_rule": by_rule, "clean": total == 0,
    })

    # ---- STAGE 3: severity, reusing the summary row's own values -----------
    summary = g.sql(
        "SELECT total_violations, by_rule FROM ops_reports "
        "WHERE report = 'referential_integrity' AND batch_code = '" + batch + "'"
    )[0]
    live_by_rule = summary.get("by_rule") or {}
    live_total = summary.get("total_violations") or 0
    worst_rule = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    g.push("ops_reports", {
        "report": "referential_integrity_severity", "batch_code": batch,
        "worst_rule": worst_rule,
        "worst_rule_count": live_by_rule.get(worst_rule, 0),
        "overall_severity": "critical" if live_total > severity_critical else "low",
    })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
