#!/usr/bin/env python3
"""Alternate gold for 018_chain_of_custody_timeline_audit (v2).

Same end-state as gold.py, materially different path: every per-collection
verdict comes from ONE SQL jsonb aggregation over the live collections
(jsonb_array_elements + LAG window for the gap rule), instead of REST reads
plus Python per-entry analysis. Idempotent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

SQL_VERDICTS = """
WITH exp AS (
    SELECT c.id AS cid, c.collection_id AS ref, t.idx, t.e,
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
    SELECT e.*,
           LAG(e.wd) OVER (PARTITION BY e.cid ORDER BY e.idx) AS prev_wd
    FROM exp e
),
agg AS (
    SELECT w.cid, w.ref,
           COUNT(*) AS entries,
           MIN(w.wd) AS first_w,
           MAX(w.wd) AS last_w,
           BOOL_AND(w.prev_wd IS NULL OR w.wd >= w.prev_wd) AS chronological,
           BOOL_AND(w.action IS NOT NULL AND w.actor IS NOT NULL) AS complete_entries,
           MAX(CASE WHEN w.prev_wd IS NULL THEN 0 ELSE w.wd - w.prev_wd END) AS max_gap,
           MIN(CASE WHEN w.action = 'preservation' THEN w.wd END) AS pres_w,
           MIN(CASE WHEN w.action = 'collection' THEN w.wd END) AS coll_w
    FROM w w
    GROUP BY w.cid, w.ref
)
SELECT cid, ref, entries, first_w, last_w, (last_w - first_w) AS span_days,
       chronological, complete_entries, max_gap, pres_w, coll_w
FROM agg
"""


def main():
    g = glib.Gold()
    batch = g.nonce()
    gap_days = int(g.nonce(field="custody_gap_days"))

    verdicts = {}
    for r in g.sql(SQL_VERDICTS):
        cid = str(r["cid"])
        chronological = bool(r["chronological"])
        complete = bool(r["complete_entries"])
        max_gap = int(r["max_gap"] or 0)
        pres_w = r["pres_w"]
        coll_w = r["coll_w"]
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
        verdicts[cid] = {
            "ref": r["ref"], "entries": int(r["entries"]),
            "chronological": chronological,
            "preservation_before_collection": pres_ok,
            "complete_entries": complete,
            "max_gap_days": max_gap,
            "span_days": int(r["span_days"] or 0),
            "compliant": chronological and pres_ok and complete and gap_ok,
            "reason_codes": reasons,
        }

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "custody_timeline", "custody_defect_summary", "custody_duration_rank"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
    for cid, res in verdicts.items():
        g.push("ops_reports", {
            "report": "custody_timeline", "batch_code": batch,
            "collection_id": int(cid) if cid.isdigit() else cid,
            "collection_ref": res["ref"],
            "entries_count": res["entries"],
            "chronological": res["chronological"],
            "preservation_before_collection": res["preservation_before_collection"],
            "complete_entries": res["complete_entries"],
            "max_gap_days": res["max_gap_days"],
            "span_days": res["span_days"],
            "compliant": res["compliant"],
            "reason_codes": res["reason_codes"],
        })

    # ---- STAGE 2 (read back) --------------------------------------------
    live = [r for r in g.all("ops_reports")
            if r.get("report") == "custody_timeline" and r.get("batch_code") == batch]
    defective = [r for r in live if not r.get("compliant")]
    g.push("ops_reports", {
        "report": "custody_defect_summary", "batch_code": batch,
        "collections_audited": len(live),
        "compliant_count": len(live) - len(defective),
        "defective_count": len(defective),
        "non_chronological": sum(1 for r in live if not r.get("chronological")),
        "preservation_violations": sum(1 for r in live if not r.get("preservation_before_collection")),
        "incomplete_entries": sum(1 for r in live if not r.get("complete_entries")),
        "over_gap": sum(1 for r in live if (r.get("max_gap_days") or 0) > gap_days),
    })

    # ---- STAGE 3 ---------------------------------------------------------
    ranked = sorted(defective, key=lambda r: (-(r.get("span_days") or 0), int(r.get("collection_id"))))
    for rank, row in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "custody_duration_rank", "batch_code": batch, "rank": rank,
            "collection_id": row.get("collection_id"),
            "collection_ref": row.get("collection_ref"),
            "span_days": row.get("span_days"),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
