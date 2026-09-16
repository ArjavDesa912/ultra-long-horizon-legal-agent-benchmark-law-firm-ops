#!/usr/bin/env python3
"""Gold solution for 012_firmwide_conflict_of_interest_audit (v2). Run
against a FRESH container. Idempotent: this batch's ops_reports rows are
deleted and rewritten keyed by batch_code (first-run missing-table guarded).

The conflict rule is INTAKE-01's (read live from firm_policies): a contact's
organisation matches a client's name after the policy's normalization
(lowercase, alphanumeric only, legal-form words stripped), AND the contact is
linked to a matter whose own client is a DIFFERENT client. Same-org same-client
contacts are near-misses, never conflicts."""
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

    # Policy gate: the conflict screen's rules live in firm_policies.
    if not any(p.get("policy_id") == "INTAKE-01" for p in g.all("firm_policies")):
        raise RuntimeError("firm policy INTAKE-01 missing")

    clients = g.all("clients")
    contacts = g.all("contacts")
    matters = g.all("matters")

    matter_by_id = {str(m["id"]): m for m in matters}
    client_by_id = {str(c["id"]): c for c in clients}
    clients_by_norm = defaultdict(list)
    for c in clients:
        clients_by_norm.setdefault(norm(c.get("name")), []).append(c)

    # ---- Stage 1: conflict discovery --------------------------------------
    # A conflict exists when a contact's organisation matches a client's name
    # under INTAKE-01's normalization AND the contact is linked to a matter
    # whose own client is a DIFFERENT client (compared on the normalized name,
    # so duplicate client rows sharing a normalized name count as one client).
    conflicts = []
    seen = set()
    for ct in contacts:
        key = norm(ct.get("organisation"))
        if not key:
            continue
        for matched in clients_by_norm.get(key, []):
            for mid in ct.get("matter_ids") or []:
                m = matter_by_id.get(str(mid))
                if m is None:
                    continue
                matter_client = client_by_id.get(str(m.get("client_id")))
                if matter_client is None:
                    continue
                if norm(matter_client.get("name")) == norm(matched.get("name")):
                    continue  # the linked matter belongs to the matched client
                dedupe_key = (str(ct["id"]), str(matched["id"]), str(m["id"]))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                conflicts.append((ct, matched, m, matter_client))

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

    # ---- STAGE 2 (dependent on stage 1's own just-pushed rows) -------------
    # Reads the conflict rows BACK from ops_reports and looks up each
    # conflicted matter's CURRENT status live -- a wrong stage-1
    # contact/matter identification silently targets the wrong matter here.
    live_conflicts = [r for r in g.all("ops_reports")
                      if r.get("report") == "conflict_of_interest" and r.get("batch_code") == batch]
    live_matters = {str(m["id"]): m for m in g.all("matters")}
    for row in live_conflicts:
        matter = live_matters.get(str(row.get("conflicted_matter_id")), {})
        g.push("ops_reports", {
            "report": "conflict_severity", "batch_code": batch,
            "contact_id": row.get("contact_id"),
            "conflicted_matter_id": row.get("conflicted_matter_id"),
            "severity": "high" if matter.get("status") == "open" else "low",
        })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
