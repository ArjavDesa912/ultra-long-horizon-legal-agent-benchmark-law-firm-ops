#!/usr/bin/env python3
"""Gold solution for 016_top_matter_profitability_report (v2). Run against a
FRESH container. Idempotent: safe to run twice.

Stages (dependencies NOT narrated in the instruction; policy KPI-REAL-01
carries the realization basis, SEVERITY-01 the reuse rule):
  1. top-N matter_profitability rows ranked by logged_value (ALL entries by
     amount per the policy basis; division guard when logged is 0), plus one
     billing_anomaly row per matter billed with zero logged time
     (diagnosis category: mistake)
  2. profitability_alert rows for the ranked slice below the episode row's
     realization_alert_threshold, read back from stage 1
  3. one partner-review tasks row per alert, assigned to the supervising
     partner derived from staff_roster (the only active partner)
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    top_n = int(g.nonce(field="top_n"))
    threshold = int(g.nonce(field="realization_alert_threshold"))
    ep = glib.Gold.dp(g.nonce(field="episode_date"))

    matters = {str(m["id"]): m for m in g.all("matters")}
    entries = g.all("time_entries")
    invoices = g.all("invoices")
    roster = g.all("staff_roster")

    # Supervising partner: the only roster row with role=partner active at the
    # episode date (active_from <= ep and active_to null or >= ep).
    active = [r for r in roster
              if r.get("active_from") and r["active_from"][:10] <= ep
              and (r.get("active_to") is None or r.get("active_to")[:10] >= ep)]
    partners = sorted(str(r.get("employee_id")) for r in active if r.get("role") == "partner")
    supervisor = partners[0]

    logged = defaultdict(float)
    for e in entries:
        logged[str(e.get("matter_id"))] += e.get("amount") or 0
    invoiced = defaultdict(float)
    collected = defaultdict(float)
    for inv in invoices:
        if inv.get("status") == "draft":
            continue
        mid = str(inv.get("matter_id"))
        invoiced[mid] += inv.get("total", 0)
        collected[mid] += inv.get("amount_paid", 0)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "matter_profitability", "billing_anomaly", "profitability_alert"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: top-N profitability rows + billing-integrity anomalies
    ranked = sorted(matters.keys(), key=lambda mid: (-logged.get(mid, 0.0), int(mid)))[:top_n]
    for rank, mid in enumerate(ranked, start=1):
        lv = logged.get(mid, 0.0)
        pct = round(100 * collected.get(mid, 0.0) / lv, 1) if lv else 0.0
        g.push("ops_reports", {
            "report": "matter_profitability", "batch_code": batch, "rank": rank,
            "matter_id": mid, "matter_number": matters[mid].get("matter_number"),
            "logged_value": lv, "invoiced_value": invoiced.get(mid, 0.0),
            "collected_value": collected.get(mid, 0.0), "realization_pct": pct,
        })
    # Billing-integrity anomaly: billed with no logged time (division guard).
    for mid, m in matters.items():
        if invoiced.get(mid, 0.0) > 0 and not logged.get(mid, 0.0):
            g.push("ops_reports", {
                "report": "billing_anomaly", "batch_code": batch, "matter_id": mid,
                "matter_number": m.get("matter_number"), "invoiced_value": invoiced[mid],
                "diagnosis": "mistake",
            })

    # ---- STAGE 2: alerts, read back from the stage-1 rows (SEVERITY-01) --
    live_top = [r for r in g.all("ops_reports")
                if r.get("report") == "matter_profitability" and r.get("batch_code") == batch]
    for row in live_top:
        if float(row.get("realization_pct") or 0) < threshold:
            g.push("ops_reports", {
                "report": "profitability_alert", "batch_code": batch,
                "rank": row.get("rank"), "matter_id": row.get("matter_id"),
                "realization_pct": row.get("realization_pct"),
            })

    # ---- STAGE 3: partner-review tasks for the alerts -------------------
    alerts = [r for r in g.all("ops_reports")
              if r.get("report") == "profitability_alert" and r.get("batch_code") == batch]
    for a in alerts:
        mid = str(a.get("matter_id"))
        title = f"Partner review: {matters[mid].get('matter_number')}"
        existing = [t for t in g.all("tasks") if t.get("title") == title]
        payload = {
            "matter_id": matters[mid]["id"], "title": title,
            "assigned_to": supervisor, "status": "open", "priority": "high",
            "description": "Partner review of a low-realization top matter (matter profitability report).",
        }
        if existing:
            g.update("tasks", existing[0]["id"], payload)
        else:
            g.push("tasks", payload)

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
