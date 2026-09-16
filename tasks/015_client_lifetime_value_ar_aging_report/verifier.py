#!/usr/bin/env python3
"""Verifier for 015_client_lifetime_value_ar_aging_report (v2).

Dual-path on every derived number: per-client lifetime_value / open_ar /
invoice_count are recomputed from the live invoices in Python AND via a
SQL GROUP BY (EXISTS-joined to clients so dangling-client invoices stay
excluded); both paths must agree with each other and with the rows the
agent wrote. Hazard coverage: folding trust_balance into lifetime value,
attributing dangling-client invoices anywhere, and re-ranking instead of
reading back the stage-1 rows all FAIL here. Expectations are recomputed
from the live + seed snapshot, so the verifier is idempotent across
repeated gold runs and fails closed via vlib.run.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

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


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(vlib.get_nonce(v.token, field="top_n"))

    clients = vlib.fetch_all(v.token, "clients")
    invoices = vlib.fetch_all(v.token, "invoices")
    client_ids = {str(c["id"]) for c in clients}
    client_number = {str(c["id"]): c.get("client_number") for c in clients}
    trust = {str(c["id"]): c.get("trust_balance") for c in clients}

    # ------------------------------------------------------ path 1: Python
    py_ltv = {str(c["id"]): 0.0 for c in clients}
    py_ar = {str(c["id"]): 0.0 for c in clients}
    py_cnt = {str(c["id"]): 0 for c in clients}
    for inv in invoices:
        cid = str(inv.get("client_id"))
        if cid not in py_ltv:
            continue  # dangling-client invoices resolve to no client
        status = inv.get("status")
        if status != "draft":
            py_ltv[cid] += inv.get("amount_paid") or 0
            py_cnt[cid] += 1
        if status in OPEN_STATUSES:
            py_ar[cid] += (inv.get("total") or 0) - (inv.get("amount_paid") or 0)

    # ------------------------------------------------------ path 2: SQL
    sql_rows = vlib.sql(
        v.token,
        "SELECT i.client_id AS cid, "
        "COALESCE(SUM(CASE WHEN i.status <> 'draft' THEN i.amount_paid ELSE 0 END), 0) AS ltv, "
        "COALESCE(SUM(CASE WHEN i.status IN ('sent','overdue','disputed') "
        "THEN i.total - i.amount_paid ELSE 0 END), 0) AS ar, "
        "COALESCE(SUM(CASE WHEN i.status <> 'draft' THEN 1 ELSE 0 END), 0) AS cnt "
        "FROM invoices i "
        "WHERE EXISTS (SELECT 1 FROM clients c WHERE c.id::text = i.client_id::text) "
        "GROUP BY i.client_id",
    )
    sql_by_client = {str(r["cid"]): r for r in sql_rows}
    # GROUP BY emits no row for clients with zero invoices; the SQL result must
    # cover exactly the clients that have at least one resolvable invoice.
    invoiced_cids = {str(inv.get("client_id")) for inv in invoices
                     if str(inv.get("client_id")) in client_ids}
    v.expect_equal(set(sql_by_client), invoiced_cids,
                   "dual-path: SQL per-client group count")
    for cid in py_ltv:
        r = sql_by_client.get(cid)
        if r is None:
            v.expect(py_ltv[cid] == 0 and py_ar[cid] == 0 and py_cnt[cid] == 0,
                     f"dual-path: client {cid} missing from SQL GROUP BY with nonzero figures")
            continue
        v.expect_cents(r["ltv"], py_ltv[cid], f"dual-path: client {cid} lifetime_value")
        v.expect_cents(r["ar"], py_ar[cid], f"dual-path: client {cid} open_ar")
        v.expect_equal(int(r["cnt"]), py_cnt[cid], f"dual-path: client {cid} invoice_count")

    # ------------------------------------------------------ stage 1: rows
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "client_value" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(clients) + 1, "client_value row count")
    written = {str(r.get("client_id")): r for r in rows}
    v.expect("FIRM" in written, "missing FIRM rollup row")
    for cid in written:
        v.expect(cid == "FIRM" or cid in client_ids,
                 f"client_value row attributes figures to non-resolving client {cid}")
    firm_ltv = firm_ar = 0.0
    firm_cnt = 0
    for c in clients:
        cid = str(c["id"])
        row = written.get(cid)
        v.expect(row is not None, f"missing client_value row for client {cid}")
        v.expect_equal(row.get("client_number"), client_number[cid], f"client {cid} client_number")
        v.expect_cents(row.get("lifetime_value"), py_ltv[cid], f"client {cid} lifetime_value")
        v.expect_cents(row.get("open_ar"), py_ar[cid], f"client {cid} open_ar")
        v.expect_cents(row.get("trust_balance"), trust[cid], f"client {cid} trust_balance")
        v.expect_equal(row.get("invoice_count"), py_cnt[cid], f"client {cid} invoice_count")
        firm_ltv += py_ltv[cid]
        firm_ar += py_ar[cid]
        firm_cnt += py_cnt[cid]
    firm_row = written.get("FIRM")
    v.expect_cents(firm_row.get("lifetime_value"), firm_ltv, "FIRM lifetime_value")
    v.expect_cents(firm_row.get("open_ar"), firm_ar, "FIRM open_ar")
    v.expect_equal(firm_row.get("invoice_count"), firm_cnt, "FIRM invoice_count")

    # ------------------------------------------------------ stage 2: top-N
    expected_ranked = sorted(py_ar.keys(), key=lambda cid: (-py_ar[cid], int(cid)))[:top_n]
    high_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "high_ar_client" and r.get("batch_code") == batch]
    v.expect_equal(len(high_rows), len(expected_ranked), "high_ar_client row count")
    by_rank = {r.get("rank"): r for r in high_rows}
    for rank, cid in enumerate(expected_ranked, start=1):
        row = by_rank.get(rank)
        v.expect(row is not None, f"missing high_ar_client rank {rank}")
        v.expect_equal(str(row.get("client_id")), cid, f"high_ar_client rank {rank} client_id")
        v.expect_cents(row.get("open_ar"), py_ar[cid], f"high_ar_client rank {rank} open_ar")
        v.expect_cents(row.get("lifetime_value"), py_ltv[cid], f"high_ar_client rank {rank} lifetime_value")
        # SEVERITY-01: the ranking must reuse the agent's own stage-1 rows
        stage1 = written.get(cid)
        v.expect(stage1 is not None, f"high_ar_client rank {rank} has no matching client_value row")
        v.expect_cents(row.get("open_ar"), stage1.get("open_ar"), f"high_ar_client rank {rank} reuses stage-1 open_ar")

    # ------------------------------------------------- stage 3: as-of AR
    expected_qes = [q.isoformat() for q in quarter_ends(ep_dt)]
    hist_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "ar_history" and r.get("batch_code") == batch]
    v.expect_equal(len(hist_rows), len(expected_qes), "ar_history row count")
    hist_by_date = {vlib.dp(r.get("as_of")): r for r in hist_rows}
    for qe in expected_qes:
        exp_total = 0.0
        exp_cnt = 0
        for inv in invoices:
            if inv.get("status") not in OPEN_STATUSES:
                continue
            issued = vlib.dp(inv.get("issued_date"))
            if issued is None or issued > qe:
                continue  # an invoice issued after a close date must not appear in it
            unpaid = (inv.get("total") or 0) - (inv.get("amount_paid") or 0)
            if unpaid > 0:
                exp_total += unpaid
                exp_cnt += 1
        row = hist_by_date.get(qe)
        v.expect(row is not None, f"missing ar_history row for close date {qe}")
        v.expect_cents(row.get("open_ar"), exp_total, f"ar_history {qe} open_ar")
        v.expect_equal(row.get("open_invoices"), exp_cnt, f"ar_history {qe} open_invoices")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
