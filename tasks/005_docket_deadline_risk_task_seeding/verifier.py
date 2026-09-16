#!/usr/bin/env python3
"""Verifier for 005_docket_deadline_risk_task_seeding (v2).

Expected prep tasks derive from the SEED snapshot + the live episode knobs
(idempotent by construction). Dual-path: the qualifying set is computed via
Python filtering over seed_rows AND an independent vlib.sql join; both must
agree. Hazard coverage: the stale prep task must be UPDATED in place (never
duplicated), non-qualifying deadlines untouched, and the summary rows must
derive from the sweep's qualifying set. Fail-closed via vlib.run."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    window_days = int(vlib.get_nonce(v.token, field="docket_window_days"))
    urgent_hours = int(vlib.get_nonce(v.token, field="urgent_window_hours"))
    lead_days = int(vlib.get_nonce(v.token, field="prep_lead_days"))
    window_end = (ep_dt + timedelta(days=window_days)).isoformat()

    live_matters = {str(m["id"]): m for m in vlib.fetch_all(v.token, "matters")}
    live_tasks = vlib.fetch_all(v.token, "tasks")
    live_deadlines = vlib.fetch_all(v.token, "deadlines")
    ops_rows = vlib.fetch_all(v.token, "ops_reports")

    seed_matters = {str(m["id"]): m for m in vlib.seed_rows("matters")}
    seed_deadlines = vlib.seed_rows("deadlines")
    seed_tasks = vlib.seed_rows("tasks")

    # ------------------------------------------------- expected qualifying set
    # Path A (Python over the SEED snapshot): status upcoming, matter open,
    # due within the episode row's docket_window_days (both ends inclusive).
    expected = []
    for d in seed_deadlines:
        if d.get("status") != "upcoming":
            continue
        matter = seed_matters.get(str(d.get("matter_id")))
        if matter is None or matter.get("status") != "open":
            continue
        due = vlib.dp(d.get("due_date"))
        if not due or not (ep <= due <= window_end):
            continue
        due_dt = datetime.strptime(due, "%Y-%m-%d").date()
        prep_due = (due_dt - timedelta(days=lead_days)).isoformat()
        urgent = (due_dt - ep_dt).days * 24 <= urgent_hours
        expected.append({
            "matter_id": str(d.get("matter_id")),
            "title": f"Prepare for: {d.get('title')}",
            "assigned_to": d.get("assigned_to"),
            "due_date": prep_due,
            "priority": "urgent" if urgent else "high",
            "urgent": urgent,
        })

    # Path B: independent SQL join over the live rows (deadlines/matters are
    # canaried apart from the single updated prep row).
    sql_rows = vlib.sql(
        v.token,
        "SELECT d.id AS deadline_id FROM deadlines d "
        "JOIN matters m ON m.id::text = d.matter_id #>> '{}' "
        "WHERE d.status = 'upcoming' AND m.status = 'open' "
        "AND LEFT(d.due_date::text, 10) >= '" + ep + "' AND LEFT(d.due_date::text, 10) <= '" + window_end + "'",
    )
    v.expect_equal(len(sql_rows), len(expected),
                   "qualifying deadlines: raw vs SQL disagree (dual-path)")

    # --------------------------------------------- prep rows: exact set check
    # Exactly one prep row per qualifying deadline across tasks AND deadlines
    # (this firm's pre-migration data kept prep tasks alongside deadlines).
    live_prep = {}
    for t in live_tasks:
        title = t.get("title") or ""
        if title.startswith("Prepare for: "):
            key = (str(t.get("matter_id")), title)
            v.expect(key not in live_prep, f"duplicate prep task for {key}")
            live_prep[key] = ("tasks", t)
    for d in live_deadlines:
        title = d.get("title") or ""
        if title.startswith("Prepare for: "):
            key = (str(d.get("matter_id")), title)
            v.expect(key not in live_prep, f"duplicate prep row for {key}")
            live_prep[key] = ("deadlines", d)

    expected_keys = {(e["matter_id"], e["title"]) for e in expected}
    # Pre-existing prep rows seeded among deadlines are legal residents of the
    # set even when their deadline does not qualify (they are historical rows
    # that must simply stay untouched).
    stale_keys = {(str(d.get("matter_id")), d.get("title")) for d in seed_deadlines
                  if (d.get("title") or "").startswith("Prepare for: ")}
    v.expect(expected_keys <= set(live_prep.keys()),
             "missing prep task for a qualifying deadline")
    v.expect(set(live_prep.keys()) <= (expected_keys | stale_keys),
             "extra prep rows beyond the qualifying set (fabricated or duplicated)")
    for exp in expected:
        key = (exp["matter_id"], exp["title"])
        coll, row = live_prep.get(key, (None, None))
        v.expect(row is not None, f"missing prep task for {key}")
        v.expect_equal(vlib.dp(row.get("due_date")), exp["due_date"], f"{key} due_date")
        v.expect_equal(row.get("priority"), exp["priority"], f"{key} priority")
        v.expect_equal(str(row.get("assigned_to")), str(exp["assigned_to"]), f"{key} assigned_to")
        v.expect_equal(row.get("status"), "open", f"{key} status")
        if coll == "tasks":
            v.expect_equal(str(row.get("created_by")), "system", f"{key} created_by")

    # The stale planted prep row: when its deadline qualifies it must be the
    # SAME seed row updated in place (never duplicated, never deleted); when
    # it does not qualify it must stay byte-identical.
    stale_seed = [d for d in seed_deadlines
                  if (d.get("title") or "").startswith("Prepare for: ")]
    for seed in stale_seed:
        live = next((d for d in live_deadlines if str(d["id"]) == str(seed["id"])), None)
        v.expect(live is not None, f"seeded prep row {seed['id']} deleted (update in place, never duplicate)")
        key = (str(seed.get("matter_id")), seed.get("title"))
        if key in expected_keys:
            exp = next(e for e in expected if (e["matter_id"], e["title"]) == key)
            v.expect_equal(vlib.dp(live.get("due_date")), exp["due_date"],
                           f"stale prep row {seed['id']} due_date updated")
            v.expect_equal(live.get("priority"), exp["priority"], f"stale prep row {seed['id']} priority")
        else:
            v.expect(vlib.row_eq(live, seed), f"seeded prep row {seed['id']} changed (deadline not in window)")

    # Every other seeded deadline and task byte-identical.
    live_deadline_by_id = {str(d["id"]): d for d in live_deadlines}
    for seed in seed_deadlines:
        if (seed.get("title") or "").startswith("Prepare for: "):
            continue  # handled above
        live = live_deadline_by_id.get(str(seed["id"]))
        v.expect(live is not None and vlib.row_eq(live, seed),
                 f"seeded deadline {seed['id']} changed (non-qualifying deadlines untouched)")
    live_task_by_id = {str(t["id"]): t for t in live_tasks}
    for seed in seed_tasks:
        live = live_task_by_id.get(str(seed["id"]))
        v.expect(live is not None and vlib.row_eq(live, seed), f"seeded task {seed['id']} changed")

    # ------------------------------------------------ stage 2: summary rows
    # Derived from the SAME qualifying set used above: a wrong window/status
    # filter silently changes these counts too.
    by_matter = {}
    for exp in expected:
        entry = by_matter.setdefault(exp["matter_id"], {"created": 0, "urgent": 0})
        entry["created"] += 1
        entry["urgent"] += 1 if exp["urgent"] else 0
    summary_rows = [r for r in ops_rows
                    if r.get("report") == "docket_risk_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), len(by_matter) + 1, "docket_risk_summary row count")
    by_matter_row = {r.get("matter_id"): r for r in summary_rows}
    for mid, counts in by_matter.items():
        row = by_matter_row.get(mid)
        v.expect(row is not None, f"missing docket_risk_summary row for matter {mid}")
        v.expect_equal(row.get("prep_tasks_created"), counts["created"], f"matter {mid} prep_tasks_created")
        v.expect_equal(row.get("urgent_count"), counts["urgent"], f"matter {mid} urgent_count")
    firm_row = by_matter_row.get("FIRM")
    v.expect(firm_row is not None, "missing FIRM docket_risk_summary rollup")
    v.expect_equal(firm_row.get("prep_tasks_created"), len(expected), "FIRM prep_tasks_created")
    v.expect_equal(firm_row.get("urgent_count"), sum(1 for e in expected if e["urgent"]), "FIRM urgent_count")

    v.check_canaries([
        "clients", "matters", "contacts", "time_entries", "invoices", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
