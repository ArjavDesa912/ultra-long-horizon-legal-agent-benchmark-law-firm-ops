#!/usr/bin/env python3
"""Alternate gold for 021_invoice_aging_dispute_risk_report (v2).

Same end-state as gold.py, materially different path: the live aging is
computed by one SQL date-arithmetic GROUP BY over the live invoices (bucket
membership via CASE on due_date age), and the dispute-risk slice by a SQL
threshold query, instead of REST reads + Python bucketing. Stages 2-3 keep
the same read-back contract.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

OPEN_STATUSES = ("sent", "overdue", "disputed")


def month_ends(episode, n=6):
    out = []
    y, m = episode.year, episode.month
    while len(out) < n:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        me = (datetime(y, m, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        if me.date() < episode:
            out.append(me.date())
    return out


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    threshold = int(g.nonce(field="dispute_risk_threshold"))

    # SQL-first: live bucket membership straight from the database.
    rows = g.sql(
        f"SELECT CASE WHEN (DATE '{ep}' - due_date::date) <= 0 THEN 'current' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 30 THEN '1-30' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 60 THEN '31-60' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 90 THEN '61-90' "
        f"ELSE '90+' END AS bucket, "
        f"COUNT(*) AS cnt, COALESCE(SUM(total - amount_paid), 0) AS unpaid "
        f"FROM invoices WHERE status IN ('sent','overdue','disputed') "
        f"AND (total - amount_paid) > 0 AND due_date::date IS NOT NULL "
        f"GROUP BY 1"
    )
    bucket_count = {r["bucket"]: int(r["cnt"]) for r in rows}
    bucket_total = {r["bucket"]: float(r["unpaid"]) for r in rows}

    dispute_risks = []
    for inv in g.all("invoices"):
        if inv.get("status") != "disputed":
            continue
        unpaid = (inv.get("total", 0) or 0) - (inv.get("amount_paid", 0) or 0)
        if unpaid > threshold:
            dispute_risks.append((inv, unpaid))

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "ar_aging", "dispute_risk", "ar_concentration", "ar_aging_history"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
    for b in sorted(bucket_count):
        g.push("ops_reports", {
            "report": "ar_aging", "batch_code": batch, "bucket": b,
            "invoice_count": bucket_count[b], "unpaid_total": bucket_total[b],
        })
    for inv, unpaid in dispute_risks:
        g.push("ops_reports", {
            "report": "dispute_risk", "batch_code": batch, "invoice_id": inv["id"],
            "invoice_number": inv.get("invoice_number"), "matter_id": inv.get("matter_id"),
            "client_id": inv.get("client_id"), "unpaid_balance": unpaid,
        })

    # ---- STAGE 2 (read back) --------------------------------------------
    live_buckets = [r for r in g.all("ops_reports")
                    if r.get("report") == "ar_aging" and r.get("batch_code") == batch]
    total_unpaid = sum(float(r.get("unpaid_total") or 0) for r in live_buckets)
    plus90 = next((float(r.get("unpaid_total") or 0) for r in live_buckets if r.get("bucket") == "90+"), 0.0)
    pct = round(100.0 * plus90 / total_unpaid, 1) if total_unpaid else 0.0
    g.push("ops_reports", {
        "report": "ar_concentration", "batch_code": batch, "pct_in_90_plus": pct,
    })

    # ---- STAGE 3 (SQL per close date) ------------------------------------
    for me in month_ends(ep_dt):
        me_s = me.isoformat()
        rows = g.sql(
            f"SELECT CASE WHEN (DATE '{me_s}' - due_date::date) <= 0 THEN 'current' "
            f"WHEN (DATE '{me_s}' - due_date::date) <= 30 THEN '1-30' "
            f"WHEN (DATE '{me_s}' - due_date::date) <= 60 THEN '31-60' "
            f"WHEN (DATE '{me_s}' - due_date::date) <= 90 THEN '61-90' "
            f"ELSE '90+' END AS bucket, "
            f"COUNT(*) AS cnt, COALESCE(SUM(total - amount_paid), 0) AS unpaid "
            f"FROM invoices WHERE status IN ('sent','overdue','disputed') "
            f"AND issued_date::date <= DATE '{me_s}' AND (total - amount_paid) > 0 "
            f"AND due_date::date IS NOT NULL GROUP BY 1"
        )
        for r in sorted(rows, key=lambda x: x["bucket"]):
            g.push("ops_reports", {
                "report": "ar_aging_history", "batch_code": batch, "as_of": me_s,
                "bucket": r["bucket"], "invoice_count": int(r["cnt"]),
                "unpaid_total": float(r["unpaid"]),
            })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
