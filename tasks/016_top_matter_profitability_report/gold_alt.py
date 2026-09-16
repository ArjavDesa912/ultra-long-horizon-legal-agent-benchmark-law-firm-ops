#!/usr/bin/env python3
"""Alternate gold for 016_top_matter_profitability_report (v2).

Same end-state as gold.py, materially different path: the per-matter
logged / invoiced / collected aggregates come from SQL GROUP BY queries
(g.sql) instead of REST reads + Python defaultdicts; only the ranking,
alerting and task writes stay in Python. Idempotent.
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
    roster = g.all("staff_roster")
    active = [r for r in roster
              if r.get("active_from") and r["active_from"][:10] <= ep
              and (r.get("active_to") is None or r.get("active_to")[:10] >= ep)]
    partners = sorted(str(r["employee_id"]) for r in active if r.get("role") == "partner")
    supervisor = partners[0]

    logged = {str(r["mid"]): float(r["val"] or 0) for r in g.sql(
        "SELECT matter_id AS mid, COALESCE(SUM(amount), 0) AS val FROM time_entries GROUP BY matter_id")}
    inv_rows = g.sql(
        "SELECT matter_id AS mid, COALESCE(SUM(total), 0) AS inv, "
        "COALESCE(SUM(amount_paid), 0) AS col FROM invoices "
        "WHERE status <> 'draft' GROUP BY matter_id")
    invoiced = {str(r["mid"]): float(r["inv"]) for r in inv_rows}
    collected = {str(r["mid"]): float(r["col"]) for r in inv_rows}

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "matter_profitability", "billing_anomaly", "profitability_alert"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
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
    for mid, m in matters.items():
        if invoiced.get(mid, 0.0) > 0 and not logged.get(mid, 0.0):
            g.push("ops_reports", {
                "report": "billing_anomaly", "batch_code": batch, "matter_id": mid,
                "matter_number": m.get("matter_number"), "invoiced_value": invoiced[mid],
                "diagnosis": "mistake",
            })

    # ---- STAGE 2 (read back) --------------------------------------------
    live_top = [r for r in g.all("ops_reports")
                if r.get("report") == "matter_profitability" and r.get("batch_code") == batch]
    for row in live_top:
        if float(row.get("realization_pct") or 0) < threshold:
            g.push("ops_reports", {
                "report": "profitability_alert", "batch_code": batch,
                "rank": row.get("rank"), "matter_id": row.get("matter_id"),
                "realization_pct": row.get("realization_pct"),
            })

    # ---- STAGE 3 ---------------------------------------------------------
    alerts = [r for r in g.all("ops_reports")
              if r.get("report") == "profitability_alert" and r.get("batch_code") == batch]
    for a in alerts:
        mid = str(a.get("matter_id"))
        title = f"Partner review: {matters[mid].get('matter_number')}"
        payload = {
            "matter_id": matters[mid]["id"], "title": title,
            "assigned_to": supervisor, "status": "open", "priority": "high",
            "description": "Partner review of a low-realization top matter (matter profitability report).",
        }
        existing_task = next((t for t in g.all("tasks") if t.get("title") == title), None)
        if existing_task:
            g.update("tasks", existing_task["id"], payload)
        else:
            g.push("tasks", payload)

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
