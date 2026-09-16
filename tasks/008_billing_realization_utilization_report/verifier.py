#!/usr/bin/env python3
"""Verifier for 008_billing_realization_utilization_report (v2).

Expectations derived from live source data (time_entries is canaried, so live
== seed) with dual-path confirmation (Python aggregation vs SQL GROUP BY).
Hazard coverage: the written_off-in-denominator basis, the zero-amount
division guard, and the episode-driven flag threshold."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def r1(x):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def checks(v: vlib.Verifier) -> None:
    batch = vlib.get_nonce(v.token)
    gap_knob = int(vlib.get_nonce(v.token, field="realization_flag_gap"))

    entries = vlib.fetch_all(v.token, "time_entries")

    # ---- Python path (KPI-REAL-01 basis)
    per = {}
    for e in entries:
        emp = str(e.get("employee_id"))
        amt = vlib.cents(e.get("amount")) / 100.0
        d = per.setdefault(emp, {"total": 0.0, "billed": 0.0, "hours": 0.0, "billable": 0.0, "count": 0})
        d["total"] += amt
        d["billed"] += amt if e.get("status") in ("approved", "invoiced") else 0.0
        d["hours"] += float(e.get("hours") or 0)
        if e.get("is_billable"):
            d["billable"] += float(e.get("hours") or 0)
        d["count"] += 1

    firm_total = sum(d["total"] for d in per.values())
    firm_billed = sum(d["billed"] for d in per.values())
    firm_pct = r1(100.0 * firm_billed / firm_total) if firm_total else 0.0

    # ---- SQL path (must agree with Python on every employee + firm rate)
    sql_rows = vlib.sql(
        v.token,
        "SELECT employee_id, SUM(amount) AS total_amt, "
        "SUM(CASE WHEN status IN ('approved','invoiced') THEN amount ELSE 0 END) AS billed_amt "
        "FROM time_entries GROUP BY employee_id",
    )
    v.expect_equal(len(sql_rows), len(per), "employee set: SQL vs Python")
    for r in sql_rows:
        emp = str(r["employee_id"])
        total = vlib.cents(r["total_amt"]) / 100.0
        billed = vlib.cents(r["billed_amt"]) / 100.0
        exp_rate = r1(100.0 * per[emp]["billed"] / per[emp]["total"]) if per[emp]["total"] else 0.0
        sql_rate = r1(100.0 * billed / total) if total else 0.0
        v.expect_equal(sql_rate := sql_rate, sql_rate) if False else None
        v.expect_equal(exp_rate, exp_rate, "noop") if False else None
        v.expect_equal(sql_rate, exp_rate, f"{emp}: SQL vs Python realization (dual-path)")

    # ---- Written rows
    reports = vlib.fetch_all(v.token, "ops_reports")
    real_rows = [r for r in reports if r.get("report") == "realization" and r.get("batch_code") == batch]
    flag_rows = [r for r in reports if r.get("report") == "realization_flag" and r.get("batch_code") == batch]

    v.expect_equal(len(real_rows), len(per) + 1, "realization row count (per employee + FIRM)")
    by_emp = {str(r.get("employee_id")): r for r in real_rows}
    for emp, d in per.items():
        row = by_emp.get(emp)
        v.expect(row is not None, f"missing realization row for {emp}")
        rate = r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0
        v.expect_equal(r1(row.get("realization_pct")), exp_rate_of(d), f"{emp} realization_pct")
        v.expect_equal(vlib.cents(row.get("billed_value")), vlib.cents(d["billed"]), f"{emp} billed_value")
        v.expect_equal(int(row.get("entry_count")), d["count"], f"{emp} entry_count")
    firm_row = by_emp.get("FIRM")
    v.expect(firm_row is not None, "missing FIRM rollup row")
    v.expect_equal(r1(firm_row.get("realization_pct")), firm_pct, "FIRM realization_pct")

    # Flag set (episode-knob driven).
    expected_flags = set()
    for emp, d in per.items():
        rate = r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0
        if firm_pct - rate > gap_knob:
            expected_flags.add(emp)
    got_flags = {str(r.get("employee_id")) for r in flag_rows}
    v.expect_equal(got_flags, expected_flags, "realization_flag set")
    for r in flag_rows:
        emp = str(r.get("employee_id"))
        d = per[emp]
        rate = r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0
        v.expect_equal(r1(r.get("realization_pct")), rate, f"flag realization_pct for {emp}")
        v.expect_equal(r1(r.get("firm_realization_pct")), firm_pct, f"flag firm pct for {emp}")
        v.expect_equal(r1(r.get("gap")), r1(firm_pct - rate), f"flag gap for {emp}")

    # Hazard coverage: an agent that EXCLUDES written_off entries from the
    # denominator produces a different firm rate on this seed -- assert the
    # exclusion basis would actually differ here (else the check is vacuous).
    wo_total = sum(vlib.cents(e.get("amount")) for e in entries if e.get("status") == "written_off") / 100.0
    if wo_total > 0 and firm_total - wo_total > 0:
        excl_pct = r1(100.0 * firm_billed / (firm_total - wo_total))
        v.expect(abs(excl_pct - firm_pct) > 0.05,
                 "seed no longer discriminates the write-off basis (hazard dissolved)")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "invoices", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
        "time_entries",
    ])


def exp_rate_of(d):
    return r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0


if __name__ == "__main__":
    vlib.run(None, checks)
