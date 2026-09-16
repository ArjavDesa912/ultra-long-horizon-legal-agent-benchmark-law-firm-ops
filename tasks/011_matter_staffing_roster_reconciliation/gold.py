#!/usr/bin/env python3
"""Gold solution for 011_matter_staffing_roster_reconciliation (v2). Run
against a FRESH container. Idempotent: the roster repair recomputes to the
same end-state (a second run finds no gaps left) and report rows are deleted
and rewritten keyed by batch_code (first-run missing-table guarded).

The governing rules are read from data, not narrated: staff_roster decides who
is active at the episode date (REF-INTEG-01's employee rule), so assignments
to departed staff are excluded from the append set and diagnosed instead."""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("staffing_reconciliation", "staffing_addition_detail", "staffing_diagnosis")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))

    # Policy gate: the active-roster rule lives in firm_policies.
    if not any(p.get("policy_id") == "REF-INTEG-01" for p in g.all("firm_policies")):
        raise RuntimeError("firm policy REF-INTEG-01 missing")

    roster = g.all("staff_roster")
    matters = g.all("matters")
    deadlines = g.all("deadlines")
    tasks = g.all("tasks")

    def is_active(emp_row):
        """staff_roster activity at the episode date (active_to in the past
        means departed -- their assignments are excluded, never appended)."""
        frm = glib.Gold.dp(emp_row.get("active_from"))
        to = glib.Gold.dp(emp_row.get("active_to"))
        if frm and ep < frm:
            return False
        if to and ep > to:
            return False
        return True

    roster_by_id = {str(r.get("employee_id")): r for r in roster}

    # ---- Stage 1: reconcile team_members against actual assignments -------
    assigned_by_matter = defaultdict(set)
    for row in deadlines:
        if row.get("assigned_to") and row.get("matter_id") is not None:
            assigned_by_matter[str(row["matter_id"])].add(str(row["assigned_to"]))
    for row in tasks:
        if row.get("assigned_to") and row.get("matter_id") is not None:
            assigned_by_matter[str(row["matter_id"])].add(str(row["assigned_to"]))

    matters_updated = 0
    additions_total = 0
    updated_ids = {}  # matter_id -> (matter_number, appended employee ids)
    for m in matters:
        mid = str(m["id"])
        team = m.get("team_members") or []
        have = {t.get("employee_id") for t in team}
        gap = sorted(
            emp for emp in assigned_by_matter.get(mid, set())
            if emp in roster_by_id and is_active(roster_by_id[emp]) and emp not in have
        )
        if not gap:
            continue
        new_team = team + [{"employee_id": emp, "name": emp, "role": "Contributor"}
                           for emp in gap]
        g.update("matters", m["id"], {"team_members": new_team})
        matters_updated += 1
        additions_total += len(gap)
        updated_ids[mid] = (m.get("matter_number"), gap)

    # Assignments the active-roster rule excludes (departed staff): never
    # appended, each diagnosed as latent_organizational.
    excluded = []
    for coll, rows in (("deadlines", deadlines), ("tasks", tasks)):
        for row in rows:
            emp_row = roster_by_id.get(str(row.get("assigned_to") or ""))
            if emp_row is not None and not is_active(emp_row):
                excluded.append((coll, row))

    # Idempotent report rows: this batch's prior rows are reused verbatim where
    # they carry sweep findings (the counters and per-matter detail describe the
    # gaps found at sweep time, which a second run can no longer re-derive from
    # the now-repaired live state), then deleted and rewritten keyed by batch.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    prior_by_report = {}
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in REPORTS:
            prior_by_report.setdefault(r["report"], []).append(r)
            g.delete("ops_reports", r["id"])

    prior_summary = (prior_by_report.get("staffing_reconciliation") or [None])[0]
    g.push("ops_reports", {
        "report": "staffing_reconciliation", "batch_code": batch,
        "matters_updated": int(prior_summary["matters_updated"]) if prior_summary else matters_updated,
        "additions_total": int(prior_summary["additions_total"]) if prior_summary else additions_total,
    })

    # ---- STAGE 2 (dependent on stage 1's writes actually landing) ----------
    # Re-reads matters LIVE (post-update) rather than trusting the in-memory
    # new_team -- a stage-1 write that silently didn't land shows up here as a
    # wrong team_size_after.
    live_matters = {str(m["id"]): m for m in g.all("matters")}
    prior_details = prior_by_report.get("staffing_addition_detail") or []
    if prior_details:
        for r in sorted(prior_details, key=lambda x: str(x.get("matter_id"))):
            mid = str(r.get("matter_id"))
            g.push("ops_reports", {
                "report": "staffing_addition_detail", "batch_code": batch,
                "matter_id": r.get("matter_id"), "matter_number": r.get("matter_number"),
                "added_employee_ids": r.get("added_employee_ids"),
                "team_size_after": len((live_matters.get(mid) or {}).get("team_members") or []),
            })
    else:
        for mid, (matter_number, added) in updated_ids.items():
            live_team = live_matters.get(mid, {}).get("team_members") or []
            g.push("ops_reports", {
                "report": "staffing_addition_detail", "batch_code": batch,
                "matter_id": mid, "matter_number": matter_number,
                "added_employee_ids": added, "team_size_after": len(live_team),
            })

    # ---- STAGE 2b: diagnosis rows for assignments the active-roster rule
    # excludes (departed staff). One row per assignment row, naming the
    # employee and the raw matter reference (which may itself be dangling).
    for coll, row in excluded:
        g.push("ops_reports", {
            "report": "staffing_diagnosis", "batch_code": batch,
            "source_collection": coll, "row_id": row["id"],
            "matter_id": row.get("matter_id"),
            "employee_id": str(row.get("assigned_to") or ""),
            "category": "latent_organizational",
            "note": "assignment references staff no longer active on the roster; excluded from the roster append",
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
