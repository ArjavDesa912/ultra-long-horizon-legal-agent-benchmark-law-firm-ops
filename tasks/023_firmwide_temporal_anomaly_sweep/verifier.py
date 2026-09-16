#!/usr/bin/env python3
"""Verifier for 023_firmwide_temporal_anomaly_sweep (v2).

Dual-path on every per-control count: each enabled temporal control's violation
count is computed from the live rows in Python AND via a SQL aggregate; both
paths must agree with each other and with the agent's finding rows. Dormant
controls must be absent. The summary and severity rows are checked against the
same live values, so a wrong sweep count silently corrupts them here too."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

# control_id -> (scope table, SQL predicate). CTL-TMP-01/02/03 are strictly
# before; CTL-TMP-04 is on-or-before (the registry's rule text says so).
SQL_SPECS = {
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


def before(a, b):
    da, db = vlib.dp(a), vlib.dp(b)
    return bool(da and db and da < db)


def on_or_before(a, b):
    da, db = vlib.dp(a), vlib.dp(b)
    return bool(da and db and da <= db)


def py_count(cid, rows):
    if cid == "CTL-TMP-01":
        return sum(1 for r in rows if before(r.get("due_date"), r.get("issued_date")))
    if cid == "CTL-TMP-02":
        return sum(1 for r in rows if r.get("date_closed")
                   and before(r.get("date_closed"), r.get("date_opened")))
    if cid == "CTL-TMP-03":
        return sum(1 for r in rows if r.get("reviewed_at")
                   and before(r.get("reviewed_at"), r.get("date_created")))
    if cid == "CTL-TMP-04":
        return sum(1 for r in rows if r.get("decision_date") and r.get("submitted_date")
                   and on_or_before(r.get("decision_date"), r.get("submitted_date")))
    raise RuntimeError(f"unknown control {cid}")


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    severity_critical = int(vlib.get_nonce(v.token, field="severity_critical_count"))

    controls = {c.get("control_id"): c for c in vlib.fetch_all(v.token, "firm_controls")}
    temporal_ids = [cid for cid in controls if str(cid).startswith("CTL-TMP")]
    enabled = {cid for cid in temporal_ids if controls[cid].get("enabled")}
    dormant = set(temporal_ids) - enabled

    expected = {}
    for cid in sorted(enabled):
        table, predicate = SQL_SPECS[cid]
        rows = vlib.fetch_all(v.token, table)
        python_n = py_count(cid, rows)
        sql_rows = vlib.sql(v.token, f"SELECT COUNT(*) AS n FROM {table} WHERE {predicate}")
        v.expect(len(sql_rows) == 1, f"{cid}: SQL dual-path returned unexpected shape")
        v.expect_equal(int(sql_rows[0]["n"]), python_n, f"{cid}: raw vs SQL disagree (dual-path)")
        expected[cid] = {"table": table, "count": python_n,
                         "rule_key": str(controls[cid].get("rule", "")).split(":", 1)[0].strip()}

    reports = vlib.fetch_all(v.token, "ops_reports")
    finding_rows = [r for r in reports
                    if r.get("report") == "temporal_control_finding" and r.get("batch_code") == batch]
    v.expect_equal(len(finding_rows), len(expected), "temporal_control_finding row count")
    by_control = {r.get("control_id"): r for r in finding_rows}
    for cid, exp in expected.items():
        row = by_control.get(cid)
        v.expect(row is not None, f"missing finding row for enabled control {cid}")
        if row is None:
            continue
        v.expect_equal(row.get("rule_key"), exp["rule_key"], f"{cid} rule_key")
        v.expect_equal(row.get("scope_collection"), exp["table"], f"{cid} scope_collection")
        v.expect_equal(row.get("violation_count"), exp["count"], f"{cid} violation_count")
    for cid in dormant:
        v.expect(all(r.get("control_id") != cid for r in finding_rows),
                 f"dormant control {cid} must not be reported")

    # ---------------------------------------------------- STAGE 2 (dependent)
    summary_rows = [r for r in reports
                    if r.get("report") == "temporal_anomalies" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "temporal_anomalies row count")
    sum_row = summary_rows[0]
    expected_by_rule = {exp["rule_key"]: exp["count"] for exp in expected.values()}
    v.expect_equal(sum_row.get("by_rule"), expected_by_rule, "temporal_anomalies by_rule")
    total = sum(expected_by_rule.values())
    v.expect_equal(sum_row.get("total_anomalies"), total, "total_anomalies")
    v.expect_equal(sum_row.get("clean"), total == 0, "temporal_anomalies clean")

    # ---------------------------------------------------- STAGE 3 (dependent)
    sev_rows = [r for r in reports
                if r.get("report") == "temporal_anomaly_severity" and r.get("batch_code") == batch]
    v.expect_equal(len(sev_rows), 1, "temporal_anomaly_severity row count")
    sev_row = sev_rows[0]
    live_by_rule = sum_row.get("by_rule") or {}
    expected_worst = max(sorted(live_by_rule), key=lambda k: live_by_rule[k]) if live_by_rule else None
    v.expect_equal(sev_row.get("worst_rule"), expected_worst, "temporal_anomaly_severity worst_rule")
    v.expect_equal(sev_row.get("worst_rule_count"), live_by_rule.get(expected_worst, 0),
                   "temporal_anomaly_severity worst_rule_count")
    v.expect_equal(sev_row.get("overall_severity"),
                   "critical" if total > severity_critical else "low",
                   "temporal_anomaly_severity overall_severity")

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
