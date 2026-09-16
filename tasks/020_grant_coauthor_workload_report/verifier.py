#!/usr/bin/env python3
"""Verifier for 020_grant_coauthor_workload_report (v2).

Dual-path on every derived number: per-employee lead counts (SQL GROUP BY vs
Python), co-author membership (jsonb containment vs Python array-membership
testing — an employee who leads AND co-authors one application counts twice)
and billable hours are recomputed both ways and must agree with each other
and with the written rows. Hazard coverage: including departed EMP-006,
deduplicating roles, ranking by billable hours, or hardcoding a prose roster
all FAIL here. Idempotent across repeated gold runs; fails closed via
vlib.run.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    cutoff = int(vlib.get_nonce(v.token, field="workload_share_cutoff"))

    roster_rows = vlib.fetch_all(v.token, "staff_roster")
    active = sorted(
        (r for r in roster_rows
         if r.get("active_from") and vlib.dp(r["active_from"]) <= ep
         and (r.get("active_to") is None or (vlib.dp(r["active_to"]) or "9999") >= ep)),
        key=lambda r: str(r.get("employee_id")))
    roster = [str(r["employee_id"]) for r in active]
    roster_set = set(roster)
    supervisor = next(str(r["employee_id"]) for r in active if r.get("role") == "partner")

    applications = vlib.fetch_all(v.token, "grant_applications")
    entries = vlib.fetch_all(v.token, "time_entries")

    # ------------------------------------------------------ path 1: Python
    lead_count = {e: 0 for e in roster}
    co_count = {e: 0 for e in roster}
    for app in applications:
        lead = app.get("lead_author")
        if lead in lead_count:
            lead_count[lead] += 1
        for co in app.get("co_authors") or []:
            if co in co_count:
                co_count[co] += 1  # lead+co-author on one application counts twice
    py_hours = {e: 0.0 for e in roster}
    for e in entries:
        emp = str(e.get("employee_id"))
        if emp in py_hours:
            py_hours[emp] += e.get("hours") or 0
    total_involvements = sum(lead_count[e] + co_count[e] for e in roster)

    expected = {}
    for emp in roster:
        involvements = lead_count[emp] + co_count[emp]
        share = round(100.0 * involvements / total_involvements, 1) if total_involvements else 0.0
        expected[emp] = {"lead": lead_count[emp], "coauthor": co_count[emp],
                         "hours": py_hours[emp], "share": share}

    # ------------------------------------------------------ path 2: SQL
    sql_lead = {str(r["lead_author"]): int(r["cnt"]) for r in vlib.sql(
        v.token, "SELECT lead_author, COUNT(*) AS cnt FROM grant_applications GROUP BY lead_author")}
    id_list = ",".join(f"'{e}'" for e in roster)
    sql_hours = {str(r["employee_id"]): float(r["h"] or 0) for r in vlib.sql(
        v.token,
        "SELECT employee_id, COALESCE(SUM(hours), 0) AS h FROM time_entries "
        f"WHERE employee_id IN ({id_list}) GROUP BY employee_id")}
    sql_co = {}
    for emp in roster:
        rows_co = vlib.sql(
            v.token,
            "SELECT COUNT(*) AS cnt FROM grant_applications WHERE co_authors::jsonb "
            f"@> '\"{emp}\"'::jsonb")
        sql_co[emp] = int(rows_co[0]["cnt"])
    for emp in roster:
        v.expect_equal(sql_lead.get(emp, 0), expected[emp]["lead"],
                       f"dual-path: {emp} applications_as_lead (SQL vs Python)")
        v.expect_equal(sql_co.get(emp, 0), expected[emp]["coauthor"],
                       f"dual-path: {emp} applications_as_coauthor (SQL vs Python)")
        v.expect_cents(sql_hours.get(emp, 0.0), expected[emp]["hours"],
                       f"dual-path: {emp} billable_hours (SQL vs Python)")

    # ---------------------------------------------------- stage 1: rows
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "grant_workload" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(roster), "grant_workload row count")
    by_emp = {str(r.get("employee_id")): r for r in rows}
    v.expect("EMP-006" not in by_emp, "departed EMP-006 must not appear in the workload report")
    for emp in by_emp:
        v.expect(emp in roster_set, f"workload row for non-roster employee {emp}")
    for emp, exp in expected.items():
        row = by_emp.get(emp)
        v.expect(row is not None, f"missing grant_workload row for {emp}")
        v.expect_equal(row.get("applications_as_lead"), exp["lead"], f"{emp} applications_as_lead")
        v.expect_equal(row.get("applications_as_coauthor"), exp["coauthor"],
                       f"{emp} applications_as_coauthor")
        v.expect_cents(row.get("billable_hours"), exp["hours"], f"{emp} billable_hours")
        actual_share = row.get("grant_share_pct")
        v.expect(isinstance(actual_share, (int, float)) and abs(float(actual_share) - exp["share"]) <= 0.05,
                 f"{emp} grant_share_pct")

    # --------------------------------------------------- stage 2: ranking
    expected_ranked = sorted(((exp["share"], emp) for emp, exp in expected.items()),
                             key=lambda t: (-t[0], t[1]))
    rank_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "grant_workload_ranking" and r.get("batch_code") == batch]
    v.expect_equal(len(rank_rows), len(expected_ranked), "grant_workload_ranking row count")
    by_rank = {r.get("rank"): r for r in rank_rows}
    for rank, (share, emp) in enumerate(expected_ranked, start=1):
        row = by_rank.get(rank)
        v.expect(row is not None, f"missing grant_workload_ranking rank {rank}")
        v.expect_equal(str(row.get("employee_id")), emp, f"rank {rank} employee_id")
        v.expect_cents(row.get("grant_share_pct"), share, f"rank {rank} grant_share_pct")
        # SEVERITY-01: the ranking must reuse the stage-1 rows' own shares
        stage1 = by_emp.get(emp)
        v.expect(stage1 is not None, f"rank {rank} has no matching workload row")
        v.expect_cents(row.get("grant_share_pct"), stage1.get("grant_share_pct"),
                       f"rank {rank} reuses stage-1 share")

    # ------------------------------------------------ stage 3: capacity alerts
    live_tasks = vlib.fetch_all(v.token, "tasks")
    seed_tasks = vlib.seed_rows("tasks")
    seed_ids = {str(t["id"]) for t in seed_tasks}
    new_tasks = [t for t in live_tasks if str(t["id"]) not in seed_ids]
    expected_alerts = sorted(emp for emp, exp in expected.items() if exp["share"] > cutoff)
    expected_titles = sorted(f"Grant workload review: {emp}" for emp in expected_alerts)
    v.expect_equal(sorted(str(t.get("title")) for t in new_tasks), expected_titles,
                   "capacity-alert task set")
    for emp in expected_alerts:
        title = f"Grant workload review: {emp}"
        matches = [t for t in new_tasks if t.get("title") == title]
        v.expect_equal(len(matches), 1, f"capacity-alert task count for {emp}")
        t = matches[0]
        v.expect_equal(str(t.get("assigned_to")), supervisor, f"task {title} assigned_to")
        v.expect_equal(t.get("status"), "open", f"task {title} status")
        v.expect_equal(t.get("priority"), "high", f"task {title} priority")
    # no other tasks rows may be created or touched
    for t in seed_tasks:
        live = next((r for r in live_tasks if str(r["id"]) == str(t["id"])), None)
        v.expect(live is not None and vlib.row_eq(live, t), f"seeded task {t['id']} changed")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections", "ediscovery_documents",
        "ediscovery_productions", "grant_opportunities", "grant_applications", "grant_awards",
        "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
