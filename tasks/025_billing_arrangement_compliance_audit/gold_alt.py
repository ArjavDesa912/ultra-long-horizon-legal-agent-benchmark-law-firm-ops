#!/usr/bin/env python3
"""Independent second gold for 025_billing_arrangement_compliance_audit (v2).

Materially different path from gold.py: the scoped-client ranking, the
violation set and the per-client exposure grouping are all computed in SQL
(join + GROUP BY in the database) instead of Python passes over fetched rows.
Writes still go through the REST API (SQL is read-only here). Run against a
FRESH container."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("billing_arrangement_violation", "billing_arrangement_exposure",
           "billing_arrangement_alert", "billing_arrangement_summary")


def main():
    g = glib.Gold()
    batch = g.nonce()
    top_n = int(g.nonce(field="audit_top_clients"))
    alert_threshold = int(g.nonce(field="exposure_alert_threshold"))

    # ---- scope: the policy's ranked slice, computed entirely in SQL --------
    ranked = g.sql(
        "SELECT c.id AS client_id, COALESCE(SUM(i.total), 0) AS billed "
        "FROM clients c LEFT JOIN invoices i "
        "ON i.client_id #>> '{}' = c.id::text AND i.status <> 'draft' "
        "WHERE c.preferred_billing = 'contingency' "
        "GROUP BY c.id ORDER BY billed DESC, c.id ASC LIMIT " + str(int(top_n))
    )
    scoped_ids = {str(r["client_id"]) for r in ranked}

    # ---- violations: SQL join, policy filter in the WHERE clause -----------
    violating = g.sql(
        "SELECT i.id, i.invoice_number, i.client_id, c.client_number, i.status, i.total "
        "FROM invoices i JOIN clients c ON i.client_id #>> '{}' = c.id::text "
        "WHERE c.preferred_billing = 'contingency' AND i.status <> 'draft' "
        "AND NOT (COALESCE(i.fees_total, 0) = 0 AND COALESCE(i.disbursements_total, 0) > 0) "
        "AND i.client_id #>> '{}' IN (" + ",".join("'" + cid + "'" for cid in sorted(scoped_ids)) + ") "
        "ORDER BY i.id ASC"
    )

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    for inv in violating:
        g.push("ops_reports", {
            "report": "billing_arrangement_violation", "batch_code": batch,
            "invoice_id": inv["id"], "invoice_number": inv.get("invoice_number"),
            "client_id": inv.get("client_id"), "client_number": inv.get("client_number"),
            "status": inv.get("status"), "total": inv.get("total"),
            "note": ("violation: bypassed fee agreement -- contingency client invoiced a "
                     "non-draft fee invoice (status " + str(inv.get("status")) + "); "
                     "BILL-ARR-01 permits only draft"),
        })

    # ---- STAGE 2: exposure + alerts + summary (SQL-grouped) ----------------
    grouped = g.sql(
        "SELECT client_id, MIN(client_number) AS client_number, COUNT(*) AS n, "
        "SUM(total) AS exposure FROM ops_reports "
        "WHERE report = 'billing_arrangement_violation' AND batch_code = '" + batch + "' "
        "GROUP BY client_id ORDER BY client_id"
    )
    total_exposure = 0.0
    for r in grouped:
        exposure = r["exposure"] or 0
        g.push("ops_reports", {
            "report": "billing_arrangement_exposure", "batch_code": batch,
            "client_id": str(r["client_id"]), "client_number": r["client_number"],
            "violation_count": int(r["n"]), "total_exposure": exposure,
        })
        if (exposure or 0) > alert_threshold:
            g.push("ops_reports", {
                "report": "billing_arrangement_alert", "batch_code": batch,
                "client_id": str(r["client_id"]), "client_number": r["client_number"],
                "total_exposure": exposure, "threshold": alert_threshold,
            })
        total_exposure += exposure or 0

    g.push("ops_reports", {
        "report": "billing_arrangement_summary", "batch_code": batch,
        "clients_in_scope": len(scoped_ids),
        "clients_with_violations": len(grouped),
        "violations_found": len(violating),
        "total_exposure": total_exposure,
    })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
