#!/usr/bin/env python3
"""Gold solution for 021_invoice_aging_dispute_risk_report (v2). Run against
a FRESH container. Idempotent: safe to run twice.

Open AR per firm policy KPI-CLIENT-01: invoices with status in (sent,
overdue, disputed) and positive unpaid balance. Disputed invoices age inside
their due-date bucket AND get risk-flagged when their unpaid balance exceeds
the episode row's dispute_risk_threshold. Stage 3 reconstructs the aging at
the six calendar month-ends immediately preceding the episode month.
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

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


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    threshold = int(g.nonce(field="dispute_risk_threshold"))

    invoices = g.all("invoices")
    bucket_count = {}
    bucket_total = {}
    dispute_risks = []
    for inv in invoices:
        if inv.get("status") not in ("sent", "overdue", "disputed"):
            continue
        unpaid = (inv.get("total", 0) or 0) - (inv.get("amount_paid", 0) or 0)
        if unpaid <= 0:
            continue
        due = glib.Gold.dp(inv.get("due_date"))
        due_dt = datetime.strptime(due, "%Y-%m-%d").date()
        age = (ep_dt - due_dt).days
        b = bucket_for(age)
        bucket_count[b] = bucket_count.get(b, 0) + 1
        bucket_total[b] = bucket_total.get(b, 0.0) + unpaid
        if inv.get("status") == "disputed" and unpaid > threshold:
            dispute_risks.append((inv, unpaid))

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "ar_aging", "dispute_risk", "ar_concentration", "ar_aging_history"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: aging buckets + dispute-risk flags ---------------------
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

    # ---- STAGE 2: concentration, read back from the bucket rows ----------
    live_buckets = [r for r in g.all("ops_reports")
                    if r.get("report") == "ar_aging" and r.get("batch_code") == batch]
    total_unpaid = sum(float(r.get("unpaid_total") or 0) for r in live_buckets)
    plus90 = next((float(r.get("unpaid_total") or 0) for r in live_buckets if r.get("bucket") == "90+"), 0.0)
    pct = round(100.0 * plus90 / total_unpaid, 1) if total_unpaid else 0.0
    g.push("ops_reports", {
        "report": "ar_concentration", "batch_code": batch, "pct_in_90_plus": pct,
    })

    # ---- STAGE 3: point-in-time aging at the six month-ends --------------
    for me in month_ends(ep_dt):
        me_s = me.isoformat()
        bucket_counts = {}
        bucket_totals = {}
        for inv in invoices:
            if inv.get("status") not in ("sent", "overdue", "disputed"):
                continue
            issued = glib.Gold.dp(inv.get("issued_date"))
            if issued is None or issued > me_s:
                continue  # an invoice issued after a close date must not appear in it
            unpaid = (inv.get("total", 0) or 0) - (inv.get("amount_paid", 0) or 0)
            if unpaid <= 0:
                continue
            due = glib.Gold.dp(inv.get("due_date"))
            due_dt = datetime.strptime(due, "%Y-%m-%d").date()
            age = (me - due_dt).days
            b = bucket_for(age)
            bucket_counts[b] = bucket_counts.get(b, 0) + 1
            bucket_totals[b] = bucket_totals.get(b, 0.0) + unpaid
        for b in sorted(bucket_counts):
            g.push("ops_reports", {
                "report": "ar_aging_history", "batch_code": batch, "as_of": me_s,
                "bucket": b, "invoice_count": bucket_counts[b],
                "unpaid_total": bucket_totals[b],
            })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
