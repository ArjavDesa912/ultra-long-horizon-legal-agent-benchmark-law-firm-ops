#!/usr/bin/env python3
"""Gold solution for 025_billing_arrangement_compliance_audit (v2). Run
against a FRESH container. Idempotent: this batch's ops_reports rows are
deleted and rewritten, so a second run leaves identical state.

The arrangement matrix, the disbursement-only exception, the ranked-slice scope
and the exposure alert rule all come from firm policy BILL-ARR-01 (with the
episode row supplying audit_top_clients and exposure_alert_threshold)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("billing_arrangement_violation", "billing_arrangement_exposure",
           "billing_arrangement_alert", "billing_arrangement_summary")


def disbursement_only(inv):
    return (inv.get("fees_total") or 0) == 0 and (inv.get("disbursements_total") or 0) > 0


def main():
    g = glib.Gold()
    batch = g.nonce()
    audit_top = int(g.nonce(field="audit_top_clients"))
    alert_threshold = int(g.nonce(field="exposure_alert_threshold"))

    clients = g.all("clients")
    invoices = g.all("invoices")
    contingency = {str(c["id"]): c for c in clients if c.get("preferred_billing") == "contingency"}

    billed = {cid: 0.0 for cid in contingency}
    for inv in invoices:
        cid = str(inv.get("client_id"))
        if cid in contingency and inv.get("status") != "draft":
            billed[cid] += inv.get("total", 0) or 0

    scoped = {cid for cid, _ in sorted(billed.items(), key=lambda kv: (-kv[1], int(kv[0])))[:audit_top]}

    violations = [inv for inv in invoices
                  if str(inv.get("client_id")) in scoped
                  and inv.get("status") != "draft"
                  and not disbursement_only(inv)]

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    for inv in violations:
        client = contingency[str(inv.get("client_id"))]
        g.push("ops_reports", {
            "report": "billing_arrangement_violation", "batch_code": batch,
            "invoice_id": inv["id"], "invoice_number": inv.get("invoice_number"),
            "client_id": inv.get("client_id"), "client_number": client.get("client_number"),
            "status": inv.get("status"), "total": inv.get("total"),
            "note": ("violation: bypassed fee agreement -- contingency client invoiced a "
                     "non-draft fee invoice (status " + str(inv.get("status")) + "), "
                     "which BILL-ARR-01 permits only as draft"),
        })

    # ---- STAGE 2: exposure + alerts + summary, reusing the violation rows --
    live_violations = [r for r in g.all("ops_reports")
                       if r.get("report") == "billing_arrangement_violation" and r.get("batch_code") == batch]
    by_client = {}
    for row in live_violations:
        cid = str(row.get("client_id"))
        entry = by_client.setdefault(cid, {"client_number": row.get("client_number"),
                                           "count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += row.get("total", 0) or 0
    for cid, entry in by_client.items():
        g.push("ops_reports", {
            "report": "billing_arrangement_exposure", "batch_code": batch,
            "client_id": cid, "client_number": entry["client_number"],
            "violation_count": entry["count"], "total_exposure": entry["total"],
        })
        if entry["total"] > alert_threshold:
            g.push("ops_reports", {
                "report": "billing_arrangement_alert", "batch_code": batch,
                "client_id": cid, "client_number": entry["client_number"],
                "total_exposure": entry["total"], "threshold": alert_threshold,
            })

    g.push("ops_reports", {
        "report": "billing_arrangement_summary", "batch_code": batch,
        "clients_in_scope": len(scoped),
        "clients_with_violations": len(by_client),
        "violations_found": len(live_violations),
        "total_exposure": sum(entry["total"] for entry in by_client.values()),
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
