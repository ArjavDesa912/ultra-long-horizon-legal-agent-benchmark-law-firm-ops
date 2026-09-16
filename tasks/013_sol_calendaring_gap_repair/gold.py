#!/usr/bin/env python3
"""Gold solution for 013_sol_calendaring_gap_repair (v2). Run against a FRESH
container. Idempotent: safe to run twice.

Applies firm policy SOL-01 (read live from firm_policies): every open
litigation matter with a statute_of_limitations value must end with exactly
one statute-type deadline dated AT the SOL. Gaps are created; a matter's
earliest-dated statute deadline is retained (re-dated if it sits on/after the
SOL); surplus statute deadlines are removed; a single compliant lead-time
buffer is untouched. Risk ranking covers every candidate matter's statute
deadline in the post-repair state (delete-then-rewrite keyed by batch_code)."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def dp(v):
    return glib.Gold.dp(v)


def due_key(d):
    return (dp(d.get("due_date")) or "", str(d.get("id")))


def main():
    g = glib.Gold()
    batch = g.nonce()

    # Policy gate: the audit exists because SOL-01 says so.
    policies = [p for p in g.all("firm_policies") if p.get("policy_id") == "SOL-01"]
    if not policies:
        raise RuntimeError("firm policy SOL-01 missing from firm_policies")

    matters = g.all("matters")
    deadlines = g.all("deadlines")

    # Stage 1 -- candidates per SOL-01 (open litigation matters with an SOL).
    candidates = [
        m for m in matters
        if m.get("matter_type") == "litigation" and m.get("status") == "open"
        and m.get("statute_of_limitations")
    ]
    cand_ids = {str(m["id"]) for m in candidates}

    # Stage 2 -- classify per candidate: no statute deadline = gap; the
    # earliest-dated statute deadline is the retained one (buffer if it
    # precedes the SOL, non-compliant if on/after); the rest are surplus.
    statute_by_matter = {}
    for d in deadlines:
        if d.get("type") == "statute" and str(d.get("matter_id")) in cand_ids:
            statute_by_matter.setdefault(str(d["matter_id"]), []).append(d)
    gaps, noncompliant, buffers, surplus = [], [], [], []
    for m in candidates:
        mid = str(m["id"])
        sol = dp(m["statute_of_limitations"])
        rows = sorted(statute_by_matter.get(mid, []), key=due_key)
        if not rows:
            gaps.append(m)
            continue
        keep = rows[0]
        if dp(keep.get("due_date")) < sol:
            buffers.append(keep)
        else:
            noncompliant.append(keep)
        surplus.extend(rows[1:])

    # Idempotent report rows: delete this batch's own rows before rewriting.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in (
            "sol_calendaring_audit", "sol_diagnosis", "sol_risk_ranking"
        ):
            g.delete("ops_reports", r["id"])

    # Stage 3 -- repair: create gaps, re-date the retained non-compliant
    # deadline, remove surplus statute deadlines.
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
        sol = next(dp(mm["statute_of_limitations"]) for mm in candidates
                   if str(mm["id"]) == str(d["matter_id"]))
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

    # ---- Stage 4: risk ranking over every candidate matter's statute
    # deadline in the POST-REPAIR state (re-read live; deterministic from
    # final state, so re-running the audit reproduces identical rows).
    ep = dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    cutoff = int(g.nonce(field="urgency_cutoff_days"))
    live_deadlines = g.all("deadlines")
    for d in live_deadlines:
        if d.get("type") != "statute" or str(d.get("matter_id")) not in cand_ids:
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
