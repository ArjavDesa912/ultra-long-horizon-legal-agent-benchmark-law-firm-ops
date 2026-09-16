#!/usr/bin/env python3
"""Independent second gold for 022_firmwide_kpi_pack (v2).

Materially different path from gold.py: every KPI is computed by a single SQL
statement (seven scalar subqueries) instead of fetching seven collections and
filtering in Python, and the ratio/alert/tie-out chain reads its rows back
through SQL as well. Writes still go through the REST API (SQL is read-only
in this env). Reaches the IDENTICAL end-state as gold.py. Run against a FRESH
container."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    ar_alert = int(g.nonce(field="ar_alert_ratio"))
    trust_alert = int(g.nonce(field="trust_alert_ratio"))

    # ---- STAGE 1: the whole pack from one SQL statement --------------------
    kpi = g.sql(
        "SELECT "
        "  (SELECT COUNT(*) FROM matters WHERE status = 'open') AS open_matters, "
        "  (SELECT COALESCE(SUM(amount), 0) FROM time_entries "
        "     WHERE status IN ('draft','submitted','approved')) AS total_wip_value, "
        "  (SELECT COALESCE(SUM(total - amount_paid), 0) FROM invoices "
        "     WHERE status IN ('sent','overdue','disputed') "
        "       AND (total - amount_paid) > 0) AS total_open_ar, "
        "  (SELECT COALESCE(SUM(trust_balance), 0) FROM clients) AS total_trust_liability, "
        "  (SELECT COUNT(*) FROM ediscovery_holds WHERE status = 'active') AS active_holds, "
        "  (SELECT COUNT(*) FROM ediscovery_documents "
        "     WHERE review_status IN ('unreviewed','in_review')) AS documents_under_review, "
        "  (SELECT COALESCE(SUM(max_award), 0) FROM grant_opportunities "
        "     WHERE status NOT IN ('awarded','declined','withdrawn')) "
        "  AS active_grant_pipeline_value"
    )[0]

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in ("enterprise_kpi", "enterprise_kpi_ratios",
                               "enterprise_kpi_alert", "enterprise_kpi_tieout") \
                and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    g.push("ops_reports", {
        "report": "enterprise_kpi", "batch_code": batch,
        "open_matters": int(kpi["open_matters"]),
        "total_wip_value": kpi["total_wip_value"],
        "total_open_ar": kpi["total_open_ar"],
        "total_trust_liability": kpi["total_trust_liability"],
        "active_holds": int(kpi["active_holds"]),
        "documents_under_review": int(kpi["documents_under_review"]),
        "active_grant_pipeline_value": kpi["active_grant_pipeline_value"],
    })

    # ---- STAGE 2: ratios from the pushed pack row (read back via SQL) ------
    pack = g.sql(
        "SELECT total_wip_value, total_open_ar, total_trust_liability FROM ops_reports "
        "WHERE report = 'enterprise_kpi' AND batch_code = '" + batch + "'"
    )[0]
    wip = pack["total_wip_value"] or 0
    ar = pack["total_open_ar"] or 0
    trust = pack["total_trust_liability"] or 0
    g.push("ops_reports", {
        "report": "enterprise_kpi_ratios", "batch_code": batch,
        "ar_to_wip_ratio": round(ar / wip, 2) if wip else None,
        "trust_coverage_ratio": round(trust / ar, 2) if ar else None,
    })

    # ---- STAGE 3: alerts where a ratio breaches its episode threshold ------
    ratios = g.sql(
        "SELECT ar_to_wip_ratio, trust_coverage_ratio FROM ops_reports "
        "WHERE report = 'enterprise_kpi_ratios' AND batch_code = '" + batch + "'"
    )[0]
    alerts = []
    if ratios["ar_to_wip_ratio"] is not None and ratios["ar_to_wip_ratio"] * 100 > ar_alert:
        alerts.append({"ratio": "ar_to_wip_ratio", "value": ratios["ar_to_wip_ratio"],
                       "threshold": ar_alert, "direction": "above"})
    if ratios["trust_coverage_ratio"] is not None \
            and ratios["trust_coverage_ratio"] * 100 < trust_alert:
        alerts.append({"ratio": "trust_coverage_ratio", "value": ratios["trust_coverage_ratio"],
                       "threshold": trust_alert, "direction": "below"})
    for a in alerts:
        g.push("ops_reports", {"report": "enterprise_kpi_alert", "batch_code": batch, **a})

    # ---- STAGE 4: tie-out re-reading the batch's own rows ------------------
    ratio_row = g.sql(
        "SELECT ar_to_wip_ratio, trust_coverage_ratio FROM ops_reports "
        "WHERE report = 'enterprise_kpi_ratios' AND batch_code = '" + batch + "'"
    )[0]
    ratio_values = {"ar_to_wip_ratio": ratio_row["ar_to_wip_ratio"],
                    "trust_coverage_ratio": ratio_row["trust_coverage_ratio"]}
    live_alerts = [r for r in g.all("ops_reports")
                   if r.get("report") == "enterprise_kpi_alert" and r.get("batch_code") == batch]
    consistent = (len(live_alerts) == len(alerts) and all(
        r.get("ratio") in ratio_values and r.get("value") == ratio_values[r.get("ratio")]
        for r in live_alerts
    ))
    g.push("ops_reports", {
        "report": "enterprise_kpi_tieout", "batch_code": batch,
        "ratios_checked": len(ratio_values),
        "alerts_raised": len(live_alerts),
        "alert_values_consistent": consistent,
        "tie_out_ok": bool(consistent and len(live_alerts) == len(alerts)),
    })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
