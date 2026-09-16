#!/usr/bin/env python3
"""gold_alt for 012_firmwide_conflict_of_interest_audit (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
all reads are SQL-first (clients/contacts/matters fetched through g.sql), and
the matching is traversed CLIENT-first -- an index from normalized client name
to contacts -- instead of contact-first, so the matched-client iteration order
and the matter-linkage walk differ from gold.py. Writes still go through the
public REST API. Idempotent like gold.py."""
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

REPORTS = ("conflict_of_interest", "conflict_severity", "conflict_of_interest_summary")


def norm(name):
    """INTAKE-01 normalization: lowercase, alphanumeric only, legal-form words
    (llc/inc/corp/llp) removed."""
    s = str(name or "").lower()
    s = re.sub(r"\b(llc|inc|corp|llp)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def main():
    g = glib.Gold()
    batch = g.nonce()

    if not g.sql("SELECT 1 AS ok FROM firm_policies WHERE policy_id = 'INTAKE-01' LIMIT 1"):
        raise RuntimeError("firm policy INTAKE-01 missing from firm_policies")

    # ---- SQL-first reads ----------------------------------------------------
    clients = g.sql("SELECT id, client_number, name FROM clients ORDER BY id")
    contacts = g.sql("SELECT id, full_name, organisation, matter_ids FROM contacts")
    matters = g.sql("SELECT id, matter_number, client_id, status FROM matters")

    client_by_id = {str(c["id"]): c for c in clients}
    matter_by_id = {str(m["id"]): m for m in matters}
    # Client-side index: normalized client name -> clients (duplicate client
    # rows sharing a normalized name are one client for conflict purposes).
    by_norm = defaultdict(list)
    for c in clients:
        by_norm.setdefault(norm(c.get("name")), []).append(c)

    # ---- Stage 1: conflict discovery (client-first traversal) ---------------
    conflicts = []
    seen = set()
    for c in clients:
        key = norm(c.get("name"))
        # Contacts whose organisation normalizes to this client's name.
        linked = [ct for ct in contacts if norm(ct.get("organisation")) == key]
        if not linked:
            continue
        for ct in linked:
            for mid in ct.get("matter_ids") or []:
                m = matter_by_id.get(str(mid))
                if m is None:
                    continue
                matter_client = client_by_id.get(str(m.get("client_id")))
                if matter_client is None:
                    continue
                if norm(matter_client.get("name")) == key:
                    continue  # the linked matter belongs to the matched client
                dedupe_key = (str(ct["id"]), str(c["id"]), str(m["id"]))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                conflicts.append((ct, c, m, matter_client))

    # Idempotent report rows: delete this batch's own rows before rewriting.
    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("batch_code") == batch and r.get("report") in REPORTS:
            g.delete("ops_reports", r["id"])

    for ct, matched, m, matter_client in conflicts:
        g.push("ops_reports", {
            "report": "conflict_of_interest", "batch_code": batch,
            "contact_id": ct["id"],
            "contact_name": ct.get("full_name"),
            "matched_client_id": matched["id"],
            "matched_client_name": matched.get("name"),
            "conflicted_matter_id": m["id"],
            "conflicted_matter_number": m.get("matter_number"),
            "note": (f"{ct.get('full_name')} of {ct.get('organisation')} is linked to matter "
                     f"{m.get('matter_number')}, whose client is {matter_client.get('name')} -- "
                     f"the firm's own client {matched.get('name')} is adverse on that matter."),
        })

    g.push("ops_reports", {
        "report": "conflict_of_interest_summary", "batch_code": batch,
        "conflicts_found": len(conflicts),
    })

    # ---- STAGE 2: severity from the pushed rows, read back via SQL ---------
    live_conflicts = g.sql(
        "SELECT contact_id, conflicted_matter_id FROM ops_reports "
        "WHERE report = 'conflict_of_interest' AND batch_code = '" + batch + "'"
    )
    live_status_by_mid = {str(m["id"]): str(m.get("status") or "")
                          for m in g.sql("SELECT id, status FROM matters")}
    for row in live_conflicts:
        status = live_status_by_mid.get(str(row.get("conflicted_matter_id")), "")
        g.push("ops_reports", {
            "report": "conflict_severity", "batch_code": batch,
            "contact_id": row.get("contact_id"),
            "conflicted_matter_id": row.get("conflicted_matter_id"),
            "severity": "high" if status == "open" else "low",
        })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
