#!/usr/bin/env python3
"""Alternative gold solution for 001_trust_ledger_reconciliation (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
all reads are SQL-first through g.sql (single-statement joins/aggregates
instead of REST fetch-all + Python filtering), and the invoice/withdrawal
matching is executed inside Postgres as a boundary-delimited regex join.
Writes still go through the public REST API (the only write surface an
agent has). Idempotent for the same reasons as gold.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def tier_for(balance, cutoff):
    if not glib.Gold.cents(balance):
        return "zero"
    return "low" if glib.Gold.cents(balance) <= cutoff else "high"


def main():
    g = glib.Gold()
    batch = g.nonce()
    cutoff = int(g.nonce(field="exposure_tier_cutoff"))

    # ---- SQL-first reads ---------------------------------------------------
    # Per-client true balance + transaction count in ONE statement: latest row
    # by (tx_date DESC, id DESC) restricted to clients that actually exist, so
    # dangling trust_transactions rows (a different task's plant) never touch
    # a real client's ledger truth.
    bal_rows = g.sql(
        "SELECT DISTINCT ON (t.client_id) t.client_id, t.balance_after, "
        " (SELECT COUNT(*) FROM trust_transactions t2 WHERE t2.client_id #>> '{}' = t.client_id #>> '{}') AS tx_count "
        "FROM trust_transactions t "
        "WHERE t.client_id #>> '{}' IN (SELECT id::text FROM clients) "
        "ORDER BY t.client_id, t.tx_date DESC, t.id DESC"
    )
    true_balance = {str(r["client_id"]): r["balance_after"] for r in bal_rows}
    tx_count = {str(r["client_id"]): int(r["tx_count"] or 0) for r in bal_rows}

    clients = g.sql("SELECT id, client_number, trust_balance FROM clients ORDER BY id")

    # Boundary-delimited invoice <-> withdrawal matching executed by the
    # database: the reference must contain the invoice's full number with a
    # non-alphanumeric character (or a string boundary) on both sides.
    applied_rows = g.sql(
        "SELECT i.id AS invoice_id, MAX(t.amount) AS applied "
        "FROM invoices i JOIN trust_transactions t "
        "  ON t.type = 'withdrawal' "
        " AND t.reference ~ ('(^|[^A-Za-z0-9])' || i.invoice_number || '($|[^A-Za-z0-9])') "
        "GROUP BY i.id"
    )
    applied_by_invoice = {str(r["invoice_id"]): r["applied"] for r in applied_rows}

    invoice_rows = g.sql("SELECT id, invoice_number, trust_applied FROM invoices ORDER BY id")

    # ---- Stage 1: correct stored client balances ---------------------------
    # On a re-run the stored balance is already corrected; the pre-correction
    # figure for the audit record comes from this batch's own prior report row.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    prior_recon = {str(r.get("client_id")): r for r in existing
                   if r.get("report") == "trust_reconciliation" and r.get("batch_code") == batch}
    stored_before = {}
    for c in clients:
        cid = str(c["id"])
        prior = prior_recon.get(cid)
        stored_before[cid] = prior.get("old_trust_balance") if prior is not None else c.get("trust_balance")
    for c in clients:
        cid = str(c["id"])
        if glib.Gold.cents(stored_before[cid]) != glib.Gold.cents(true_balance.get(cid, 0)):
            g.update("clients", c["id"], {"trust_balance": true_balance.get(cid, 0)})

    # ---- Stage 2: backfill invoices.trust_applied ---------------------------
    for inv in invoice_rows:
        applied = applied_by_invoice.get(str(inv["id"]))
        if applied is not None and glib.Gold.cents(inv.get("trust_applied")) != glib.Gold.cents(applied):
            g.update("invoices", inv["id"], {"trust_applied": applied})

    # ---- Stage 3: three-way reconciliation report ---------------------------
    in_scope = [
        c for c in clients
        if tx_count.get(str(c["id"]), 0) > 0
        or glib.Gold.cents(stored_before[str(c["id"])]) != 0
        or str(c["id"]) in prior_recon
    ]

    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("trust_reconciliation", "trust_exposure_tier"):
            g.delete("ops_reports", r["id"])

    stored_total = sum(glib.Gold.cents(stored_before[str(c["id"])]) for c in in_scope)
    true_total = sum(glib.Gold.cents(true_balance.get(str(c["id"]), 0)) for c in in_scope)
    clients_corrected = sum(
        1 for c in in_scope
        if glib.Gold.cents(stored_before[str(c["id"])]) != glib.Gold.cents(true_balance.get(str(c["id"]), 0))
    )

    for c in in_scope:
        cid = str(c["id"])
        g.push("ops_reports", {
            "report": "trust_reconciliation",
            "batch_code": batch,
            "client_id": cid,
            "client_number": c.get("client_number"),
            "old_trust_balance": stored_before[cid],
            "new_trust_balance": true_balance.get(cid, 0),
            "transaction_count": tx_count.get(cid, 0),
        })
    g.push("ops_reports", {
        "report": "trust_reconciliation",
        "batch_code": batch,
        "client_id": "ALL",
        "client_number": "ALL",
        "stored_total": stored_total,
        "true_total": true_total,
        "discrepancy_cents": stored_total - true_total,
        "clients_corrected": clients_corrected,
    })

    # ---- Stage 4: exposure tiers (identical landing shape) ------------------
    by_tier = {"zero": 0, "low": 0, "high": 0}
    for c in in_scope:
        cid = str(c["id"])
        tier = tier_for(true_balance.get(cid, 0), cutoff)
        by_tier[tier] += 1
        g.push("ops_reports", {
            "report": "trust_exposure_tier",
            "batch_code": batch,
            "client_id": cid,
            "client_number": c.get("client_number"),
            "trust_balance": true_balance.get(cid, 0),
            "tier": tier,
        })
    g.push("ops_reports", {
        "report": "trust_exposure_tier",
        "batch_code": batch,
        "client_id": "ALL",
        "client_number": "ALL",
        "trust_balance": true_total,
        "tier": "ALL",
        "zero_count": by_tier["zero"],
        "low_count": by_tier["low"],
        "high_count": by_tier["high"],
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
