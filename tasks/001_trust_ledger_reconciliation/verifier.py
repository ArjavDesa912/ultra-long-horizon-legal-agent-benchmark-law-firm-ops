#!/usr/bin/env python3
"""Verifier for 001_trust_ledger_reconciliation (v2).

Every derived aggregate is computed two independent ways -- Python filtering
over vlib.fetch_all / vlib.seed_rows rows AND an independent vlib.sql
GROUP BY / DISTINCT ON statement -- and both paths must agree with each other
AND with the rows the agent wrote. Expected values derive from the SEED
snapshot (vlib.seed_rows), never from live post-mutation state, so the
verifier is idempotent by construction. Fail-closed via vlib.run."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def id_key(value):
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s)


def boundary_match(reference, invoice_number):
    """The policy's boundary-delimited reference rule, used to derive the
    expected trust_applied per invoice from SNAPSHOT withdrawals."""
    for m in re.finditer(re.escape(invoice_number), reference or ""):
        before = reference[m.start() - 1] if m.start() > 0 else ""
        after = reference[m.end()] if m.end() < len(reference) else ""
        if not before.isalnum() and not after.isalnum():
            return True
    return False


def tier_for(balance, cutoff):
    if not vlib.cents(balance):
        return "zero"
    return "low" if vlib.cents(balance) <= cutoff else "high"


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")
    cutoff = int(vlib.get_nonce(v.token, field="exposure_tier_cutoff"))

    live_clients = vlib.fetch_all(v.token, "clients")
    live_txs = vlib.fetch_all(v.token, "trust_transactions")
    live_invoices = vlib.fetch_all(v.token, "invoices")

    seed_clients = vlib.seed_rows("clients")
    seed_txs = vlib.seed_rows("trust_transactions")
    seed_invoices = vlib.seed_rows("invoices")

    # ------------------------------------------------- expected ledger truth
    # Path A (Python over the SEED snapshot): latest balance_after per client
    # by (date, then id ascending); zero-transaction clients balance 0.
    by_client: dict = {}
    for tx in seed_txs:
        by_client.setdefault(str(tx["client_id"]), []).append(tx)
    for rows in by_client.values():
        rows.sort(key=lambda r: (vlib.dp(r["tx_date"]) or "", id_key(r["id"])))
    exp_true = {}
    exp_tx_count = {}
    for c in seed_clients:
        cid = str(c["id"])
        rows = by_client.get(cid, [])
        exp_true[cid] = rows[-1]["balance_after"] if rows else 0
        exp_tx_count[cid] = len(rows)
    exp_stored = {str(c["id"]): c.get("trust_balance") for c in seed_clients}

    # Path B (SQL over the live ledger, restricted to real clients so the
    # dangling-transaction plant never touches a real client): must agree
    # with path A row by row.
    sql_rows = vlib.sql(
        v.token,
        "SELECT DISTINCT ON (t.client_id) t.client_id, t.balance_after, "
        " (SELECT COUNT(*) FROM trust_transactions t2 "
        "  WHERE t2.client_id #>> '{}' = t.client_id #>> '{}') AS tx_count "
        "FROM trust_transactions t "
        "WHERE t.client_id #>> '{}' IN (SELECT id::text FROM clients) "
        "ORDER BY t.client_id, t.tx_date DESC, t.id DESC",
    )
    sql_balance = {str(r["client_id"]): r["balance_after"] for r in sql_rows}
    sql_tx_count = {str(r["client_id"]): int(r["tx_count"] or 0) for r in sql_rows}
    for c in seed_clients:
        cid = str(c["id"])
        if cid not in sql_balance:
            # Clients with no ledger activity have no DISTINCT ON row by
            # construction; the Python path already expects balance 0 there.
            v.expect_equal(vlib.cents(exp_true[cid]), 0, f"client {cid}: no SQL row but seed expects a balance")
            continue
        v.expect(vlib.cents(sql_balance[cid]) == vlib.cents(exp_true[cid]),
                 f"client {cid}: raw vs SQL balance disagree (dual-path)")
        v.expect(sql_tx_count.get(cid, 0) == exp_tx_count[cid],
                 f"client {cid}: raw vs SQL transaction_count disagree (dual-path)")

    # ------------------------------------------ stored balances corrected
    # Hazard coverage (lapse): the planted drifted subset must be corrected;
    # every other client byte-identical apart from trust_balance.
    v.expect_equal(len(live_clients), len(seed_clients), "clients row count changed")
    for c in seed_clients:
        cid = str(c["id"])
        live = next((r for r in live_clients if str(r["id"]) == cid), None)
        v.expect(live is not None, f"client {cid} missing from live clients")
        v.expect_cents(live.get("trust_balance"), exp_true.get(cid, 0),
                       f"client {cid} trust_balance corrected to ledger truth")
        v.expect(vlib.row_eq(live, c, ignore=("trust_balance", "updated_at")),
                 f"client {cid} non-trust_balance fields changed")

    # ------------------------------------------------------- trust_applied
    withdrawals = [tx for tx in seed_txs if tx.get("type") == "withdrawal"]
    seed_inv_by_id = {str(r["id"]): r for r in seed_invoices}
    v.expect_equal(len(live_invoices), len(seed_invoices), "invoices row count changed")
    for inv in live_invoices:
        seed = seed_inv_by_id.get(str(inv["id"]))
        v.expect(seed is not None, f"invoice {inv['id']} not in seed")
        matches = [tx for tx in withdrawals
                   if boundary_match(tx.get("reference"), inv["invoice_number"])]
        if matches:
            expected_applied = max(matches, key=lambda tx: vlib.cents(tx["amount"]))["amount"]
            v.expect_cents(inv.get("trust_applied"), expected_applied,
                           f"invoice {inv['invoice_number']} trust_applied")
        else:
            v.expect_cents(inv.get("trust_applied"), seed.get("trust_applied"),
                           f"invoice {inv['invoice_number']} trust_applied (no matching withdrawal, must be unchanged)")
        v.expect(vlib.row_eq(inv, seed, ignore=("trust_applied", "updated_at")),
                 f"invoice {inv['invoice_number']} non-trust_applied fields changed")

    # Explicit trap assertion: the INV-2026-0071 trap withdrawals exist in the
    # seed; neither INV-2026-007 nor INV-2026-071 may be touched.
    trap_refs = [tx for tx in withdrawals if "INV-2026-0071" in (tx.get("reference") or "")]
    if trap_refs:
        for number in ("INV-2026-007", "INV-2026-071"):
            live_inv = next((r for r in live_invoices if r.get("invoice_number") == number), None)
            seed_inv = next((r for r in seed_invoices if r.get("invoice_number") == number), None)
            if live_inv is not None and seed_inv is not None:
                v.expect_cents(live_inv.get("trust_applied"), seed_inv.get("trust_applied"),
                               f"trap invoice {number} trust_applied must stay unchanged (boundary rule)")

    # ------------------------------------------- trust_transactions untouched
    v.expect_equal(len(live_txs), len(seed_txs), "trust_transactions row count changed")
    seed_tx_by_id = {str(r["id"]): r for r in seed_txs}
    for tx in live_txs:
        seed = seed_tx_by_id.get(str(tx["id"]))
        v.expect(seed is not None and vlib.row_eq(tx, seed),
                 f"trust_transactions {tx['id']} modified (source ledger must stay untouched)")

    # ------------------------------------------------- stage 3: report rows
    # Scope (policy): clients with >= 1 ledger transaction or a nonzero stored
    # balance. Expectations derive from the SEED snapshot (idempotent).
    in_scope = [
        c for c in seed_clients
        if exp_tx_count.get(str(c["id"]), 0) > 0 or vlib.cents(exp_stored.get(str(c["id"]))) != 0
    ]
    stored_total = sum(vlib.cents(exp_stored.get(str(c["id"]), 0)) for c in in_scope)
    true_total = sum(vlib.cents(exp_true.get(str(c["id"]), 0)) for c in in_scope)
    clients_corrected = sum(
        1 for c in in_scope
        if vlib.cents(exp_stored.get(str(c["id"]))) != vlib.cents(exp_true.get(str(c["id"]), 0))
    )

    reports = vlib.fetch_all(v.token, "ops_reports")
    recon_rows = [r for r in reports
                  if r.get("report") == "trust_reconciliation" and r.get("batch_code") == batch]
    v.expect_equal(len(recon_rows), len(in_scope) + 1, "trust_reconciliation row count")

    # Dual-path: SQL COUNT over the same report rows must agree.
    sql_count_rows = vlib.sql(
        v.token,
        "SELECT report, COUNT(*) AS n FROM ops_reports "
        "WHERE batch_code = '" + batch_lit + "' "
        "AND report IN ('trust_reconciliation', 'trust_exposure_tier') GROUP BY report",
    )
    sql_report_counts = {r["report"]: int(r["n"]) for r in sql_count_rows}
    for name in ("trust_reconciliation", "trust_exposure_tier"):
        py_count = len([r for r in reports
                        if r.get("report") == name and r.get("batch_code") == batch])
        v.expect_equal(sql_report_counts.get(name, 0), py_count,
                       f"{name}: raw vs SQL row count disagree (dual-path)")

    recon_by_cid = {r.get("client_id"): r for r in recon_rows}
    for c in in_scope:
        cid = str(c["id"])
        row = recon_by_cid.get(cid)
        v.expect(row is not None, f"missing trust_reconciliation row for client {cid}")
        v.expect_cents(row.get("new_trust_balance"), exp_true.get(cid, 0),
                       f"client {cid} report new_trust_balance")
        v.expect_cents(row.get("old_trust_balance"), exp_stored.get(cid, 0),
                       f"client {cid} report old_trust_balance")
        v.expect_equal(row.get("client_number"), c.get("client_number"),
                       f"client {cid} report client_number")
        v.expect_equal(row.get("transaction_count"), exp_tx_count.get(cid, 0),
                       f"client {cid} report transaction_count")
    all_row = recon_by_cid.get("ALL")
    v.expect(all_row is not None, "missing ALL trust_reconciliation rollup row")
    v.expect_cents(all_row.get("stored_total"), stored_total,
                   "ALL stored_total (pre-correction third leg)")
    v.expect_cents(all_row.get("true_total"), true_total, "ALL true_total")
    v.expect_cents(all_row.get("discrepancy_cents"), stored_total - true_total,
                   "ALL discrepancy_cents")
    v.expect_equal(all_row.get("clients_corrected"), clients_corrected, "ALL clients_corrected")

    # SQL cross-check of the tie-out: live stored balances (post-correction)
    # must sum to the true total, independent of the report rows.
    sql_sum = vlib.sql(v.token, "SELECT COALESCE(SUM(trust_balance), 0) AS total FROM clients")
    live_stored_total = vlib.cents(sql_sum[0].get("total")) if sql_sum else 0
    v.expect_cents(live_stored_total, true_total,
                   "sum(live clients.trust_balance) vs true_total (dual-path)")

    # ------------------------------------------------ stage 4: exposure tiers
    tier_rows = [r for r in reports
                 if r.get("report") == "trust_exposure_tier" and r.get("batch_code") == batch]
    v.expect_equal(len(tier_rows), len(in_scope) + 1, "trust_exposure_tier row count")
    tier_by_cid = {r.get("client_id"): r for r in tier_rows}
    exp_tier_counts = {"zero": 0, "low": 0, "high": 0}
    for c in in_scope:
        cid = str(c["id"])
        expected_tier = tier_for(exp_true.get(cid, 0), cutoff)
        exp_tier_counts[expected_tier] += 1
        row = tier_by_cid.get(cid)
        v.expect(row is not None, f"missing trust_exposure_tier row for client {cid}")
        v.expect_cents(row.get("trust_balance"), exp_true.get(cid, 0),
                       f"client {cid} exposure tier trust_balance")
        v.expect_equal(row.get("tier"), expected_tier, f"client {cid} exposure tier")
    all_tier = tier_by_cid.get("ALL")
    v.expect(all_tier is not None, "missing ALL trust_exposure_tier rollup row")
    v.expect_equal(all_tier.get("zero_count"), exp_tier_counts["zero"], "ALL exposure tier zero_count")
    v.expect_equal(all_tier.get("low_count"), exp_tier_counts["low"], "ALL exposure tier low_count")
    v.expect_equal(all_tier.get("high_count"), exp_tier_counts["high"], "ALL exposure tier high_count")

    # Dual-path on the agent's own tier rows: SQL GROUP BY over the written
    # rows must reproduce the same tier counts.
    sql_tier = vlib.sql(
        v.token,
        "SELECT tier, COUNT(*) AS n FROM ops_reports "
        "WHERE batch_code = '" + batch_lit + "' "
        "AND report = 'trust_exposure_tier' AND client_id <> 'ALL' GROUP BY tier",
    )
    sql_tier_counts = {r["tier"]: int(r["n"]) for r in sql_tier}
    for tier_name in ("zero", "low", "high"):
        v.expect_equal(sql_tier_counts.get(tier_name, 0), exp_tier_counts[tier_name],
                       f"tier {tier_name}: written rows vs expectation disagree (dual-path)")

    v.check_canaries([
        "contacts", "deadlines", "tasks", "time_entries",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "matters", "hold_reminders",
        "trust_transactions",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
