#!/usr/bin/env python3
"""Verifier for 012_firmwide_conflict_of_interest_audit (v2).

Expected conflicts derive from the SEED snapshot (vlib.seed_rows) under
INTAKE-01's normalized organisation match plus the different-client condition,
never from live post-mutation state, so the verifier is idempotent by
construction. Dual-path: the conflict set is computed via Python filtering AND
an independent SQL join (normalization resolved in the database); both paths
must agree with each other AND with the rows the agent wrote. Hazard coverage:
the 3 same-org same-client near-miss contacts must NOT be flagged (name-only
matching over-reports by exactly 3), and the severity rows must reflect the
conflicted matter's live status. Fail-closed via vlib.run."""
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import vlib  # noqa: E402


def norm(name):
    """INTAKE-01 normalization: lowercase, alphanumeric only, legal-form words
    (llc/inc/corp/llp) removed."""
    s = str(name or "").lower()
    s = re.sub(r"\b(llc|inc|corp|llp)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def checks(v: vlib.Verifier) -> None:
    batch = str(vlib.get_nonce(v.token))
    batch_lit = batch.replace("'", "")

    seed_clients = vlib.seed_rows("clients")
    seed_contacts = vlib.seed_rows("contacts")
    seed_matters = vlib.seed_rows("matters")

    matter_by_id = {str(m["id"]): m for m in seed_matters}
    seed_matters_by_id = matter_by_id
    client_by_id = {str(c["id"]): c for c in seed_clients}
    clients_by_exact = defaultdict(list)
    for c in seed_clients:
        clients_by_exact.setdefault(str(c.get("name")), []).append(c)
    clients_by_norm = defaultdict(list)
    for c in seed_clients:
        clients_by_norm.setdefault(norm(c.get("name")), []).append(c)

    # --------------------------------------------- expected conflicts (Python)
    # A conflict exists when a contact's organisation matches a client's name
    # under INTAKE-01's normalization AND the contact is linked to a matter
    # whose own client is a DIFFERENT client (compared on the normalized name,
    # so duplicate client rows sharing a normalized name count as one client).
    expected = set()  # (contact_id, matched_client_id, matter_id)
    for ct in seed_contacts:
        key = norm(ct.get("organisation"))
        if not key:
            continue
        for matched in clients_by_norm.get(key, []):
            for mid in ct.get("matter_ids") or []:
                m = seed_matters_by_id.get(str(mid))
                if m is None:
                    continue
                matter_client = client_by_id.get(str(m.get("client_id")))
                if matter_client is None:
                    continue
                if norm(matter_client.get("name")) == norm(matched.get("name")):
                    continue  # the linked matter belongs to the matched client
                expected.add((str(ct["id"]), str(matched["id"]), str(m["id"])))

    # Dual path: the same conflict set via an independent SQL join (the
    # normalization and the different-client condition resolved in SQL).
    sql_rows = vlib.sql(
        v.token,
        "SELECT DISTINCT ct.id::text AS contact_id, c.id::text AS client_id, "
        "       mm.id::text AS matter_id "
        "FROM contacts ct "
        "JOIN clients c ON REGEXP_REPLACE(REGEXP_REPLACE(LOWER(c.name), "
        "      '\\m(llc|inc|corp|llp)\\M', ' ', 'g'), '[^a-z0-9]+', '', 'g') = "
        "     REGEXP_REPLACE(REGEXP_REPLACE(LOWER(ct.organisation), "
        "       '\\m(llc|inc|corp|llp)\\M', ' ', 'g'), '[^a-z0-9]+', '', 'g') "
        "JOIN matters mm ON mm.id::text IN ("
        "  SELECT CAST(x AS TEXT) FROM jsonb_array_elements_text(ct.matter_ids::jsonb) x) "
        "JOIN clients mc ON mc.id::text = mm.client_id::text "
        "WHERE REGEXP_REPLACE(REGEXP_REPLACE(LOWER(mc.name), "
        "        '\\m(llc|inc|corp|llp)\\M', ' ', 'g'), '[^a-z0-9]+', '', 'g') <> "
        "      REGEXP_REPLACE(REGEXP_REPLACE(LOWER(c.name), "
        "        '\\m(llc|inc|corp|llp)\\M', ' ', 'g'), '[^a-z0-9]+', '', 'g')",
    )
    sql_conflicts = {(str(r["contact_id"]), str(r["client_id"]), str(r["matter_id"]))
                     for r in sql_rows}
    v.expect_equal(sql_conflicts, expected,
                   "conflict set: SQL vs Python (dual-path)")

    # ------------------------------------------------ written conflict rows
    reports = vlib.fetch_all(v.token, "ops_reports")
    conflict_rows = [r for r in reports
                     if r.get("report") == "conflict_of_interest" and r.get("batch_code") == batch]
    v.expect_equal(len(conflict_rows), len(expected), "conflict_of_interest row count")
    got_keys = {(str(r.get("contact_id")), str(r.get("matched_client_id")),
                 str(r.get("conflicted_matter_id"))) for r in conflict_rows}
    v.expect_equal(got_keys, expected,
                   "conflict_of_interest set (exact: the true conflict only)")
    got_contacts = {str(r.get("contact_id")) for r in conflict_rows}
    for r in conflict_rows:
        v.expect(bool(str(r.get("note") or "").strip()),
                 f"conflict row {r.get('contact_id')} note must diagnose the conflict")
        matched_name = str(r.get("matched_client_name") or "")
        v.expect(matched_name and matched_name in str(r.get("note") or ""),
                 f"conflict row {r.get('contact_id')} note must name the matched client")
    # Hazard coverage (scope_boundary): the 3 same-org same-client near-miss
    # contacts must NOT be flagged; name-only matching over-reports by 3.
    near_miss_names = ("General Counsel Tech", "Green Earth Liaison", "State AG Liaison")
    near_miss_ids = {str(ct["id"]) for ct in seed_contacts
                     if ct.get("full_name") in near_miss_names}
    v.expect_equal(len(near_miss_ids), 3,
                   "same-org same-client near-miss contacts planted (hazard present)")
    for cid in near_miss_ids:
        v.expect(cid not in got_contacts,
                 f"near-miss contact {cid} must not be flagged as a conflict")
    # Non-vacuity: matching WITHOUT the different-client rule over-reports by
    # exactly the 3 near-miss contacts (one of them links to two matters of the
    # same client, so the PAIR delta is 4 — assert on the contact set, which is
    # the hazard the task is built around).
    naive_keys = set()
    for ct in seed_contacts:
        key = norm(ct.get("organisation"))
        if not key:
            continue
        for matched in clients_by_norm.get(key, []):
            for mid in ct.get("matter_ids") or []:
                if str(mid) in matter_by_id:
                    naive_keys.add((str(ct["id"]), str(matched["id"]), str(mid)))
    naive_extra_contacts = {k[0] for k in naive_keys} - {e[0] for e in expected}
    v.expect_equal(naive_extra_contacts, near_miss_ids,
                   "seed no longer discriminates the different-client rule (hazard dissolved)")

    # ------------------------------------------------------ severity rows
    severity_rows = [r for r in reports
                     if r.get("report") == "conflict_severity" and r.get("batch_code") == batch]
    v.expect_equal(len(severity_rows), len(expected), "conflict_severity row count")
    want_severity = {}
    for cid, mcid, mid in expected:
        matter = seed_matters_by_id.get(str(mid)) or {}
        want_severity[(cid, mid)] = "high" if (matter.get("status") or "") == "open" else "low"
    got_severity = {(str(r.get("contact_id")), str(r.get("conflicted_matter_id"))): str(r.get("severity") or "")
                    for r in severity_rows}
    v.expect_equal(got_severity, want_severity, "conflict_severity set (exact)")
    # Dual-path: severity must match the LIVE matter status (the same lookup
    # the agent had to do), not just the seed.
    live_matters = {str(m["id"]): m for m in vlib.fetch_all(v.token, "matters")}
    for (cid, mid), sev in got_severity.items():
        live_status = str((live_matters.get(mid) or {}).get("status") or "")
        v.expect_equal(sev, "high" if live_status == "open" else "low",
                       f"conflict_severity for contact {cid} / matter {mid}")

    # ------------------------------------------------------ summary row
    summary_rows = [r for r in reports
                    if r.get("report") == "conflict_of_interest_summary" and r.get("batch_code") == batch]
    v.expect_equal(len(summary_rows), 1, "conflict_of_interest_summary row count")
    v.expect_equal(int(summary_rows[0].get("conflicts_found") or 0), len(expected),
                   "conflicts_found")
    # Dual-path over the written rows: SQL COUNT must agree with the Python
    # filter over the same rows.
    sql_count_rows = vlib.sql(
        v.token,
        "SELECT COUNT(*) AS n FROM ops_reports "
        "WHERE report = 'conflict_of_interest' AND batch_code = '" + batch_lit + "'",
    )
    v.expect_equal(int(sql_count_rows[0]["n"]), len(expected),
                   "conflict_of_interest row count: SQL vs Python (dual-path)")

    v.check_canaries([
        "clients", "matters", "contacts", "deadlines", "tasks", "time_entries", "invoices",
        "trust_transactions", "ediscovery_holds", "ediscovery_collections",
        "ediscovery_documents", "ediscovery_productions",
        "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
        "hold_reminders",
    ])


if __name__ == "__main__":
    vlib.run(None, checks)
