#!/usr/bin/env python3
"""Gold solution for 001_trust_ledger_reconciliation (v2, 4 stages). Run
against a FRESH container via the public REST API. Idempotent: client/invoice
corrections recompute to the same end-state, and report rows are deleted and
rewritten keyed by batch_code (first-run missing-table guarded)."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def id_key(value):
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s)


def boundary_match(reference, invoice_number):
    """TRUST-REC-01: the invoice's full number must appear inside the
    reference as a boundary-delimited token -- the characters immediately
    before and after it must be non-alphanumeric or the string boundary.
    A longer number that merely CONTAINS a shorter invoice's number is a
    different invoice and never matches."""
    for m in re.finditer(re.escape(invoice_number), reference or ""):
        before = reference[m.start() - 1] if m.start() > 0 else ""
        after = reference[m.end()] if m.end() < len(reference) else ""
        if not before.isalnum() and not after.isalnum():
            return True
    return False


def tier_for(balance, cutoff):
    if not glib.Gold.cents(balance):
        return "zero"
    return "low" if glib.Gold.cents(balance) <= cutoff else "high"


def main():
    g = glib.Gold()
    batch = g.nonce()
    cutoff = int(g.nonce(field="exposure_tier_cutoff"))

    clients = g.all("clients")
    txs = g.all("trust_transactions")
    invoices = g.all("invoices")

    # ---- Stage 1: per-client true balance from the ledger ---------------
    # TRUST-REC-01: the sub-ledger balance is the balance_after of the
    # client's most recent trust_transactions row (date, then id ascending
    # among same-date rows); a client with no transactions has balance 0.
    by_client = {}
    for tx in txs:
        by_client.setdefault(str(tx["client_id"]), []).append(tx)
    for rows in by_client.values():
        rows.sort(key=lambda r: (glib.Gold.dp(r["tx_date"]) or "", id_key(r["id"])))

    true_balance = {}
    tx_count = {}
    for c in clients:
        cid = str(c["id"])
        rows = by_client.get(cid, [])
        true_balance[cid] = rows[-1]["balance_after"] if rows else 0
        tx_count[cid] = len(rows)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    prior_recon = {str(r.get("client_id")): r for r in existing
                   if r.get("report") == "trust_reconciliation" and r.get("batch_code") == batch}

    # The reconciliation report is an audit record of what was found. On a
    # re-run the stored balance is already corrected, so the pre-correction
    # figure is taken from this batch's own prior report row when present.
    stored_before = {}
    for c in clients:
        cid = str(c["id"])
        prior = prior_recon.get(cid)
        stored_before[cid] = prior.get("old_trust_balance") if prior is not None else c.get("trust_balance")
    for c in clients:
        cid = str(c["id"])
        if glib.Gold.cents(stored_before[cid]) != glib.Gold.cents(true_balance[cid]):
            g.update("clients", c["id"], {"trust_balance": true_balance[cid]})

    # ---- Stage 2: trust-applied backfill (boundary-delimited) -----------
    withdrawals = [tx for tx in txs if tx.get("type") == "withdrawal"]
    for inv in invoices:
        matches = [
            tx for tx in withdrawals
            if boundary_match(tx.get("reference"), inv["invoice_number"])
        ]
        if matches:
            best = max(matches, key=lambda tx: glib.Gold.cents(tx["amount"]))
            if glib.Gold.cents(inv.get("trust_applied")) != glib.Gold.cents(best["amount"]):
                g.update("invoices", inv["id"], {"trust_applied": best["amount"]})

    # ---- Stage 3: three-way reconciliation report ------------------------
    # Scope (TRUST-REC-01): clients with at least one ledger transaction or a
    # nonzero stored balance. The third leg of the tie-out is the PRE-correction
    # stored-balance total.
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
    true_total = sum(glib.Gold.cents(true_balance[str(c["id"])]) for c in in_scope)
    clients_corrected = sum(
        1 for c in in_scope
        if glib.Gold.cents(stored_before[str(c["id"])]) != glib.Gold.cents(true_balance[str(c["id"])])
    )

    for c in in_scope:
        cid = str(c["id"])
        g.push("ops_reports", {
            "report": "trust_reconciliation",
            "batch_code": batch,
            "client_id": cid,
            "client_number": c.get("client_number"),
            "old_trust_balance": stored_before[cid],
            "new_trust_balance": true_balance[cid],
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

    # ---- Stage 4: exposure tiers from the episode knob --------------------
    # Tiers off the CORRECTED balances (stage 1's output) -- a wrong stage-1
    # correction mis-tiers here too.
    by_tier = {"zero": 0, "low": 0, "high": 0}
    for c in in_scope:
        cid = str(c["id"])
        tier = tier_for(true_balance[cid], cutoff)
        by_tier[tier] += 1
        g.push("ops_reports", {
            "report": "trust_exposure_tier",
            "batch_code": batch,
            "client_id": cid,
            "client_number": c.get("client_number"),
            "trust_balance": true_balance[cid],
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
