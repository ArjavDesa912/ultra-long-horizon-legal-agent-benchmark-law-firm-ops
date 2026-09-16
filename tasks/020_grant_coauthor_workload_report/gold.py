#!/usr/bin/env python3
"""Gold solution for 020_grant_coauthor_workload_report (v2). Run against a
FRESH container. Idempotent: safe to run twice.

The roster comes from staff_roster (active at the episode date); departed
EMP-006 is excluded, so the 3 applications they lead are near-miss volume.
An application counts once per role a person holds on it: leading AND
co-authoring the same application is two involvements.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    cutoff = int(g.nonce(field="workload_share_cutoff"))

    roster_rows = g.all("staff_roster")
    active = sorted(
        (r for r in roster_rows
         if r.get("active_from") and r["active_from"][:10] <= ep
         and (r.get("active_to") is None or r["active_to"][:10] >= ep)),
        key=lambda r: str(r.get("employee_id")))
    roster = [str(r["employee_id"]) for r in active]
    supervisor = next(str(r["employee_id"]) for r in active if r.get("role") == "partner")

    applications = g.all("grant_applications")
    entries = g.all("time_entries")

    lead_count = {e: 0 for e in roster}
    coauthor_count = {e: 0 for e in roster}
    for app in applications:
        lead = app.get("lead_author")
        if lead in lead_count:
            lead_count[lead] += 1
        for co in app.get("co_authors") or []:
            if co in coauthor_count:
                coauthor_count[co] += 1  # counts even when the person also leads

    hours = {e: 0.0 for e in roster}
    for e in entries:
        emp = str(e.get("employee_id"))
        if emp in hours:
            hours[emp] += e.get("hours", 0) or 0

    total_involvements = sum(lead_count[e] + coauthor_count[e] for e in roster)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "grant_workload", "grant_workload_ranking"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1: one workload row per active roster employee ------------
    for emp in roster:
        involvements = lead_count[emp] + coauthor_count[emp]
        share = round(100.0 * involvements / total_involvements, 1) if total_involvements else 0.0
        g.push("ops_reports", {
            "report": "grant_workload", "batch_code": batch, "employee_id": emp,
            "applications_as_lead": lead_count[emp],
            "applications_as_coauthor": coauthor_count[emp],
            "billable_hours": hours[emp],
            "grant_share_pct": share,
        })

    # ---- STAGE 2: ranking, read back from the workload rows --------------
    live = [r for r in g.all("ops_reports")
            if r.get("report") == "grant_workload" and r.get("batch_code") == batch]
    ranked = sorted(live, key=lambda r: (-float(r.get("grant_share_pct") or 0), str(r.get("employee_id"))))
    for rank, row in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "grant_workload_ranking", "batch_code": batch, "rank": rank,
            "employee_id": row.get("employee_id"),
            "grant_share_pct": row.get("grant_share_pct"),
        })

    # ---- STAGE 3: capacity alerts above the cutoff -----------------------
    for row in live:
        if float(row.get("grant_share_pct") or 0) > cutoff:
            emp = str(row.get("employee_id"))
            title = f"Grant workload review: {emp}"
            payload = {
                "title": title, "assigned_to": supervisor,
                "status": "open", "priority": "high",
                "description": "Capacity review: grant-writing share above the episode cutoff.",
            }
            existing_task = next((t for t in g.all("tasks") if t.get("title") == title), None)
            if existing_task:
                g.update("tasks", existing_task["id"], payload)
            else:
                g.push("tasks", payload)

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
