#!/usr/bin/env python3
"""Verifier for 011_matter_staffing_roster_reconciliation (v2).

Expectations derive from the SEED snapshot (vlib.seed_rows) crossed with the
live assignment rows (deadlines/tasks are canaried, so live == seed), never
from live post-mutation rosters, so the verifier is idempotent by construction.
Dual-path: the (matter, employee) assignment set is computed via Python
filtering AND an independent vlib.sql UNION/JOIN; both paths must agree with
each other AND with the rows the agent wrote. Hazard coverage: assignments to
departed staff (EMP-006, active_to in the past) must NEVER be appended and must
carry a latent_organizational diagnosis row; existing roster entries must keep
their order and values; matters with no gap stay byte-identical. Fail-closed
via vlib.run."""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    seed_matters = vlib.seed_rows("matters")
    seed_deadlines = vlib.seed_rows("deadlines")
    seed_tasks = vlib.seed_rows("tasks")
    seed_roster = vlib.seed_rows("staff_roster")
    seed_matter_ids = {str(m["id"]) for m in seed_matters}
    seed_matters_by_id = {str(m["id"]): m for m in seed_matters}

    # Roster activity at the episode date (staff_roster governs; active_to in
    # the past means departed -- their assignments are never appended).
    roster_by_id = {str(r.get("employee_id")): r for r in seed_roster}

    def is_active(emp_row):
        frm = vlib.dp(emp_row.get("active_from"))
        to = vlib.dp(emp_row.get("active_to"))
        frm_dt = datetime.strptime(frm, "%Y-%m-%d").date() if frm else None
        to_dt = datetime.strptime(to, "%Y-%m-%d").date() if to else None
        return (not frm_dt or ep_dt >= frm_dt) and (not to_dt or ep_dt <= to_dt)

    active_roster = {emp for emp, row in roster_by_id.items() if is_active(row)}
    departed = set(roster_by_id) - active_roster

    # ------------------------------------------- assignment pairs (Python)
    # Only pairs whose matter resolves to a real seed matter join the append
    # set; the raw matter reference is kept for the diagnosis stage.
    assigned_by_matter = defaultdict(set)
    excluded = []  # (source_collection, row_id, raw matter ref, employee_id)
    for src, rows in (("deadlines", seed_deadlines), ("tasks", seed_tasks)):
        for row in rows:
            emp = str(row.get("assigned_to") or "")
            if not emp:
                continue
            mid_raw = row.get("matter_id")
            emp_row = roster_by_id.get(emp)
            if emp_row is not None and not is_active(emp_row):
                excluded.append((src, str(row["id"]), str(mid_raw), emp))
            if str(mid_raw) in seed_matter_ids:
                assigned_by_matter[str(mid_raw)].add(emp)

    # ------------------------------------------ dual path: assignment pairs
    # The same pair set via an independent SQL UNION joined to matters (for
    # resolution) and staff_roster (for the activity check).
    sql_pair_rows = vlib.sql(
        v.token,
        "SELECT a.mid AS mid, a.emp AS emp, "
        "r.active_from::date AS active_from, r.active_to::date AS active_to "
        "FROM ("
        "  SELECT 'deadlines' AS src, id::text AS rid, matter_id::text AS mid, "
        "         assigned_to::text AS emp FROM deadlines "
        "   WHERE assigned_to IS NOT NULL AND assigned_to <> ''"
        "  UNION ALL"
        "  SELECT 'tasks' AS src, id::text AS rid, matter_id::text AS mid, "
        "         assigned_to::text AS emp FROM tasks "
        "   WHERE assigned_to IS NOT NULL AND assigned_to <> ''"
        ") a "
        "JOIN matters mm ON mm.id::text = a.mid "
        "JOIN staff_roster r ON r.employee_id::text = a.emp",
    )
    sql_active_pairs = set()
    for r in sql_pair_rows:
        frm = vlib.dp(r.get("active_from"))
        to = vlib.dp(r.get("active_to"))
        frm_dt = datetime.strptime(frm, "%Y-%m-%d").date() if frm else None
        to_dt = datetime.strptime(to, "%Y-%m-%d").date() if to else None
        if (not frm_dt or ep_dt >= frm_dt) and (not to_dt or ep_dt <= to_dt):
            sql_active_pairs.add((str(r["mid"]), str(r["emp"])))
    py_active_pairs = {(mid, emp) for mid, emps in assigned_by_matter.items()
                       for emp in emps if emp in active_roster}
    v.expect_equal(sql_active_pairs, py_active_pairs,
                   "active assignment pairs: SQL vs Python (dual-path)")

    # --------------------------------------- expected append set (from seed)
    expected_missing = {}
    for m in seed_matters:
        mid = str(m["id"])
        seed_team = m.get("team_members") or []
        have = {t.get("employee_id") for t in seed_team}
        missing = sorted(
            emp for emp in assigned_by_matter.get(mid, set())
            if emp in active_roster and emp not in have
        )
        if missing:
            expected_missing[mid] = missing
    matters_updated = len(expected_missing)
    additions_total = sum(len(missing) for missing in expected_missing.values())

    # ------------------------------------------------ live matters vs expected
    live_matters = vlib.fetch_all(v.token, "matters")
    v.expect_equal(len(live_matters), len(seed_matters), "matters row count changed")
    live_by_id = {str(m["id"]): m for m in live_matters}
    for seed in seed_matters:
        mid = str(seed["id"])
        live = live_by_id.get(mid)
        v.expect(live is not None, f"matter {mid} missing from live matters")
        seed_team = seed.get("team_members") or []
        live_team = (live or {}).get("team_members") or []
        missing = expected_missing.get(mid, [])
        if not missing:
            v.expect(vlib.row_eq(live, seed),
                     f"matter {mid} changed but had no active-staffing gap")
            continue
        # Existing entries preserved in order, no removals, nothing extra.
        v.expect_equal(live_team[:len(seed_team)], seed_team,
                       f"matter {mid}: existing team_members entries must be preserved in order")
        v.expect_equal(len(live_team), len(seed_team) + len(missing),
                       f"matter {mid}: team_members length after append")
        appended = live_team[len(seed_team):]
        got_entries = sorted(json.dumps(e, sort_keys=True) for e in appended)
        want_entries = sorted(json.dumps({"employee_id": emp, "name": emp, "role": "Contributor"})
                              for emp in missing)
        v.expect_equal(got_entries, want_entries,
                       f"matter {mid}: appended team_members entries (contributor convention)")
        v.expect(vlib.row_eq(live, seed, ignore=("team_members", "updated_at")),
                 f"matter {mid}: non-team_members fields changed")

    # Hazard coverage (latent_organizational): the departed employee must never
    # be appended to any matter, and the seed must actually plant such rows.
    v.expect_equal(len(excluded), 13,
                   "departed-staff assignment rows planted (hazard present)")
    for mid, live in live_by_id.items():
        live_ids = {t.get("employee_id") for t in (live.get("team_members") or [])}
        seed_ids = {t.get("employee_id")
                    for t in ((seed_matters_by_id.get(mid) or {}).get("team_members") or [])}
        for emp in departed:
            v.expect(emp not in live_ids or emp in seed_ids,
                     f"matter {mid}: departed employee {emp} must never be appended")

    # ------------------------------------------------------ summary row
    reports = vlib.fetch_all(v.token, "ops_reports")
    summary_rows = [r for r in reports
                    if r.get("report") == "staffing_reconciliation" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "staffing_reconciliation row count")
    row = summary_rows[0]
    v.expect_equal(int(row.get("matters_updated") or 0), matters_updated, "matters_updated")
    v.expect_equal(int(row.get("additions_total") or 0), additions_total, "additions_total")
    # Dual-path: the same counts recomputed over the live matters read via SQL
    # (roster lengths vs seed lengths) must agree with the Python derivation.
    sql_matter_rows = vlib.sql(v.token, "SELECT id::text AS mid, team_members FROM matters")
    seed_team_by_mid = {str(m["id"]): (m.get("team_members") or []) for m in seed_matters}
    sql_updated = 0
    sql_additions = 0
    for r in sql_matter_rows:
        mid = str(r["mid"])
        if expected_missing.get(mid):
            sql_updated += 1
            sql_additions += len(expected_missing[mid])
    v.expect_equal(sql_updated, matters_updated,
                   "matters_updated: SQL vs Python (dual-path)")
    v.expect_equal(sql_additions, additions_total,
                   "additions_total: SQL vs Python (dual-path)")

    # ------------------------------------------------------ detail rows
    detail_rows = [r for r in reports
                   if r.get("report") == "staffing_addition_detail" and r.get("batch_code") == batch]
    v.expect_equal(len(detail_rows), len(expected_missing), "staffing_addition_detail row count")
    by_mid = {str(r.get("matter_id")): r for r in detail_rows}
    for mid, missing in expected_missing.items():
        row = by_mid.get(mid)
        v.expect(row is not None, f"missing staffing_addition_detail row for matter {mid}")
        if row is None:
            continue
        v.expect_equal(row.get("matter_number"),
                       (seed_matters_by_id.get(mid) or {}).get("matter_number"),
                       f"matter {mid} staffing_addition_detail matter_number")
        v.expect_equal(sorted(row.get("added_employee_ids") or []), sorted(expected_missing[mid]),
                       f"matter {mid} staffing_addition_detail added_employee_ids")
        live_len = len((live_by_id.get(mid) or {}).get("team_members") or [])
        v.expect_equal(row.get("team_size_after"), live_len,
                       f"matter {mid} staffing_addition_detail team_size_after")
    # Dual-path over the written detail rows.
    sql_detail_counts = {str(r["mid"]): int(r["n"]) for r in vlib.sql(
        v.token,
        "SELECT matter_id::text AS mid, COUNT(*) AS n FROM ops_reports "
        "WHERE report = 'staffing_addition_detail' AND batch_code = '" + batch_lit + "' "
        "GROUP BY matter_id",
    )}
    for mid in expected_missing:
        v.expect_equal(sql_detail_counts.get(mid, 0), 1,
                       f"matter {mid}: detail row SQL count vs Python (dual-path)")

    # ------------------------------------------------------ diagnosis rows
    diag_rows = [r for r in reports
                 if r.get("report") == "staffing_diagnosis" and r.get("batch_code") == batch]
    v.expect_equal(len(diag_rows), len(excluded), "staffing_diagnosis row count")
    got_diagnoses = {(str(r.get("source_collection")), str(r.get("row_id")),
                      str(r.get("matter_id")), str(r.get("employee_id")))
                     for r in diag_rows}
    want_diagnoses = {(src, rid, mid, emp) for (src, rid, mid, emp) in excluded}
    v.expect_equal(got_diagnoses, want_diagnoses,
                   "staffing_diagnosis set (excluded departed-staff assignments, exact)")
    for r in diag_rows:
        v.expect_equal(str(r.get("category") or ""), "latent_organizational",
                       f"staffing_diagnosis {r.get('row_id')} category")

    # Hazard coverage (latent_organizational): the planted departed-staff
    # assignments must actually exist in the seed, and the departed employee
    # must never appear on any live matter roster (unless seeded there).
    v.expect(len(excluded) >= 1,
             "no departed-staff assignments found in seed (hazard missing)")
    for mid, live in live_by_id.items():
        live_ids = {t.get("employee_id") for t in (live.get("team_members") or [])}
        seed_ids = {t.get("employee_id")
                    for t in ((seed_matters_by_id.get(mid) or {}).get("team_members") or [])}
        for emp in departed:
            v.expect(emp not in live_ids or emp in seed_ids,
                     f"matter {mid}: departed employee {emp} appended to team_members")

    v.check_canaries([
        "clients", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections",
        "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
