#!/usr/bin/env python3
"""Gold for 008_billing_realization_utilization_report (v2).

Realization per KPI-REAL-01 (read live from firm_policies): billed basis =
amounts on approved+invoiced entries over ALL entries (written_off stays in
the denominator at full value); employees with no amounts realize 0.0. Flags
derive from THIS batch's own rows: more than `realization_flag_gap` points
below the FIRM rollup. Idempotent via delete-then-rewrite keyed by batch."""
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

    # Policy presence gate (the definitions live in firm_policies).
    if not any(p.get("policy_id") == "KPI-REAL-01" for p in g.all("firm_policies")):
        raise RuntimeError("firm policy KPI-REAL-01 missing")

    entries = g.all("time_entries")
    per = {}
    for e in entries:
        emp = str(e.get("employee_id"))
        amt = glib.Gold.cents(e.get("amount")) / 100.0
        d = per.setdefault(emp, {"total": 0.0, "billed": 0.0, "hours": 0.0,
                                 "billable": 0.0, "count": 0})
        d["total"] += amt
        d["hours"] += float(e.get("hours") or 0)
        if e.get("is_billable"):
            d["billable"] += float(e.get("hours") or 0)
        if e.get("status") in ("approved", "invoiced"):
            d["billed"] += amt
        d["count"] += 1

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in ("realization", "realization_flag"):
            g.delete("ops_reports", r["id"])

    total_hours = sum(d["hours"] for d in per.values())
    billable_hours = sum(d["billable"] for d in per.values())
    entry_count = sum(d["count"] for d in per.values())
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
        "total_hours": r1(total_hours), "billable_hours": r1(billable_hours),
        "billed_value": r1(firm_billed), "entry_count": entry_count,
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
