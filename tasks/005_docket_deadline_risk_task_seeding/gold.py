#!/usr/bin/env python3
"""Gold solution for 005_docket_deadline_risk_task_seeding (v2). Run against a
FRESH container via the public REST API. Idempotent: prep-task creation is
keyed on (matter_id, title) and the stale pre-existing prep row is updated in
place; report rows are deleted and rewritten keyed by batch_code."""
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

    matters = {str(m["id"]): m for m in g.all("matters")}
    deadlines = g.all("deadlines")
    tasks = g.all("tasks")

    # ---- Stage 1: the sweep (DOCKET-01) ------------------------------------
    # Qualifying deadline: status upcoming, matter open, due within the
    # episode row's docket_window_days after the episode date (both ends
    # inclusive). Each gets exactly one prep task titled
    # 'Prepare for: <deadline title>'; an existing task with that exact title
    # for the matter is UPDATED to the policy-derived values, never
    # duplicated. Deadlines on suspended/closed matters or outside the window
    # never get tasks.
    existing_prep = {}
    for t in tasks:
        key = (str(t.get("matter_id")), t.get("title"))
        existing_prep.setdefault(key, t)
    # This firm's pre-migration data also kept prep tasks alongside deadlines;
    # search there too so an existing prep row is updated, not duplicated.
    for d in deadlines:
        title = d.get("title") or ""
        if title.startswith("Prepare for: "):
            key = (str(d.get("matter_id")), title)
            existing_prep.setdefault(key, {"id": d["id"], "collection": "deadlines"})

    qualifying = []  # (matter_id, urgent) for every deadline in the risk window
    for d in deadlines:
        if d.get("status") != "upcoming":
            continue
        matter = matters.get(str(d.get("matter_id")))
        if matter is None or matter.get("status") != "open":
            continue
        due = glib.Gold.dp(d.get("due_date"))
        if not due or not (ep <= due <= window_end):
            continue
        due_dt = datetime.strptime(due, "%Y-%m-%d").date()
        urgent = (due_dt - ep_dt).days * 24 <= urgent_hours
        prep_due = (due_dt - timedelta(days=lead_days)).isoformat()
        priority = "urgent" if urgent else "high"
        qualifying.append((str(d.get("matter_id")), urgent))
        title = f"Prepare for: {d.get('title')}"
        key = (str(d.get("matter_id")), title)
        existing = existing_prep.get(key)
        if existing is not None:
            # DOCKET-01: update the existing prep task to the policy-derived
            # values instead of creating a duplicate.
            coll = existing.get("collection", "tasks")
            updates = {}
            if glib.Gold.dp(existing.get("due_date")) != prep_due:
                updates["due_date"] = prep_due + "T00:00:00.000Z"
            if existing.get("priority") != priority:
                updates["priority"] = priority
            if updates:
                g.update(coll, existing["id"], updates)
        else:
            g.push("tasks", {
                "matter_id": d.get("matter_id"),
                "title": title,
                "description": f"Preparation task auto-generated for upcoming deadline \"{d.get('title')}\" due {due}.",
                "assigned_to": d.get("assigned_to"),
                "due_date": prep_due + "T00:00:00.000Z",
                "priority": priority,
                "status": "open",
                "created_by": "system",
                "created_at": ep + "T00:00:00.000Z",
            })

    # ---- Stage 2: summary rows derived from the sweep -----------------------
    # Reports on EVERY qualifying deadline (whichever run created/updated its
    # prep task), not a run-local delta -- a re-run reports the same totals.
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
    for mid, counts in by_matter.items():
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
