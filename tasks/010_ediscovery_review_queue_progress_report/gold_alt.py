#!/usr/bin/env python3
"""gold_alt for 010_ediscovery_review_queue_progress_report (v2).

Same end-state as gold.py via a materially different path: the scoped matter
set, the per-matter review aggregates (with the legacy synonym folded in), and
the collected-item totals all come from single SQL statements instead of REST
fetch-all + Python filtering, and the priority queue is ranked from a SQL
read-back of the pushed progress rows. Writes still go through the REST API.
Idempotent like gold.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("ediscovery_progress", "ediscovery_priority_queue")


def r1(x):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def id_key(value):
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s)


def main():
    g = glib.Gold()
    batch = g.nonce()
    n = int(g.nonce(field="priority_queue_size"))

    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'REVIEW-STATUS-01' LIMIT 1"):
        raise RuntimeError("firm policy REVIEW-STATUS-01 missing")

    # ---- SQL-first scope + aggregation -------------------------------------
    scoped_rows = g.sql(
        "SELECT matter_id FROM ediscovery_holds WHERE status = 'active' "
        "UNION SELECT matter_id FROM ediscovery_productions "
        "WHERE status IN ('finalised', 'served')"
    )
    scoped = {str(r["matter_id"]) for r in scoped_rows if r.get("matter_id") is not None}

    # REVIEW-STATUS-01 in SQL: the hyphenated legacy synonym counts as
    # qc_complete (reviewed); anything else unrecognized falls out of the
    # counted set exactly as the policy's unreviewed bucket requires.
    doc_rows = g.sql(
        "SELECT matter_id, COUNT(*) AS total, "
        "SUM(CASE WHEN LOWER(review_status) IN "
        "  ('reviewed', 'qc_complete', 'qc-complete') THEN 1 ELSE 0 END) AS reviewed, "
        "SUM(CASE WHEN privilege IS NOT NULL AND privilege <> 'none' "
        "  THEN 1 ELSE 0 END) AS privileged "
        "FROM ediscovery_documents GROUP BY matter_id"
    )
    per = {}
    for r in doc_rows:
        if r.get("matter_id") is None:
            continue
        per[str(r["matter_id"])] = {
            "total": int(r["total"]),
            "reviewed": int(r["reviewed"] or 0),
            "privileged": int(r["privileged"] or 0),
        }
    items = {}
    for r in g.sql(
        "SELECT matter_id, SUM(item_count) AS items FROM ediscovery_collections "
        "WHERE matter_id IS NOT NULL GROUP BY matter_id"
    ):
        items[str(r["matter_id"])] = int(r["items"] or 0)

    # Idempotent report rows: delete this batch's own rows before rewriting.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in REPORTS:
            g.delete("ops_reports", r["id"])

    def pct_of(m):
        return r1(100.0 * m["reviewed"] / m["total"]) if m["total"] else 0.0

    for mid in sorted(scoped, key=id_key):
        m = per.get(mid, {"total": 0, "reviewed": 0, "privileged": 0})
        item_total = items.get(mid, 0)
        g.push("ops_reports", {
            "report": "ediscovery_progress", "batch_code": batch, "matter_id": mid,
            "total_documents": m["total"], "reviewed_count": m["reviewed"],
            "percent_reviewed": pct_of(m),
            "privileged_count": m["privileged"],
            "items_collected": item_total,
            "review_backlog": item_total - m["total"],
        })

    # ---- Priority queue from the pushed rows, read back via SQL ------------
    live = g.sql(
        "SELECT matter_id, percent_reviewed, review_backlog FROM ops_reports "
        "WHERE report = 'ediscovery_progress' AND batch_code = '" + batch + "'"
    )
    ranked = sorted(
        live,
        key=lambda r: (r1(r.get("percent_reviewed") or 0.0), id_key(r.get("matter_id"))),
    )[:n]
    for rank, row in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "ediscovery_priority_queue", "batch_code": batch,
            "rank": rank, "matter_id": row.get("matter_id"),
            "percent_reviewed": r1(row.get("percent_reviewed")),
            "review_backlog": row.get("review_backlog"),
        })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
