#!/usr/bin/env python3
"""Gold solution for 007_time_entry_invoice_conversion (v2, 4 stages). Run
against a FRESH container via the public REST API. The billing pass itself is
single-run by design (approved entries are exhausted after it); the report
rows are deleted and rewritten keyed by batch_code so re-running the report
stages stays safe."""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")


def matter_number_key(m):
    """Numeric ordering key for a matter_number like '2026-013' (lexicographic
    ordering breaks once 4-digit numbers exist)."""
    num = m.get("matter_number") or ""
    try:
        return (0, int(num.split("-")[-1]))
    except (ValueError, IndexError):
        return (1, num)


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(g.nonce(field="top_n"))
    due_offset = int(g.nonce(field="due_offset_days"))
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    matters = {str(m["id"]): m for m in g.all("matters")}
    time_entries = g.all("time_entries")
    invoices = g.all("invoices")

    try:
        existing_ops = g.all("ops_reports")
    except RuntimeError:
        existing_ops = []
    for r in existing_ops:
        if r.get("batch_code") == batch and r.get("report") in (
                "wip_snapshot", "realization_impact", "billing_exceptions"):
            g.delete("ops_reports", r["id"])

    # Invoices this batch already produced carry a batch_code marker; their
    # line items identify the entries this batch flipped. A re-run must see the
    # SAME pre-batch world: approved billable entries UNION the batch-covered
    # (now-invoiced) entries.
    batch_invoices = [inv for inv in invoices if inv.get("batch_code") == batch]
    covered_ids = {str(li.get("entry_id")) for inv in batch_invoices
                   for li in (inv.get("line_items") or [])}

    # ---- Stage 1: pre-batch WIP snapshot (BEFORE any mutation) --------------
    # Approved billable value per matter: approved entries only, zero-rate
    # approved entries skipped (BILL-GEN-01).
    approved_by_matter = {}
    invoiced_before_cents = 0
    for te in time_entries:
        tid = str(te["id"])
        if te.get("status") == "approved" and (te.get("rate") or 0) > 0:
            approved_by_matter.setdefault(str(te.get("matter_id")), []).append(te)
        elif tid in covered_ids:
            approved_by_matter.setdefault(str(te.get("matter_id")), []).append(te)
    for te in time_entries:
        if te.get("status") == "invoiced" and str(te["id"]) not in covered_ids:
            invoiced_before_cents += glib.Gold.cents(te.get("amount", 0))
    approved_value = {
        mid: sum(glib.Gold.cents(te.get("amount", 0)) for te in entries)
        for mid, entries in approved_by_matter.items()
    }
    wip_before_cents = sum(approved_value.values())

    for mid in sorted(approved_value, key=lambda x: matter_number_key(matters.get(x, {}))):
        entries = approved_by_matter[mid]
        g.push("ops_reports", {
            "report": "wip_snapshot",
            "batch_code": batch,
            "matter_id": mid,
            "matter_number": (matters.get(mid) or {}).get("matter_number"),
            "approved_value": approved_value[mid] / 100.0,
            "approved_entries": len(entries),
        })

    # ---- Stage 2: invoice generation + entry flips ---------------------------
    # Top-N matters by approved value; numbering continues the highest existing
    # INV-YYYY-NNN sequence, lowest matter number first among the cut.
    cut = sorted(
        approved_value.keys(),
        key=lambda mid: (-approved_value[mid], matter_number_key(matters.get(mid, {}))),
    )[:top_n]

    # Invoice numbering is positional over the cut and continues the sequence
    # of NON-batch invoices only, so a re-run assigns identical numbers (and a
    # partially completed batch resumes correctly).
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
            te["status"] = "invoiced"

    billed_value_cents = sum(
        glib.Gold.cents(inv.get("fees_total")) for inv in batch_invoices + created_invoices
    )

    # ---- Stage 3: realization impact (before vs after) -----------------------
    # Realization = the invoiced share of billable work (KPI-REAL-01's billed
    # basis), before the batch versus after. The after-value is driven by the
    # batch's own exact flip set: a wrong flip corrupts it silently. wip_after
    # counts every still-approved entry (any rate), matching the ledger.
    wip_after_cents = sum(
        glib.Gold.cents(te.get("amount", 0)) for te in time_entries
        if te.get("status") == "approved"
    )
    invoiced_after_cents = invoiced_before_cents + billed_value_cents
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
    # Matters that carried approved work but fell outside this batch's cut.
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
