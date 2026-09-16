#!/usr/bin/env python3
"""Verifier for 019_billing_disruption_window_detection (v2).

Dual-path on every derived number: buckets are recomputed from the live
invoices in Python AND via a SQL date-arithmetic GROUP BY; both paths must
agree with each other and with the rows the agent wrote. Hazard coverage:
applying the superseded DISRUPT-01 version (or the live multiplier without
dividing by 10), ignoring the episode row's min_bucket_size guard, or
recomputing severity instead of reading back the stage-1 rows all FAIL here.
Expectations are recomputed from the live + seed snapshot, so the verifier is
idempotent across repeated gold runs and fails closed via vlib.run.
"""
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

BAD_STATUSES = ("overdue", "disputed", "written_off")


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    multiplier = int(vlib.get_nonce(v.token, field="severity_multiplier_tenths")) / 10.0
    min_bucket_size = int(vlib.get_nonce(v.token, field="min_bucket_size"))

    invoices = vlib.fetch_all(v.token, "invoices")

    # ------------------------------------------------------ path 1: Python
    bucket_total = {}
    bucket_bad = {}
    for inv in invoices:
        issued = vlib.dp(inv.get("issued_date"))
        if not issued:
            continue
        days_ago = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days_ago < 0:
            continue
        b = days_ago // 30
        bucket_total[b] = bucket_total.get(b, 0) + 1
        if inv.get("status") in BAD_STATUSES:
            bucket_bad[b] = bucket_bad.get(b, 0) + 1

    considered = {b: t for b, t in bucket_total.items() if t >= min_bucket_size}
    bad_rates = {b: 100.0 * bucket_bad.get(b, 0) / t for b, t in considered.items()}
    median = statistics.median(bad_rates.values()) if bad_rates else 0.0
    disrupted = {b: rate for b, rate in bad_rates.items() if rate > multiplier * median}

    # ------------------------------------------------------ path 2: SQL
    sql_rows = vlib.sql(
        v.token,
        f"SELECT (DATE '{ep}' - issued_date::date) / 30 AS bucket, COUNT(*) AS total, "
        f"COALESCE(SUM(CASE WHEN status IN ('overdue','disputed','written_off') THEN 1 ELSE 0 END), 0) AS bad "
        f"FROM invoices WHERE issued_date::date IS NOT NULL AND issued_date::date <= DATE '{ep}' "
        f"GROUP BY bucket",
    )
    sql_total = {int(r["bucket"]): int(r["total"]) for r in sql_rows}
    sql_bad = {int(r["bucket"]): int(r["bad"]) for r in sql_rows}
    v.expect_equal(len(sql_total), len(bucket_total), "dual-path: considered bucket universe")
    for b, total in bucket_total.items():
        v.expect_equal(sql_total.get(b), total, f"dual-path: bucket {b} total")
        v.expect_equal(sql_bad.get(b, 0), bucket_bad.get(b, 0), f"dual-path: bucket {b} bad count")

    # ------------------------------------------------------ stage 1: rows
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "billing_disruption" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(disrupted), "billing_disruption row count")
    by_bucket = {r.get("bucket_index"): r for r in rows}
    for b, rate in disrupted.items():
        row = by_bucket.get(b)
        v.expect(row is not None, f"missing billing_disruption row for bucket {b}")
        v.expect_equal(row.get("age_range_days"), [30 * b, 30 * b + 29], f"bucket {b} age_range_days")
        v.expect_equal(row.get("invoice_count"), bucket_total[b], f"bucket {b} invoice_count")
        actual = row.get("bad_rate")
        v.expect(isinstance(actual, (int, float)) and abs(float(actual) - round(rate, 1)) <= 0.05,
                 f"bucket {b} bad_rate")
    for b in by_bucket:
        v.expect(b in disrupted, f"bucket {b} flagged but not disrupted under the live policy")

    summary = [r for r in vlib.fetch_all(v.token, "ops_reports")
               if r.get("report") == "billing_disruption_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary), 1, "billing_disruption_summary row count")
    srow = summary[0]
    actual_median = srow.get("median_bad_rate")
    v.expect(isinstance(actual_median, (int, float)) and abs(float(actual_median) - round(median, 1)) <= 0.05,
             "median_bad_rate")
    v.expect_equal(srow.get("disrupted_bucket_count"), len(disrupted), "disrupted_bucket_count")
    v.expect_equal(srow.get("total_buckets"), len(considered), "total_buckets")

    # --------------------------------------------- stage 2: severity multiples
    sev_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                if r.get("report") == "disruption_severity" and r.get("batch_code") == batch]
    v.expect_equal(len(sev_rows), len(rows), "disruption_severity row count")
    sev_by_bucket = {r.get("bucket_index"): r for r in sev_rows}
    live_median = float(srow.get("median_bad_rate") or 0)
    for row in rows:
        b = row.get("bucket_index")
        rate = float(row.get("bad_rate") or 0)
        expected_sev = round(rate / live_median, 2) if live_median else 999.0
        srow2 = sev_by_bucket.get(b)
        v.expect(srow2 is not None, f"missing disruption_severity row for bucket {b}")
        actual_sev = srow2.get("severity_multiple")
        v.expect(isinstance(actual_sev, (int, float)) and abs(float(actual_sev) - expected_sev) <= 0.05,
                 f"bucket {b} severity_multiple")
        # SEVERITY-01: the multiple must derive from the stage-1 row's own
        # bad_rate and the summary row's own median (both read back live) —
        # expected_sev above is computed from exactly those live values.

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
