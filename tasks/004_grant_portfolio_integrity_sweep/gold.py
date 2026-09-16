#!/usr/bin/env python3
"""Gold solution for 004_grant_portfolio_integrity_sweep (v2, 5 stages). Run
against a FRESH container via the public REST API. Idempotent: application
corrections and report creates/corrections recompute to the same end-state,
and report rows are deleted and rewritten keyed by batch_code (first-run
missing-table reads guarded)."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402


def quarter_ends(ep_dt, count=4):
    """The `count` most recent calendar quarter-ends on or before the episode
    date (GRANT-REPORT-01's historical reconstruction anchors)."""
    candidates = []
    for y in (ep_dt.year - 1, ep_dt.year):
        for m in (3, 6, 9, 12):
            if m == 12:
                last = datetime(y + 1, 1, 1).date() - timedelta(days=1)
            else:
                last = datetime(y, m + 1, 1).date() - timedelta(days=1)
            candidates.append(last)
    past = sorted(set(q for q in candidates if q <= ep_dt))
    return past[-count:]


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()

    matters = {str(m["id"]): m for m in g.all("matters")}
    applications = g.all("grant_applications")
    awards = g.all("grant_awards")
    reports = g.all("grant_reports")

    # ---- Stage 1: application client_id must match its matter's client ----
    for app in applications:
        matter = matters.get(str(app.get("matter_id")))
        if matter is None:
            continue
        if str(app.get("client_id")) != str(matter.get("client_id")):
            g.update("grant_applications", app["id"], {"client_id": matter["client_id"]})

    # ---- Stage 2: schedule-vs-live report reconciliation -------------------
    # GRANT-REPORT-01: the reporting_schedule is authoritative; a report is
    # due when no live row with the same (award_id, report_type) exists or its
    # due_date differs; waived reports are not owed and are never recreated.
    for award in awards:
        schedule = award.get("reporting_schedule") or []
        award_reports = [r for r in reports if str(r.get("award_id")) == str(award["id"])]
        by_type = {r.get("report_type"): r for r in award_reports}
        for entry in schedule:
            rtype = entry.get("report_type")
            due = entry.get("due_date")
            existing = by_type.get(rtype)
            if existing is None:
                g.push("grant_reports", {
                    "award_id": award["id"],
                    "report_type": rtype,
                    "due_date": due,
                    "submitted_date": None,
                    "status": "upcoming",
                    "document_url": None,
                    "notes": "auto-created from award reporting schedule",
                    "submitted_by": None,
                })
            elif existing.get("status") == "waived":
                continue  # GRANT-REPORT-01: waived reports are never recreated/corrected
            elif glib.Gold.dp(existing.get("due_date")) != glib.Gold.dp(due):
                g.update("grant_reports", existing["id"], {"due_date": due})

    # ---- Stage 3: integrity summary from LIVE post-fix state ---------------
    # Re-derived from live end-state (not a run-local delta) so a second run
    # reports identical numbers.
    live_apps = g.all("grant_applications")
    live_matters = {str(m["id"]): m for m in g.all("matters")}
    applications_consistent = sum(
        1 for a in live_apps
        if str(a.get("client_id")) == str((live_matters.get(str(a.get("matter_id"))) or {}).get("client_id"))
    )
    live_reports = g.all("grant_reports")
    schedule_due = {}
    for award in awards:
        for entry in award.get("reporting_schedule") or []:
            schedule_due[(str(award["id"]), entry.get("report_type"))] = glib.Gold.dp(entry.get("due_date"))
    reports_matching_schedule = sum(
        1 for r in live_reports
        if schedule_due.get((str(r.get("award_id")), r.get("report_type"))) == glib.Gold.dp(r.get("due_date"))
    )

    try:
        existing_ops = g.all("ops_reports")
    except RuntimeError:
        existing_ops = []
    for r in existing_ops:
        if r.get("batch_code") == batch and r.get("report") in (
                "grant_portfolio_integrity", "reporting_compliance", "grant_schedule_history"):
            g.delete("ops_reports", r["id"])

    g.push("ops_reports", {
        "report": "grant_portfolio_integrity",
        "batch_code": batch,
        "applications_consistent": applications_consistent,
        "reports_total": len(live_reports),
        "reports_matching_schedule": reports_matching_schedule,
    })

    # ---- Stage 4: per-award compliance rollup -------------------------------
    # Groups the POST-stage-2 live reports -- a wrong create/correct silently
    # mis-counts an award's overdue reports here.
    reports_by_award = {}
    for r in live_reports:
        reports_by_award.setdefault(str(r.get("award_id")), []).append(r)
    for award in awards:
        aid = str(award["id"])
        award_reports = reports_by_award.get(aid, [])
        overdue = sum(
            1 for r in award_reports
            if r.get("status") == "upcoming" and (glib.Gold.dp(r.get("due_date")) or "9999") < ep
        )
        g.push("ops_reports", {
            "report": "reporting_compliance", "batch_code": batch,
            "award_id": aid, "reports_total_for_award": len(award_reports),
            "reports_overdue": overdue,
        })

    # ---- Stage 5: point-in-time reconstruction ------------------------------
    # For each of the last 4 quarter-ends: schedule entries already due by
    # that date (only entries due on/before the quarter-end, per policy), and
    # how many were unmet THEN (no live report row submitted on or before it).
    schedule_entries = []
    for award in awards:
        for entry in award.get("reporting_schedule") or []:
            due = glib.Gold.dp(entry.get("due_date"))
            if due:
                schedule_entries.append((str(award["id"]), entry.get("report_type"), due))
    live_by_pair = {}
    for r in live_reports:
        live_by_pair.setdefault((str(r.get("award_id")), r.get("report_type")), []).append(r)
    for qe in quarter_ends(ep_dt, 4):
        qe_iso = qe.isoformat()
        entries_due = 0
        entries_unmet = 0
        for aid, rtype, due in schedule_entries:
            if due > qe_iso:
                continue
            entries_due += 1
            rows = live_by_pair.get((aid, rtype), [])
            met = any(
                r.get("submitted_date") and glib.Gold.dp(r.get("submitted_date")) <= qe_iso
                for r in rows
            )
            if not met:
                entries_unmet += 1
        g.push("ops_reports", {
            "report": "grant_schedule_history",
            "batch_code": batch,
            "quarter_end": qe_iso,
            "entries_due": entries_due,
            "entries_unmet": entries_unmet,
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
