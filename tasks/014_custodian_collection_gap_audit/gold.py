#!/usr/bin/env python3
"""Gold solution for 014_custodian_collection_gap_audit (v2). Run against a
FRESH container. Idempotent: this batch's ops_reports rows are deleted and
rewritten keyed by batch_code (first-run missing-table guarded).

Scope per HOLD-ACK-01 (read live from firm_policies): active holds issued at
least the episode row's gap_grace_days days before the episode date. A
custodian is covered only by a collection belonging to the hold's own matter --
a collection under a different matter does not cover the custodian. Aging
buckets derive from THIS batch's own gap rows (read back after the push)."""
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("collection_gap", "collection_gap_summary", "collection_gap_aging")


def bucket_for(days):
    if days < 30:
        return "under_30"
    if days <= 90:
        return "30_90"
    return "over_90"


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    grace = int(g.nonce(field="gap_grace_days"))

    # Policy gate: the hold scope rule lives in firm_policies.
    if not any(p.get("policy_id") == "HOLD-ACK-01" for p in g.all("firm_policies")):
        raise RuntimeError("firm policy HOLD-ACK-01 missing")

    holds = g.all("ediscovery_holds")
    collections = g.all("ediscovery_collections")

    # Coverage is per-MATTER: a custodian is covered for a hold only by a
    # collection belonging to the hold's own matter. A collection under a
    # different matter (a mis-scoped collection from a superseded matter) does
    # not cover the custodian.
    covered = defaultdict(set)
    for c in collections:
        if c.get("matter_id") is not None:
            covered[str(c["matter_id"])].add(str(c.get("custodian_id")))

    gaps = []
    for h in holds:
        if h.get("status") != "active":
            continue
        issued = glib.Gold.dp(h.get("issued_date"))
        if not issued:
            continue
        days = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days < grace:
            continue
        custodians_covered = covered.get(str(h.get("matter_id")), set())
        for cust in h.get("custodians") or []:
            if str(cust.get("contact_id")) not in custodians_covered:
                gaps.append((h, cust, days))

    # Idempotent report rows: delete this batch's own rows before rewriting.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in REPORTS:
            g.delete("ops_reports", r["id"])

    for h, cust, days in gaps:
        g.push("ops_reports", {
            "report": "collection_gap", "batch_code": batch,
            "hold_id": h["id"],
            "hold_number": h.get("hold_number"),
            "custodian_contact_id": cust.get("contact_id"),
            "custodian_name": cust.get("name"),
            "matter_id": h.get("matter_id"),
            "days_since_hold_issued": days,
        })

    g.push("ops_reports", {
        "report": "collection_gap_summary", "batch_code": batch,
        "gaps_found": len(gaps),
    })

    # ---- STAGE 2 (dependent on stage 1's own just-pushed rows) -------------
    # Buckets the collection_gap rows read BACK from ops_reports (not the
    # in-memory gap list) by days_since_hold_issued -- a wrong stage-1 gap
    # identification or day count silently mis-buckets here.
    live_gaps = [r for r in g.all("ops_reports")
                 if r.get("report") == "collection_gap" and r.get("batch_code") == batch]
    bucket_counts = {}
    for row in live_gaps:
        b = bucket_for(int(row.get("days_since_hold_issued") or 0))
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    for b in ("under_30", "30_90", "over_90"):
        if bucket_counts.get(b, 0) > 0:
            g.push("ops_reports", {
                "report": "collection_gap_aging", "batch_code": batch,
                "bucket": b, "gap_count": bucket_counts[b],
            })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
