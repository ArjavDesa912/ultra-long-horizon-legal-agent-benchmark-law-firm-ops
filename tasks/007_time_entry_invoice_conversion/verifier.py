#!/usr/bin/env python3
"""Verifier for 007_time_entry_invoice_conversion (v2).

Expected invoices and flips derive from the frozen SEED time_entries (live
state shows no 'approved' rows after a correct run, so seed-derived
expectations keep the verifier idempotent). Dual-path: per-matter approved
values, the top-N cut, and flip sets are computed via Python filtering over
seed_rows AND an independent vlib.sql GROUP BY; both must agree with each
other and with the rows the agent wrote. Hazard coverage: billing written_off
work, including zero-rate entries, cutting the wrong side of top_n, or
mis-numbering the batch all FAIL. Fail-closed via vlib.run."""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")


def matter_number_key(m):
    num = m.get("matter_number") or ""
    try:
        return (0, int(num.split("-")[-1]))
    except ValueError:
        return (1, num)


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    top_n = int(vlib.get_nonce(v.token, field="top_n"))
    due_offset = int(vlib.get_nonce(v.token, field="due_offset_days"))
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    seed_matters = {str(m["id"]): m for m in vlib.seed_rows("matters")}
    seed_entries = vlib.seed_rows("time_entries")
    seed_invoices = vlib.seed_rows("invoices")

    # ------------------------------------------------- expected batch (path A)
    # Approved billable entries only (zero-rate skipped, written_off never
    # billed), from the SEED snapshot -- idempotent across reruns.
    approved_by_matter = {}
    for te in seed_entries:
        if te.get("status") == "approved" and (te.get("rate") or 0) > 0:
            approved_by_matter.setdefault(str(te.get("matter_id")), []).append(te)
    approved_value = {
        mid: sum(vlib.cents(te.get("amount", 0)) for te in entries)
        for mid, entries in approved_by_matter.items()
    }
    cut = sorted(
        approved_value.keys(),
        key=lambda mid: (-approved_value[mid], matter_number_key(seed_matters.get(mid, {}))),
    )[:top_n]

    max_year, max_num = ep[:4], 0
    for inv in seed_invoices:
        m = INV_RE.match(inv.get("invoice_number") or "")
        if m:
            max_year, max_num = m.group(1), max(max_num, int(m.group(2)))

    expected_invoices = {}
    expected_flipped_ids = set()
    billed_value_cents = 0
    for mid in sorted(cut, key=lambda x: matter_number_key(seed_matters.get(x, {}))):
        entries = sorted(approved_by_matter[mid], key=lambda te: te["id"])
        max_num += 1
        invoice_number = f"INV-{max_year}-{max_num:03d}"
        fees = sum(vlib.cents(te.get("amount", 0)) for te in entries)
        expected_invoices[invoice_number] = {
            "matter_id": mid,
            "client_id": (seed_matters.get(mid) or {}).get("client_id"),
            "fees_cents": fees,
            "entry_ids": [te["id"] for te in entries],
        }
        expected_flipped_ids.update(te["id"] for te in entries)
        billed_value_cents += fees

    # Dual-path: the same ranking as an independent SQL aggregate.
    sql_value_rows = vlib.sql(
        v.token,
        "SELECT te.matter_id AS matter_id, SUM(te.amount) AS value "
        "FROM time_entries te WHERE te.status = 'approved' AND COALESCE(te.rate, 0) > 0 "
        "GROUP BY te.matter_id",
    )
    sql_value = {str(r["matter_id"]): vlib.cents(r["value"]) for r in sql_value_rows}
    # After a correct run every cut matter's approved entries are flipped to
    # 'invoiced', so the live 'approved' aggregate must equal the seed-approved
    # values for exactly the NON-cut matters -- no more, no less.
    v.expect_equal(sql_value,
                   {mid: val for mid, val in approved_value.items() if mid not in set(cut)},
                   "top-N cut: raw vs SQL disagree (dual-path)")

    # --------------------------------------------------------- invoice checks
    live_invoices = vlib.fetch_all(v.token, "invoices")
    seed_inv_by_id = {str(r["id"]): r for r in seed_invoices}
    new_invoices = [inv for inv in live_invoices if str(inv["id"]) not in {str(r["id"]) for r in seed_invoices}]
    v.expect_equal(len(new_invoices), len(expected_invoices), "new invoice count")
    new_by_number = {inv.get("invoice_number"): inv for inv in new_invoices}
    for number, exp in expected_invoices.items():
        inv = new_by_number.get(number)
        v.expect(inv is not None, f"missing invoice {number}")
        v.expect_equal(str(inv.get("matter_id")), str(exp["matter_id"]), f"{number} matter_id")
        v.expect_equal(str(inv.get("client_id")), str(exp["client_id"]), f"{number} client_id")
        v.expect_cents(inv.get("fees_total"), exp["fees_cents"] / 100.0, f"{number} fees_total")
        v.expect_cents(inv.get("total"), exp["fees_cents"] / 100.0, f"{number} total")
        v.expect_cents(inv.get("amount_paid"), 0, f"{number} amount_paid")
        v.expect_cents(inv.get("trust_applied"), 0, f"{number} trust_applied")
        v.expect_cents(inv.get("disbursements_total"), 0, f"{number} disbursements_total")
        v.expect_cents(inv.get("tax"), 0, f"{number} tax")
        v.expect_equal(inv.get("status"), "sent", f"{number} status")
        v.expect_equal(vlib.dp(inv.get("issued_date")), ep, f"{number} issued_date")
        v.expect_equal(vlib.dp(inv.get("due_date")), due, f"{number} due_date")
        line_ids = [li.get("entry_id") for li in (inv.get("line_items") or [])]
        v.expect_equal([str(x) for x in line_ids], [str(x) for x in exp["entry_ids"]],
                       f"{number} line_items entry_id order")

    # Seeded invoices untouched.
    for seed_inv in seed_invoices:
        live = next((r for r in live_invoices if str(r["id"]) == str(seed_inv["id"])), None)
        v.expect(live is not None and vlib.row_eq(live, seed_inv), f"seeded invoice {seed_inv['id']} changed")

    # --------------------------------------------------------- flip exactness
    # Hazard coverage: written_off entries stay written_off; zero-rate approved
    # entries stay approved (skipped per policy); only billed entries flip.
    live_entries = vlib.fetch_all(v.token, "time_entries")
    v.expect_equal(len(live_entries), len(seed_entries), "time_entries row count changed")
    seed_te_by_id = {str(r["id"]): r for r in seed_entries}
    flipped = 0
    for te in live_entries:
        seed = seed_te_by_id.get(str(te["id"]))
        v.expect(seed is not None, f"time_entry {te['id']} not in seed")
        if str(te["id"]) in {str(i) for i in expected_flipped_ids}:
            v.expect_equal(te.get("status"), "invoiced", f"time_entry {te['id']} should be invoiced")
            v.expect(vlib.row_eq(te, seed, ignore=("status", "updated_at")),
                     f"time_entry {te['id']} non-status fields changed")
        else:
            v.expect(vlib.row_eq(te, seed), f"time_entry {te['id']} changed (must stay untouched)")
        if te.get("status") == "written_off":
            v.expect_equal(seed_te_by_id.get(str(te["id"]), {}).get("status"), "written_off",
                           f"time_entry {te['id']} written_off state changed (never billed)")

    # ------------------------------------------------------ ops_reports rows
    ops_rows = vlib.fetch_all(v.token, "ops_reports")

    # Stage 1: WIP snapshot rows must match the SEED per-matter aggregates.
    snapshot_rows = [r for r in ops_rows
                     if r.get("report") == "wip_snapshot" and r.get("batch_code") == batch]
    v.expect_equal(len(snapshot_rows), len(approved_value), "wip_snapshot row count")
    snapshot_by_matter = {r.get("matter_id"): r for r in snapshot_rows}
    for mid, value in approved_value.items():
        row = snapshot_by_matter.get(mid)
        v.expect(row is not None, f"missing wip_snapshot row for matter {mid}")
        v.expect_cents(row.get("approved_value"), value / 100.0, f"matter {mid} wip_snapshot approved_value")
        v.expect_equal(row.get("approved_entries"), len(approved_by_matter[mid]),
                       f"matter {mid} wip_snapshot approved_entries")

    # Stage 3: realization impact driven by the batch's own flip set.
    wip_before_cents = sum(approved_value.values())
    wip_after_cents = sum(
        vlib.cents(te.get("amount", 0)) for te in live_entries if te.get("status") == "approved"
    )
    live_invoiced = [te for te in live_entries if te.get("status") == "invoiced"]
    invoiced_after_cents = sum(vlib.cents(te.get("amount", 0)) for te in live_invoiced)
    invoiced_before_cents = invoiced_after_cents - billed_value_cents
    realization_before = (
        round(100 * invoiced_before_cents / (wip_before_cents + invoiced_before_cents), 2)
        if (wip_before_cents + invoiced_before_cents) else 0.0
    )
    realization_after = (
        round(100 * invoiced_after_cents / (wip_after_cents + invoiced_after_cents), 2)
        if (wip_after_cents + invoiced_after_cents) else 0.0
    )
    impact_rows = [r for r in ops_rows
                   if r.get("report") == "realization_impact" and r.get("batch_code") == batch]
    v.expect_equal(len(impact_rows), 1, "realization_impact row count")
    impact = impact_rows[0]
    v.expect_cents(impact.get("wip_value_before"), wip_before_cents / 100.0, "wip_value_before")
    v.expect_cents(impact.get("wip_value_after"), wip_after_cents / 100.0, "wip_value_after")
    v.expect_cents(impact.get("billed_value"), billed_value_cents / 100.0, "billed_value")
    v.expect(abs((impact.get("realization_before_pct") or -1) - realization_before) < 0.005,
             "realization_before_pct")
    v.expect(abs((impact.get("realization_after_pct") or -1) - realization_after) < 0.005,
             "realization_after_pct (a wrong flip set silently corrupts this)")

    # Stage 4: exception row.
    cut_set = set(cut)
    excluded = [mid for mid in approved_value if mid not in cut_set]
    exception_rows = [r for r in ops_rows
                      if r.get("report") == "billing_exceptions" and r.get("batch_code") == batch]
    v.expect_equal(len(exception_rows), 1, "billing_exceptions row count")
    exc = exception_rows[0]
    v.expect_equal(exc.get("matters_with_wip"), len(approved_value), "matters_with_wip")
    v.expect_equal(exc.get("matters_billed"), len(cut), "matters_billed")
    v.expect_equal(exc.get("matters_excluded"), len(excluded), "matters_excluded")
    v.expect_cents(exc.get("excluded_value"),
                   sum(approved_value[mid] for mid in excluded) / 100.0, "excluded_value")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
