#!/usr/bin/env python3
"""Gold solution for 019_billing_disruption_window_detection (v2). Run
against a FRESH container. Idempotent: safe to run twice.

The detection procedure comes from firm policy DISRUPT-01 as in force at the
episode date: two effective-dated versions exist; the live one (effective
2026-01-01, superseding the 2024 version) takes the severity multiplier from
the episode row's severity_multiplier_tenths (divided by 10) and computes the
median over considered buckets only. Buckets below the episode row's
min_bucket_size invoices are not considered at all.
"""
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

BAD_STATUSES = ("overdue", "disputed", "written_off")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    multiplier = int(g.nonce(field="severity_multiplier_tenths")) / 10.0
    min_bucket_size = int(g.nonce(field="min_bucket_size"))

    invoices = g.all("invoices")
    bucket_total = {}
    bucket_bad = {}
    for inv in invoices:
        issued = glib.Gold.dp(inv.get("issued_date"))
        if not issued:
            continue
        issued_dt = datetime.strptime(issued, "%Y-%m-%d").date()
        days_ago = (ep_dt - issued_dt).days
        if days_ago < 0:
            continue  # invoices issued after the episode date are excluded
        b = days_ago // 30
        bucket_total[b] = bucket_total.get(b, 0) + 1
        if inv.get("status") in BAD_STATUSES:
            bucket_bad[b] = bucket_bad.get(b, 0) + 1

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

    # ---- STAGE 1: disrupted buckets + summary ----------------------------
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

    # ---- STAGE 2: severity multiples, read back from stage 1 -------------
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
