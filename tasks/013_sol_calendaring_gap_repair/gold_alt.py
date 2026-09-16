#!/usr/bin/env python3
"""gold_alt for 013_sol_calendaring_gap_repair (v2): SQL-first variant.

Same end-state as gold.py (SOL-01 with surplus-removal), reached via
/v1/sql/query for candidate discovery and classification; REST only for writes.
Idempotent like gold.py."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def dp(v):
    return glib.Gold.dp(v)


def main():
    g = glib.Gold()
    batch = g.nonce()

    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'SOL-01' LIMIT 1"):
        raise RuntimeError("firm policy SOL-01 missing from firm_policies")

    candidates = g.sql(
        "SELECT id, matter_number, responsible_attorney, statute_of_limitations "
        "FROM matters WHERE matter_type = 'litigation' AND status = 'open' "
        "AND statute_of_limitations IS NOT NULL"
    )
    cand_ids = {str(c["id"]) for c in candidates}
    statute_rows = g.sql("SELECT id, matter_id, due_date FROM deadlines WHERE type = 'statute'")

    gaps, noncompliant, buffers, surplus = [], [], [], []
    for c in candidates:
        mid = str(c["id"])
        sol = dp(c["statute_of_limitations"])
        mine = sorted(
            [d for d in statute_rows if str(d.get("matter_id")) == mid],
            key=lambda d: (dp(d.get("due_date")) or "", str(d.get("id"))),
        )
        if not mine:
            gaps.append(c)
            continue
        keep = mine[0]
        if dp(keep.get("due_date")) < sol:
            buffers.append(keep)
        else:
            noncompliant.append(keep)
        surplus.extend(mine[1:])

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
            "sol_calendaring_audit", "sol_diagnosis", "sol_risk_ranking"
        ):
            g.delete("ops_reports", r["id"])

    for m in gaps:
        g.push("deadlines", {
            "matter_id": m["id"],
            "title": "Statute of Limitations",
            "type": "statute",
            "due_date": m["statute_of_limitations"],
            "status": "upcoming",
            "assigned_to": m.get("responsible_attorney"),
            "notes": "Auto-calendared from matters.statute_of_limitations by the SOL audit.",
        })
    redated = 0
    for d in noncompliant:
        sol = next(dp(c["statute_of_limitations"]) for c in candidates
                   if str(c["id"]) == str(d["matter_id"]))
        if dp(d.get("due_date")) != sol:
            g.update("deadlines", d["id"], {
                "due_date": sol,
                "notes": f"Re-dated per SOL-01 by the SOL audit; prior due date {dp(d.get('due_date'))}; cause: mistake (docketed on or after the bar date)",
            })
            redated += 1
    for d in surplus:
        g.delete("deadlines", d["id"])

    g.push("ops_reports", {
        "report": "sol_calendaring_audit", "batch_code": batch,
        "matters_checked": len(candidates),
        "gaps_remaining": 0,
        "deadlines_at_sol": len(candidates),
        "buffers_confirmed": len(buffers),
        "surplus_remaining": 0,
    })

    ep = dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    cutoff = int(g.nonce(field="urgency_cutoff_days"))
    live = g.sql("SELECT id, matter_id, due_date FROM deadlines WHERE type = 'statute'")
    for d in live:
        if str(d.get("matter_id")) not in cand_ids:
            continue
        due = dp(d.get("due_date"))
        days = (datetime.strptime(due, "%Y-%m-%d").date() - ep_dt).days
        g.push("ops_reports", {
            "report": "sol_risk_ranking", "batch_code": batch,
            "matter_id": d.get("matter_id"), "due_date": due,
            "days_until_due": days,
            "urgency": "critical" if days <= cutoff else "normal",
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
