#!/usr/bin/env python3
"""Verifier for 003_hold_linkage_repair_and_ack_sweep (v2).

Expected linkage corrections and the exact reminder set derive from the SEED
snapshot + the live episode knob (idempotent by construction). Dual-path:
per-hold coverage counts are computed via Python filtering over fetch_all AND
an independent vlib.sql GROUP BY; both must agree with each other and with the
rows the agent wrote. Hazard coverage: duplicate reminders, reminders for
acknowledged or grace-window-excluded custodians, rewritten historical rows,
and mis-repaired linkage all FAIL. Fail-closed via vlib.run."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    grace_days = int(vlib.get_nonce(v.token, field="hold_ack_grace_days"))

    holds = vlib.fetch_all(v.token, "ediscovery_holds")
    collections = vlib.fetch_all(v.token, "ediscovery_collections")
    live_reminders = vlib.fetch_all(v.token, "hold_reminders")
    reports = vlib.fetch_all(v.token, "ops_reports")

    seed_holds = vlib.seed_rows("ediscovery_holds")
    seed_collections = vlib.seed_rows("ediscovery_collections")
    seed_reminders = vlib.seed_rows("hold_reminders")

    # ------------------------------------------------ stage 1: linkage repair
    # Expected hold_id per collection: the real id of the hold on the same
    # matter (at most one hold per matter -- discoverable from the data); a
    # collection whose matter has no hold keeps its seeded hold_id.
    hold_by_matter = {}
    for h in seed_holds:
        hold_by_matter.setdefault(str(h.get("matter_id")), h)

    live_col_by_id = {str(c["id"]): c for c in collections}
    v.expect_equal(len(collections), len(seed_collections), "ediscovery_collections row count changed")
    repaired = 0
    for seed in seed_collections:
        cid = str(seed["id"])
        live = live_col_by_id.get(cid)
        v.expect(live is not None, f"collection {cid} missing from live collections")
        hold = hold_by_matter.get(str(seed.get("matter_id")))
        expected_hold_id = str(hold["id"]) if hold is not None else seed.get("hold_id")
        v.expect_equal(str(live.get("hold_id")), str(expected_hold_id), f"collection {cid} hold_id")
        if hold is not None and str(seed.get("hold_id")) != str(hold["id"]):
            repaired += 1
        v.expect(vlib.row_eq(live, seed, ignore=("hold_id", "updated_at")),
                 f"collection {cid} non-hold_id fields changed")
    # Hazard coverage (integration_fault): the planted broken references must
    # actually exist in the seed for this task to be non-vacuous.
    v.expect(repaired >= 1, "no broken hold_id references found in seed (hazard missing)")

    # ------------------------------------------------ stage 2: reminder sweep
    # Expected reminders: unacknowledged custodians of ACTIVE holds issued at
    # least hold_ack_grace_days before the episode date, minus pairs already
    # reminded (the pre-seeded history counts -- HOLD-ACK-01: never a second
    # reminder for the same (hold_id, custodian_name) pair even across runs).
    seed_pairs = {(str(s.get("hold_id")), s.get("custodian_name")) for s in seed_reminders}
    expected_reminders = []
    for hold in seed_holds:
        if hold.get("status") != "active":
            continue
        issued = vlib.dp(hold.get("issued_date"))
        if not issued:
            continue
        issued_dt = datetime.strptime(issued, "%Y-%m-%d").date()
        if (ep_dt - issued_dt).days < grace_days:
            continue
        for cust in hold.get("custodians") or []:
            if cust.get("acknowledged"):
                continue
            if (str(hold["id"]), cust.get("name")) in seed_pairs:
                continue  # already reminded historically -> no new row (HOLD-ACK-01)
            expected_reminders.append({
                "hold_id": str(hold["id"]),
                "hold_number": hold.get("hold_number"),
                "custodian_name": cust.get("name"),
                "custodian_email": cust.get("email"),
                "matter_id": hold.get("matter_id"),
            })

    # Historical rows byte-identical (never rewritten, legacy format included).
    seed_rem_by_id = {str(r["id"]): r for r in seed_reminders}
    live_rem_by_id = {str(r["id"]): r for r in live_reminders}
    v.expect_equal(len(live_reminders), len(seed_reminders) + len(expected_reminders),
                   "hold_reminders row count")
    for rid, seed in seed_rem_by_id.items():
        live = live_rem_by_id.get(rid)
        v.expect(live is not None and vlib.row_eq(live, seed),
                 f"historical hold_reminders row {rid} modified (records of what was sent)")

    live_pairs = {}
    for r in live_reminders:
        key = (str(r.get("hold_id")), r.get("custodian_name"))
        v.expect(key not in live_pairs, f"duplicate reminder for pair {key}")
        live_pairs[key] = r
    expected_pairs = {(e["hold_id"], e["custodian_name"]) for e in expected_reminders}
    seed_pairs = {(str(s.get("hold_id")), s.get("custodian_name")) for s in seed_reminders}
    v.expect_equal(set(live_pairs.keys()), expected_pairs | seed_pairs,
                   "hold_reminders pair set (exact: no duplicates, none extra)")
    for key in expected_pairs:
        v.expect(key in live_pairs, f"missing reminder for hold/custodian pair {key}")
    for key, row in live_pairs.items():
        if key in {(str(s.get("hold_id")), s.get("custodian_name")) for s in seed_reminders}:
            continue  # pre-seeded history
        match = next((e for e in expected_reminders
                      if (e["hold_id"], e["custodian_name"]) == key), None)
        v.expect(match is not None, f"reminder {key} is not in the policy-derived set")
        if match:
            v.expect_equal(row.get("hold_number"), match["hold_number"], f"reminder {key} hold_number")
            v.expect_equal(row.get("custodian_email"), match["custodian_email"], f"reminder {key} custodian_email")
            v.expect_equal(str(row.get("matter_id")), str(match["matter_id"]), f"reminder {key} matter_id")
            v.expect(vlib.dp(row.get("sent_at")) == ep, f"reminder {key} sent_at")
            v.expect(str(match["hold_number"]) in str(row.get("message") or ""),
                     f"reminder {key} message must identify the hold")

    # ------------------------------------------------ stage 3: hold_coverage
    # Coverage counts from the LIVE post-repair linkage (checked above).
    live_hold_by_matter = {}
    for h in holds:
        live_hold_by_matter.setdefault(str(h.get("matter_id")), h)
    linked_count = {}
    for col in collections:
        hold = live_hold_by_matter.get(str(col.get("matter_id")))
        if hold is not None and str(col.get("hold_id")) == str(hold["id"]):
            linked_count[str(hold["id"])] = linked_count.get(str(hold["id"]), 0) + 1
    reminders_by_hold = {}
    for r in live_reminders:
        reminders_by_hold[str(r.get("hold_id"))] = reminders_by_hold.get(str(r.get("hold_id")), 0) + 1

    # Dual-path: the same aggregation as an independent SQL GROUP BY.
    sql_linked = vlib.sql(
        v.token,
        "SELECT c.hold_id AS hold_id, COUNT(*) AS n "
        "FROM ediscovery_collections c "
        "JOIN ediscovery_holds h ON h.id::text = c.hold_id::text "
        "WHERE h.status <> 'superseded' "
        "GROUP BY c.hold_id",
    )
    sql_linked = {str(r["hold_id"]): int(r["n"]) for r in sql_linked}
    for hid, n in linked_count.items():
        v.expect_equal(sql_linked.get(hid, 0), n, f"hold {hid} collections_linked: raw vs SQL disagree (dual-path)")

    sql_rem = vlib.sql(
        v.token,
        "SELECT hold_id, COUNT(*) AS n FROM hold_reminders GROUP BY hold_id",
    )
    sql_rem_counts = {str(r["hold_id"]): int(r["n"]) for r in sql_rem}
    for hid, n in reminders_by_hold.items():
        v.expect_equal(sql_rem_counts.get(hid, 0), n, f"hold {hid} reminders_outstanding: raw vs SQL disagree (dual-path)")

    coverage_rows = [r for r in reports
                     if r.get("report") == "hold_coverage" and r.get("batch_code") == batch]
    expected_holds = [h for h in holds if h.get("status") != "superseded"]
    v.expect_equal(len(coverage_rows), len(expected_holds), "hold_coverage row count")
    coverage_by_hold = {r.get("hold_id"): r for r in coverage_rows}
    for hold in holds:
        if hold.get("status") == "superseded":
            v.expect(str(hold["id"]) not in {str(r.get("hold_id")) for r in coverage_rows},
                     f"superseded hold {hold['id']} must have no coverage row")
            continue
        hid = str(hold["id"])
        row = coverage_by_hold.get(hid)
        v.expect(row is not None, f"missing hold_coverage row for hold {hid}")
        v.expect_equal(row.get("collections_linked"), linked_count.get(hid, 0), f"hold {hid} collections_linked")
        v.expect_equal(row.get("custodians_total"), len(hold.get("custodians") or []), f"hold {hid} custodians_total")
        v.expect_equal(row.get("reminders_outstanding"), reminders_by_hold.get(hid, 0), f"hold {hid} reminders_outstanding")

    # ------------------------------------------------ stage 4: per-matter rollup
    matter_stats = {}
    for hold in holds:
        if hold.get("status") == "superseded":
            continue
        mid = str(hold.get("matter_id"))
        stats = matter_stats.setdefault(mid, {"holds": 0, "custodians": 0, "unacknowledged": 0, "reminders": 0})
        stats["holds"] += 1
        custodians = hold.get("custodians") or []
        stats["custodians"] += len(custodians)
        stats["unacknowledged"] += sum(1 for c in custodians if not c.get("acknowledged"))
        stats["reminders"] += reminders_by_hold.get(str(hold["id"]), 0)
    exposure_rows = [r for r in reports
                     if r.get("report") == "hold_exposure_by_matter" and r.get("batch_code") == batch]
    v.expect_equal(len(exposure_rows), len(matter_stats), "hold_exposure_by_matter row count")
    exposure_by_matter = {r.get("matter_id"): r for r in exposure_rows}
    for matter_id, stats in matter_stats.items():
        row = exposure_by_matter.get(matter_id)
        v.expect(row is not None, f"missing hold_exposure_by_matter row for matter {matter_id}")
        v.expect_equal(row.get("holds_count"), stats["holds"], f"matter {matter_id} holds_count")
        v.expect_equal(row.get("custodians_total"), stats["custodians"], f"matter {matter_id} custodians_total")
        v.expect_equal(row.get("unacknowledged_count"), stats["unacknowledged"], f"matter {matter_id} unacknowledged_count")
        v.expect_equal(row.get("reminders_outstanding"), stats["reminders"], f"matter {matter_id} reminders_outstanding")

    # ------------------------------------------------- holds themselves untouched
    v.expect_equal(len(holds), len(seed_holds), "ediscovery_holds row count changed")
    seed_hold_by_id = {str(r["id"]): r for r in seed_holds}
    for h in holds:
        seed = seed_hold_by_id.get(str(h["id"]))
        v.expect(seed is not None and vlib.row_eq(h, seed),
                 f"ediscovery_holds {h['id']} modified (acknowledgement changes only when custodians act)")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions",
        "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "ediscovery_holds",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
