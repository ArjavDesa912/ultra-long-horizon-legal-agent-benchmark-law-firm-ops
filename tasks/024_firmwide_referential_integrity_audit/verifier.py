#!/usr/bin/env python3
"""Verifier for 024_firmwide_referential_integrity_audit (v2).

Dual-path on every per-control count: each enabled referential control's
violation count is computed from the live rows in Python AND via a SQL
anti-join; both paths must agree with each other and with the agent's finding
rows. The PLACEHOLDER- exemption and the roster-active resolution for
employee-style fields are enforced here (counting placeholders FAILs), and the
dormant CTL-REF-07 must be absent."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

# control_id -> (scope table, SQL count of violating rows). The anti-joins
# implement REF-INTEG-01: a reference is a violation when it does not resolve
# to a real row of the referenced collection, except PLACEHOLDER- scaffolding.
# NB: matter_id/client_id are JSONB columns in the live store; ::text keeps the
# JSON quotes ("\"PLACEHOLDER-x\"") so LIKE-based exemptions silently fail, and
# a JSON null is not SQL NULL. `col #>> '{}'` unwraps to plain text and maps
# JSON null to SQL NULL, matching the Python path's semantics exactly.
SQL_COUNTS = {
    "CTL-REF-01": ("deadlines",
                   "SELECT COUNT(*) AS n FROM deadlines d LEFT JOIN matters m "
                   "ON d.matter_id #>> '{}' = m.id::text "
                   "WHERE m.id IS NULL AND d.matter_id #>> '{}' IS NOT NULL "
                   "AND d.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'"),
    "CTL-REF-02": ("tasks",
                   "SELECT COUNT(*) AS n FROM tasks t LEFT JOIN matters m "
                   "ON t.matter_id #>> '{}' = m.id::text "
                   "WHERE m.id IS NULL AND t.matter_id #>> '{}' IS NOT NULL "
                   "AND t.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'"),
    "CTL-REF-03": ("time_entries",
                   "SELECT COUNT(*) AS n FROM time_entries e LEFT JOIN matters m "
                   "ON e.matter_id #>> '{}' = m.id::text "
                   "WHERE m.id IS NULL AND e.matter_id #>> '{}' IS NOT NULL "
                   "AND e.matter_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'"),
    "CTL-REF-04": ("invoices",
                   "SELECT COUNT(*) AS n FROM invoices i LEFT JOIN clients c "
                   "ON i.client_id #>> '{}' = c.id::text "
                   "WHERE c.id IS NULL AND i.client_id #>> '{}' IS NOT NULL "
                   "AND i.client_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'"),
    "CTL-REF-05": ("trust_transactions",
                   "SELECT COUNT(*) AS n FROM trust_transactions x LEFT JOIN clients c "
                   "ON x.client_id #>> '{}' = c.id::text "
                   "WHERE c.id IS NULL AND x.client_id #>> '{}' IS NOT NULL "
                   "AND x.client_id #>> '{}' NOT LIKE 'PLACEHOLDER-%'"),
    "CTL-REF-06": ("time_entries",
                   "SELECT COUNT(*) AS n FROM time_entries e LEFT JOIN staff_roster r "
                   "ON e.employee_id = r.employee_id "
                   "AND r.active_from::date <= '{ep}'::date "
                   "AND (r.active_to IS NULL OR r.active_to::date >= '{ep}'::date) "
                   "WHERE r.employee_id IS NULL AND e.employee_id IS NOT NULL "
                   "AND e.employee_id::text NOT LIKE 'PLACEHOLDER-%'"),
}


def is_placeholder(value):
    return value is not None and str(value).startswith("PLACEHOLDER-")


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    severity_critical = int(vlib.get_nonce(v.token, field="severity_critical_count"))
    episode_date = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))

    controls = {c.get("control_id"): c for c in vlib.fetch_all(v.token, "firm_controls")}
    ref_ids = [cid for cid in controls if str(cid).startswith("CTL-REF")]
    enabled = {cid for cid in ref_ids if controls[cid].get("enabled")}
    dormant = set(ref_ids) - enabled

    matter_ids = {str(m["id"]) for m in vlib.fetch_all(v.token, "matters")}
    client_ids = {str(c["id"]) for c in vlib.fetch_all(v.token, "clients")}
    active_roster = set()
    for emp in vlib.fetch_all(v.token, "staff_roster"):
        start = vlib.dp(emp.get("active_from"))
        end = vlib.dp(emp.get("active_to"))
        if start and start <= episode_date and (emp.get("active_to") is None or end >= episode_date):
            active_roster.add(emp.get("employee_id"))

    def python_count(cid):
        if cid == "CTL-REF-06":
            rows = vlib.fetch_all(v.token, "time_entries")
            return sum(1 for r in rows if r.get("employee_id") is not None
                       and r["employee_id"] not in active_roster
                       and not is_placeholder(r["employee_id"]))
        table = SQL_COUNTS[cid][0]
        field = "matter_id" if cid in ("CTL-REF-01", "CTL-REF-02", "CTL-REF-03") else "client_id"
        referenced = matter_ids if cid in ("CTL-REF-01", "CTL-REF-02", "CTL-REF-03") else client_ids
        rows = vlib.fetch_all(v.token, table)
        return sum(1 for r in rows if r.get(field) is not None
                   and str(r[field]) not in referenced
                   and not is_placeholder(r[field]))

    expected = {}
    for cid in sorted(enabled):
        n = python_count(cid)
        sql_rows = vlib.sql(v.token, SQL_COUNTS[cid][1].replace("{ep}", episode_date))
        v.expect(len(sql_rows) == 1, f"{cid}: SQL dual-path returned unexpected shape")
        v.expect_equal(int(sql_rows[0]["n"]), n, f"{cid}: raw vs SQL disagree (dual-path)")
        rule_key = str(controls[cid].get("rule", "")).split(":", 1)[0].strip()
        scope = controls[cid].get("scope_collection")
        expected[cid] = {"count": n, "rule_key": rule_key, "scope": scope}

    reports = vlib.fetch_all(v.token, "ops_reports")
    finding_rows = [r for r in reports
                    if r.get("report") == "ref_integrity_control_finding" and r.get("batch_code") == batch]
    v.expect_equal(len(finding_rows), len(expected), "ref_integrity_control_finding row count")
    by_control = {r.get("control_id"): r for r in finding_rows}
    for cid, exp in expected.items():
        row = by_control.get(cid)
        v.expect(row is not None, f"missing finding row for enabled control {cid}")
        if row is None:
            continue
        v.expect_equal(row.get("rule_key"), exp["rule_key"], f"{cid} rule_key")
        v.expect_equal(row.get("scope_collection"), exp["scope"], f"{cid} scope_collection")
        v.expect_equal(row.get("violation_count"), exp["count"], f"{cid} violation_count")
    reported_ids = {r.get("control_id") for r in finding_rows}
    for cid in dormant:
        v.expect(cid not in reported_ids, f"dormant control {cid} must not be reported")

    # ---------------------------------------------------- STAGE 2 (dependent)
    summary_rows = [r for r in reports
                    if r.get("report") == "referential_integrity" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "referential_integrity row count")
    sum_row = summary_rows[0]
    expected_by_rule = {exp["rule_key"]: exp["count"] for exp in expected.values()}
    v.expect_equal(sum_row.get("by_rule"), expected_by_rule, "referential_integrity by_rule")
    total = sum(expected_by_rule.values())
    v.expect_equal(sum_row.get("total_violations"), total, "total_violations")
    v.expect_equal(sum_row.get("clean"), total == 0, "referential_integrity clean")

    # ---------------------------------------------------- STAGE 3 (dependent)
    severity_rows = [r for r in reports
                     if r.get("report") == "referential_integrity_severity" and r.get("batch_code") == batch]
    v.expect_equal(len(severity_rows), 1, "referential_integrity_severity row count")
    sev_row = severity_rows[0]
    live_by_rule = sum_row.get("by_rule") or {}
    expected_worst = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    v.expect_equal(sev_row.get("worst_rule"), expected_worst, "referential_integrity_severity worst_rule")
    v.expect_equal(sev_row.get("worst_rule_count"), live_by_rule.get(expected_worst, 0),
                   "referential_integrity_severity worst_rule_count")
    v.expect_equal(sev_row.get("overall_severity"),
                   "critical" if total > severity_critical else "low",
                   "referential_integrity_severity overall_severity")

    v.check_canaries([
        "contacts", "deadlines", "tasks", "time_entries", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "clients",
        "hold_reminders",
        "invoices",
        "matters",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
