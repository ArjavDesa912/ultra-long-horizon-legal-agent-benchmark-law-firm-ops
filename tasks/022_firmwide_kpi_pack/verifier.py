#!/usr/bin/env python3
"""Verifier for 022_firmwide_kpi_pack (v2).

Dual-path on every derived number: each KPI is computed from the live rows in
Python AND via a SQL aggregate; both paths must agree with each other and with
the agent's pushed pack row. The ratio row, alert rows, and tie-out row are
checked against the SAME live pack values, so a wrong stage-1 KPI silently
corrupts the later stages here exactly the way it does for the agent."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

AR_STATUSES = ("sent", "overdue", "disputed")
WIP_STATUSES = ("draft", "submitted", "approved")
TERMINAL_OPP_STATUSES = ("awarded", "declined", "withdrawn")


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ar_alert = int(vlib.get_nonce(v.token, field="ar_alert_ratio"))
    trust_alert = int(vlib.get_nonce(v.token, field="trust_alert_ratio"))

    matters = vlib.fetch_all(v.token, "matters")
    entries = vlib.fetch_all(v.token, "time_entries")
    invoices = vlib.fetch_all(v.token, "invoices")
    clients = vlib.fetch_all(v.token, "clients")
    holds = vlib.fetch_all(v.token, "ediscovery_holds")
    documents = vlib.fetch_all(v.token, "ediscovery_documents")
    opportunities = vlib.fetch_all(v.token, "grant_opportunities")

    # ---------------- path 1: independent Python over the live rows ---------
    open_matters = sum(1 for m in matters if m.get("status") == "open")
    total_wip_value = sum(e.get("amount", 0) or 0 for e in entries
                          if e.get("status") in WIP_STATUSES)
    total_open_ar = sum((i.get("total", 0) or 0) - (i.get("amount_paid", 0) or 0) for i in invoices
                        if i.get("status") in AR_STATUSES
                        and (i.get("total", 0) or 0) - (i.get("amount_paid", 0) or 0) > 0)
    total_trust_liability = sum(c.get("trust_balance", 0) or 0 for c in clients)
    active_holds = sum(1 for h in holds if h.get("status") == "active")
    documents_under_review = sum(1 for d in documents
                                 if d.get("review_status") in ("unreviewed", "in_review"))
    pipeline = sum(o.get("max_award", 0) or 0 for o in opportunities
                   if o.get("status") not in TERMINAL_OPP_STATUSES)

    # ---------------- path 2: one SQL statement, the same seven scalars -----
    sql = vlib.sql(v.token, (
        "SELECT "
        " (SELECT COUNT(*) FROM matters WHERE status = 'open') AS open_matters, "
        " (SELECT COALESCE(SUM(amount), 0) FROM time_entries "
        "   WHERE status IN ('draft','submitted','approved')) AS total_wip_value, "
        " (SELECT COALESCE(SUM(total - amount_paid), 0) FROM invoices "
        "   WHERE status IN ('sent','overdue','disputed') "
        "     AND (total - amount_paid) > 0) AS total_open_ar, "
        " (SELECT COALESCE(SUM(trust_balance), 0) FROM clients) AS total_trust_liability, "
        " (SELECT COUNT(*) FROM ediscovery_holds WHERE status = 'active') AS active_holds, "
        " (SELECT COUNT(*) FROM ediscovery_documents "
        "   WHERE review_status IN ('unreviewed','in_review')) AS documents_under_review, "
        " (SELECT COALESCE(SUM(max_award), 0) FROM grant_opportunities "
        "   WHERE status NOT IN ('awarded','declined','withdrawn')) "
        " AS active_grant_pipeline_value"
    ))
    v.expect(len(sql) == 1, "SQL dual-path returned unexpected shape")
    srow = sql[0]
    v.expect_equal(int(srow["open_matters"]), open_matters, "open_matters: raw vs SQL disagree (dual-path)")
    v.expect_cents(srow["total_wip_value"], total_wip_value, "total_wip_value: raw vs SQL disagree (dual-path)")
    v.expect_cents(srow["total_open_ar"], total_open_ar, "total_open_ar: raw vs SQL disagree (dual-path)")
    v.expect_cents(srow["total_trust_liability"], total_trust_liability,
                   "total_trust_liability: raw vs SQL disagree (dual-path)")
    v.expect_equal(int(srow["active_holds"]), active_holds, "active_holds: raw vs SQL disagree (dual-path)")
    v.expect_equal(int(srow["documents_under_review"]), documents_under_review,
                   "documents_under_review: raw vs SQL disagree (dual-path)")
    v.expect_cents(srow["active_grant_pipeline_value"], pipeline, "active_grant_pipeline_value: raw vs SQL disagree (dual-path)")

    # ---------------- the agent's pack row must carry all seven, exact ------
    reports = vlib.fetch_all(v.token, "ops_reports")
    kpi_rows = [r for r in reports if r.get("report") == "enterprise_kpi" and r.get("batch_code") == batch]
    v.expect_equal(len(kpi_rows), 1, "enterprise_kpi row count")
    row = kpi_rows[0]
    v.expect_equal(row.get("open_matters"), open_matters, "open_matters")
    v.expect_cents(row.get("total_wip_value"), total_wip_value, "total_wip_value")
    v.expect_cents(row.get("total_open_ar"), total_open_ar, "total_open_ar")
    v.expect_cents(row.get("total_trust_liability"), total_trust_liability, "total_trust_liability")
    v.expect_equal(row.get("active_holds"), active_holds, "active_holds")
    v.expect_equal(row.get("documents_under_review"), documents_under_review, "documents_under_review")
    v.expect_cents(row.get("active_grant_pipeline_value"), pipeline, "active_grant_pipeline_value")

    # ------------------------------------------------- STAGE 2 (dependent) --
    # The ratios are asserted against the agent's OWN pack row (read back
    # above) -- a wrong stage-1 KPI silently produces a wrong ratio here.
    wip = row.get("total_wip_value") or 0
    ar = row.get("total_open_ar") or 0
    trust = row.get("total_trust_liability") or 0
    expected_ar_wip = round(total_open_ar / total_wip_value, 2) if total_wip_value else None
    expected_trust_cov = round(total_trust_liability / total_open_ar, 2) if total_open_ar else None
    ratio_rows = [r for r in reports if r.get("report") == "enterprise_kpi_ratios" and r.get("batch_code") == batch]
    v.expect_equal(len(ratio_rows), 1, "enterprise_kpi_ratios row count")
    rrow = ratio_rows[0]
    if expected_ar_wip is None:
        v.expect(rrow.get("ar_to_wip_ratio") is None, "ar_to_wip_ratio should be null")
    else:
        v.expect(rrow.get("ar_to_wip_ratio") is not None
                 and abs(rrow.get("ar_to_wip_ratio") - expected_ar_wip) <= 0.02,
                 "ar_to_wip_ratio off")
    if expected_trust_cov is None:
        v.expect(rrow.get("trust_coverage_ratio") is None, "trust_coverage_ratio should be null")
    else:
        v.expect(rrow.get("trust_coverage_ratio") is not None
                 and abs(rrow.get("trust_coverage_ratio") - expected_trust_cov) <= 0.02,
                 "trust_coverage_ratio off")

    # ---- STAGE 3: alerts, exactly the policy-breaching ratios --------------
    expected_alerts = set()
    if expected_ar_wip is not None and expected_ar_wip * 100 > ar_alert:
        expected_alerts.add("ar_to_wip_ratio")
    if expected_trust_cov is not None and expected_trust_cov * 100 < trust_alert:
        expected_alerts.add("trust_coverage_ratio")
    alert_rows = [r for r in reports if r.get("report") == "enterprise_kpi_alert" and r.get("batch_code") == batch]
    v.expect_equal(len(alert_rows), len(expected_alerts), "enterprise_kpi_alert row count")
    seen = {r.get("ratio") for r in alert_rows}
    v.expect_equal(seen, expected_alerts, "alert set (exactly the policy-breaching ratios)")
    for r in alert_rows:
        v.expect(r.get("direction") in ("above", "below"), f"alert {r.get('ratio')} direction missing")
        v.expect(r.get("threshold") is not None, f"alert {r.get('ratio')} threshold missing")
        if r.get("ratio") == "ar_to_wip_ratio":
            v.expect_equal(r.get("direction"), "above", "ar_to_wip alert direction")
            v.expect_equal(r.get("threshold"), ar_alert, "ar alert threshold")
            v.expect(abs(vlib.cents(r.get("value")) - vlib.cents(rrow.get("ar_to_wip_ratio"))) <= 2,
                     "ar alert value must reuse the ratios row's value")
        else:
            v.expect_equal(r.get("direction"), "below", "trust alert direction")
            v.expect_equal(r.get("threshold"), trust_alert, "trust alert threshold")
            v.expect(abs(vlib.cents(r.get("value")) - vlib.cents(rrow.get("trust_coverage_ratio"))) <= 2,
                     "trust alert value must reuse the ratios row's value")

    # --------------------------------------------------- STAGE 4 (dependent)
    tie_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                if r.get("report") == "enterprise_kpi_tieout" and r.get("batch_code") == batch]
    v.expect_equal(len(tie_rows), 1, "enterprise_kpi_tieout row count")
    trow = tie_rows[0]
    v.expect_equal(trow.get("ratios_checked"), 2, "tie-out ratios_checked")
    v.expect_equal(trow.get("alerts_raised"), len(alert_rows), "tie-out alerts_raised")
    v.expect_equal(trow.get("alert_values_consistent"), True, "tie-out alert_values_consistent")
    v.expect_equal(trow.get("tie_out_ok"), True, "tie_out_ok")

    v.check_canaries([
        "contacts", "deadlines", "tasks", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "clients",
        "hold_reminders",
        "invoices",
        "matters",
        "time_entries",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
