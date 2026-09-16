#!/usr/bin/env python3
"""Alternative gold solution for 007_time_entry_invoice_conversion (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the per-matter WIP aggregate, the pre-batch invoiced total, and the post-batch
realization inputs are computed with SQL statements through g.sql (GROUP BY /
SUM instead of REST fetch-all + Python filtering), while the cut selection and
invoice ordering apply the same deterministic ordering as gold.py. Writes
still go through the public REST API. The billing pass itself is single-run by
design (approved entries are exhausted after it)."""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")


def matter_number_key(m):
    num = m.get("matter_number") or ""
    try:
        return (0, int(num.split("-")[-1]))
    except ValueError:
        return (1, num)


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(g.nonce(field="top_n"))
    due_offset = int(g.nonce(field="due_offset_days"))
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    matters = {str(r["id"]): r for r in g.sql("SELECT * FROM matters")}
    invoices = g.all("invoices")

    # Invoices this batch already produced carry a batch_code marker; their
    # line items identify the entries this batch flipped. A re-run must see the
    # SAME pre-batch world: approved billable entries UNION the batch-covered
    # (now-invoiced) entries.
    batch_invoices = [inv for inv in invoices if inv.get("batch_code") == batch]
    covered_ids = {str(li.get("entry_id")) for inv in batch_invoices
                   for li in (inv.get("line_items") or [])}
    cov_list = ",".join(sorted((cid for cid in covered_ids), key=lambda s: int(s)
                               if s.isdigit() else 0)) or "-1"

    # ---- SQL-first: per-matter approved billable aggregates ------------------
    # Approved entries only; zero-rate approved entries skipped (BILL-GEN-01);
    # entries this batch already billed are folded back in so the aggregate is
    # the pre-batch universe on every run.
    value_rows = g.sql(
        "SELECT t.matter_id AS matter_id, COUNT(*) AS n, SUM(t.amount) AS value "
        "FROM time_entries t "
        "WHERE (t.status = 'approved' AND COALESCE(t.rate, 0) > 0) "
        "OR t.id IN (" + cov_list + ") "
        "GROUP BY t.matter_id"
    )
    approved_value = {str(r["matter_id"]): int(glib.Gold.cents(r["value"])) for r in value_rows}
    counts_by_matter = {str(r["matter_id"]): int(r["n"]) for r in value_rows}
    wip_before_cents = sum(approved_value.values())

    # Entry rows for line items (SQL read, Python grouping -- the aggregate
    # and the ranking below are what differ from gold.py's path).
    approved_by_matter = {}
    for te in g.sql("SELECT * FROM time_entries WHERE (status = 'approved' AND COALESCE(rate, 0) > 0) "
                    "OR id IN (" + cov_list + ")"):
        approved_by_matter.setdefault(str(te.get("matter_id")), []).append(te)

    # Pre-batch invoiced total: every 'invoiced' entry EXCEPT the ones this
    # batch covered (those were approved before the batch ran).
    invoiced_before_rows = g.sql(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM time_entries "
        "WHERE status = 'invoiced' AND id NOT IN (" + cov_list + ")"
    )
    invoiced_before_cents = int(glib.Gold.cents(invoiced_before_rows[0]["total"])) if invoiced_before_rows else 0

    # ---- Stage 1: pre-batch WIP snapshot (BEFORE any mutation) ---------------
    try:
        existing_ops = g.all("ops_reports")
    except RuntimeError:
        existing_ops = []
    for r in existing_ops:
        if r.get("batch_code") == batch and r.get("report") in (
                "wip_snapshot", "realization_impact", "billing_exceptions"):
            g.delete("ops_reports", r["id"])

    for mid in sorted(approved_value, key=lambda x: matter_number_key(matters.get(x, {}))):
        g.push("ops_reports", {
            "report": "wip_snapshot",
            "batch_code": batch,
            "matter_id": mid,
            "matter_number": (matters.get(mid) or {}).get("matter_number"),
            "approved_value": approved_value[mid] / 100.0,
            "approved_entries": counts_by_matter.get(mid, 0),
        })

    # ---- Stage 2: invoice generation + flips ---------------------------------
    # The cut is selected from the SQL aggregate with the same deterministic
    # ordering as gold.py (value DESC, then lowest matter number).
    cut = sorted(
        approved_value.keys(),
        key=lambda mid: (-approved_value[mid], matter_number_key(matters.get(mid, {}))),
    )[:top_n]

    # Positional numbering over the cut, continuing the NON-batch sequence, so
    # a re-run assigns identical numbers and a partial batch resumes correctly.
    max_year, max_num = ep[:4], 0
    for inv in invoices:
        if inv.get("batch_code") == batch:
            continue
        m = INV_RE.match(inv.get("invoice_number") or "")
        if m:
            max_year, max_num = m.group(1), max(max_num, int(m.group(2)))

    batch_inv_by_matter = {str(inv.get("matter_id")): inv for inv in batch_invoices}
    created_invoices = []
    for pos, mid in enumerate(
            sorted(cut, key=lambda x: matter_number_key(matters.get(x, {}))), start=1):
        entries = sorted(approved_by_matter[mid], key=lambda te: te["id"])
        matter = matters.get(mid) or {}
        if mid in batch_inv_by_matter:
            continue  # already billed by this batch -- nothing to redo
        invoice_number = f"INV-{max_year}-{max_num + pos:03d}"
        line_items = [
            {
                "entry_id": te["id"],
                "date": te.get("entry_date"),
                "description": te.get("description"),
                "hours": te.get("hours"),
                "rate": te.get("rate"),
                "amount": te.get("amount"),
            }
            for te in entries
        ]
        fees_cents = sum(glib.Gold.cents(te.get("amount", 0)) for te in entries)
        inv_doc = {
            "invoice_number": invoice_number,
            "batch_code": batch,
            "matter_id": mid,
            "client_id": matter.get("client_id"),
            "line_items": line_items,
            "fees_total": fees_cents / 100.0,
            "disbursements_total": 0,
            "tax": 0,
            "total": fees_cents / 100.0,
            "amount_paid": 0,
            "trust_applied": 0,
            "status": "sent",
            "issued_date": ep + "T00:00:00.000Z",
            "due_date": due + "T00:00:00.000Z",
        }
        g.push("invoices", inv_doc)
        created_invoices.append(inv_doc)
        for te in entries:
            g.update("time_entries", te["id"], {"status": "invoiced"})

    billed_value_cents = sum(
        glib.Gold.cents(inv.get("fees_total")) for inv in batch_invoices + created_invoices
    )

    # ---- Stage 3: realization impact (same landing shape) --------------------
    # The SQL re-read happens after the flips, so the before-values are
    # reconstructed from the batch's own billed amount (identical arithmetic,
    # different path from gold.py's pre-batch snapshot).
    invoiced_after_rows = g.sql(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM time_entries WHERE status = 'invoiced'"
    )
    invoiced_after_cents = int(glib.Gold.cents(invoiced_after_rows[0]["total"])) if invoiced_after_rows else 0
    wip_after_rows = g.sql("SELECT COALESCE(SUM(amount), 0) AS total FROM time_entries WHERE status = 'approved'")
    wip_after_cents = int(glib.Gold.cents(wip_after_rows[0]["total"])) if wip_after_rows else 0
    invoiced_before_cents = invoiced_after_cents - billed_value_cents
    realization_before = (
        round(100 * invoiced_before_cents / (wip_before_cents + invoiced_before_cents), 2)
        if (wip_before_cents + invoiced_before_cents) else 0.0
    )
    realization_after = (
        round(100 * invoiced_after_cents / (wip_after_cents + invoiced_after_cents), 2)
        if (wip_after_cents + invoiced_after_cents) else 0.0
    )
    g.push("ops_reports", {
        "report": "realization_impact",
        "batch_code": batch,
        "wip_value_before": wip_before_cents / 100.0,
        "wip_value_after": wip_after_cents / 100.0,
        "billed_value": billed_value_cents / 100.0,
        "realization_before_pct": realization_before,
        "realization_after_pct": realization_after,
    })

    # ---- Stage 4: exception report -------------------------------------------
    cut_set = set(cut)
    excluded = [mid for mid in approved_value if mid not in cut_set]
    g.push("ops_reports", {
        "report": "billing_exceptions",
        "batch_code": batch,
        "matters_with_wip": len(approved_value),
        "matters_billed": len(cut),
        "matters_excluded": len(excluded),
        "excluded_value": sum(approved_value[mid] for mid in excluded) / 100.0,
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
