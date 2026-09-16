#!/usr/bin/env python3
"""Verifier for 002_privilege_clawback_audit (v2).

In-range and violation sets are computed two independent ways -- Python
Bates-range filtering over vlib.seed_rows rows AND an independent vlib.sql
join -- and both paths must agree with each other AND with the privilege_log
rows the agent wrote. Hazard coverage: flagging a clawback-order-covered
production, a draft production, or an out-of-range privileged document must
FAIL. Expectations derive from the SEED snapshot (idempotent by
construction). Fail-closed via vlib.run."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

BATES_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def parse_bates(bates_number):
    m = BATES_RE.match(bates_number or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")

    all_productions = vlib.fetch_all(v.token, "ediscovery_productions")
    all_documents = vlib.fetch_all(v.token, "ediscovery_documents")
    live_log = vlib.fetch_all(v.token, "privilege_log")
    reports = vlib.fetch_all(v.token, "ops_reports")

    seed_productions = vlib.seed_rows("ediscovery_productions")
    seed_documents = vlib.seed_rows("ediscovery_documents")

    # ------------------------------------------------- expected sets (path A)
    # Python Bates-range filtering over the SEED snapshot.
    productions = [p for p in seed_productions if p.get("status") in ("finalised", "served")]
    in_range = 0
    in_range_by_matter = {}
    violations = []  # (doc, prod) pairs on uncovered productions
    for doc in seed_documents:
        m = BATES_RE.match(doc.get("bates_number") or "")
        if not m:
            continue
        prefix, num = m.group(1), int(m.group(2))
        for prod in productions:
            if prod.get("bates_prefix") == prefix and prod["bates_start"] <= num <= prod["bates_end"]:
                in_range += 1
                mid = doc.get("matter_id")
                in_range_by_matter[mid] = in_range_by_matter.get(mid, 0) + 1
                if doc.get("privilege") != "none" and prod.get("clawback_order_status") != "covered":
                    violations.append((doc, prod))
                break
    expected_bates = sorted(doc.get("bates_number") for doc, _ in violations)

    # Path B: the same computation as one independent SQL join (Bates prefix
    # and numeric suffix split with SQL regular expressions). Must agree with
    # path A -- the source collections are canaried untouched.
    sql_rows = vlib.sql(
        v.token,
        "SELECT d.bates_number, d.privilege, d.matter_id, p.clawback_order_status "
        "FROM ediscovery_documents d "
        "JOIN ediscovery_productions p "
        "  ON substring(d.bates_number from '^[A-Za-z]+') = p.bates_prefix "
        " AND substring(d.bates_number from '[0-9]+$')::int BETWEEN p.bates_start AND p.bates_end "
        "WHERE p.status IN ('finalised', 'served')",
    )
    v.expect_equal(len(sql_rows), in_range, "documents_in_range: raw vs SQL disagree (dual-path)")
    sql_violation_bates = sorted(
        r["bates_number"] for r in sql_rows
        if r.get("privilege") not in (None, "none") and r.get("clawback_order_status") != "covered"
    )
    v.expect_equal(sql_violation_bates, expected_bates,
                   "violations: raw vs SQL disagree (dual-path)")

    # ------------------------------------------------------- privilege_log
    live_log = [r for r in vlib.fetch_all(v.token, "privilege_log") if r.get("event") == "clawback_required"]
    v.expect_equal(len(live_log), len(violations), "privilege_log row count")
    live_bates = sorted(r.get("bates_number") for r in live_log)
    v.expect_equal(live_bates, expected_bates, "privilege_log bates_numbers (exact set)")

    seed_doc_by_bates = {r.get("bates_number"): r for r in seed_documents}
    for row in live_log:
        bates = row.get("bates_number")
        doc = seed_doc_by_bates.get(bates)
        v.expect(doc is not None, f"privilege_log row {bates} has no in-range seed document")
        prod = next((p for p in seed_productions if str(p["id"]) == str(row.get("production_id"))), None)
        v.expect(prod is not None, f"{bates}: privilege_log production_id does not resolve")
        # Hazard coverage: covered productions must never be flagged.
        v.expect(prod.get("clawback_order_status") != "covered",
                 f"{bates}: covered production logged (PRIV-CLAWBACK-01 forbids it)")
        # Hazard coverage: draft productions are never logged.
        v.expect(prod.get("status") in ("finalised", "served"),
                 f"{bates}: draft production logged")
        v.expect_equal(row.get("privilege"), doc.get("privilege"), f"{bates} privilege")
        v.expect_equal(row.get("classification"), doc.get("privilege"), f"{bates} classification")
        v.expect(doc.get("privilege") in str(row.get("reason") or ""),
                 f"{bates}: reason must name the privilege classification")
        v.expect_equal(str(row.get("document_id")), str(doc["id"]), f"{bates} document_id")
        v.expect_equal(str(row.get("production_id")), str(prod["id"]), f"{bates} production_id")

    # ------------------------------------------------------ summary row
    summary_rows = [r for r in reports
                    if r.get("report") == "privilege_clawback_audit" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "privilege_clawback_audit row count")
    summary = summary_rows[0]
    v.expect_equal(summary.get("productions_checked"), len(productions), "productions_checked")
    v.expect_equal(summary.get("documents_in_range"), in_range, "documents_in_range")
    v.expect_equal(summary.get("violations_found"), len(violations), "violations_found")

    # --------------------------------------------- stage 3: per-matter rollup
    # Derived from the SAME live log rows checked above (read-back enforced):
    # a wrong stage-1 Bates match silently changes matter grouping here.
    logged_by_matter = {}
    for row in live_log:
        logged_by_matter.setdefault(row.get("matter_id"), []).append(row)
    followup_rows = [r for r in reports
                     if r.get("report") == "clawback_followup" and r.get("batch_code") == batch]
    v.expect_equal(len(followup_rows), len(logged_by_matter), "clawback_followup row count")
    followup_by_matter = {r.get("matter_id"): r for r in followup_rows}
    for matter_id, rows in logged_by_matter.items():
        row = followup_by_matter.get(matter_id)
        v.expect(row is not None, f"missing clawback_followup row for matter {matter_id}")
        v.expect_equal(row.get("violation_count"), len(rows),
                       f"matter {matter_id} clawback_followup violation_count")
        total_produced = in_range_by_matter.get(matter_id, 0)
        v.expect_equal(row.get("total_produced_documents"), total_produced,
                       f"matter {matter_id} clawback_followup total_produced_documents")
        expected_ratio = round(100 * len(rows) / total_produced, 1) if total_produced else 0.0
        v.expect(abs((row.get("exposure_ratio_pct") or -1) - expected_ratio) < 0.05,
                 f"matter {matter_id} clawback_followup exposure_ratio_pct")

    # --------------------------------------------------- source rows untouched
    v.expect_equal(len(all_documents), len(seed_documents), "ediscovery_documents row count changed")
    v.expect_equal(len(all_productions), len(seed_productions), "ediscovery_productions row count changed")
    seed_doc_by_id = {str(r["id"]): r for r in seed_documents}
    for d in all_documents:
        seed = seed_doc_by_id.get(str(d["id"]))
        v.expect(seed is not None and vlib.row_eq(d, seed), f"ediscovery_documents {d['id']} modified")
    seed_prod_by_id = {str(r["id"]): r for r in seed_productions}
    for p in all_productions:
        seed = seed_prod_by_id.get(str(p["id"]))
        v.expect(seed is not None and vlib.row_eq(p, seed), f"ediscovery_productions {p['id']} modified")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries",
        "trust_transactions", "invoices",
        "ediscovery_holds", "ediscovery_collections",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
        "ediscovery_documents",
        "ediscovery_productions",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
