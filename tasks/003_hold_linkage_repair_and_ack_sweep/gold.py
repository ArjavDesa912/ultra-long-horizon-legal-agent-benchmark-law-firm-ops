#!/usr/bin/env python3
"""Gold solution for 003_hold_linkage_repair_and_ack_sweep (v2, 4 stages).
Run against a FRESH container via the public REST API. Idempotent: linkage
corrections recompute to the same end-state, reminders dedupe on
(hold_id, custodian_name) across runs, and report rows are deleted and
rewritten keyed by batch_code (first-run missing-table reads guarded)."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    grace_days = int(g.nonce(field="hold_ack_grace_days"))

    holds = g.all("ediscovery_holds")
    hold_by_matter = {}
    for h in holds:
        hold_by_matter.setdefault(str(h.get("matter_id")), h)
    collections = g.all("ediscovery_collections")

    # ---- Stage 1: repair broken hold_id references ------------------------
    # Each matter has at most one hold (discoverable from the data); a
    # collection whose matter has no hold keeps its hold_id unchanged.
    for col in collections:
        hold = hold_by_matter.get(str(col.get("matter_id")))
        if hold is not None and str(col.get("hold_id")) != str(hold["id"]):
            g.update("ediscovery_collections", col["id"], {"hold_id": str(hold["id"])})

    # ---- Stage 2: reminder sweep per HOLD-ACK-01 ---------------------------
    # Reminders only for unacknowledged custodians of ACTIVE holds issued at
    # least hold_ack_grace_days before the episode date; never a second
    # reminder for the same (hold_id, custodian_name) pair even across runs;
    # historical reminder rows are never rewritten.
    try:
        existing_reminders = g.all("hold_reminders")
    except RuntimeError:
        existing_reminders = []
    sent_pairs = {(str(r.get("hold_id")), r.get("custodian_name")) for r in existing_reminders}

    for hold in holds:
        if hold.get("status") != "active":
            continue
        issued = glib.Gold.dp(hold.get("issued_date"))
        if not issued:
            continue
        issued_dt = datetime.strptime(issued, "%Y-%m-%d").date()
        if (ep_dt - issued_dt).days < grace_days:
            continue
        for cust in hold.get("custodians") or []:
            if cust.get("acknowledged"):
                continue
            key = (str(hold["id"]), cust.get("name"))
            if key in sent_pairs:
                continue
            g.push("hold_reminders", {
                # native id type (the seed stores hold_id as an int; pushing a
                # string splits the SQL GROUP BY into two groups per hold)
                "hold_id": hold["id"],
                "hold_number": hold.get("hold_number"),
                "custodian_name": cust.get("name"),
                "custodian_email": cust.get("email"),
                "matter_id": hold.get("matter_id"),
                "sent_at": ep + "T00:00:00.000Z",
                "message": f"Reminder: please acknowledge legal hold {hold.get('hold_number')}.",
            })
            sent_pairs.add(key)

    # ---- Stage 3: hold_coverage report (post-repair linkage) ---------------
    # collections_linked counts collections whose hold_id NOW points at the
    # hold -- a wrong stage-1 repair silently mis-attributes coverage here.
    try:
        existing_reports = g.all("ops_reports")
    except RuntimeError:
        existing_reports = []
    for r in existing_reports:
        if r.get("batch_code") == batch and r.get("report") in ("hold_coverage", "hold_exposure_by_matter"):
            g.delete("ops_reports", r["id"])

    live_collections = g.all("ediscovery_collections")
    linked_count = {}
    for col in live_collections:
        hold = hold_by_matter.get(str(col.get("matter_id")))
        if hold is not None and str(col.get("hold_id")) == str(hold["id"]):
            linked_count[str(hold["id"])] = linked_count.get(str(hold["id"]), 0) + 1

    reminders_now = g.all("hold_reminders")
    reminders_by_hold = {}
    for r in reminders_now:
        reminders_by_hold[str(r.get("hold_id"))] = reminders_by_hold.get(str(r.get("hold_id")), 0) + 1

    for hold in holds:
        if hold.get("status") == "superseded":
            continue  # HOLD-ACK-01: superseded holds are excluded from coverage
        hid = str(hold["id"])
        g.push("ops_reports", {
            "report": "hold_coverage",
            "batch_code": batch,
            "hold_id": hid,
            "hold_number": hold.get("hold_number"),
            "collections_linked": linked_count.get(hid, 0),
            "custodians_total": len(hold.get("custodians") or []),
            "reminders_outstanding": reminders_by_hold.get(hid, 0),
        })

    # ---- Stage 4: per-matter exposure rollup --------------------------------
    matter_stats = {}
    for hold in holds:
        if hold.get("status") == "superseded":
            continue
        mid = str(hold.get("matter_id"))
        stats = matter_stats.setdefault(mid, {"holds": 0, "custodians": 0, "unacknowledged": 0, "reminders": 0})
        stats["holds"] += 1
        custodians = hold.get("custodians") or []
        stats["custodians"] += len(custodians)
        stats["unacknowledged"] += sum(1 for c in custodians if not c.get("acknowledged"))
        stats["reminders"] += reminders_by_hold.get(str(hold["id"]), 0)
    for matter_id, stats in matter_stats.items():
        g.push("ops_reports", {
            "report": "hold_exposure_by_matter",
            "batch_code": batch,
            "matter_id": matter_id,
            "holds_count": stats["holds"],
            "custodians_total": stats["custodians"],
            "unacknowledged_count": stats["unacknowledged"],
            "reminders_outstanding": stats["reminders"],
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
