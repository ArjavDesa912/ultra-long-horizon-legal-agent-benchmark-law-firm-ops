#!/usr/bin/env python3
"""Alternate gold for 019_billing_disruption_window_detection (v2).

Same end-state as gold.py, materially different path: the bucketing is done
by one SQL date-arithmetic GROUP BY over the live invoices instead of REST
reads + Python bucketing. Same policy application, same report contract.
"""
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

BAD_STATUSES = ("overdue", "disputed", "written_off")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    multiplier = int(g.nonce(field="severity_multiplier_tenths")) / 10.0
    min_bucket_size = int(g.nonce(field="min_bucket_size"))

    rows = g.sql(
        f"SELECT (DATE '{ep}' - issued_date::date) / 30 AS bucket, COUNT(*) AS total, "
        f"COALESCE(SUM(CASE WHEN status IN ('overdue','disputed','written_off') THEN 1 ELSE 0 END), 0) AS bad "
        f"FROM invoices WHERE issued_date::date IS NOT NULL AND issued_date::date <= DATE '{ep}' "
        f"GROUP BY bucket"
    )
    bucket_total = {int(r["bucket"]): int(r["total"]) for r in rows}
    bucket_bad = {int(r["bucket"]): int(r["bad"]) for r in rows}

    considered = {b: t for b, t in bucket_total.items() if t >= min_bucket_size}
    bad_rates = {b: 100.0 * bucket_bad.get(b, 0) / t for b, t in considered.items()}
    median = statistics.median(bad_rates.values()) if bad_rates else 0.0
    disrupted = {b: rate for b, rate in bad_rates.items() if rate > multiplier * median}

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "billing_disruption", "billing_disruption_summary", "disruption_severity"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
    for b, rate in sorted(disrupted.items()):
        g.push("ops_reports", {
            "report": "billing_disruption", "batch_code": batch, "bucket_index": b,
            "age_range_days": [30 * b, 30 * b + 29],
            "invoice_count": bucket_total[b], "bad_rate": round(rate, 1),
        })
    g.push("ops_reports", {
        "report": "billing_disruption_summary", "batch_code": batch,
        "median_bad_rate": round(median, 1), "disrupted_bucket_count": len(disrupted),
        "total_buckets": len(considered),
    })

    # ---- STAGE 2 (read back) --------------------------------------------
    live_buckets = [r for r in g.all("ops_reports")
                    if r.get("report") == "billing_disruption" and r.get("batch_code") == batch]
    live_summary = next(r for r in g.all("ops_reports")
                        if r.get("report") == "billing_disruption_summary" and r.get("batch_code") == batch)
    live_median = float(live_summary.get("median_bad_rate") or 0)
    for row in live_buckets:
        rate = float(row.get("bad_rate") or 0)
        severity = round(rate / live_median, 2) if live_median else 999.0
        g.push("ops_reports", {
            "report": "disruption_severity", "batch_code": batch,
            "bucket_index": row.get("bucket_index"), "severity_multiple": severity,
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
