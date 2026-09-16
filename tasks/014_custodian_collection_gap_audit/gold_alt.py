#!/usr/bin/env python3
"""gold_alt for 014_custodian_collection_gap_audit (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the custodian-coverage map is resolved inside Postgres (a GROUP BY over
ediscovery_collections keyed by matter and custodian) instead of a REST
fetch-all + Python set build, the in-scope hold set is filtered in SQL, and
the aging buckets are read back via a SQL GROUP BY over this batch's own gap
rows. Writes still go through the public REST API. Idempotent like gold.py."""
import os
import sys
from collections import defaultdict
from datetime import datetime
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("collection_gap", "collection_gap_summary", "collection_gap_aging")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    grace = int(g.nonce(field="gap_grace_days"))

    # Policy gate via SQL: the hold scope rule lives in firm_policies.
    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'HOLD-ACK-01' LIMIT 1"):
        raise RuntimeError("firm policy HOLD-ACK-01 missing from firm_policies")

    # SQL-first: every (matter, custodian) coverage pair that actually exists,
    # plus the in-scope holds (active, in effect at least gap_grace_days).
    coverage_rows = g.sql(
        "SELECT matter_id::text AS mid, custodian_id::text AS cust, COUNT(*) AS n "
        "FROM ediscovery_collections WHERE matter_id IS NOT NULL "
        "GROUP BY matter_id::text, custodian_id::text"
    )
    covered = defaultdict(set)
    for r in coverage_rows:
        covered[str(r["mid"])].add(str(r["cust"]))

    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    hold_rows = g.sql(
        "SELECT id::text AS hid, hold_number, matter_id::text AS mid, "
        "       issued_date::date AS issued, custodians "
        "FROM ediscovery_holds WHERE status = 'active'"
    )
    gaps = []
    for h in hold_rows:
        issued = glib.Gold.dp(h.get("issued"))
        if not issued:
            continue
        days = (ep_dt - datetime.strptime(issued, "%Y-%m-%d").date()).days
        if days < grace:
            continue
        for cust in (h.get("custodians") or []):
            if str(cust.get("contact_id")) not in covered.get(str(h.get("mid")), set()):
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
            "hold_id": h["hid"],
            "hold_number": h.get("hold_number"),
            "custodian_contact_id": cust.get("contact_id"),
            "custodian_name": cust.get("name"),
            "matter_id": h.get("mid"),
            "days_since_hold_issued": days,
        })
    g.push("ops_reports", {
        "report": "collection_gap_summary", "batch_code": batch,
        "gaps_found": len(gaps),
    })

    # ---- STAGE 2: buckets from the pushed rows, read back via SQL ---------
    live_gap_rows = g.sql(
        "SELECT days_since_hold_issued FROM ops_reports "
        "WHERE report = 'collection_gap' AND batch_code = '" + batch + "'"
    )
    bucket_counts = {}
    for r in live_gap_rows:
        d = int(r.get("days_since_hold_issued") or 0)
        b = "under_30" if d < 30 else ("30_90" if d <= 90 else "over_90")
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    for b in ("under_30", "30_90", "over_90"):
        if bucket_counts.get(b, 0) > 0:
            g.push("ops_reports", {
                "report": "collection_gap_aging", "batch_code": batch,
                "bucket": b, "gap_count": bucket_counts[b],
            })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
