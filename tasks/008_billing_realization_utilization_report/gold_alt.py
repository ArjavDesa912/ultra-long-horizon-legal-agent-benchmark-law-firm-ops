#!/usr/bin/env python3
"""gold_alt for 008 (v2): SQL-first. Per-employee and firm aggregates come
from /v1/sql/query GROUP BY; only the report writes use REST. Same end-state
as gold.py (KPI-REAL-01 basis, episode-row flag gap)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def r1(x):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def main():
    g = glib.Gold()
    batch = g.nonce()
    gap = int(g.nonce(field="realization_flag_gap"))

    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'KPI-REAL-01' LIMIT 1"):
        raise RuntimeError("firm policy KPI-REAL-01 missing")

    rows = g.sql(
        "SELECT employee_id, SUM(amount) AS total_amt, "
        "SUM(CASE WHEN status IN ('approved','invoiced') THEN amount ELSE 0 END) AS billed, "
        "SUM(hours) AS hours, "
        "SUM(CASE WHEN is_billable THEN hours ELSE 0 END) AS billable, "
        "COUNT(*) AS n FROM time_entries GROUP BY employee_id ORDER BY employee_id"
    )
    per = {}
    for r in rows:
        per[str(r["employee_id"])] = {
            "total": float(r["total_amt"] or 0),
            "billed": float(r["billed"]),
            "hours": float(r["hours"] or 0),
            "billable": float(r["billable"] or 0),
            "count": int(r["n"]),
        }

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("realization", "realization_flag"):
            g.delete("ops_reports", r["id"])

    firm_total = sum(d["total"] for d in per.values())
    firm_billed = sum(d["billed"] for d in per.values())
    firm_pct = r1(100.0 * firm_billed / firm_total) if firm_total else 0.0

    for emp in sorted(per):
        d = per[emp]
        g.push("ops_reports", {
            "report": "realization", "batch_code": batch, "employee_id": emp,
            "total_hours": r1(d["hours"]), "billable_hours": r1(d["billable"]),
            "billed_value": r1(d["billed"]), "entry_count": d["count"],
            "realization_pct": r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0,
        })
    g.push("ops_reports", {
        "report": "realization", "batch_code": batch, "employee_id": "FIRM",
        "total_hours": r1(sum(d["hours"] for d in per.values())),
        "billable_hours": r1(sum(d["billable"] for d in per.values())),
        "billed_value": r1(firm_billed),
        "entry_count": sum(d["count"] for d in per.values()),
        "realization_pct": firm_pct,
    })

    for emp in sorted(per):
        d = per[emp]
        rate = r1(100.0 * d["billed"] / d["total"]) if d["total"] else 0.0
        if firm_pct - rate > gap:
            g.push("ops_reports", {
                "report": "realization_flag", "batch_code": batch, "employee_id": emp,
                "realization_pct": rate, "firm_realization_pct": firm_pct,
                "gap": r1(firm_pct - rate),
            })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
