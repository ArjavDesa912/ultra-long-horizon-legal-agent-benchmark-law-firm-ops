#!/usr/bin/env python3
"""Alternative gold solution for 003_hold_linkage_repair_and_ack_sweep (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the hold/collection/reminder reads and the post-repair coverage aggregation
run as SQL statements through g.sql (GROUP BY over a join instead of REST
fetch-all + Python grouping), and the reminder sweep iterates holds ordered
by hold_number rather than collection order. Writes still go through the
public REST API. Idempotent for the same reasons as gold.py."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    grace_days = int(g.nonce(field="hold_ack_grace_days"))

    # ---- SQL-first reads ----------------------------------------------------
    holds = g.sql("SELECT * FROM ediscovery_holds ORDER BY hold_number")
    hold_by_matter = {}
    for h in holds:
        hold_by_matter.setdefault(str(h.get("matter_id")), h)
    collections = g.sql("SELECT * FROM ediscovery_collections ORDER BY id")

    # ---- Stage 1: repair broken hold_id references --------------------------
    for col in collections:
        hold = hold_by_matter.get(str(col.get("matter_id")))
        if hold is not None and str(col.get("hold_id")) != str(hold["id"]):
            g.update("ediscovery_collections", col["id"], {"hold_id": str(hold["id"])})

    # ---- Stage 2: reminder sweep per HOLD-ACK-01 ----------------------------
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

    # ---- Stage 3: hold_coverage via SQL GROUP BY ----------------------------
    try:
        existing_reports = g.all("ops_reports")
    except RuntimeError:
        existing_reports = []
    for r in existing_reports:
        if r.get("batch_code") == batch and r.get("report") in ("hold_coverage", "hold_exposure_by_matter"):
            g.delete("ops_reports", r["id"])

    # Post-repair linkage aggregated in SQL: collections grouped by the hold
    # their (corrected) hold_id points at, joined back to the holds table.
    linked_rows = g.sql(
        "SELECT c.hold_id AS hold_id, COUNT(*) AS n "
        "FROM ediscovery_collections c "
        "JOIN ediscovery_holds h ON h.id::text = c.hold_id::text "
        "WHERE h.status <> 'superseded' "
        "GROUP BY c.hold_id"
    )
    linked_count = {str(r["hold_id"]): int(r["n"]) for r in linked_rows}

    reminder_rows = g.sql(
        "SELECT hold_id, COUNT(*) AS n FROM hold_reminders GROUP BY hold_id"
    )
    reminders_by_hold = {str(r["hold_id"]): int(r["n"]) for r in reminder_rows}

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
