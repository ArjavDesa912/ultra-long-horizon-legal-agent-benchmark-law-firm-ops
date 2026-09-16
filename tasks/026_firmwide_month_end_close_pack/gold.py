#!/usr/bin/env python3
"""Gold solution for 026_firmwide_month_end_close_pack (v2). Run against a
FRESH container. Idempotent: safe to run twice in the same episode -- the
close's opening controls are captured once per batch (a re-run reuses them),
already-billed matters are not billed again, and this batch's report rows are
deleted and rewritten.

Seven chained stages. The trust reconciliation and the payment-matching rule
live in firm policy TRUST-REC-01; the billing scope, cut and numbering live in
BILL-GEN-01 (episode row: top_n, due_offset_days); the close calendar is the 12
calendar month-ends preceding the episode month."""
import os
import re
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

AR_STATUSES = ("sent", "overdue", "disputed")
WIP_STATUSES = ("draft", "submitted", "approved")
INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")
MUTATED_REPORTS = ("ar_aging_history", "close_pack", "close_tieout")


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
    """TRUST-REC-01: the reference must contain the invoice's full number as a
    boundary-delimited token (non-alphanumeric neighbours or string bounds)."""
    if not invoice_number or not reference:
        return False
    text = str(reference)
    for m in re.finditer(re.escape(str(invoice_number)), text):
        before_ok = m.start() == 0 or not text[m.start() - 1].isalnum()
        after_ok = m.end() >= len(text) or not text[m.end()].isalnum()
        if before_ok and after_ok:
            return True
    return False


