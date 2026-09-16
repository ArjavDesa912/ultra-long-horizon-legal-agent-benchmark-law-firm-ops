#!/usr/bin/env python3
"""Alternate gold for 020_grant_coauthor_workload_report (v2).

Same end-state as gold.py, materially different path: the lead-author counts
and co-author membership come from SQL (GROUP BY lead_author; jsonb
array-membership via @>), and the roster read filters on the same active
rule. Same report contract, same ranking, same capacity alerts.
"""
import os
import sys

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
    roster_set = set(roster)
    supervisor = next(str(r["employee_id"]) for r in active if r.get("role") == "partner")

    # SQL-first counting: leads via GROUP BY, co-authors via jsonb membership.
    lead_rows = g.sql("SELECT lead_author, COUNT(*) AS cnt FROM grant_applications GROUP BY lead_author")
    lead_count = {e: 0 for e in roster}
    for r in lead_rows:
        emp = str(r["lead_author"])
        if emp in roster_set:
            lead_count[emp] = int(r["cnt"])
    coauthor_count = {e: 0 for e in roster}
    for emp in roster:
        hit = g.sql(
            f"SELECT COUNT(*) AS cnt FROM grant_applications WHERE co_authors::jsonb @> '\"{emp}\"'")
        coauthor_count[emp] = int(hit[0]["cnt"]) if hit else 0

    hours = {e: 0.0 for e in roster}
    emp_list = ",".join(f"'{e}'" for e in roster)
    for r in g.sql("SELECT employee_id, COALESCE(SUM(hours), 0) AS h FROM time_entries "
                   f"WHERE employee_id IN ({emp_list}) GROUP BY employee_id"):
        if str(r["employee_id"]) in roster_set:
            hours[str(r["employee_id"])] = float(r["h"] or 0)

    total_involvements = sum(lead_count[e] + coauthor_count[e] for e in roster)

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
                "grant_workload", "grant_workload_ranking"):
            g.delete("ops_reports", r["id"])

    # ---- STAGE 1 ---------------------------------------------------------
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

    # ---- STAGE 2 (read back) --------------------------------------------
    live = [r for r in g.all("ops_reports")
            if r.get("report") == "grant_workload" and r.get("batch_code") == batch]
    ranked = sorted(live, key=lambda r: (-float(r.get("grant_share_pct") or 0), str(r.get("employee_id"))))
    for rank, row in enumerate(ranked, start=1):
        g.push("ops_reports", {
            "report": "grant_workload_ranking", "batch_code": batch, "rank": rank,
            "employee_id": row.get("employee_id"),
            "grant_share_pct": row.get("grant_share_pct"),
        })

    # ---- STAGE 3 ---------------------------------------------------------
    all_tasks = g.all("tasks")
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
