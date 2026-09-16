#!/usr/bin/env python3
"""Gold solution for 022_firmwide_kpi_pack (v2). Run against a FRESH container.
Idempotent: this batch's ops_reports rows are deleted and rewritten, so a
second run leaves identical state. Every KPI definition is applied exactly as
firm policy KPI-PACK-01 states it; the alert thresholds come from the episode
row."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("enterprise_kpi", "enterprise_kpi_ratios", "enterprise_kpi_alert", "enterprise_kpi_tieout")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ar_alert = int(g.nonce(field="ar_alert_ratio"))
    trust_alert = int(g.nonce(field="trust_alert_ratio"))

    # ---- STAGE 1: the pack row (each KPI spans a different collection) -----
    matters = g.all("matters")
    entries = g.all("time_entries")
    invoices = g.all("invoices")
    clients = g.all("clients")
    holds = g.all("ediscovery_holds")
    documents = g.all("ediscovery_documents")
    opportunities = g.all("grant_opportunities")

    open_matters = sum(1 for m in matters if m.get("status") == "open")
    total_wip_value = sum(e.get("amount", 0) or 0 for e in entries
                          if e.get("status") in ("draft", "submitted", "approved"))
    total_open_ar = sum((i.get("total", 0) or 0) - (i.get("amount_paid", 0) or 0) for i in invoices
                        if i.get("status") in ("sent", "overdue", "disputed")
                        and (i.get("total", 0) or 0) - (i.get("amount_paid", 0) or 0) > 0)
    total_trust_liability = sum(c.get("trust_balance", 0) or 0 for c in clients)
    active_holds = sum(1 for h in holds if h.get("status") == "active")
    documents_under_review = sum(1 for d in documents
                                 if d.get("review_status") in ("unreviewed", "in_review"))
    active_grant_pipeline_value = sum(o.get("max_award", 0) or 0 for o in opportunities
                                      if o.get("status") not in ("awarded", "declined", "withdrawn"))

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []  # ops_reports not created yet on a fresh episode
    for r in existing:
        if r.get("report") in REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    g.push("ops_reports", {
        "report": "enterprise_kpi", "batch_code": batch,
        "open_matters": open_matters, "total_wip_value": total_wip_value,
        "total_open_ar": total_open_ar, "total_trust_liability": total_trust_liability,
        "active_holds": active_holds, "documents_under_review": documents_under_review,
        "active_grant_pipeline_value": active_grant_pipeline_value,
    })

    # ---- STAGE 2: ratios, computed from the pack row read BACK -------------
    # (a wrong stage-1 number silently produces a wrong ratio here)
    kpi_row = next(r for r in g.all("ops_reports")
                   if r.get("report") == "enterprise_kpi" and r.get("batch_code") == batch)
    wip = kpi_row.get("total_wip_value") or 0
    ar = kpi_row.get("total_open_ar") or 0
    trust = kpi_row.get("total_trust_liability") or 0
    ar_to_wip = round(ar / wip, 2) if wip else None
    trust_coverage = round(trust / ar, 2) if ar else None
    g.push("ops_reports", {
        "report": "enterprise_kpi_ratios", "batch_code": batch,
        "ar_to_wip_ratio": ar_to_wip, "trust_coverage_ratio": trust_coverage,
    })

    # ---- STAGE 3: alerts where a ratio breaches its episode threshold ------
    alerts = []
    if ar_to_wip is not None and ar_to_wip * 100 > ar_alert:
        alerts.append({"ratio": "ar_to_wip_ratio", "value": ar_to_wip,
                       "threshold": ar_alert, "direction": "above"})
    if trust_coverage is not None and trust_coverage * 100 < trust_alert:
        alerts.append({"ratio": "trust_coverage_ratio", "value": trust_coverage,
                       "threshold": trust_alert, "direction": "below"})
    for a in alerts:
        g.push("ops_reports", {"report": "enterprise_kpi_alert", "batch_code": batch, **a})

    # ---- STAGE 4: tie-out re-reading this batch's own rows -----------------
    ratio_row = next(r for r in g.all("ops_reports")
                     if r.get("report") == "enterprise_kpi_ratios" and r.get("batch_code") == batch)
    ratio_values = {"ar_to_wip_ratio": ratio_row.get("ar_to_wip_ratio"),
                    "trust_coverage_ratio": ratio_row.get("trust_coverage_ratio")}
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

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