def main():
    g = glib.Gold()
    batch = g.nonce()
    top_n = int(g.nonce(field="top_n"))
    due_offset = int(g.nonce(field="due_offset_days"))
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    clients = g.all("clients")
    txs = g.all("trust_transactions")
    invoices = g.all("invoices")
    entries = g.all("time_entries")
    matters = {str(m["id"]): m for m in g.all("matters")}

    # Idempotent re-run: clear this batch's stage-4/5/6 rows before regenerating
    # them. The stage-0 capture is NOT cleared: the opening state is fixed at
    # the open of the close and a re-run reuses it.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in MUTATED_REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    # ---- STAGE 0: capture the close's opening state (push-once) ------------
    if not any(r.get("report") == "close_controls" and r.get("batch_code") == batch
               for r in existing):
        approved_by_matter = defaultdict(float)
        for te in entries:
            if te.get("status") == "approved":
                approved_by_matter[str(te.get("matter_id"))] += te.get("amount", 0) or 0
        ranked = sorted(((val, mid) for mid, val in approved_by_matter.items() if val > 0),
                        key=lambda t: (-t[0], matters.get(t[1], {}).get("matter_number", "")))
        candidates = [{"matter_id": mid, "approved_value": val}
                      for val, mid in ranked[:top_n]]
        wip_control = sum(te.get("amount", 0) or 0 for te in entries
                          if te.get("status") in WIP_STATUSES)
        ar_control = 0.0
        for inv in invoices:
            if inv.get("status") in AR_STATUSES:
                unpaid = (inv.get("total", 0) or 0) - (inv.get("amount_paid", 0) or 0) \
                    - (inv.get("trust_applied", 0) or 0)
                if unpaid > 0:
                    ar_control += unpaid
        trust_control = sum(c.get("trust_balance", 0) or 0 for c in clients)
        g.push("ops_reports", {
            "report": "close_controls", "batch_code": batch,
            "wip_control": wip_control, "ar_control": ar_control,
            "trust_control": trust_control, "billing_candidates": candidates,
        })
    controls_row = next(r for r in g.all("ops_reports")
                        if r.get("report") == "close_controls" and r.get("batch_code") == batch)
    candidates = controls_row.get("billing_candidates") or []

    # ---- STAGE 1: trust reconciliation (TRUST-REC-01 sub-ledger rule) ------
    by_client = defaultdict(list)
    for tx in txs:
        by_client[str(tx["client_id"])].append(tx)
    for rows in by_client.values():
        rows.sort(key=lambda r: (glib.Gold.dp(r["tx_date"]) or "", str(r["id"])))
    true_balance = {}
    for c in clients:
        cid = str(c["id"])
        rows = by_client.get(cid) or []
        want = rows[-1]["balance_after"] if rows else 0
        true_balance[cid] = want
        if c.get("trust_balance") != want:
            g.update("clients", c["id"], {"trust_balance": want})

    # ---- STAGE 2: trust-applied backfill (TRUST-REC-01, status-blind) ------
    withdrawals = [tx for tx in txs if tx.get("type") == "withdrawal"]
    trust_applied = {}
    for inv in invoices:
        matches = [tx for tx in withdrawals
                   if boundary_match(inv.get("invoice_number"), tx.get("reference"))]
        if matches:
            best = max(matches, key=lambda tx: tx.get("amount", 0) or 0)
            trust_applied[str(inv["id"])] = best.get("amount", 0) or 0
            if inv.get("trust_applied") != best.get("amount"):
                g.update("invoices", inv["id"], {"trust_applied": best.get("amount")})
        else:
            trust_applied[str(inv["id"])] = inv.get("trust_applied", 0) or 0

    # ---- STAGE 3: bill this close's candidates (BILL-GEN-01) ---------------
    # BILL-GEN-01: approved entries only, zero-rate approved entries skipped,
    # written_off never billed. Only the entries included in an invoice flip.
    approved_by_matter = defaultdict(list)
    for te in entries:
        if te.get("status") == "approved" and (te.get("rate") or 0) > 0:
            approved_by_matter[str(te.get("matter_id"))].append(te)

    max_year, max_num = "2026", 0
    for inv in invoices:
        m = INV_RE.match(inv.get("invoice_number") or "")
        if m:
            max_year = m.group(1)
            max_num = max(max_num, int(m.group(2)))

    billed_value = 0
    new_invoices = []
    for cand in sorted(candidates, key=lambda c: matters.get(str(c["matter_id"]), {}).get("matter_number", "")):
        mid = str(cand["matter_id"])
        rows = sorted(approved_by_matter.get(mid, []), key=lambda te: str(te["id"]))
        if not rows:
            continue  # already billed by an earlier run of this close
        matter = matters.get(mid) or {}
        max_num += 1
        line_items = [
            {"entry_id": te["id"], "date": te.get("entry_date"),
             "description": te.get("description"), "hours": te.get("hours"),
             "rate": te.get("rate"), "amount": te.get("amount")}
            for te in rows
        ]
        fees_total = sum(te.get("amount", 0) or 0 for te in rows)
        g.push("invoices", {
            "invoice_number": f"INV-{max_year}-{max_num:03d}",
            "matter_id": mid, "client_id": matter.get("client_id"),
            "line_items": line_items, "fees_total": fees_total,
            "disbursements_total": 0, "tax": 0, "total": fees_total,
            "amount_paid": 0, "trust_applied": 0, "status": "sent",
            "issued_date": ep + "T00:00:00.000Z", "due_date": due + "T00:00:00.000Z",
        })
        billed_value += fees_total
        new_invoices.append({"total": fees_total, "amount_paid": 0,
                             "trust_applied": 0, "status": "sent",
                             "issued": ep, "due": due})
        for te in rows:
            g.update("time_entries", te["id"], {"status": "invoiced"})
            te["status"] = "invoiced"  # keep the local copy consistent for stage 5

    # ---- STAGE 4: point-in-time AR aging at the close calendar's dates -----
    history_invoices = []
    for inv in invoices:
        iid = str(inv["id"])
        history_invoices.append({
            "issued": glib.Gold.dp(inv.get("issued_date")),
            "status": inv.get("status"),
            "total": inv.get("total", 0) or 0,
            "amount_paid": inv.get("amount_paid", 0) or 0,
            "trust_applied": trust_applied.get(iid, inv.get("trust_applied", 0) or 0),
            "due": glib.Gold.dp(inv.get("due_date")),
        })
    for ni in new_invoices:
        history_invoices.append(dict(ni))
    for month_end in month_ends_before(ep_dt):
        me = month_end.strftime("%Y-%m-%d")
        bucket_count = defaultdict(int)
        bucket_total = defaultdict(float)
        for inv in history_invoices:
            issued = inv["issued"]
            if not issued or issued > me:
                continue  # did not exist at this month-end
            if inv["status"] not in AR_STATUSES:
                continue
            unpaid = inv["total"] - inv["amount_paid"] - inv["trust_applied"]
            if unpaid <= 0:
                continue
            due_dt = datetime.strptime(inv["due"], "%Y-%m-%d").date()
            b = bucket_for((month_end - due_dt).days)
            bucket_count[b] += 1
            bucket_total[b] += unpaid
        for b in bucket_count:
            g.push("ops_reports", {
                "report": "ar_aging_history", "batch_code": batch, "month_end": me,
                "bucket": b, "invoice_count": bucket_count[b],
                "unpaid_total": bucket_total[b],
            })

    # ---- STAGE 5: close pack rollup ----------------------------------------
    total_trust_liability = sum(true_balance.get(str(c["id"]), 0) for c in clients)
    total_wip_value = sum(te.get("amount", 0) or 0 for te in entries
                          if te.get("status") in WIP_STATUSES)
    total_open_ar = 0.0
    for inv in history_invoices:
        if inv["status"] in AR_STATUSES and inv["total"] - inv["amount_paid"] - inv["trust_applied"] > 0:
            total_open_ar += inv["total"] - inv["amount_paid"] - inv["trust_applied"]
    g.push("ops_reports", {
        "report": "close_pack", "batch_code": batch,
        "total_trust_liability": total_trust_liability,
        "invoices_generated": len(candidates),
        "total_wip_value": total_wip_value,
        "total_open_ar": total_open_ar,
    })

    # ---- STAGE 6: tie-out against the captured opening controls ------------
    # billed_value is read from the CAPTURED billing candidates (stable across
    # re-runs): after a complete close it equals the total actually billed,
    # because each candidate's invoice covers all of its approved entries.
    wip_drop = (controls_row.get("wip_control") or 0) - total_wip_value
    ar_delta = total_open_ar - (controls_row.get("ar_control") or 0)
    billed_value = sum(cand.get("approved_value", 0) or 0 for cand in candidates)
    g.push("ops_reports", {
        "report": "close_tieout", "batch_code": batch,
        "wip_drop": wip_drop, "billed_value": billed_value,
        "ar_delta": ar_delta,
        "ties_out": glib.Gold.cents(wip_drop) == glib.Gold.cents(billed_value),
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
