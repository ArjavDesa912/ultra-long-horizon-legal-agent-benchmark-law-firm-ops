#!/usr/bin/env python3
"""Verifier for 006_new_matter_intake_conflict_check (v2).

Expectations derive from the SEED snapshot (matters/clients/contacts/
time_entries/staff_roster) + the live episode knob, so the check is idempotent
by construction. Hazard coverage: a name-only exact-match screen (missing the
normalized dba-variant contact), citing window-edge matter 2026-009 as a
conflict, picking a departed/wrong-role attorney, or opening the matter as
'open' despite a conflict all FAIL. Fail-closed via vlib.run."""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402

NUM_RE = re.compile(r"^(\d{4})-(\d+)$")
LEGAL_FORMS = {"llc", "inc", "corp", "llp"}
OPPOSING = "Tech Innovations Inc"


def normalize(name):
    tokens = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in LEGAL_FORMS]
    return "".join(tokens)


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    ep = vlib.dp(vlib.get_nonce(v.token, field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    window_years = int(vlib.get_nonce(v.token, field="former_client_window_years"))
    window_days = window_years * 365

    seed_matters = vlib.seed_rows("matters")
    seed_clients = vlib.seed_rows("clients")
    seed_contacts = vlib.seed_rows("contacts")
    seed_entries = vlib.seed_rows("time_entries")
    seed_roster = vlib.seed_rows("staff_roster")
    ops_rows = vlib.fetch_all(v.token, "ops_reports")

    client_by_name = {c["name"]: c for c in seed_clients}
    green_earth = client_by_name["Green Earth Foundation"]
    opposing_norm = normalize(OPPOSING)

    # ------------------------------------------------- expected screen outcome
    matches = []
    for c in seed_clients:
        if normalize(c.get("name") or "") == opposing_norm:
            matches.append(("client", c.get("client_number"), c.get("name")))
    for ct in seed_contacts:
        if normalize(ct.get("organisation") or "") == opposing_norm:
            matches.append(("contact", None, ct.get("organisation")))
    seed_clients_by_id = {str(c["id"]): c for c in seed_clients}
    for m in seed_matters:
        if m.get("status") != "closed" or not m.get("date_closed"):
            continue
        client = seed_clients_by_id.get(str(m.get("client_id")))
        if client is None or normalize(client.get("name") or "") != opposing_norm:
            continue
        closed = vlib.dp(m.get("date_closed"))
        closed_dt = datetime.strptime(closed, "%Y-%m-%d").date()
        if (ep_dt - closed_dt).days < window_years * 365:
            matches.append(("former_client_matter", m.get("matter_number"), client.get("name")))
    conflict_found = len(matches) > 0

    # ------------------------------------------------- expected attorney pick
    active_fee_earners = []
    for r in seed_roster:
        if r.get("role") not in ("partner", "associate"):
            continue
        active_from = vlib.dp(r.get("active_from")) or "0000"
        active_to = vlib.dp(r.get("active_to"))
        if active_from > ep:
            continue
        if active_to and active_to < ep:
            continue
        active_fee_earners.append(r.get("employee_id"))
    hours_by_emp = {eid: 0.0 for eid in active_fee_earners}
    for te in seed_entries:
        eid = te.get("employee_id")
        if eid in hours_by_emp:
            hours_by_emp[eid] += te.get("hours", 0) or 0
    expected_attorney = min(active_fee_earners, key=lambda e: (hours_by_emp.get(e, 0.0), e)) if active_fee_earners else None
    partners_active = [r.get("employee_id") for r in seed_roster
                       if r.get("role") == "partner" and (vlib.dp(r.get("active_from")) or "0000") <= ep
                       and not (vlib.dp(r.get("active_to")) and vlib.dp(r.get("active_to")) < ep)]
    expected_partner = partners_active[0] if partners_active else None

    # ------------------------------------------------- expected matter number
    max_num = 0
    for m in seed_matters:
        match = NUM_RE.match(m.get("matter_number") or "")
        if match and match.group(1) == ep[:4]:
            max_num = max(max_num, int(match.group(2)))
    expected_number = f"{ep[:4]}-{(max_num + 1):03d}"

    # --------------------------------------------------------- matter row check
    live_matters = vlib.fetch_all(v.token, "matters")
    seed_ids = {str(m["id"]) for m in seed_matters}
    new_matters = [m for m in live_matters if str(m["id"]) not in seed_ids]
    v.expect_equal(len(new_matters), 1, "new matters count")
    m = new_matters[0]

    v.expect_equal(m.get("matter_number"), expected_number, "matter_number")
    v.expect_equal(m.get("matter_name"), "Green Earth Foundation v. Tech Innovations Inc", "matter_name")
    v.expect_equal(m.get("status"), "conflict_check" if conflict_found else "open", "status")
    v.expect_equal(str(m.get("client_id")), str(green_earth["id"]), "client_id")
    v.expect_equal(m.get("responsible_attorney"), expected_attorney, "responsible_attorney")
    v.expect_equal(m.get("supervising_partner"), expected_partner, "supervising_partner")
    v.expect_equal(m.get("opposing_counsel"), OPPOSING, "opposing_counsel")
    v.expect_equal(vlib.dp(m.get("date_opened")), ep, "date_opened")
    v.expect(bool(m.get("conflict_checked")), "conflict_checked must record that the screen ran")
    v.expect_equal(vlib.dp(m.get("conflict_checked_at")), ep, "conflict_checked_at")
    description = m.get("description") or ""
    if conflict_found:
        v.expect(OPPOSING.lower() in description.lower(), "description must note the flagged counterparty")
        v.expect("conflict" in description.lower(), "description must note the conflict")
        # Hazard coverage: the window-edge near-miss must NOT be cited as a conflict.
        for sentence in re.split(r"[.\n]", description):
            if "2026-009" in sentence:
                negated = any(cue in sentence.lower() for cue in (
                    "not a conflict", "no conflict", "outside the", "outside any",
                    "clear", "excluded", "beyond the", "not flagged", "does not",
                ))
                v.expect(negated, f"description cites matter 2026-009 as a conflict (it is outside the window)")
    else:
        v.expect("conflict flagged" not in description.lower(),
                 "description must not flag a conflict when the screen found none")

    # Seeded matters untouched.
    live_matter_by_id = {str(m["id"]): m for m in live_matters}
    for seed in seed_matters:
        live = live_matter_by_id.get(str(seed["id"]))
        v.expect(live is not None and vlib.row_eq(live, seed), f"seeded matter {seed['id']} changed")

    # ------------------------------------------------ stage 4: audit note row
    audit_rows = [r for r in ops_rows
                  if r.get("report") == "intake_conflict_audit" and r.get("batch_code") == batch]
    v.expect_equal(len(audit_rows), 1, "intake_conflict_audit row count")
    audit = audit_rows[0]
    v.expect_equal(str(audit.get("matter_id")), str(m["id"]), "intake_conflict_audit matter_id")
    v.expect_equal(audit.get("matter_number"), expected_number, "intake_conflict_audit matter_number")
    v.expect_equal(audit.get("conflict_found"), conflict_found, "intake_conflict_audit conflict_found")
    note_text = str(audit.get("note") or "")
    v.expect(OPPOSING.lower() in note_text.lower(), "audit note must name the screened counterparty")
    v.expect("conflict" in note_text.lower(), "audit note must state the screen outcome")
    if conflict_found:
        basis_named = any(cue in note_text.lower() for cue in ("client", "contact", "2026-008"))
        v.expect(basis_named, "audit note must name the conflict basis")
        # Hazard coverage: the outside-window matter must not be cited as a conflict.
        for sentence in re.split(r"[.\n]", note_text):
            if "2026-009" in sentence and "conflict" in sentence.lower():
                v.expect(any(cue in sentence.lower() for cue in (
                    "not a conflict", "no conflict", "outside the", "outside any",
                    "clear", "excluded", "beyond the", "not flagged", "does not",
                )), "audit note cites matter 2026-009 as a conflict (it is outside the window)")

    v.check_canaries([
        "clients", "contacts", "deadlines", "tasks", "time_entries", "invoices", "trust_transactions",
        "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
