#!/usr/bin/env python3
"""Verifier for 021_invoice_aging_dispute_risk_report (v2).

Dual-path on every derived number: aging buckets and dispute-risk flags are
recomputed from the live invoices in Python AND via SQL date arithmetic; both
paths must agree with each other and with the rows the agent wrote. Hazard
coverage: excluding disputed invoices from the aging, flagging sub-threshold
disputes, computing concentration from counts instead of dollars, and
month-end rows that include invoices issued after the close date all FAIL
here. Idempotent across repeated gold runs; fails closed via vlib.run.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

OPEN_STATUSES = ("sent", "overdue", "disputed")


def bucket_for(age):
    if age <= 0:
        return "current"
    if age <= 30:
        return "1-30"
    if age <= 60:
        return "31-60"
    if age <= 90:
        return "61-90"
    return "90+"


def month_ends(episode, n=6):
    """The n calendar month-ends immediately preceding the episode month."""
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


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    threshold = int(vlib.get_nonce(v.token, field="dispute_risk_threshold"))

    invoices = vlib.fetch_all(v.token, "invoices")

    # ------------------------------------------------------ path 1: Python
    py_count = {}
    py_total = {}
    py_risks = []
    for inv in invoices:
        if inv.get("status") not in OPEN_STATUSES:
            continue
        unpaid = (inv.get("total") or 0) - (inv.get("amount_paid") or 0)
        if unpaid <= 0:
            continue
        due = vlib.dp(inv.get("due_date"))
        if not due:
            continue
        age = (ep_dt - datetime.strptime(due, "%Y-%m-%d").date()).days
        b = bucket_for(age)
        py_count[b] = py_count.get(b, 0) + 1
        py_total[b] = py_total.get(b, 0.0) + unpaid
        if inv.get("status") == "disputed" and unpaid > threshold:
            py_risks.append((inv, unpaid))

    # ------------------------------------------------------ path 2: SQL
    sql_rows = vlib.sql(
        v.token,
        f"SELECT CASE WHEN (DATE '{ep}' - due_date::date) <= 0 THEN 'current' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 30 THEN '1-30' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 60 THEN '31-60' "
        f"WHEN (DATE '{ep}' - due_date::date) <= 90 THEN '61-90' "
        f"ELSE '90+' END AS bucket, "
        f"COUNT(*) AS cnt, COALESCE(SUM(total - amount_paid), 0) AS unpaid "
        f"FROM invoices WHERE status IN ('sent','overdue','disputed') "
        f"AND (total - amount_paid) > 0 AND due_date::date IS NOT NULL "
        f"GROUP BY 1",
    )
    sql_count = {r["bucket"]: int(r["cnt"]) for r in sql_rows}
    sql_total = {r["bucket"]: float(r["unpaid"]) for r in sql_rows}
    v.expect_equal(len(sql_count), len(py_count), "dual-path: non-empty bucket count")
    for b in py_count:
        v.expect_equal(sql_count.get(b), py_count[b], f"dual-path: bucket {b} invoice_count")
        v.expect_cents(sql_total.get(b, 0.0), py_total[b], f"dual-path: bucket {b} unpaid_total")
    sql_risk_rows = vlib.sql(
        v.token,
        f"SELECT id, invoice_number, matter_id, client_id, (total - amount_paid) AS unpaid "
        f"FROM invoices WHERE status = 'disputed' AND (total - amount_paid) > {threshold}",
    )
    v.expect_equal(len(sql_risk_rows), len(py_risks), "dual-path: dispute_risk count")

    # ------------------------------------------------------ stage 1: rows
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "ar_aging" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(py_count), "ar_aging row count")
    by_bucket = {r.get("bucket"): r for r in rows}
    for b, cnt in py_count.items():
        row = by_bucket.get(b)
        v.expect(row is not None, f"missing ar_aging bucket {b}")
        v.expect_equal(row.get("invoice_count"), cnt, f"bucket {b} invoice_count")
        v.expect_cents(row.get("unpaid_total"), py_total[b], f"bucket {b} unpaid_total")
    for b in by_bucket:
        v.expect(b in py_count, f"ar_aging row for empty bucket {b}")

    risk_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "dispute_risk" and r.get("batch_code") == batch]
    v.expect_equal(len(risk_rows), len(py_risks), "dispute_risk row count")
    by_invoice = {str(r.get("invoice_id")): r for r in risk_rows}
    for inv, unpaid in py_risks:
        row = by_invoice.get(str(inv["id"]))
        v.expect(row is not None, f"missing dispute_risk row for invoice {inv['id']}")
        v.expect_equal(row.get("invoice_number"), inv.get("invoice_number"),
                       f"invoice {inv['id']} invoice_number")
        v.expect_cents(row.get("unpaid_balance"), unpaid, f"invoice {inv['id']} unpaid_balance")
    # sub-threshold disputes must NOT be flagged
    for inv in invoices:
        if inv.get("status") != "disputed":
            continue
        unpaid = (inv.get("total") or 0) - (inv.get("amount_paid") or 0)
        if unpaid <= threshold:
            v.expect(str(inv["id"]) not in by_invoice,
                     f"invoice {inv['id']} flagged below the dispute threshold")

    # ------------------------------------------------ stage 2: concentration
    total_unpaid = sum(py_total.values())
    plus90 = py_total.get("90+", 0.0)
    exp_pct = round(100.0 * plus90 / total_unpaid, 1) if total_unpaid else 0.0
    conc = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "ar_concentration" and r.get("batch_code") == batch]
    v.expect_equal(len(conc), 1, "ar_concentration row count")
    actual_pct = conc[0].get("pct_in_90_plus")
    v.expect(isinstance(actual_pct, (int, float)) and abs(float(actual_pct) - exp_pct) <= 0.05,
             "ar_concentration pct_in_90_plus")
    # SEVERITY-01: the percentage must reuse the stage-1 bucket rows' own totals
    live_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "ar_aging" and r.get("batch_code") == batch]
    live_total = sum(float(r.get("unpaid_total") or 0) for r in live_rows)
    live_90 = next((float(r.get("unpaid_total") or 0) for r in live_rows if r.get("bucket") == "90+"), 0.0)
    live_pct = round(100.0 * live_90 / live_total, 1) if live_total else 0.0
    v.expect(abs(float(actual_pct) - live_pct) <= 0.05,
             "ar_concentration reuses stage-1 bucket totals")

    # ------------------------------------------- stage 3: month-end history
    expected_mes = [m.isoformat() for m in month_ends(ep_dt)]
    hist = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "ar_aging_history" and r.get("batch_code") == batch]
    exp_cnt = 0
    hist_by_key = {(vlib.dp(r.get("as_of")), r.get("bucket")): r for r in hist}
    for me in expected_mes:
        exp_count = {}
        exp_totals = {}
        for inv in invoices:
            if inv.get("status") not in OPEN_STATUSES:
                continue
            issued = vlib.dp(inv.get("issued_date"))
            if issued is None or issued > me:
                continue  # interior-month: an invoice issued after a close date must not appear
            unpaid = (inv.get("total") or 0) - (inv.get("amount_paid") or 0)
            if unpaid <= 0:
                continue
            due = vlib.dp(inv.get("due_date"))
            if due is None:
                continue
            age = (datetime.strptime(me, "%Y-%m-%d").date()
                   - datetime.strptime(due, "%Y-%m-%d").date()).days
            b = bucket_for(age)
            exp_count[b] = exp_count.get(b, 0) + 1
            exp_totals[b] = exp_totals.get(b, 0.0) + unpaid
        for b in exp_count:
            exp_cnt += 1
            row = hist_by_key.get((me, b))
            v.expect(row is not None, f"missing ar_aging_history row for {me} bucket {b}")
            v.expect_equal(row.get("invoice_count"), exp_count[b], f"{me} bucket {b} invoice_count")
            v.expect_cents(row.get("unpaid_total"), exp_totals[b], f"{me} bucket {b} unpaid_total")
    v.expect_equal(len(hist), exp_cnt, "ar_aging_history row count")
    for key in hist_by_key:
        v.expect(key[0] in expected_mes, f"ar_aging_history row for unexpected close date {key[0]}")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
