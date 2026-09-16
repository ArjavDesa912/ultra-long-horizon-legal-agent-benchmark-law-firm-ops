#!/usr/bin/env python3
"""Gold solution for 010_ediscovery_review_queue_progress_report (v2).

Scope: matters with an active hold OR a finalised/served production. Review
vocabulary normalized per REVIEW-STATUS-01 (legacy 'qc-complete' counts as
qc_complete, i.e. reviewed; unrecognized values count as unreviewed). One
ediscovery_progress row per in-scope matter (including matters with zero
documents -- their backlog is the collected-items total), then a bottom-N
priority queue (N = episode row's priority_queue_size) ranked by percent
reviewed ascending, ties by lowest matter id (numeric-aware), derived from
THIS batch's own rows read back after the push. Idempotent via
delete-then-rewrite keyed by batch_code."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("ediscovery_progress", "ediscovery_priority_queue")


def r1(x):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def norm_status(value):
    """REVIEW-STATUS-01: four recognized states; the hyphenated legacy
    synonym 'qc-complete' counts as qc_complete; anything unrecognized
    counts as unreviewed."""
    s = str(value or "").strip().lower()
    if s == "qc-complete":
        return "qc_complete"
    if s in ("unreviewed", "in_review", "reviewed", "qc_complete"):
        return s
    return "unreviewed"


def id_key(value):
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s)


def main():
    g = glib.Gold()
    batch = g.nonce()
    n = int(g.nonce(field="priority_queue_size"))

    # Policy gate: the vocabulary normalization lives in firm_policies.
    if not any(p.get("policy_id") == "REVIEW-STATUS-01" for p in g.all("firm_policies")):
        raise RuntimeError("firm policy REVIEW-STATUS-01 missing")

    holds = g.all("ediscovery_holds")
    prods = g.all("ediscovery_productions")
    docs = g.all("ediscovery_documents")
    cols = g.all("ediscovery_collections")

    # ---- Scope: active hold OR finalised/served production ----------------
    scoped = {str(h.get("matter_id")) for h in holds if h.get("status") == "active"}
    scoped |= {str(p.get("matter_id")) for p in prods if p.get("status") in ("finalised", "served")}

    # ---- Per-matter review aggregates (normalized vocabulary) -------------
    per = {}
    for d in docs:
        mid = str(d.get("matter_id"))
        m = per.setdefault(mid, {"total": 0, "reviewed": 0, "privileged": 0})
        m["total"] += 1
        if norm_status(d.get("review_status")) in ("reviewed", "qc_complete"):
            m["reviewed"] += 1
        if (d.get("privilege") or "none") != "none":
            m["privileged"] += 1
    items = {}
    for c in cols:
        mid = str(c.get("matter_id"))
        items[mid] = items.get(mid, 0) + int(c.get("item_count") or 0)

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

    # ---- Priority queue from THIS batch's own rows (read back) ------------
    live_progress = [r for r in g.all("ops_reports")
                     if r.get("report") == "ediscovery_progress" and r.get("batch_code") == batch]
    ranked = sorted(
        live_progress,
        key=lambda r: (r1(r.get("percent_reviewed") or 0.0), id_key(r.get("matter_id"))),
    )[:n]
    for rank, row in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "ediscovery_priority_queue", "batch_code": batch,
            "rank": rank, "matter_id": row.get("matter_id"),
            "percent_reviewed": r1(row.get("percent_reviewed")),
            "review_backlog": row.get("review_backlog"),
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
