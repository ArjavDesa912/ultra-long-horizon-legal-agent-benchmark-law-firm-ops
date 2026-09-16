#!/usr/bin/env python3
"""Verifier for 010_ediscovery_review_queue_progress_report (v2).

Expectations derive from the SEED snapshot (vlib.seed_rows), never from live
post-mutation state, so the verifier is idempotent by construction. Dual-path:
the scoped matter set and the per-matter document/reviewed/privileged/item
aggregates are computed via Python filtering AND via independent vlib.sql
statements; both paths must agree with each other AND with the rows the agent
wrote. Hazard coverage: the 12 legacy 'qc-complete' synonym documents must be
counted as reviewed (REVIEW-STATUS-01 normalization), and the priority queue
must be the episode-knob-sized bottom set ranked from this batch's own rows.
Fail-closed via vlib.run."""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def r1(x):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def norm_status(value):
    """REVIEW-STATUS-01: 'qc-complete' is the recognized legacy synonym of
    qc_complete (reviewed); unrecognized values count as unreviewed."""
    s = str(value or "").strip().lower()
    if s == "qc-complete":
        return "qc_complete"
    if s in ("unreviewed", "in_review", "reviewed", "qc_complete"):
        return s
    return "unreviewed"


def id_key(value):
    """Numeric-aware identifier order (matter ids sort numerically)."""
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s)


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")
    n = int(vlib.get_nonce(v.token, field="priority_queue_size"))

    seed_holds = vlib.seed_rows("ediscovery_holds")
    seed_prods = vlib.seed_rows("ediscovery_productions")
    seed_docs = vlib.seed_rows("ediscovery_documents")
    seed_cols = vlib.seed_rows("ediscovery_collections")

    # ------------------------------------------------ expected scope (Python)
    scoped = {str(h.get("matter_id")) for h in seed_holds if h.get("status") == "active"}
    scoped |= {str(p.get("matter_id")) for p in seed_prods
               if p.get("status") in ("finalised", "served")}

    # Dual path: the same scoped set via an independent SQL UNION.
    sql_scope_rows = vlib.sql(
        v.token,
        "SELECT matter_id FROM ediscovery_holds WHERE status = 'active' "
        "UNION SELECT matter_id FROM ediscovery_productions "
        "WHERE status IN ('finalised','served')",
    )
    sql_scoped = {str(r["matter_id"]) for r in sql_scope_rows
                  if r.get("matter_id") is not None}
    v.expect_equal(sql_scoped, scoped, "scoped matter set: SQL vs Python (dual-path)")

    # ------------------------------------------ per-matter counts (Python)
    per = defaultdict(lambda: {"total": 0, "reviewed": 0, "privileged": 0})
    for d in seed_docs:
        m = per[str(d.get("matter_id"))]
        m["total"] += 1
        if norm_status(d.get("review_status")) in ("reviewed", "qc_complete"):
            m["reviewed"] += 1
        if (d.get("privilege") or "none") != "none":
            m["privileged"] += 1
    items = defaultdict(int)
    for c in seed_cols:
        if c.get("matter_id") is not None:
            items[str(c["matter_id"])] += int(c.get("item_count") or 0)

    # Dual path: the same aggregates via SQL GROUP BY (synonym folded in).
    sql_doc_rows = vlib.sql(
        v.token,
        "SELECT matter_id, COUNT(*) AS total, "
        "SUM(CASE WHEN LOWER(review_status) IN ('reviewed','qc_complete','qc-complete') "
        "THEN 1 ELSE 0 END) AS reviewed, "
        "SUM(CASE WHEN privilege IS NOT NULL AND LOWER(privilege) <> 'none' "
        "THEN 1 ELSE 0 END) AS privileged "
        "FROM ediscovery_documents GROUP BY matter_id",
    )
    sql_per = {str(r["matter_id"]): (int(r["total"]), int(r["reviewed"] or 0), int(r["privileged"] or 0))
               for r in sql_doc_rows if r.get("matter_id") is not None}
    sql_item_rows = vlib.sql(
        v.token,
        "SELECT matter_id, SUM(item_count) AS items FROM ediscovery_collections "
        "WHERE matter_id IS NOT NULL GROUP BY matter_id",
    )
    sql_items = {str(r["matter_id"]): int(r["items"] or 0) for r in sql_item_rows}
    for mid in sorted(scoped, key=id_key):
        exp = per.get(mid, {"total": 0, "reviewed": 0, "privileged": 0})
        st, sr, sp = sql_per.get(mid, (0, 0, 0))
        v.expect_equal(st, exp["total"], f"matter {mid}: total docs SQL vs Python (dual-path)")
        v.expect_equal(sr, exp["reviewed"], f"matter {mid}: reviewed SQL vs Python (dual-path)")
        v.expect_equal(sp, exp["privileged"], f"matter {mid}: privileged SQL vs Python (dual-path)")
        v.expect_equal(sql_items.get(mid, 0), items.get(mid, 0),
                       f"matter {mid}: items_collected SQL vs Python (dual-path)")

    def pct_of(mid):
        m = per.get(mid, {"total": 0, "reviewed": 0})
        return r1(100.0 * m["reviewed"] / m["total"]) if m["total"] else 0.0

    # ------------------------------------------------ written progress rows
    reports = vlib.fetch_all(v.token, "ops_reports")
    progress = [r for r in reports
                if r.get("report") == "ediscovery_progress" and r.get("batch_code") == batch]
    v.expect_equal(len(progress), len(scoped), "ediscovery_progress row count")
    by_mid = {str(r.get("matter_id")): r for r in progress}
    got_matters = sorted(by_mid, key=id_key)
    v.expect_equal(got_matters, sorted(scoped, key=id_key),
                   "progress rows cover exactly the in-scope matters")
    # Dual-path: SQL GROUP BY over the written rows must reproduce the set.
    sql_progress_counts = {str(r["matter_id"]): int(r["n"]) for r in vlib.sql(
        v.token,
        "SELECT matter_id, COUNT(*) AS n FROM ops_reports "
        "WHERE report = 'ediscovery_progress' AND batch_code = '" + batch_lit + "' "
        "GROUP BY matter_id",
    )}
    for mid in scoped:
        v.expect_equal(sql_progress_counts.get(mid, 0), 1,
                       f"matter {mid}: progress row SQL count vs Python (dual-path)")
    for mid in sorted(scoped, key=id_key):
        row = by_mid.get(mid)
        v.expect(row is not None, f"missing ediscovery_progress row for in-scope matter {mid}")
        if row is None:
            continue
        exp = per.get(mid, {"total": 0, "reviewed": 0, "privileged": 0})
        item_total = items.get(mid, 0)
        v.expect_equal(int(row.get("total_documents") or 0), exp["total"], f"{mid} total_documents")
        v.expect_equal(int(row.get("reviewed_count") or 0), exp["reviewed"], f"{mid} reviewed_count")
        v.expect_equal(r1(row.get("percent_reviewed")), pct_of(mid), f"{mid} percent_reviewed")
        v.expect_equal(int(row.get("privileged_count") or 0), exp["privileged"], f"{mid} privileged_count")
        v.expect_equal(int(row.get("items_collected") or 0), item_total, f"{mid} items_collected")
        v.expect_equal(int(row.get("review_backlog") or 0), item_total - exp["total"], f"{mid} review_backlog")

    # ------------------------------------------------- priority queue exact
    # Ranked from THIS batch's own progress rows (all in-scope matters have a
    # row, including zero-document matters whose reviewed share is 0.0).
    eligible = sorted(scoped, key=lambda mid: (pct_of(mid), id_key(mid)))
    want_ranked = eligible[:n]
    queue = [r for r in reports
             if r.get("report") == "ediscovery_priority_queue" and r.get("batch_code") == batch]
    v.expect_equal(len(queue), min(n, len(scoped)), "ediscovery_priority_queue row count")
    got_order = [str(r.get("matter_id"))
                 for r in sorted(queue, key=lambda x: int(x.get("rank") or 0))]
    v.expect_equal(got_order, want_ranked, "priority queue order (bottom-N by percent, id tie-break)")
    got_ranks = sorted(int(r.get("rank") or 0) for r in queue)
    v.expect_equal(got_ranks, list(range(1, len(want_ranked) + 1)),
                   "priority queue ranks contiguous from 1")
    for r in queue:
        mid = str(r.get("matter_id"))
        v.expect_equal(r1(r.get("percent_reviewed")), pct_of(mid), f"queue percent_reviewed for {mid}")
        v.expect_equal(int(r.get("review_backlog") or 0),
                       items.get(mid, 0) - per.get(mid, {"total": 0})["total"],
                       f"queue review_backlog for {mid}")
    # Dual-path over the written queue rows.
    sql_queue_counts = {str(r["matter_id"]): int(r["n"]) for r in vlib.sql(
        v.token,
        "SELECT matter_id, COUNT(*) AS n FROM ops_reports "
        "WHERE report = 'ediscovery_priority_queue' AND batch_code = '" + batch_lit + "' "
        "GROUP BY matter_id",
    )}
    for mid in want_ranked:
        v.expect_equal(sql_queue_counts.get(mid, 0), 1, f"queue SQL count for matter {mid}")

    # ------------------------------------------------------- hazard coverage
    # The 12 legacy 'qc-complete' docs must exist (non-vacuity) and, wherever
    # one of them sits on an in-scope matter, that matter's written row must
    # count it as reviewed (the normalized count, not the raw-value count).
    legacy = [d for d in seed_docs
              if str(d.get("review_status") or "").strip().lower() == "qc-complete"]
    v.expect_equal(len(legacy), 12, "legacy qc-complete docs planted (hazard present)")
    legacy_in_scope = [d for d in legacy if str(d.get("matter_id")) in scoped]
    for d in legacy_in_scope:
        mid = str(d.get("matter_id"))
        row = by_mid.get(mid)
        v.expect(row is not None, f"missing ediscovery_progress row for legacy-doc matter {mid}")
        if row:
            raw_reviewed = sum(1 for x in seed_docs
                               if str(x.get("matter_id")) == mid
                               and str(x.get("review_status") or "").strip().lower()
                               in ("reviewed", "qc_complete"))
            v.expect(int(row.get("reviewed_count") or 0) >= raw_reviewed,
                     f"matter {mid}: legacy qc-complete docs not counted as reviewed")
    if legacy_in_scope:
        # Non-vacuity: on an in-scope legacy matter the normalized count must
        # differ from the naive raw-value count, else the hazard dissolved.
        naive_per = defaultdict(int)
        for d in seed_docs:
            if str(d.get("review_status") or "").strip().lower() in ("reviewed", "qc_complete"):
                naive_per[str(d.get("matter_id"))] += 1
        differs = any(
            naive_per.get(mid, 0) != per.get(mid, {"reviewed": 0})["reviewed"]
            for mid in naive_per
        )
        v.expect(differs, "seed no longer discriminates the legacy synonym (hazard dissolved)")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
        "ediscovery_documents",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
