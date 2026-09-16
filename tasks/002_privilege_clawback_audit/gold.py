#!/usr/bin/env python3
"""Gold solution for 002_privilege_clawback_audit (v2, 4 stages). Run against
a FRESH container via the public REST API. Idempotent: privilege_log rows are
deleted (by the sweep's own event/flagged_by signature) and rewritten, and
ops_reports rows are deleted and rewritten keyed by batch_code; first-run
missing-table reads are guarded."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

BATES_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def parse_bates(bates_number):
    m = BATES_RE.match(bates_number or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))

    productions = g.all("ediscovery_productions")
    documents = g.all("ediscovery_documents")

    # ---- Stage 1: Bates-range membership + policy violations -------------
    # PRIV-CLAWBACK-01: only finalised/served productions carry exposure; a
    # produced document is a violation when its privilege field is not none
    # AND the production is NOT covered by a clawback order; a document in
    # range of both a covered and an uncovered production is a violation only
    # for the uncovered one; draft productions are never logged.
    in_range = 0
    in_range_by_matter = {}
    violations = []  # (doc, prod) on uncovered productions
    covered_hit_bates = set()
    for doc in documents:
        prefix, num = parse_bates(doc.get("bates_number"))
        if prefix is None:
            continue
        for prod in productions:
            if prod.get("bates_prefix") == prefix and prod["bates_start"] <= num <= prod["bates_end"]:
                if prod.get("status") in ("finalised", "served"):
                    in_range += 1
                    mid = doc.get("matter_id")
                    in_range_by_matter[mid] = in_range_by_matter.get(mid, 0) + 1
                    if doc.get("privilege") != "none":
                        if prod.get("clawback_order_status") == "covered":
                            covered_hit_bates.add(doc.get("bates_number"))
                        else:
                            violations.append((doc, prod))
                break

    # ---- Stage 2: privilege_log rows with classification + reason ---------
    try:
        existing_log = g.all("privilege_log")
    except RuntimeError:
        existing_log = []
    for r in existing_log:
        if r.get("event") == "clawback_required" and r.get("flagged_by") == "system":
            g.delete("privilege_log", r["id"])

    flagged_at = ep + "T00:00:00.000Z"
    for doc, prod in violations:
        priv = doc.get("privilege")
        g.push("privilege_log", {
            "document_id": doc["id"],
            "doc_id": doc.get("doc_id"),
            "matter_id": doc.get("matter_id"),
            "production_id": prod["id"],
            "bates_number": doc.get("bates_number"),
            "privilege": priv,
            "classification": priv,
            "event": "clawback_required",
            "flagged_at": flagged_at,
            "flagged_by": "system",
            "reason": (
                f"produced on {prod.get('production_number')} without a covered clawback order; "
                f"privilege classification: {priv} - clawback required under FRE 502(b)"
            ),
        })

    # ---- Stage 3: per-matter exposure rollup from the written log rows ----
    # Reads back the privilege_log rows just written (a wrong stage-1 Bates
    # match silently changes which matters get a followup row here).
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

    # ---- Stage 4: firm-wide summary row -----------------------------------
    g.push("ops_reports", {
        "report": "privilege_clawback_audit",
        "batch_code": batch,
        "productions_checked": len([p for p in productions if p.get("status") in ("finalised", "served")]),
        "documents_in_range": in_range,
        "violations_found": len(violations),
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
