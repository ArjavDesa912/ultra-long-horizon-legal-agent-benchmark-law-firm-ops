#!/usr/bin/env python3
"""Verifier for 018_chain_of_custody_timeline_audit (v2).

Dual-path on every derived verdict: each collection's five CUSTODY-01 rule
verdicts are recomputed from the live collections in Python AND via a SQL
jsonb aggregation (jsonb_array_elements + LAG window); both paths must agree
with each other and with the rows the agent wrote. Hazard coverage: skipping
any policy rule, flagging empty-array collections, misreading the who/actor
spellings, or hardcoding the gap tolerance instead of the episode knob all
FAIL here. Idempotent across repeated gold runs; fails closed via vlib.run.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

ADMIN_ACTIONS = ("copy", "export")

SQL_VERDICTS = """
WITH exp AS (
    SELECT c.id AS cid, t.idx, t.e,
           (t.e->>'when')::timestamptz::date AS wd,
           t.e->>'action' AS action,
           COALESCE(NULLIF(t.e->>'actor', ''), NULLIF(t.e->>'who', '')) AS actor
    FROM ediscovery_collections c
    CROSS JOIN LATERAL jsonb_array_elements(c.chain_of_custody::jsonb) WITH ORDINALITY AS t(e, idx)
    WHERE c.chain_of_custody IS NOT NULL
      AND jsonb_typeof(c.chain_of_custody::jsonb) = 'array'
      AND jsonb_array_length(c.chain_of_custody::jsonb) > 0
),
w AS (
    SELECT e.*, LAG(e.wd) OVER (PARTITION BY e.cid ORDER BY e.idx) AS prev_wd FROM exp e
),
agg AS (
    SELECT w.cid,
           COUNT(*) AS entries,
           BOOL_AND(w.prev_wd IS NULL OR w.wd >= w.prev_wd) AS chronological,
           BOOL_AND(w.action IS NOT NULL AND w.actor IS NOT NULL) AS complete_entries,
           MAX(CASE WHEN w.prev_wd IS NULL THEN 0 ELSE w.wd - w.prev_wd END) AS max_gap,
           MIN(CASE WHEN w.action = 'preservation' THEN w.wd END) AS pres_w,
           MIN(CASE WHEN w.action = 'collection' THEN w.wd END) AS coll_w,
           MAX(w.wd) - MIN(w.wd) AS span_days
    FROM w w
    GROUP BY w.cid
)
SELECT cid, entries, chronological, complete_entries, max_gap, pres_w, coll_w, span_days FROM agg
"""


def py_analyze(chain, gap_days):
    dated = []
    for e in chain:
        w = vlib.dp(e.get("when"))
        d = datetime.strptime(w, "%Y-%m-%d").date() if w else None
        dated.append((e, d))
    whens = [d for _, d in dated]
    chronological = all(whens[i] <= whens[i + 1] for i in range(len(whens) - 1))
    complete = all((e.get("actor") or e.get("who")) and e.get("action") and d is not None
                   for e, d in dated)
    pres_whens = [d for e, d in dated if e.get("action") == "preservation"]
    coll_whens = [d for e, d in dated if e.get("action") == "collection"]
    if not coll_whens:
        pres_ok = True
    else:
        pres_ok = bool(pres_whens) and min(pres_whens) <= min(coll_whens)
    gaps = [whens[i] - whens[i - 1] for i in range(1, len(whens))] if len(whens) > 1 else []
    gap_list = [g.days for g in gaps]
    gap_ok = all(gd <= gap_days for gd in gap_list)
    max_gap = max(gap_list) if gap_list else 0
    span = (max(whens) - min(whens)).days if whens else 0
    reasons = []
    if not chronological:
        reasons.append("chronology")
    if not pres_ok:
        reasons.append("preservation")
    if not complete:
        reasons.append("completeness")
    if not gap_ok:
        reasons.append("gap")
    return {
        "entries": len(chain), "chronological": chronological,
        "preservation_before_collection": pres_ok, "complete_entries": complete,
        "max_gap_days": max_gap, "span_days": span,
        "compliant": chronological and pres_ok and complete and gap_ok,
        "reason_codes": reasons,
    }


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    gap_days = int(vlib.get_nonce(v.token, field="custody_gap_days"))

    live_cols = vlib.fetch_all(v.token, "ediscovery_collections")
    audited = [c for c in live_cols if c.get("chain_of_custody")]
    empty_cols = [c for c in live_cols if not c.get("chain_of_custody")]

    # ------------------------------------------------------ path 1: Python
    py = {str(c["id"]): py_analyze(c["chain_of_custody"], gap_days) for c in audited}

    # ------------------------------------------------------ path 2: SQL
    sql = {}
    for r in vlib.sql(v.token, SQL_VERDICTS):
        cid = str(r["cid"])
        chronological = bool(r["chronological"])
        complete = bool(r["complete_entries"])
        max_gap = int(r["max_gap"] or 0)
        coll_w = r["coll_w"]
        pres_w = r["pres_w"]
        pres_ok = True if coll_w is None else (pres_w is not None and pres_w <= coll_w)
        gap_ok = max_gap <= gap_days
        reasons = []
        if not chronological:
            reasons.append("chronology")
        if not pres_ok:
            reasons.append("preservation")
        if not complete:
            reasons.append("completeness")
        if not gap_ok:
            reasons.append("gap")
        sql[cid] = {
            "entries": int(r["entries"]), "chronological": chronological,
            "preservation_before_collection": pres_ok, "complete_entries": complete,
            "max_gap_days": max_gap, "span_days": int(r["span_days"] or 0),
            "compliant": chronological and pres_ok and complete and gap_ok,
            "reason_codes": reasons,
        }

    v.expect_equal(len(sql), len(py), "dual-path: audited collection count (SQL vs Python)")
    for cid, exp in py.items():
        got = sql.get(cid)
        v.expect(got is not None, f"dual-path: collection {cid} missing from SQL verdicts")
        for field in ("entries", "chronological", "preservation_before_collection",
                      "complete_entries", "max_gap_days", "span_days", "compliant"):
            v.expect_equal(got[field], exp[field], f"dual-path: collection {cid} {field}")
        v.expect_equal(sorted(got["reason_codes"]), sorted(exp["reason_codes"]),
                       f"dual-path: collection {cid} reason_codes")

    # ------------------------------------------------------ stage 1: rows
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "custody_timeline" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(py), "custody_timeline row count")
    by_id = {str(r.get("collection_id")): r for r in rows}
    for cid in by_id:
        v.expect(cid in py, f"custody_timeline row flags a non-auditable collection {cid}")
    for cid, exp in py.items():
        row = by_id.get(cid)
        v.expect(row is not None, f"missing custody_timeline row for collection {cid}")
        v.expect_equal(row.get("entries_count"), exp["entries"], f"collection {cid} entries_count")
        v.expect_equal(row.get("chronological"), exp["chronological"], f"collection {cid} chronological")
        v.expect_equal(row.get("preservation_before_collection"),
                       exp["preservation_before_collection"], f"collection {cid} preservation rule")
        v.expect_equal(row.get("complete_entries"), exp["complete_entries"], f"collection {cid} completeness")
        v.expect_equal(row.get("max_gap_days"), exp["max_gap_days"], f"collection {cid} max_gap_days")
        v.expect_equal(row.get("span_days"), exp["span_days"], f"collection {cid} span_days")
        v.expect_equal(row.get("compliant"), exp["compliant"], f"collection {cid} compliant")
        v.expect_equal(sorted(row.get("reason_codes") or []), sorted(exp["reason_codes"]),
                       f"collection {cid} reason_codes")

    # ---------------------------------------------- stage 2: defect summary
    summary = [r for r in vlib.fetch_all(v.token, "ops_reports")
               if r.get("report") == "custody_defect_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary), 1, "custody_defect_summary row count")
    srow = summary[0]
    defective = [exp for exp in py.values() if not exp["compliant"]]
    v.expect_equal(srow.get("collections_audited"), len(py), "collections_audited")
    v.expect_equal(srow.get("compliant_count"), len(py) - len(defective), "compliant_count")
    v.expect_equal(srow.get("defective_count"), len(defective), "defective_count")
    v.expect_equal(srow.get("non_chronological"),
                   sum(1 for exp in py.values() if not exp["chronological"]), "non_chronological")
    v.expect_equal(srow.get("preservation_violations"),
                   sum(1 for exp in py.values() if not exp["preservation_before_collection"]),
                   "preservation_violations")
    v.expect_equal(srow.get("incomplete_entries"),
                   sum(1 for exp in py.values() if not exp["complete_entries"]), "incomplete_entries")
    v.expect_equal(srow.get("over_gap"),
                   sum(1 for exp in py.values() if exp["max_gap_days"] > gap_days), "over_gap")

    # ---------------------------------------------------- stage 3: ranking
    # rank order needs the collection ids: rebuild (span, id) pairs from py
    id_by_span = sorted(((exp["span_days"], int(cid)) for cid, exp in py.items() if not exp["compliant"]),
                        key=lambda t: (-t[0], t[1]))
    rank_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "custody_duration_rank" and r.get("batch_code") == batch]
    v.expect_equal(len(rank_rows), len(id_by_span), "custody_duration_rank row count")
    by_rank = {r.get("rank"): r for r in rank_rows}
    for rank, (span, cid) in enumerate(id_by_span, start=1):
        row = by_rank.get(rank)
        v.expect(row is not None, f"missing custody_duration_rank rank {rank}")
        v.expect_equal(str(row.get("collection_id")), str(cid), f"rank {rank} collection_id")
        v.expect_equal(row.get("span_days"), span, f"rank {rank} span_days")
        # SEVERITY-01: the ranking must reuse the stage-1 rows' own spans
        stage1 = by_id.get(str(cid))
        v.expect(stage1 is not None, f"rank {rank} has no matching custody_timeline row")
        v.expect_equal(row.get("span_days"), stage1.get("span_days"), f"rank {rank} reuses stage-1 span")

    # empty-array collections must get no row at all
    audited_ids = {str(c["id"]) for c in audited}
    v.expect_equal(len(by_id), len(audited_ids), "no rows outside the audited set")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
