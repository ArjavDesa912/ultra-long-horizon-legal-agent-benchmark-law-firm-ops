#!/usr/bin/env python3
"""Gold solution for 018_chain_of_custody_timeline_audit (v2). Run against a
FRESH container. Idempotent: safe to run twice.

Scope: every ediscovery_collections row with a non-empty chain_of_custody
array. The five rules come from firm policy CUSTODY-01; the gap tolerance
comes from the episode row's custody_gap_days. Adjacent gaps are measured in
array order as signed day differences (next minus current); a gap violates
the tolerance only when it exceeds custody_gap_days. The actor of record is
the entry's 'actor' field, or 'who' on the hand-authored rows.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

ADMIN_ACTIONS = ("copy", "export")


def analyze(chain, gap_days):
    dated = []
    for e in chain:
        w = glib.Gold.dp(e.get("when"))
        d = datetime.strptime(w, "%Y-%m-%d").date() if w else None
        dated.append((e, d))
    whens = [d for _, d in dated]
    chronological = all(whens[i] <= whens[i + 1] for i in range(len(whens) - 1))

    complete = True
    for e, d in dated:
        actor = e.get("actor") or e.get("who")
        action = e.get("action")
        if not actor or not action or d is None:
            complete = False

    # Preservation rule (CUSTODY-01 rule 2 + rule 5's administrative exemption):
    # a preservation action must exist no later than the earliest collection
    # action; with no collection action there is nothing to precede, and a
    # trail made only of exempt administrative actions needs no preservation.
    pres_whens = [d for e, d in dated if e.get("action") == "preservation"]
    coll_only = [d for e, d in dated if e.get("action") == "collection"]
    if not coll_only:
        pres_ok = True  # nothing for preservation to precede
    else:
        pres_ok = bool(pres_whens) and min(pres_whens) <= min(coll_only)

    gaps = [whens[i] - whens[i - 1] for i in range(1, len(whens))] if len(whens) > 1 else []
    gap_days_list = [g.days for g in gaps]
    gap_ok = all(gd <= gap_days for gd in gap_days_list)
    max_gap = max(gap_days_list) if gap_days_list else 0
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
        "chronological": chronological,
        "preservation_before_collection": pres_ok,
        "complete_entries": complete,
        "max_gap_days": max_gap,
        "span_days": span,
        "compliant": chronological and pres_ok and complete and gap_ok,
        "reason_codes": reasons,
    }


def main():
    g = glib.Gold()
    batch = g.nonce()
    gap_days = int(g.nonce(field="custody_gap_days"))

    collections = [c for c in g.all("ediscovery_collections") if c.get("chain_of_custody")]

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "custody_timeline", "custody_defect_summary", "custody_duration_rank"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: one timeline row per audited collection ----------------
    verdicts = {}
    for c in collections:
        res = analyze(c["chain_of_custody"], gap_days)
        verdicts[str(c["id"])] = res
        g.push("ops_reports", {
            "report": "custody_timeline", "batch_code": batch,
            "collection_id": c["id"], "collection_ref": c.get("collection_id"),
            "entries_count": len(c["chain_of_custody"]),
            "chronological": res["chronological"],
            "preservation_before_collection": res["preservation_before_collection"],
            "complete_entries": res["complete_entries"],
            "max_gap_days": res["max_gap_days"],
            "span_days": res["span_days"],
            "compliant": res["compliant"],
            "reason_codes": res["reason_codes"],
        })

    # ---- STAGE 2: defect summary, read back from the timeline rows -------
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
        "over_gap": sum(1 for r in live
                        if (r.get("max_gap_days") or 0) > gap_days),
    })

    # ---- STAGE 3: rank the defective collections by span -----------------
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
