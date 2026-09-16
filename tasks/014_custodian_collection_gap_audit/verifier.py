#!/usr/bin/env python3
"""Verifier for 014_custodian_collection_gap_audit (v2).

Expected gaps derive from the SEED snapshot (vlib.seed_rows) under HOLD-ACK-01's
scope rule (active holds in effect at least the episode row's gap_grace_days)
and the per-matter coverage rule (a collection under a DIFFERENT matter does
not cover the custodian), never from live post-mutation state, so the verifier
is idempotent by construction. Dual-path: the gap set is computed via Python
filtering AND an independent SQL coverage aggregate; both paths must agree with
each other AND with the rows the agent wrote. Hazard coverage: the 5 planted
cross-matter collections must NOT count as coverage (naive any-collection
matching misses exactly those gaps), and the aging buckets must derive from the
batch's own gap rows. Fail-closed via vlib.run."""
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def bucket_of(days):
    if days < 30:
        return "under_30"
    if days <= 90:
        return "30_90"
    return "over_90"


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    grace = int(vlib.get_nonce(v.token, field="gap_grace_days"))

    seed_holds = vlib.seed_rows("ediscovery_holds")
    seed_cols = vlib.seed_rows("ediscovery_collections")

    # --------------------------------------------- expected gaps (Python path)
    # Scope: active holds in effect at least gap_grace_days before the episode
    # date. A custodian is covered only by a collection belonging to the hold's
    # own matter -- a collection under a different matter does not cover.
    covered = defaultdict(set)
    for c in seed_cols:
        if c.get("matter_id") is not None:
            covered[str(c["matter_id"])].add(str(c.get("custodian_id")))
    expected = {}  # (hold_id, custodian_contact_id) -> expected row values
    for h in seed_holds:
        if h.get("status") != "active":
            continue
        issued = vlib.dp(h.get("issued_date"))
        if not issued:
            continue
        days = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days < grace:
            continue
        for cust in (h.get("custodians") or []):
            cid = str(cust.get("contact_id"))
            if cid not in covered.get(str(h.get("matter_id")), set()):
                expected[(str(h["id"]), cid)] = {
                    "hold_number": h.get("hold_number"),
                    "custodian_name": cust.get("name"),
                    "matter_id": h.get("matter_id"),
                    "days": days,
                }

    # Dual path: the same gap set via an independent SQL coverage aggregate
    # joined to the (Python-filtered) in-scope holds.
    sql_cov_rows = vlib.sql(
        v.token,
        "SELECT matter_id::text AS mid, custodian_id::text AS cust "
        "FROM ediscovery_collections WHERE matter_id IS NOT NULL "
        "GROUP BY matter_id::text, custodian_id::text",
    )
    sql_covered = defaultdict(set)
    for r in sql_cov_rows:
        sql_covered[str(r["mid"])].add(str(r["cust"]))
    sql_gap_keys = set()
    for h in seed_holds:
        if h.get("status") != "active":
            continue
        issued = vlib.dp(h.get("issued_date"))
        if not issued:
            continue
        days = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days < grace:
            continue
        for cust in (h.get("custodians") or []):
            cid = str(cust.get("contact_id"))
            if cid not in sql_covered.get(str(h.get("matter_id")), set()):
                sql_gap_keys.add((str(h["id"]), cid))
    v.expect_equal(sql_gap_keys, set(expected),
                   "gap set: SQL vs Python (dual-path)")

    # ------------------------------------------------ written gap rows
    reports = vlib.fetch_all(v.token, "ops_reports")
    gap_rows = [r for r in reports
                if r.get("report") == "collection_gap" and r.get("batch_code") == batch]
    v.expect_equal(len(gap_rows), len(expected), "collection_gap row count")
    got_keys = {(str(r.get("hold_id")), str(r.get("custodian_contact_id"))): r
                for r in gap_rows}
    v.expect_equal(set(got_keys), set(expected),
                   "collection_gap set (exact: the per-matter coverage rule)")
    for key, row in got_keys.items():
        hid, cid = key
        exp = expected[key]
        v.expect_equal(str(row.get("hold_number")), str(exp["hold_number"]),
                       f"gap {key} hold_number")
        v.expect_equal(str(row.get("custodian_name")), str(exp["custodian_name"]),
                       f"gap {key} custodian_name")
        v.expect_equal(str(row.get("matter_id")), str(exp["matter_id"]),
                       f"gap {key} matter_id")
        v.expect_equal(int(row.get("days_since_hold_issued") or 0), exp["days"],
                       f"gap {key} days_since_hold_issued")
    # Dual-path over the written rows: SQL GROUP BY must reproduce the set.
    sql_gap_counts = {(str(r["hid"]), str(r["cid"])): int(r["n"])
                      for r in vlib.sql(
                          v.token,
                          "SELECT hold_id::text AS hid, custodian_contact_id::text AS cid, "
                          "COUNT(*) AS n FROM ops_reports "
                          "WHERE report = 'collection_gap' AND batch_code = '" + batch_lit + "' "
                          "GROUP BY hold_id::text, custodian_contact_id::text",
                      )}
    v.expect_equal(set(sql_gap_counts), set(expected),
                   "collection_gap rows: SQL vs Python (dual-path)")

    # ------------------------------------------------------ summary row
    summary_rows = [r for r in reports
                    if r.get("report") == "collection_gap_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "collection_gap_summary row count")
    v.expect_equal(int(summary_rows[0].get("gaps_found") or 0), len(expected), "gaps_found")

    # ------------------------------------------------------ aging buckets
    # Derived from the SAME gap rows this batch pushed (checked above): the
    # bucket counts must sum to the gap total and match the expected counts.
    want_buckets = {"under_30": 0, "30_90": 0, "over_90": 0}
    for exp in expected.values():
        want_buckets[bucket_of(exp["days"])] += 1
    aging_rows = [r for r in reports
                  if r.get("report") == "collection_gap_aging" and r.get("batch_code") == batch]
    got_buckets = {str(r.get("bucket")): int(r.get("gap_count") or 0) for r in aging_rows}
    want_nonempty = {b: c for b, c in want_buckets.items() if c > 0}
    v.expect_equal(got_buckets, want_nonempty,
                   "collection_gap_aging buckets (from the same gap rows)")
    v.expect_equal(sum(got_buckets.values()), len(expected),
                   "aging buckets must partition this batch's gap rows")
    # Dual-path: SQL GROUP BY over the written gap rows must reproduce the
    # same bucket counts.
    sql_bucket_rows = vlib.sql(
        v.token,
        "SELECT CASE WHEN days_since_hold_issued < 30 THEN 'under_30' "
        "WHEN days_since_hold_issued <= 90 THEN '30_90' ELSE 'over_90' END AS b, "
        "COUNT(*) AS n FROM ops_reports "
        "WHERE report = 'collection_gap' AND batch_code = '" + batch_lit + "' GROUP BY b",
    )
    sql_buckets = {str(r["b"]): int(r["n"]) for r in sql_bucket_rows}
    v.expect_equal(sql_buckets, want_nonempty,
                   "aging buckets: SQL over written rows vs expectation (dual-path)")

    # ------------------------------------------------- cross-matter hazard
    # The 5 planted cross-matter collections exist (non-vacuity); a collection
    # under a DIFFERENT matter never covers the custodian's own hold, so the
    # naive any-collection matcher under-reports exactly when one of those
    # custodians' own holds is in scope.
    xmat = [c for c in seed_cols
            if str(c.get("collection_id") or "").startswith("COL-2026-XMAT")]
    v.expect_equal(len(xmat), 5,
                   "cross-matter mis-scoped collections planted (hazard present)")
    naive_covered = defaultdict(set)
    for c in seed_cols:
        if c.get("custodian_id") is not None:
            naive_covered[str(c.get("custodian_id"))].add(str(c.get("matter_id")))
    trap_bites = False
    for h in seed_holds:
        if h.get("status") != "active":
            continue
        issued = vlib.dp(h.get("issued_date"))
        if not issued:
            continue
        days = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days < grace:
            continue
        for cust in (h.get("custodians") or []):
            cid = str(cust.get("contact_id"))
            if cid in naive_covered and (str(h.get("matter_id"))) not in naive_covered[cid]:
                trap_bites = True
                v.expect((str(h["id"]), cid) in set(expected),
                         f"cross-matter collection must not cover custodian {cid} on hold {h['id']}")
    if not trap_bites:
        # The trap is dormant this rebuild (the 5 mis-scoped custodians' own
        # holds are released/suspended or grace-excluded); the per-matter rule
        # is still enforced by the exact gap-set assertion above.
        pass

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections",
        "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
