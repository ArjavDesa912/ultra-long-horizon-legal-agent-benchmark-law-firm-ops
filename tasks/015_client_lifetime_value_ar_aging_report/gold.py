#!/usr/bin/env python3
"""Gold solution for 015_client_lifetime_value_ar_aging_report (v2). Run
against a FRESH container. Idempotent: safe to run twice.

Stages (dependencies NOT narrated in the instruction; policy KPI-CLIENT-01
carries the definitions, SEVERITY-01 the reuse rule):
  1. per-client client_value rows + FIRM rollup (trust_balance reported
     separately as a liability, never folded into lifetime value;
     dangling-client invoices resolve to no client and are excluded)
  2. top-N by open AR, ranked from the stage-1 rows (episode knob top_n)
  3. point-in-time open AR at the four calendar quarter-ends preceding the
     episode date (open-invoice basis, invoices existing at each close date)
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

OPEN_STATUSES = ("sent", "overdue", "disputed")


def quarter_ends(episode, n=4):
    """The n calendar quarter-ends immediately preceding the episode date."""
    out = []
    y, m = episode.year, episode.month
    while len(out) < n:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        if m % 3 == 0:  # quarter-end months: Mar/Jun/Sep/Dec
            qe = (datetime(y, m, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            if qe.date() < episode:
                out.append(qe.date())
    return sorted(set(out))[-n:]


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(g.nonce(field="top_n"))

    clients = g.all("clients")
    invoices = g.all("invoices")
    client_ids = {str(c["id"]) for c in clients}
    client_number = {str(c["id"]): c.get("client_number") for c in clients}
    trust = {str(c["id"]): c.get("trust_balance") for c in clients}

    by_client = defaultdict(list)
    for inv in invoices:
        cid = str(inv.get("client_id"))
        if cid in client_ids:  # dangling-client invoices resolve to no client
            by_client[cid].append(inv)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("client_value", "high_ar_client", "ar_history"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: per-client rows + FIRM rollup -------------------------
    lifetime = {}
    open_ar = {}
    counts = {}
    firm_value = firm_ar = firm_count = 0
    for c in clients:
        cid = str(c["id"])
        invs = by_client.get(cid, [])
        value = sum(i.get("amount_paid", 0) for i in invs if i.get("status") != "draft")
        ar = sum(i.get("total", 0) - i.get("amount_paid", 0) for i in invs if i.get("status") in OPEN_STATUSES)
        count = sum(1 for i in invs if i.get("status") != "draft")
        lifetime[cid] = value
        open_ar[cid] = ar
        counts[cid] = count
        firm_value += value
        firm_ar += ar
        firm_count += count
        g.push("ops_reports", {
            "report": "client_value", "batch_code": batch, "client_id": cid,
            "client_number": client_number[cid], "lifetime_value": value, "open_ar": ar,
            "trust_balance": trust[cid], "invoice_count": count,
        })
    g.push("ops_reports", {
        "report": "client_value", "batch_code": batch, "client_id": "FIRM", "client_number": "FIRM",
        "lifetime_value": firm_value, "open_ar": firm_ar, "trust_balance": None,
        "invoice_count": firm_count,
    })

    # ---- STAGE 2: top-N by open AR, ranked from the stage-1 rows --------
    ranked = sorted(open_ar.keys(), key=lambda cid: (-open_ar[cid], int(cid)))[:top_n]
    for rank, cid in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "high_ar_client", "batch_code": batch, "rank": rank,
            "client_id": cid, "open_ar": open_ar[cid], "lifetime_value": lifetime[cid],
        })

    # ---- STAGE 3: point-in-time open AR at the four close dates ---------
    for qe in quarter_ends(ep_dt):
        qe_s = qe.isoformat()
        rows = [i for i in invoices
                if i.get("status") in OPEN_STATUSES
                and glib.Gold.dp(i.get("issued_date")) is not None
                and glib.Gold.dp(i.get("issued_date")) <= qe_s
                and (i.get("total", 0) - i.get("amount_paid", 0)) > 0]
        total = sum(i.get("total", 0) - i.get("amount_paid", 0) for i in rows)
        g.push("ops_reports", {
            "report": "ar_history", "batch_code": batch, "as_of": qe_s,
            "open_ar": total, "open_invoices": len(rows),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
