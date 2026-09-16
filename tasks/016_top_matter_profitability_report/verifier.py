#!/usr/bin/env python3
"""Verifier for 016_top_matter_profitability_report (v2).

Dual-path on every derived number: per-matter logged_value (ALL time entries
by amount, per the KPI-REAL-01 basis the instruction points at), invoiced and
collected values are recomputed in Python AND via SQL GROUP BY; both paths
must agree with each other and with the written rows. Hazard coverage: a
denominator that drops written_off entries (or any status) shifts the whole
top-N and FAILs; hallucinated billing anomalies FAIL (the class is empty in
this build — the verifier recomputes the expected set from the snapshot);
alerts that re-rank instead of reading back the stage-1 rows FAIL
(SEVERITY-01); partner-review tasks with the wrong assignee, title, status
or priority FAIL. Idempotent across repeated gold runs; fails closed via
vlib.run.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    top_n = int(vlib.get_nonce(v.token, field="top_n"))
    threshold = int(vlib.get_nonce(v.token, field="realization_alert_threshold"))

    matters = {str(m["id"]): m for m in vlib.fetch_all(v.token, "matters")}
    entries = vlib.fetch_all(v.token, "time_entries")
    invoices = vlib.fetch_all(v.token, "invoices")
    roster = vlib.fetch_all(v.token, "staff_roster")

    # ------------------------------------------------- supervising partner
    # Derived from the roster, not the instruction: the only active partner.
    active = [r for r in roster
              if r.get("active_from") and vlib.dp(r["active_from"]) <= (vlib.dp(vlib.get_nonce(v.token, field="episode_date")) or "")
              and (r.get("active_to") is None or (vlib.dp(r["active_to"]) or "9999") >= (vlib.dp(vlib.get_nonce(v.token, field="episode_date")) or ""))]
    partners = sorted(str(r.get("employee_id")) for r in active if r.get("role") == "partner")
    v.expect_equal(len(partners), 1, "exactly one active partner on the roster")
    supervisor = partners[0]

    # ------------------------------------------------ path 1: Python dicts
    py_logged = defaultdict(float)
    for e in entries:
        py_logged[str(e.get("matter_id"))] += e.get("amount") or 0
    py_inv = defaultdict(float)
    py_col = defaultdict(float)
    for inv in invoices:
        if inv.get("status") == "draft":
            continue
        mid = str(inv.get("matter_id"))
        py_inv[mid] += inv.get("total") or 0
        py_col[mid] += inv.get("amount_paid") or 0

    # ------------------------------------------------ path 2: SQL GROUP BY
    sql_logged = {str(r["mid"]): float(r["val"] or 0) for r in vlib.sql(
        v.token, "SELECT matter_id AS mid, SUM(amount) AS val FROM time_entries GROUP BY matter_id")}
    inv_rows = vlib.sql(
        v.token,
        "SELECT matter_id AS mid, SUM(total) AS inv, SUM(amount_paid) AS col "
        "FROM invoices WHERE status <> 'draft' GROUP BY matter_id")
    sql_inv = {str(r["mid"]): float(r["inv"] or 0) for r in inv_rows}
    sql_col = {str(r["mid"]): float(r["col"] or 0) for r in inv_rows}
    v.expect_equal(len(sql_logged), len(py_logged), "dual-path: matter count in logged SQL")
    for mid in matters:
        v.expect_cents(sql_logged.get(mid, 0.0), py_logged.get(mid, 0.0),
                       f"dual-path: matter {mid} logged_value")
        v.expect_cents(sql_inv.get(mid, 0.0), py_inv.get(mid, 0.0),
                       f"dual-path: matter {mid} invoiced_value")
        v.expect_cents(sql_col.get(mid, 0.0), py_col.get(mid, 0.0),
                       f"dual-path: matter {mid} collected_value")

    # ------------------------------------------------------- stage 1: rows
    expected_ranked = sorted(matters.keys(), key=lambda mid: (-py_logged.get(mid, 0.0), int(mid)))[:top_n]
    rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
            if r.get("report") == "matter_profitability" and r.get("batch_code") == batch]
    v.expect_equal(len(rows), len(expected_ranked), "matter_profitability row count")
    by_rank = {r.get("rank"): r for r in rows}
    for rank, mid in enumerate(expected_ranked, start=1):
        row = by_rank.get(rank)
        v.expect(row is not None, f"missing matter_profitability rank {rank}")
        v.expect_equal(str(row.get("matter_id")), mid, f"rank {rank} matter_id")
        v.expect_equal(row.get("matter_number"), matters[mid].get("matter_number"), f"rank {rank} matter_number")
        lv = py_logged.get(mid, 0.0)
        v.expect_cents(row.get("logged_value"), lv, f"rank {rank} logged_value")
        v.expect_cents(row.get("invoiced_value"), py_inv.get(mid, 0.0), f"rank {rank} invoiced_value")
        v.expect_cents(row.get("collected_value"), py_col.get(mid, 0.0), f"rank {rank} collected_value")
        exp_pct = round(100 * py_col.get(mid, 0.0) / lv, 1) if lv else 0.0
        actual = row.get("realization_pct")
        v.expect(isinstance(actual, (int, float)) and abs(float(actual) - exp_pct) <= 0.05,
                 f"rank {rank} realization_pct off")

    # ------------------------------------------- stage 1b: anomaly rows
    expected_anoms = {mid for mid in matters
                      if py_inv.get(mid, 0.0) > 0 and not py_logged.get(mid, 0.0)}
    anomaly_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                    if r.get("report") == "billing_anomaly" and r.get("batch_code") == batch]
    v.expect_equal(len(anomaly_rows), len(expected_anoms), "billing_anomaly row count")
    seen_anoms = {str(r.get("matter_id")) for r in anomaly_rows}
    v.expect_equal(seen_anoms, {str(m) for m in expected_anoms}, "billing_anomaly matter set")
    for r in anomaly_rows:
        # error-diagnosis assertion: the cause class must be the taxonomy category
        v.expect_equal(r.get("diagnosis"), "mistake", "billing_anomaly diagnosis category")

    # ------------------------------------------------------ stage 2: alerts
    live_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                 if r.get("report") == "matter_profitability" and r.get("batch_code") == batch]
    expected_alerts = [r for r in live_rows if float(r.get("realization_pct") or 0) < threshold]
    alert_rows = [r for r in vlib.fetch_all(v.token, "ops_reports")
                  if r.get("report") == "profitability_alert" and r.get("batch_code") == batch]
    v.expect_equal(len(alert_rows), len(expected_alerts), "profitability_alert row count")
    alert_by_rank = {r.get("rank"): r for r in alert_rows}
    for exp in expected_alerts:
        row = alert_by_rank.get(exp.get("rank"))
        v.expect(row is not None, f"missing profitability_alert rank {exp.get('rank')}")
        v.expect_equal(str(row.get("matter_id")), str(exp.get("matter_id")),
                       f"profitability_alert rank {exp.get('rank')} matter_id")
        # SEVERITY-01: the alert must reuse the stage-1 row's own value
        v.expect_cents(row.get("realization_pct"), exp.get("realization_pct"),
                       f"profitability_alert rank {exp.get('rank')} reuses stage-1 realization_pct")

    # ------------------------------------------------------ stage 3: tasks
    live_tasks = vlib.fetch_all(v.token, "tasks")
    seed_tasks = vlib.seed_rows("tasks")
    seed_ids = {str(t["id"]) for t in seed_tasks}
    new_tasks = [t for t in live_tasks if str(t["id"]) not in seed_ids]
    expected_titles = {f"Partner review: {matters[str(e['matter_id'])].get('matter_number')}"
                       for e in expected_alerts}
    new_titles = [t.get("title") for t in new_tasks]
    v.expect_equal(sorted(map(str, new_titles)), sorted(map(str, expected_titles)),
                   "partner-review task set")
    for exp in expected_alerts:
        mid = str(exp.get("matter_id"))
        title = f"Partner review: {matters[mid].get('matter_number')}"
        matches = [t for t in new_tasks if t.get("title") == title]
        v.expect_equal(len(matches), 1, f"partner-review task count for matter {mid}")
        t = matches[0]
        v.expect_equal(str(t.get("assigned_to")), supervisor, f"task {title} assigned_to")
        v.expect_equal(t.get("status"), "open", f"task {title} status")
        v.expect_equal(t.get("priority"), "high", f"task {title} priority")
        v.expect_equal(str(t.get("matter_id")), str(matters[mid]["id"]), f"task {title} matter_id")
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
