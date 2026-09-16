#!/usr/bin/env python3
"""Alternative gold solution for 002_privilege_clawback_audit (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the Bates-range membership test, the status/coverage filters, and the
violation projection all run inside Postgres as a single SQL join (the
document's Bates prefix and numeric suffix are split with SQL regular
expressions), instead of REST fetch-all + Python filtering. Writes still go
through the public REST API. Idempotent for the same reasons as gold.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))

    # ---- SQL-first: membership + violations in one statement --------------
    # The document's Bates prefix and numeric suffix are split in SQL; a
    # document belongs to a production when the prefix matches and the number
    # falls inside [bates_start, bates_end]. Exposure follows PRIV-CLAWBACK-01:
    # finalised/served productions only, clawback-order-covered productions
    # excluded, privilege != none required.
    sql_rows = g.sql(
        "SELECT d.id AS document_id, d.doc_id, d.matter_id, d.bates_number, d.privilege, "
        "       p.id AS production_id, p.production_number, p.clawback_order_status "
        "FROM ediscovery_documents d "
        "JOIN ediscovery_productions p "
        "  ON substring(d.bates_number from '^[A-Za-z]+') = p.bates_prefix "
        " AND substring(d.bates_number from '[0-9]+$')::int BETWEEN p.bates_start AND p.bates_end "
        "WHERE p.status IN ('finalised', 'served')"
    )
    in_range = len(sql_rows)
    in_range_by_matter = {}
    for r in sql_rows:
        mid = r.get("matter_id")
        in_range_by_matter[mid] = in_range_by_matter.get(mid, 0) + 1

    # ---- Stage 2: privilege_log rows (same landing shape) ------------------
    try:
        existing_log = g.all("privilege_log")
    except RuntimeError:
        existing_log = []
    for r in existing_log:
        if r.get("event") == "clawback_required" and r.get("flagged_by") == "system":
            g.delete("privilege_log", r["id"])

    flagged_at = ep + "T00:00:00.000Z"
    for r in sql_rows:
        if r.get("privilege") in (None, "none"):
            continue
        if r.get("clawback_order_status") == "covered":
            continue
        priv = r.get("privilege")
        g.push("privilege_log", {
            "document_id": r["document_id"],
            "doc_id": r.get("doc_id"),
            "matter_id": r.get("matter_id"),
            "production_id": r["production_id"],
            "bates_number": r.get("bates_number"),
            "privilege": priv,
            "classification": priv,
            "event": "clawback_required",
            "flagged_at": flagged_at,
            "flagged_by": "system",
            "reason": (
                f"produced without a covered clawback order; "
                f"privilege classification: {priv} - clawback required under FRE 502(b)"
            ),
        })

    # ---- Stage 3: per-matter rollup from the written log rows --------------
    try:
        existing_reports = g.all("ops_reports")
    except RuntimeError:
        existing_reports = []
    for r in existing_reports:
        if r.get("batch_code") == batch and r.get("report") in ("privilege_clawback_audit", "clawback_followup"):
            g.delete("ops_reports", r["id"])

    logged = [r for r in g.all("privilege_log") if r.get("event") == "clawback_required"]
    logged_by_matter = {}
    for r in logged:
        logged_by_matter.setdefault(r.get("matter_id"), []).append(r)
    for matter_id, rows in logged_by_matter.items():
        total_produced = in_range_by_matter.get(matter_id, 0)
        g.push("ops_reports", {
            "report": "clawback_followup",
            "batch_code": batch,
            "matter_id": matter_id,
            "violation_count": len(rows),
            "earliest_flagged_at": flagged_at,
            "total_produced_documents": total_produced,
            "exposure_ratio_pct": round(100 * len(rows) / total_produced, 1) if total_produced else 0.0,
        })

    # ---- Stage 4: firm-wide summary row ------------------------------------
    prod_count_rows = g.sql(
        "SELECT COUNT(*) AS n FROM ediscovery_productions "
        "WHERE status IN ('finalised', 'served')"
    )
    productions_checked = int(prod_count_rows[0]["n"]) if prod_count_rows else 0
    g.push("ops_reports", {
        "report": "privilege_clawback_audit",
        "batch_code": batch,
        "productions_checked": productions_checked,
        "documents_in_range": in_range,
        "violations_found": len(logged),
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
