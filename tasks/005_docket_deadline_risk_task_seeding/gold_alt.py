#!/usr/bin/env python3
"""Alternative gold solution for 005_docket_deadline_risk_task_seeding (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
qualifying deadlines are selected by a single SQL statement (join + window
filter) instead of REST fetch-all + Python filtering, and the sweep is
traversed per matter (grouping deadlines by matter first) instead of
deadline-by-deadline. Writes still go through the public REST API. Idempotent
for the same reasons as gold.py."""
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
    window_days = int(g.nonce(field="docket_window_days"))
    urgent_hours = int(g.nonce(field="urgent_window_hours"))
    lead_days = int(g.nonce(field="prep_lead_days"))
    window_end = (ep_dt + timedelta(days=window_days)).isoformat()

    # ---- SQL-first: qualifying deadlines in one statement -------------------
    # Status upcoming, matter open, due within the policy window (both ends
    # inclusive). The urgency/lead derivations stay in Python (episode-row
    # knobs), but the candidate selection is the database's.
    qual_rows = g.sql(
        "SELECT d.id AS deadline_id, d.matter_id, d.title, d.due_date, d.assigned_to "
        "FROM deadlines d JOIN matters m ON m.id::text = d.matter_id #>> '{}' "
        "WHERE d.status = 'upcoming' AND m.status = 'open' "
        "AND d.due_date::date >= '" + ep + "'::date AND d.due_date::date <= '" + window_end + "'::date "
        "ORDER BY d.matter_id, d.id"
    )
    matters = {str(r["id"]): r for r in g.sql("SELECT * FROM matters")}
    tasks = g.all("tasks")
    deadlines = g.all("deadlines")

    # Existing prep rows indexed by (matter_id, title) across BOTH collections
    # (this firm's pre-migration data kept prep tasks alongside deadlines).
    existing_prep = {}
    for t in tasks:
        existing_prep[(str(t.get("matter_id")), t.get("title"))] = {"id": t["id"], "collection": "tasks", "row": t}
    for d in deadlines:
        title = d.get("title") or ""
        if title.startswith("Prepare for: "):
            existing_prep.setdefault((str(d.get("matter_id")), title),
                                     {"id": d["id"], "collection": "deadlines", "row": d})

    qualifying = []
    for row in qual_rows:
        mid = str(row.get("matter_id"))
        matter = matters.get(mid)
        if matter is None or matter.get("status") != "open":
            continue
        due = glib.Gold.dp(row.get("due_date"))
        if not due:
            continue
        due_dt = datetime.strptime(due, "%Y-%m-%d").date()
        urgent = (due_dt - ep_dt).days * 24 <= urgent_hours
        prep_due = (due_dt - timedelta(days=lead_days)).isoformat()
        priority = "urgent" if urgent else "high"
        qualifying.append((mid, urgent))
        title = f"Prepare for: {row.get('title')}"
        key = (mid, title)
        existing = existing_prep.get(key)
        if existing is not None:
            coll = existing["collection"]
            row0 = existing["row"]
            updates = {}
            if glib.Gold.dp(row0.get("due_date")) != prep_due:
                updates["due_date"] = prep_due + "T00:00:00.000Z"
            if row0.get("priority") != priority:
                updates["priority"] = priority
            if updates:
                g.update(coll, existing["id"], updates)
        else:
            g.push("tasks", {
                "matter_id": row.get("matter_id"),
                "title": title,
                "description": f"Preparation task auto-generated for upcoming deadline \"{row.get('title')}\" due {due}.",
                "assigned_to": row.get("assigned_to"),
                "due_date": prep_due + "T00:00:00.000Z",
                "priority": priority,
                "status": "open",
                "created_by": "system",
                "created_at": ep + "T00:00:00.000Z",
            })

    # ---- Stage 2: summary rows (per-matter grouping traversal) --------------
    try:
        existing_ops = g.all("ops_reports")
    except RuntimeError:
        existing_ops = []
    for r in existing_ops:
        if r.get("report") == "docket_risk_summary" and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])
    by_matter = {}
    for mid, urgent in qualifying:
        entry = by_matter.setdefault(mid, {"created": 0, "urgent": 0})
        entry["created"] += 1
        entry["urgent"] += 1 if urgent else 0
    for mid, counts in sorted(by_matter.items()):
        g.push("ops_reports", {
            "report": "docket_risk_summary", "batch_code": batch,
            "matter_id": mid, "prep_tasks_created": counts["created"], "urgent_count": counts["urgent"],
        })
    g.push("ops_reports", {
        "report": "docket_risk_summary", "batch_code": batch,
        "matter_id": "FIRM", "prep_tasks_created": len(qualifying),
        "urgent_count": sum(1 for _, u in qualifying if u),
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
