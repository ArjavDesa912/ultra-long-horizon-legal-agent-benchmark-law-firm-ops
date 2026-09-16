#!/usr/bin/env python3
"""gold_alt for 011_matter_staffing_roster_reconciliation (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the assignment graph is resolved inside Postgres (a UNION of deadlines and
tasks assignment rows joined to staff_roster for activity and to matters for
resolution) instead of REST fetch-all + Python filtering, and the post-update
roster sizes are read back via SQL. Writes still go through the public REST
API. Idempotent like gold.py (delete-then-rewrite keyed by batch_code)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("staffing_reconciliation", "staffing_addition_detail", "staffing_diagnosis")


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))

    # Policy gate via SQL: the active-roster rule lives in firm_policies.
    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'REF-INTEG-01' LIMIT 1"):
        raise RuntimeError("firm policy REF-INTEG-01 missing from firm_policies")

    # Every (matter, employee) assignment row joined to the employee's roster
    # row for the activity check. No matters join here: the diagnosis stage
    # must also see assignments whose matter reference dangles. matter_id is
    # extracted as a SCALAR (#>> '{}') — a ::text cast would wrap jsonb string
    # refs in quotes and corrupt the diagnosis rows' matter references.
    pair_rows = g.sql(
        "SELECT a.src AS src, a.rid AS rid, a.mid AS mid, a.emp AS emp, "
        "r.active_from::date AS active_from, r.active_to::date AS active_to "
        "FROM ("
        "  SELECT 'deadlines' AS src, id::text AS rid, matter_id #>> '{}' AS mid, "
        "         assigned_to::text AS emp FROM deadlines "
        "   WHERE assigned_to IS NOT NULL AND assigned_to <> ''"
        "  UNION ALL"
        "  SELECT 'tasks' AS src, id::text AS rid, matter_id #>> '{}' AS mid, "
        "         assigned_to::text AS emp FROM tasks "
        "   WHERE assigned_to IS NOT NULL AND assigned_to <> ''"
        ") a "
        "JOIN staff_roster r ON r.employee_id::text = a.emp"
    )
    real_matter_ids = {str(r["mid"]) for r in g.sql("SELECT id::text AS mid FROM matters")}

    active_pairs = set()  # (matter_id, employee_id) pairs that may be appended
    excluded = []         # (src, rid, mid, emp) rows the activity rule rejects
    for r in pair_rows:
        src = str(r["src"])
        rid = str(r["rid"])
        mid = str(r["mid"])
        emp = str(r["emp"])
        frm = glib.Gold.dp(r.get("active_from"))
        to = glib.Gold.dp(r.get("active_to"))
        is_active = (not frm or ep >= frm) and (not to or ep <= to)
        if is_active:
            if mid in real_matter_ids:
                active_pairs.add((mid, emp))
        else:
            excluded.append((src, rid, mid, emp))

    # ---- Stage 1: append missing contributors per matter -------------------
    matters = g.sql("SELECT id::text AS mid, matter_number, team_members FROM matters ORDER BY id")
    matters_updated = 0
    additions_total = 0
    updated_ids = {}
    for m in matters:
        mid = str(m["mid"])
        team = m.get("team_members") or []
        have = {t.get("employee_id") for t in team}
        gap = sorted(emp for (mm, emp) in active_pairs
                     if mm == mid and emp not in have)
        if not gap:
            continue
        new_team = team + [{"employee_id": emp, "name": emp, "role": "Contributor"}
                           for emp in gap]
        g.update("matters", m["mid"], {"team_members": new_team})
        matters_updated += 1
        additions_total += len(gap)
        updated_ids[mid] = (m.get("matter_number"), gap)

    # Idempotent report rows: this batch's prior rows are reused verbatim where
    # they carry sweep findings (the counters and detail describe the gaps found
    # at sweep time, which a re-run can no longer re-derive from repaired live
    # state), then deleted and rewritten keyed by batch_code.
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
    # Post-update roster sizes read back via SQL, not trusted from memory.
    live_rows = g.sql("SELECT id::text AS mid, team_members FROM matters")
    live_sizes = {str(r["mid"]): len(r.get("team_members") or []) for r in live_rows}
    prior_details = prior_by_report.get("staffing_addition_detail") or []
    if prior_details:
        for r in sorted(prior_details, key=lambda x: str(x.get("matter_id"))):
            g.push("ops_reports", {
                "report": "staffing_addition_detail", "batch_code": batch,
                "matter_id": r.get("matter_id"), "matter_number": r.get("matter_number"),
                "added_employee_ids": r.get("added_employee_ids"),
                "team_size_after": live_sizes.get(str(r.get("matter_id")), 0),
            })
    else:
        for mid, (matter_number, added) in updated_ids.items():
            g.push("ops_reports", {
                "report": "staffing_addition_detail", "batch_code": batch,
                "matter_id": mid, "matter_number": matter_number,
                "added_employee_ids": added, "team_size_after": live_sizes.get(mid, 0),
            })

    # ---- STAGE 2b: diagnosis rows for assignments the active-roster rule
    # excludes (departed staff). One row per excluded assignment row, naming
    # the employee and the raw matter reference (which may itself dangle).
    for src, rid, mid, emp in excluded:
        g.push("ops_reports", {
            "report": "staffing_diagnosis", "batch_code": batch,
            "source_collection": src, "row_id": rid,
            "matter_id": mid,
            "employee_id": emp,
            "category": "latent_organizational",
            "note": "assignment references staff no longer active on the roster; excluded from the roster append",
        })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
