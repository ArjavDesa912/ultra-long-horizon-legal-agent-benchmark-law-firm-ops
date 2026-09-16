#!/usr/bin/env python3
"""Verifier for 026_firmwide_month_end_close_pack (v2).

Checks all 7 chained stages against expectations recomputed from the seed
snapshot (never from run-local deltas), with dual-path (Python vs SQL)
assertions on every derived aggregate. The close_pack and close_tieout rows are
checked against the SAME post-mutation reads used for stages 1-3, so a skipped
or wrong earlier stage silently corrupts them here -- the far-from-cause
failure the task is built around."""
import os
import re
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

AR_STATUSES = ("sent", "overdue", "disputed")
WIP_STATUSES = ("draft", "submitted", "approved")
INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")


def month_ends_before(ep_dt, n=12):
    out = []
    y, m = ep_dt.year, ep_dt.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append(date(y, m, monthrange(y, m)[1]))
    return out


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


def boundary_match(invoice_number, reference):
    """TRUST-REC-01: a withdrawal pays an invoice when the reference contains
    the invoice's full number as a boundary-delimited token."""
    if not invoice_number or not reference:
        return False
    text = str(reference)
    for m in re.finditer(re.escape(str(invoice_number)), text):
        before_ok = m.start() == 0 or not text[m.start() - 1].isalnum()
        after_ok = m.end() >= len(text) or not text[m.end()].isalnum()
        if before_ok and after_ok:
            return True
    return False


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(vlib.get_nonce(v.token, field="top_n"))
    due_offset = int(vlib.get_nonce(v.token, field="due_offset_days"))
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    live_clients = vlib.fetch_all(v.token, "clients")
    live_txs = vlib.fetch_all(v.token, "trust_transactions")
    live_invoices = vlib.fetch_all(v.token, "invoices")
    live_entries = vlib.fetch_all(v.token, "time_entries")
    reports = vlib.fetch_all(v.token, "ops_reports")

    seed_txs = vlib.seed_rows("trust_transactions")
    seed_invoices = {str(r["id"]): r for r in vlib.seed_rows("invoices")}
    seed_entries = {str(r["id"]): r for r in vlib.seed_rows("time_entries")}
    seed_matters = {str(m["id"]): m for m in vlib.seed_rows("matters")}
    seed_clients = {str(c["id"]): c for c in vlib.seed_rows("clients")}

    # ================= STAGE 1: trust reconciliation (dual-path) ============
    by_client = {}
    for tx in live_txs:
        by_client.setdefault(str(tx["client_id"]), []).append(tx)
    for rows in by_client.values():
        rows.sort(key=lambda r: (vlib.dp(r["tx_date"]) or "", str(r["id"])))
    true_balance = {cid: rows[-1]["balance_after"] for cid, rows in by_client.items()}

    sql_latest = vlib.sql(v.token,
                          "SELECT DISTINCT ON (client_id) client_id, balance_after "
                          "FROM trust_transactions "
                          "ORDER BY client_id, tx_date DESC, id DESC")
    sql_balance = {str(r["client_id"]): r["balance_after"] for r in sql_latest}
    for cid, bal in true_balance.items():
        v.expect(str(cid) in sql_balance, f"client {cid}: SQL missing latest balance (dual-path)")
        if str(cid) in sql_balance:
            v.expect_cents(sql_balance[cid], bal,
                           f"client {cid}: raw vs SQL balance disagree (dual-path)")
    for c in live_clients:
        v.expect_cents(c.get("trust_balance"), true_balance.get(str(c["id"]), 0),
                       f"client {c['id']} trust_balance")

    v.expect(len(live_txs) == len(seed_txs), "trust_transactions row count changed")
    seed_tx_by_id = {str(r["id"]): r for r in seed_txs}
    for tx in live_txs:
        seed = seed_tx_by_id.get(str(tx["id"]))
        v.expect(seed is not None and vlib.row_eq(tx, seed),
                 f"trust_transactions {tx['id']} modified (source ledger must stay untouched)")

    # ---------------- STAGE 0: the captured opening controls ----------------
    control_rows = [r for r in reports
                    if r.get("report") == "close_controls" and r.get("batch_code") == batch]
    v.expect_equal(len(control_rows), 1, "close_controls row count")
    crow = control_rows[0]

    seed_approved = defaultdict(list)
    for te in vlib.seed_rows("time_entries"):
        if te.get("status") == "approved":
            seed_approved.setdefault(str(te.get("matter_id")), []).append(te)
    seed_value = {mid: sum(te.get("amount", 0) or 0 for te in rows)
                  for mid, rows in seed_approved.items()}
    ranked = sorted(((val, mid) for mid, val in seed_value.items() if val > 0),
                    key=lambda t: (-t[0], seed_matters.get(t[1], {}).get("matter_number", "")))
    expected_candidates = [{"matter_id": mid, "approved_value": val}
                           for val, mid in ranked[:top_n]]
    v.expect_equal(crow.get("billing_candidates"), expected_candidates,
                   "close_controls billing_candidates")

    seed_wip = sum(te.get("amount", 0) or 0 for te in vlib.seed_rows("time_entries")
                   if te.get("status") in WIP_STATUSES)
    v.expect_cents(crow.get("wip_control"), seed_wip, "close_controls wip_control")
    seed_ar = 0.0
    for inv in seed_invoices.values():
        if inv.get("status") in AR_STATUSES:
            unpaid = (inv.get("total", 0) or 0) - (inv.get("amount_paid", 0) or 0) \
                - (inv.get("trust_applied", 0) or 0)
            if unpaid > 0:
                seed_ar += unpaid
    v.expect_cents(crow.get("ar_control"), seed_ar, "close_controls ar_control")
    seed_trust = sum(c.get("trust_balance", 0) or 0 for c in seed_clients.values())
    v.expect_cents(crow.get("trust_control"), seed_trust, "close_controls trust_control")

    # ---------------- STAGE 2: backfill (status-blind, boundary rule) -------
    live_invoice_by_id = {str(r["id"]): r for r in live_invoices}
    withdrawals = [tx for tx in live_txs if tx.get("type") == "withdrawal"]
    trust_applied = {}
    for iid, seed_inv in seed_invoices.items():
        matches = [tx for tx in withdrawals
                   if boundary_match(seed_inv.get("invoice_number"), tx.get("reference"))]
        expected_applied = max((tx.get("amount", 0) or 0 for tx in matches), default=None)
        live_inv = live_invoice_by_id.get(iid)
        v.expect(live_inv is not None, f"seed invoice {seed_inv.get('invoice_number')} missing")
        if live_inv is None:
            continue
        if expected_applied is None:
            v.expect_cents(live_inv.get("trust_applied"), seed_inv.get("trust_applied"),
                           f"invoice {seed_inv['invoice_number']} trust_applied (no match, unchanged)")
            trust_applied[iid] = seed_inv.get("trust_applied", 0) or 0
        else:
            v.expect_cents(live_inv.get("trust_applied"), expected_applied,
                           f"invoice {seed_inv['invoice_number']} trust_applied")
            trust_applied[iid] = expected_applied

    # ---------------- STAGE 3: billing candidates and new invoices ----------
    # BILL-GEN-01: approved entries only, zero-rate approved entries skipped;
    # only the entries included in an invoice flip.
    new_invoices = [inv for inv in live_invoices if str(inv["id"]) not in seed_invoices]
    v.expect_equal(len(new_invoices), len(expected_candidates),
                   "count of newly-generated invoices vs billing candidates")

    max_year, max_num = "2026", 0
    for inv in seed_invoices.values():
        m = INV_RE.match(inv.get("invoice_number") or "")
        if m:
            max_year = m.group(1)
            max_num = max(max_num, int(m.group(2)))
    expected_numbers = {}
    for cand in sorted(expected_candidates,
                       key=lambda c: seed_matters.get(str(c["matter_id"]), {}).get("matter_number", "")):
        mid = str(cand["matter_id"])
        max_num += 1
        expected_numbers[mid] = f"INV-{max_year}-{max_num:03d}"

    new_by_matter = {str(inv.get("matter_id")): inv for inv in new_invoices}
    billed_entry_ids = set()
    for cand in expected_candidates:
        mid = str(cand["matter_id"])
        v.expect(mid in new_by_matter, f"matter {mid} missing its close invoice")
        inv = new_by_matter.get(mid)
        if inv is None:
            continue
        v.expect_equal(str(inv.get("invoice_number")), expected_numbers[mid],
                       f"matter {mid} invoice_number sequencing")
        v.expect_equal(inv.get("status"), "sent", f"matter {mid} new invoice status")
        entries = sorted((te for te in seed_approved[mid] if (te.get("rate") or 0) > 0),
                         key=lambda te: str(te["id"]))
        fees_total = sum(te.get("amount", 0) or 0 for te in entries)
        v.expect_cents(inv.get("fees_total"), fees_total, f"matter {mid} new invoice fees_total")
        v.expect_cents(inv.get("total"), fees_total, f"matter {mid} new invoice total")
        v.expect_cents(inv.get("trust_applied"), 0, f"matter {mid} new invoice trust_applied")
        v.expect_cents(inv.get("amount_paid"), 0, f"matter {mid} new invoice amount_paid")
        v.expect_equal(vlib.dp(inv.get("issued_date")), ep, f"matter {mid} issued_date")
        v.expect_equal(vlib.dp(inv.get("due_date")), due, f"matter {mid} due_date")
        line_ids = [str(li.get("entry_id")) for li in (inv.get("line_items") or [])]
        v.expect_equal(line_ids, [str(te["id"]) for te in entries],
                       f"matter {mid} line_items entry order")
        for te in entries:
            billed_entry_ids.add(str(te["id"]))
    for mid in new_by_matter:
        v.expect(mid in {str(c["matter_id"]) for c in expected_candidates},
                 f"matter {mid} invoiced but not a billing candidate (over-billing)")

    for te in live_entries:
        eid = str(te["id"])
        seed_te = seed_entries.get(eid)
        v.expect(seed_te is not None, f"time_entries {eid} not in seed")
        if seed_te is None:
            continue
        if eid in billed_entry_ids:
            v.expect_equal(te.get("status"), "invoiced", f"time_entries {eid} should be invoiced")
            v.expect(vlib.row_eq(te, seed_te, ignore=("status", "updated_at")),
                     f"time_entries {eid} non-status fields changed")
        else:
            v.expect(vlib.row_eq(te, seed_te),
                     f"time_entries {eid} modified but not part of a close invoice")

    # ---------------- stage 4 history set (existing + new invoices) ---------
    history_invoices = []
    for iid, seed_inv in seed_invoices.items():
        live_inv = live_invoice_by_id.get(iid) or {}
        history_invoices.append({
            "issued": vlib.dp(live_inv.get("issued_date")) if live_inv else vlib.dp(seed_inv.get("issued_date")),
            "status": live_inv.get("status") if live_inv else seed_inv.get("status"),
            "total": (live_inv.get("total", 0) if live_inv else seed_inv.get("total", 0)) or 0,
            "amount_paid": (live_inv.get("amount_paid", 0) if live_inv else seed_inv.get("amount_paid", 0)) or 0,
            "trust_applied": trust_applied.get(iid, seed_inv.get("trust_applied", 0) or 0),
            "due": vlib.dp(live_inv.get("due_date")) if live_inv else vlib.dp(seed_inv.get("due_date")),
        })
    for inv in new_invoices:
        history_invoices.append({
            "issued": vlib.dp(inv.get("issued_date")),
            "status": inv.get("status"),
            "total": inv.get("total", 0) or 0,
            "amount_paid": inv.get("amount_paid", 0) or 0,
            "trust_applied": inv.get("trust_applied", 0) or 0,
            "due": vlib.dp(inv.get("due_date")),
        })

    history_rows = [r for r in reports
                    if r.get("report") == "ar_aging_history" and r.get("batch_code") == batch]
    expected_history_rows = 0
    for month_end in month_ends_before(ep_dt):
        me = month_end.strftime("%Y-%m-%d")
        bucket_count = defaultdict(int)
        bucket_total = defaultdict(float)
        for inv in history_invoices:
            issued = inv["issued"]
            if not issued or issued > me:
                continue  # interior-month correctness: not issued yet at this month-end
            if inv["status"] not in AR_STATUSES:
                continue
            unpaid = inv["total"] - inv["amount_paid"] - inv["trust_applied"]
            if unpaid <= 0:
                continue
            due_dt = datetime.strptime(inv["due"], "%Y-%m-%d").date()
            b = bucket_for((month_end - due_dt).days)
            bucket_count[b] += 1
            bucket_total[b] += unpaid
        matching = [r for r in history_rows if r.get("month_end") == me]
        v.expect_equal(len(matching), len(bucket_count),
                       f"ar_aging_history row count for month_end {me}")
        by_bucket = {r.get("bucket"): r for r in matching}
        for b, cnt in bucket_count.items():
            row = by_bucket.get(b)
            v.expect(row is not None, f"missing ar_aging_history row for {me}/{b}")
            if row is None:
                continue
            v.expect_equal(row.get("invoice_count"), cnt, f"{me}/{b} invoice_count")
            v.expect_cents(row.get("unpaid_total"), bucket_total[b], f"{me}/{b} unpaid_total")
        expected_history_rows += len(bucket_count)
        sql_rows = vlib.sql(v.token,
                            "SELECT CASE WHEN d.age <= 0 THEN 'current' WHEN d.age <= 30 THEN '1-30' "
                            "WHEN d.age <= 60 THEN '31-60' WHEN d.age <= 90 THEN '61-90' ELSE '90+' END "
                            "AS bucket, COUNT(*) AS n, SUM(d.unpaid) AS total FROM ("
                            "  SELECT i.id, ('" + me + "'::date - i.due_date::date) AS age, "
                            "         (i.total - i.amount_paid - i.trust_applied) AS unpaid "
                            "  FROM invoices i WHERE i.issued_date::date <= '" + me + "'::date "
                            "  AND i.status IN ('sent','overdue','disputed')"
                            ") d WHERE d.unpaid > 0 GROUP BY 1")
        sql_by_bucket = {r["bucket"]: r for r in sql_rows}
        for b, row in by_bucket.items():
            sql_row = sql_by_bucket.get(b)
            v.expect(sql_row is not None, f"SQL dual-path missing bucket {b} for {me}")
            if sql_row is None:
                continue
            v.expect_equal(int(sql_row["n"]), row.get("invoice_count"),
                           f"{me}/{b} invoice_count (dual-path)")
            v.expect_cents(sql_row["total"], row.get("unpaid_total"),
                           f"{me}/{b} unpaid_total (dual-path)")
    v.expect_equal(len(history_rows), expected_history_rows,
                   "ar_aging_history total row count (no stray/extra rows)")

    # ---------------- STAGE 5: close pack rollup (dual-path) ----------------
    close_rows = [r for r in reports
                  if r.get("report") == "close_pack" and r.get("batch_code") == batch]
    v.expect_equal(len(close_rows), 1, "close_pack row count")
    pack = close_rows[0]
    total_trust = sum(true_balance.get(str(c["id"]), 0) for c in live_clients)
    v.expect_cents(pack.get("total_trust_liability"), total_trust,
                   "close_pack total_trust_liability")
    v.expect_equal(pack.get("invoices_generated"), len(expected_candidates),
                   "close_pack invoices_generated")
    total_wip = sum(te.get("amount", 0) or 0 for te in live_entries
                    if te.get("status") in WIP_STATUSES)
    v.expect_cents(pack.get("total_wip_value"), total_wip, "close_pack total_wip_value")
    sql_wip = vlib.sql(v.token, "SELECT COALESCE(SUM(amount), 0) AS n FROM time_entries "
                                "WHERE status IN ('draft','submitted','approved')")
    v.expect(len(sql_wip) == 1, "SQL dual-path missing WIP total")
    v.expect_cents(sql_wip[0]["n"], total_wip, "close_pack total_wip_value (dual-path)")
    total_open_ar = 0.0
    for inv in history_invoices:
        unpaid = inv["total"] - inv["amount_paid"] - inv["trust_applied"]
        if inv["status"] in AR_STATUSES and unpaid > 0:
            total_open_ar += unpaid
    v.expect_cents(pack.get("total_open_ar"), total_open_ar, "close_pack total_open_ar")
    sql_ar = vlib.sql(v.token, "SELECT COALESCE(SUM(total - amount_paid - trust_applied), 0) AS n "
                               "FROM invoices WHERE status IN ('sent','overdue','disputed') "
                               "AND (total - amount_paid - trust_applied) > 0")
    v.expect(len(sql_ar) == 1, "SQL dual-path missing open AR total")
    v.expect_cents(sql_ar[0]["n"], total_open_ar, "close_pack total_open_ar (dual-path)")

    # ---------------- STAGE 6: tie-out vs the opening controls --------------
    tie_rows = [r for r in reports
                if r.get("report") == "close_tieout" and r.get("batch_code") == batch]
    v.expect_equal(len(tie_rows), 1, "close_tieout row count")
    tie = tie_rows[0]
    expected_wip_drop = seed_wip - total_wip
    v.expect_cents(tie.get("wip_drop"), expected_wip_drop, "close_tieout wip_drop")
    expected_billed = sum(inv.get("total", 0) or 0 for inv in new_invoices)
    v.expect_cents(tie.get("billed_value"), expected_billed, "close_tieout billed_value")
    v.expect_cents(tie.get("ar_delta"), total_open_ar - seed_ar, "close_tieout ar_delta")
    v.expect_equal(tie.get("ties_out"), True, "close_tieout ties_out")

    v.check_canaries([
        "contacts", "deadlines", "tasks", "matters", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
