#!/usr/bin/env python3
"""Gold solution for 024_firmwide_referential_integrity_audit (v2). Run against
a FRESH container. Idempotent: this batch's ops_reports rows are deleted and
rewritten, so a second run leaves identical state.

The audited rules come from the firm_controls registry (enabled referential
controls only); the placeholder exemption and the roster-active resolution for
employee-style fields come from firm policy REF-INTEG-01."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("ref_integrity_control_finding", "referential_integrity",
           "referential_integrity_severity")


def dangling(values, referenced_ids):
    """Rows whose reference does not resolve, excluding PLACEHOLDER- scaffolding
    (REF-INTEG-01: placeholders are never violations)."""
    return sum(1 for v in values
               if v is not None and str(v) not in referenced_ids
               and not str(v).startswith("PLACEHOLDER-"))


def main():
    g = glib.Gold()
    batch = g.nonce()
    severity_critical = int(g.nonce(field="severity_critical_count"))
    episode_date = glib.Gold.dp(g.nonce(field="episode_date"))

    controls = [c for c in g.all("firm_controls")
                if c.get("enabled") and str(c.get("control_id", "")).startswith("CTL-REF")]

    matters = {str(m["id"]) for m in g.all("matters")}
    clients = {str(c["id"]) for c in g.all("clients")}
    roster = g.all("staff_roster")
    active_roster = set()
    for emp in roster:
        start = glib.Gold.dp(emp.get("active_from"))
        end = glib.Gold.dp(emp.get("active_to"))
        if start and start <= episode_date and (emp.get("active_to") is None or (glib.Gold.dp(emp.get("active_to")) or "") >= episode_date):
            active_roster.add(emp.get("employee_id"))

    tables = {
        "deadlines": g.all("deadlines"),
        "tasks": g.all("tasks"),
        "time_entries": g.all("time_entries"),
        "invoices": g.all("invoices"),
        "trust_transactions": g.all("trust_transactions"),
        "grant_reports": g.all("grant_reports"),
    }

    def evaluate(rule_text, rows):
        text = str(rule_text or "").lower()
        if "_matter_id" in text:
            return dangling([r.get("matter_id") for r in rows], matters)
        if "_client_id" in text:
            return dangling([r.get("client_id") for r in rows], clients)
        if "time_entries_employee_id" in text:
            return dangling([r.get("employee_id") for r in rows], active_roster)
        raise RuntimeError(f"unrecognized referential control rule: {rule_text}")

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
        g.push("ops_reports", {"report": "ref_integrity_control_finding", "batch_code": batch, **f})

    # ---- STAGE 2: summary, reusing this batch's finding rows ---------------
    live_findings = [r for r in g.all("ops_reports")
                     if r.get("report") == "ref_integrity_control_finding" and r.get("batch_code") == batch]
    by_rule = {f["rule_key"]: f["violation_count"] for f in findings}
    total = sum(int(r.get("violation_count") or 0) for r in live_findings)
    g.push("ops_reports", {
        "report": "referential_integrity", "batch_code": batch,
        "total_violations": total, "by_rule": by_rule, "clean": total == 0,
    })

    # ---- STAGE 3: severity, reusing the summary row's own values -----------
    summary_row = next(r for r in g.all("ops_reports")
                       if r.get("report") == "referential_integrity" and r.get("batch_code") == batch)
    live_by_rule = summary_row.get("by_rule") or {}
    live_total = summary_row.get("total_violations") or 0
    worst_rule = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    g.push("ops_reports", {
        "report": "referential_integrity_severity", "batch_code": batch,
        "worst_rule": worst_rule,
        "worst_rule_count": live_by_rule.get(worst_rule, 0),
        "overall_severity": "critical" if live_total > severity_critical else "low",
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
