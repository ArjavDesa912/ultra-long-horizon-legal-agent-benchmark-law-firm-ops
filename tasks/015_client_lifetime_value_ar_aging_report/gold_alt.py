#!/usr/bin/env python3
"""Alternate gold solution for 015_client_lifetime_value_ar_aging_report (v2).

Materially different path from gold.py: every derived aggregate is computed
SQL-first via g.sql (GROUP BY over invoices, EXISTS-joined to clients so
dangling-client invoices drop out; SQL date arithmetic for the quarter-end
reconstructions) instead of REST reads + Python filtering. Writes the
IDENTICAL end-state: same report rows, same fields, same values.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

OPEN_STATUSES = ("sent", "overdue", "disputed")


def quarter_ends(episode, n=4):
    out = []
    y, m = episode.year, episode.month
    while len(out) < n:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        if m % 3 == 0:
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
    client_ids = {str(c["id"]) for c in clients}
    client_number = {str(c["id"]): c.get("client_number") for c in clients}
    trust = {str(c["id"]): c.get("trust_balance") for c in clients}

    # ---- SQL-first: one GROUP BY resolves every per-client figure -------
    rows = g.sql(
        "SELECT i.client_id AS cid, "
        "SUM(CASE WHEN i.status <> 'draft' THEN i.amount_paid ELSE 0 END) AS ltv, "
        "SUM(CASE WHEN i.status IN ('sent','overdue','disputed') THEN i.total - i.amount_paid ELSE 0 END) AS ar, "
        "SUM(CASE WHEN i.status <> 'draft' THEN 1 ELSE 0 END) AS cnt "
        "FROM invoices i "
        "WHERE EXISTS (SELECT 1 FROM clients c WHERE c.id::text = i.client_id::text) "
        "GROUP BY i.client_id"
    )
    lifetime = {}
    open_ar = {}
    counts = {}
    for r in rows:
        cid = str(r["cid"])
        if cid not in client_ids:
            continue
        lifetime[cid] = float(r["ltv"] or 0)
        open_ar[cid] = float(r["ar"] or 0)
        counts[cid] = int(r["cnt"] or 0)
    for c in clients:
        cid = str(c["id"])
        lifetime.setdefault(cid, 0.0)
        open_ar.setdefault(cid, 0.0)
        counts[cid] = counts.get(cid, 0)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("client_value", "high_ar_client", "ar_history"):
            g.delete("ops_reports", r["id"])

    firm_value = firm_ar = firm_count = 0
    for c in clients:
        cid = str(c["id"])
        firm_value += lifetime[cid]
        firm_ar += open_ar[cid]
        firm_count += counts[cid]
        g.push("ops_reports", {
            "report": "client_value", "batch_code": batch, "client_id": cid,
            "client_number": client_number[cid], "lifetime_value": lifetime[cid],
            "open_ar": open_ar[cid], "trust_balance": trust[cid], "invoice_count": counts[cid],
        })
    g.push("ops_reports", {
        "report": "client_value", "batch_code": batch, "client_id": "FIRM", "client_number": "FIRM",
        "lifetime_value": firm_value, "open_ar": firm_ar, "trust_balance": None,
        "invoice_count": firm_count,
    })

    ranked = sorted(open_ar.keys(), key=lambda cid: (-open_ar[cid], int(cid)))[:top_n]
    for rank, cid in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "high_ar_client", "batch_code": batch, "rank": rank,
            "client_id": cid, "open_ar": open_ar[cid], "lifetime_value": lifetime[cid],
        })

    for qe in quarter_ends(ep_dt):
        qe_s = qe.isoformat()
        agg = g.sql(
            f"SELECT COALESCE(SUM(total - amount_paid), 0) AS ar, COUNT(*) AS cnt "
            f"FROM invoices WHERE status IN ('sent','overdue','disputed') "
            f"AND issued_date::date <= DATE '{qe_s}' AND (total - amount_paid) > 0"
        )
        g.push("ops_reports", {
            "report": "ar_history", "batch_code": batch, "as_of": qe_s,
            "open_ar": float(agg[0]["ar"]), "open_invoices": int(agg[0]["cnt"]),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
