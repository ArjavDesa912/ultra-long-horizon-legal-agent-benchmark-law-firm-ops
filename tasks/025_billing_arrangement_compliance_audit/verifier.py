#!/usr/bin/env python3
"""Verifier for 025_billing_arrangement_compliance_audit (v2).

Dual-path on every derived number: the violation set and the per-client
exposures are computed from the live rows in Python AND via a SQL join; both
paths must agree with each other and with the agent's pushed rows. Hazard
coverage: flagging a draft or disbursement-only invoice, ignoring the policy's
ranked slice, or auditing non-contingency clients must FAIL, and every
violation row must carry the diagnosis naming the bypassed arrangement."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

REPORTS = ("billing_arrangement_violation", "billing_arrangement_exposure",
           "billing_arrangement_alert", "billing_arrangement_summary")


def disbursement_only(inv):
    return (inv.get("fees_total") or 0) == 0 and (inv.get("disbursements_total") or 0) > 0


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    audit_top = int(vlib.get_nonce(v.token, field="audit_top_clients"))
    alert_threshold = int(vlib.get_nonce(v.token, field="exposure_alert_threshold"))

    clients = vlib.fetch_all(v.token, "clients")
    invoices = vlib.fetch_all(v.token, "invoices")
    contingency = {str(c["id"]): c for c in clients
                   if c.get("preferred_billing") == "contingency"}

    # ---------------- path 1: raw-row join in Python ------------------------
    billed = {cid: 0.0 for cid in contingency}
    for inv in invoices:
        cid = str(inv.get("client_id"))
        if inv.get("status") != "draft" and cid in billed:
            billed[cid] += inv.get("total", 0) or 0
    ranked = sorted(billed.items(), key=lambda kv: (-kv[1], int(kv[0])))[:audit_top]
    scoped = {cid for cid, _ in ranked}
    violations = [inv for inv in invoices
                  if str(inv.get("client_id")) in scoped
                  and inv.get("status") != "draft"
                  and not disbursement_only(inv)]

    expected_by_client = {}
    for inv in violations:
        cid = str(inv.get("client_id"))
        entry = expected_by_client.setdefault(cid, {"client_number":
                                                    contingency[cid].get("client_number"),
                                                    "count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += inv.get("total", 0) or 0

    # ---------------- independent SQL path (dual-path) ----------------------
    # The SQL reproduces the policy's ranked slice inside the same statement:
    # rank contingency clients by total non-draft billed value (including
    # disbursement-only rows, which still count toward the ranking metric),
    # keep the episode's audit_top slice, then count/sum only non-draft,
    # non-disbursement-only invoices. client_id is JSONB, so comparisons go
    # through #>> '{}' (unquoted text, JSON null -> SQL NULL).
    ranked_cte = (
        "WITH billed AS (SELECT c.id AS cid, COALESCE(SUM(i.total), 0) AS v "
        "FROM clients c LEFT JOIN invoices i "
        "ON i.client_id #>> '{}' = c.id::text AND i.status <> 'draft' "
        "WHERE c.preferred_billing = 'contingency' GROUP BY c.id), "
        "ranked AS (SELECT cid FROM billed ORDER BY v DESC, cid ASC LIMIT "
        + str(audit_top) + ") "
    )
    sql_count = vlib.sql(v.token, (
        ranked_cte +
        "SELECT COUNT(*) AS cnt FROM invoices i "
        "WHERE i.client_id #>> '{}' IN (SELECT cid::text FROM ranked) "
        "AND i.status <> 'draft' "
        "AND NOT (COALESCE(i.fees_total, 0) = 0 AND COALESCE(i.disbursements_total, 0) > 0)"
    ))
    v.expect(len(sql_count) == 1, "SQL dual-path returned unexpected shape")
    v.expect_equal(int(sql_count[0]["cnt"]), len(violations),
                   "violation count: raw-row join vs SQL disagree (dual-path)")
    sql_exposure = {str(r["client_id"]): (r["exposure"] or 0) for r in vlib.sql(v.token, (
        ranked_cte +
        "SELECT i.client_id AS client_id, SUM(i.total) AS exposure "
        "FROM invoices i "
        "WHERE i.client_id #>> '{}' IN (SELECT cid::text FROM ranked) "
        "AND i.status <> 'draft' "
        "AND NOT (COALESCE(i.fees_total, 0) = 0 AND COALESCE(i.disbursements_total, 0) > 0) "
        "GROUP BY i.client_id"))}
    v.expect_equal(set(sql_exposure), set(expected_by_client),
                   "exposure client set: raw vs SQL disagree (dual-path)")
    for cid, exposure in sql_exposure.items():
        v.expect_cents(expected_by_client[cid]["total"], exposure,
                       f"client {cid} exposure: raw vs SQL disagree (dual-path)")

    # ---------------- the agent's violation rows ----------------------------
    reports = vlib.fetch_all(v.token, "ops_reports")

    violation_rows = [r for r in reports
                      if r.get("report") == "billing_arrangement_violation" and r.get("batch_code") == batch]
    v.expect_equal(len(violation_rows), len(violations), "billing_arrangement_violation row count")
    by_invoice = {str(r.get("invoice_id")): r for r in violation_rows}
    for inv in violations:
        row = by_invoice.get(str(inv["id"]))
        v.expect(row is not None, f"missing violation row for invoice {inv['id']}")
        if row is None:
            continue
        v.expect_equal(row.get("invoice_number"), inv.get("invoice_number"),
                       f"invoice {inv['id']} invoice_number")
        v.expect_equal(str(row.get("client_id")), str(inv.get("client_id")),
                       f"invoice {inv['id']} client_id")
        v.expect_equal(str(row.get("client_number")),
                       str(contingency[str(inv.get("client_id"))].get("client_number")),
                       f"invoice {inv['id']} client_number")
        v.expect_equal(row.get("status"), inv.get("status"), f"invoice {inv['id']} status")
        v.expect_cents(row.get("total"), inv.get("total"), f"invoice {inv['id']} total")
        note = row.get("note")
        v.expect(isinstance(note, str) and note.strip() != "",
                 f"invoice {inv['id']} diagnosis note missing")
        v.expect("contingency" in str(note).lower(),
                 f"invoice {inv['id']} note must name the bypassed arrangement")
    flagged_ids = {str(r.get("invoice_id")) for r in violation_rows}
    v.expect_equal(flagged_ids, {str(inv["id"]) for inv in violations},
                   "violation row set (no extras, no misses)")

    # ---------------------------------------------------- STAGE 2 (dependent)
    exposure_rows = [r for r in reports
                     if r.get("report") == "billing_arrangement_exposure" and r.get("batch_code") == batch]
    alert_rows = [r for r in reports
                  if r.get("report") == "billing_arrangement_alert" and r.get("batch_code") == batch]
    summary_rows = [r for r in reports
                    if r.get("report") == "billing_arrangement_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "billing_arrangement_summary row count")

    v.expect_equal(len(exposure_rows), len(expected_by_client),
                   "billing_arrangement_exposure row count")
    by_exposure_cid = {str(r.get("client_id")): r for r in exposure_rows}
    for cid, entry in expected_by_client.items():
        row = by_exposure_cid.get(cid)
        v.expect(row is not None, f"missing exposure row for client {cid}")
        if row is None:
            continue
        v.expect_equal(row.get("client_number"), entry["client_number"],
                       f"client {cid} exposure client_number")
        v.expect_equal(row.get("violation_count"), entry["count"],
                       f"client {cid} exposure violation_count")
        v.expect_cents(row.get("total_exposure"), entry["total"],
                       f"client {cid} exposure total_exposure")
        alert = next((a for a in alert_rows if str(a.get("client_id")) == cid), None)
        if entry["total"] > alert_threshold:
            v.expect(alert is not None, f"missing alert row for client {cid}")
            if alert is not None:
                v.expect_cents(alert.get("total_exposure"), entry["total"],
                               f"client {cid} alert total_exposure")
                v.expect_equal(alert.get("threshold"), alert_threshold,
                               f"client {cid} alert threshold")
        else:
            v.expect(alert is None, f"unexpected alert row for client {cid} below threshold")
    expected_alert_ids = {cid for cid, entry in expected_by_client.items()
                          if entry["total"] > alert_threshold}
    v.expect_equal({str(r.get("client_id")) for r in alert_rows}, expected_alert_ids,
                   "alert set (exactly the clients above the episode threshold)")

    summary = summary_rows[0]
    v.expect_equal(summary.get("clients_in_scope"), len(scoped), "clients_in_scope")
    v.expect_equal(summary.get("clients_with_violations"), len(expected_by_client),
                   "clients_with_violations")
    v.expect_equal(summary.get("violations_found"), len(violations), "violations_found")
    v.expect_cents(summary.get("total_exposure"),
                   sum(entry["total"] for entry in expected_by_client.values()),
                   "summary total_exposure")

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
