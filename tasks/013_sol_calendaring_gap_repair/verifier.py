#!/usr/bin/env python3
"""Verifier for 013_sol_calendaring_gap_repair (v2).

Expectations are computed from the SEED snapshot (vlib.seed_rows), never from
live post-mutation state, so the verifier is idempotent and a stale or
hallucinated repair fails. Dual-path on the candidate set (Python filter vs
SQL) and on the risk-ranking rows. Hazard coverage: touching a compliant
buffer, deleting (instead of re-dating) a non-compliant deadline, or creating
deadlines outside the candidate set all FAIL."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def dp(v):
    return vlib.dp(v)


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    cutoff = int(vlib.get_nonce(v.token, field="urgency_cutoff_days"))
    ep = dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    # ---- Candidate set from SEED matters (the episode's ground truth)
    seed_matters = vlib.seed_rows("matters")
    candidates = [
        m for m in seed_matters
        if m.get("matter_type") == "litigation" and m.get("status") == "open"
        and m.get("statute_of_limitations")
    ]
    cand_ids = {str(m["id"]) for m in candidates}
    sol_by_matter = {str(m["id"]): dp(m["statute_of_limitations"]) for m in candidates}

    # Dual-path: the candidate set computed via SQL must equal the Python set.
    sql_cands = vlib.sql(
        v.token,
        "SELECT id FROM matters WHERE matter_type = 'litigation' AND status = 'open' "
        "AND statute_of_limitations IS NOT NULL",
    )
    v.expect_equal(len(sql_cands), len(candidates), "candidate set size (SQL vs Python)")
    v.expect({str(r["id"]) for r in sql_cands} == cand_ids, "candidate set membership (SQL vs Python)")

    # ---- Expected outcome per candidate, from SEED deadlines
    seed_deadlines = vlib.seed_rows("deadlines")
    seed_statute = {}
    for d in seed_deadlines:
        if d.get("type") == "statute" and str(d.get("matter_id")) in cand_ids:
            seed_statute.setdefault(str(d["matter_id"]), []).append(d)
    expected_gaps = [m for m in candidates if str(m["id"]) not in seed_statute]
    expected_buffers, expected_noncompliant, expected_surplus = [], [], []
    for mid, rows in seed_statute.items():
        ordered = sorted(rows, key=lambda d: (dp(d.get("due_date")) or "", str(d.get("id"))))
        keep, rest = ordered[0], ordered[1:]
        if dp(keep.get("due_date")) < sol_by_matter[mid]:
            expected_buffers.append(keep)
        else:
            expected_noncompliant.append(keep)
        expected_surplus.extend(rest)

    # ---- Live deadlines vs expected end-state
    live_deadlines = vlib.fetch_all(v.token, "deadlines")
    live_by_id = {str(d["id"]): d for d in live_deadlines}
    seed_ids = {str(d["id"]) for d in seed_deadlines}
    new_deadlines = [d for d in live_deadlines if str(d["id"]) not in seed_ids]

    live_statute_by_matter = {}
    for d in live_deadlines:
        if d.get("type") == "statute" and str(d.get("matter_id")) in cand_ids:
            live_statute_by_matter.setdefault(str(d["matter_id"]), []).append(d)

    # Per-candidate end-state, driven by the SEED classification:
    #  - gap matter: exactly one NEW statute deadline at the SOL
    #  - buffer matter: the retained (earliest) seed deadline byte-identical
    #  - non-compliant matter: the retained seed deadline re-dated to the SOL
    #  - multi-deadline matters: surplus seed statute deadlines removed
    expected_keep_ids = {str(d["id"]) for d in expected_buffers} | {str(d["id"]) for d in expected_noncompliant}
    for m in candidates:
        mid = str(m["id"])
        rows = live_statute_by_matter.get(mid, [])
        v.expect(len(rows) == 1, f"matter {m.get('matter_number')}: expected exactly 1 statute deadline, found {len(rows)}")
        if mid in {str(x["id"]) for x in expected_gaps}:
            v.expect_equal(dp(rows[0].get("due_date")), sol_by_matter[mid], f"matter {m.get('matter_number')} created statute due_date")
        elif mid in seed_statute:
            keep = sorted(seed_statute[mid], key=lambda d: (dp(d.get("due_date")) or "", str(d.get("id"))))[0]
            v.expect_equal(str(rows[0]["id"]), str(keep["id"]), f"matter {m.get('matter_number')}: wrong statute deadline retained")

    # assigned_to is only asserted on NEWLY CREATED deadlines (gaps); a
    # re-dated seed deadline keeps its original assignee.
    for d in new_deadlines:
        m = next(x for x in candidates if str(x["id"]) == str(d.get("matter_id")))
        v.expect_equal(str(d.get("assigned_to")), str(m.get("responsible_attorney")), f"matter {m.get('matter_number')} created-deadline assigned_to")

    # No new deadlines outside the candidate set.
    cand_set = cand_ids
    strays = [d for d in new_deadlines if str(d.get("matter_id")) not in cand_set]
    v.expect_equal(len(strays), 0, "new deadlines outside the candidate set")
    v.expect_equal(len(new_deadlines), len(expected_gaps), "created deadlines count")
    new_matter_ids = {str(d.get("matter_id")) for d in new_deadlines}
    for m in expected_gaps:
        v.expect(str(m["id"]) in new_matter_ids, f"missing created statute deadline for matter {m.get('matter_number')}")

    # Hazard coverage: compliant buffers byte-identical (acting on one = FAIL).
    for d in expected_buffers:
        live = live_by_id.get(str(d["id"]))
        v.expect(live is not None and vlib.row_eq(live, d), f"compliant buffer deadline {d['id']} was modified")

    # Non-compliant retained deadlines: re-dated to the SOL, not duplicated/deleted.
    for d in expected_noncompliant:
        live = live_by_id.get(str(d["id"]))
        v.expect(live is not None, f"non-compliant deadline {d['id']} deleted instead of re-dated")
        sol = sol_by_matter[str(d["matter_id"])]
        v.expect_equal(dp(live.get("due_date")), sol, f"non-compliant deadline {d['id']} not re-dated to SOL")

    # Surplus statute deadlines (matters that had several) must be removed.
    for d in expected_surplus:
        v.expect(str(d["id"]) not in live_by_id, f"surplus statute deadline {d['id']} was not removed")

    # ---- ops_reports: audit summary row (state-descriptive, idempotent)
    reports = vlib.fetch_all(v.token, "ops_reports")
    audit_rows = [r for r in reports if r.get("report") == "sol_calendaring_audit" and r.get("batch_code") == batch]
    v.expect_equal(len(audit_rows), 1, "sol_calendaring_audit row count")
    row = audit_rows[0]
    v.expect_equal(int(row.get("matters_checked")), len(candidates), "matters_checked")
    v.expect_equal(int(row.get("deadlines_at_sol")), len(candidates), "deadlines_at_sol")
    v.expect_equal(int(row.get("gaps_remaining")), 0, "gaps_remaining")
    v.expect_equal(int(row.get("surplus_remaining")), 0, "surplus_remaining")
    v.expect_equal(int(row.get("buffers_confirmed")), len(expected_buffers), "buffers_confirmed")

    # Diagnosis: each re-dated deadline's notes must record the prior due date
    # and classify the cause (mistake) -- diagnosis lives on the repaired row.
    for d in expected_noncompliant:
        live = live_by_id.get(str(d["id"]))
        notes = str((live or {}).get("notes") or "")
        v.expect(dp(d.get("due_date")) in notes, f"deadline {d['id']} notes must record the prior due date")
        v.expect("mistake" in notes.lower(), f"deadline {d['id']} notes must classify the cause (mistake)")

    # Risk ranking: one row per candidate matter, derived from post-repair state.
    ranking = [r for r in reports if r.get("report") == "sol_risk_ranking" and r.get("batch_code") == batch]
    v.expect_equal(len(ranking), len(candidates), "sol_risk_ranking row count")
    rank_by_matter = {}
    for r in ranking:
        rank_by_matter.setdefault(str(r.get("matter_id")), []).append(r)
    for m in candidates:
        mid = str(m["id"])
        rows = rank_by_matter.get(mid, [])
        v.expect(len(rows) == 1, f"sol_risk_ranking rows for matter {m.get('matter_number')}: expected 1, found {len(rows)}")
        due = dp(rows[0].get("due_date"))
        days = (datetime.strptime(due, "%Y-%m-%d").date() - ep_dt).days
        v.expect_equal(int(rows[0].get("days_until_due")), (datetime.strptime(dp(live_statute_by_matter[mid][0]["due_date"]), "%Y-%m-%d").date() - ep_dt).days, f"matter {m.get('matter_number')} days_until_due")
        v.expect_equal(rows[0].get("urgency"), "critical" if days <= cutoff else "normal", f"matter {m.get('matter_number')} urgency")

    # SQL dual-path over the written ranking rows.
    sql_rows = vlib.sql(
        v.token,
        "SELECT matter_id, COUNT(*) AS n FROM ops_reports WHERE report = 'sol_risk_ranking' "
        "AND batch_code = '" + str(batch).replace("'", "") + "' GROUP BY matter_id",
    )
    sql_counts = {str(r["matter_id"]): int(r["n"]) for r in sql_rows}
    for m in candidates:
        v.expect_equal(sql_counts.get(str(m["id"]), 0), 1, f"sol_risk_ranking SQL count for matter {m.get('matter_number')}")

    v.check_canaries([
        "clients", "contacts", "tasks", "time_entries", "invoices", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
        "matters",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
