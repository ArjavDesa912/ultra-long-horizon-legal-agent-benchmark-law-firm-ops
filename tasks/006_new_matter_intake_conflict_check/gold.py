#!/usr/bin/env python3
"""Gold solution for 006_new_matter_intake_conflict_check (v2, 4 stages). Run
against a FRESH container via the public REST API. Idempotent: the matter
create is guarded by the policy-derived matter_number, and the audit row is
deleted and rewritten keyed by batch_code."""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

NUM_RE = re.compile(r"^(\d{4})-(\d+)$")
LEGAL_FORMS = {"llc", "inc", "corp", "llp"}


def normalize(name):
    """INTAKE-01 normalization: lowercase, alphanumeric tokens only, legal-form
    words (llc/inc/corp/llp) removed."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in LEGAL_FORMS]
    return "".join(tokens)


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    window_years = int(g.nonce(field="former_client_window_years"))

    clients = g.all("clients")
    contacts = g.all("contacts")
    matters = g.all("matters")
    time_entries = g.all("time_entries")
    roster = g.all("staff_roster")

    client_by_name = {c["name"]: c for c in clients}
    clients_by_id = {str(c["id"]): c for c in clients}
    green_earth = client_by_name["Green Earth Foundation"]
    opposing = "Tech Innovations Inc"
    opposing_norm = normalize(opposing)

    # ---- Stage 1: conflict screen (INTAKE-01) ------------------------------
    # (1) current-client / contact-organisation match after normalization;
    # (2) former-client conflict when the normalized name appears on a matter
    # closed less than former_client_window_years years ago.
    matches = []
    for c in clients:
        if normalize(c.get("name") or "") == opposing_norm:
            matches.append(("client", c.get("client_number"), c.get("name")))
    for ct in contacts:
        if normalize(ct.get("organisation") or "") == opposing_norm:
            matches.append(("contact", None, ct.get("organisation")))
    window_days = window_years * 365
    for m in matters:
        if m.get("status") != "closed" or not m.get("date_closed"):
            continue
        client = clients_by_id.get(str(m.get("client_id")))
        if client is None or normalize(client.get("name") or "") != opposing_norm:
            continue
        closed = glib.Gold.dp(m.get("date_closed"))
        closed_dt = datetime.strptime(closed, "%Y-%m-%d").date()
        if (ep_dt - closed_dt).days < window_days:
            matches.append(("former_client_matter", m.get("matter_number"), client.get("name")))
    conflict_found = len(matches) > 0

    # ---- Stage 2: attorney selection (INTAKE-01) ----------------------------
    # Among staff_roster rows with role in (partner, associate) and active at
    # the episode date, the one with the fewest total time_entries hours.
    active_fee_earners = []
    for r in roster:
        if r.get("role") not in ("partner", "associate"):
            continue
        active_from = glib.Gold.dp(r.get("active_from")) or "0000"
        active_to = glib.Gold.dp(r.get("active_to"))
        if active_from > ep:
            continue
        if active_to and active_to < ep:
            continue
        active_fee_earners.append(r.get("employee_id"))
    hours_by_emp = {eid: 0.0 for eid in active_fee_earners}
    for te in time_entries:
        eid = te.get("employee_id")
        if eid in hours_by_emp:
            hours_by_emp[eid] += te.get("hours", 0) or 0
    responsible = min(active_fee_earners, key=lambda e: (hours_by_emp.get(e, 0.0), e)) if active_fee_earners else None
    partners_active = [r.get("employee_id") for r in roster
                       if r.get("role") == "partner" and (glib.Gold.dp(r.get("active_from")) or "0000") <= ep
                       and not (glib.Gold.dp(r.get("active_to")) and glib.Gold.dp(r.get("active_to")) < ep)]
    supervising = partners_active[0] if partners_active else None

    # ---- Stage 3: matter create ---------------------------------------------
    # Idempotent: detect THIS intake's own prior matter (same matter_name for
    # the same client) and update nothing if it exists -- the numbering derived
    # from live matters would otherwise advance every run and duplicate it.
    own_matter = next((m for m in matters
                       if m.get("matter_name") == "Green Earth Foundation v. Tech Innovations Inc"
                       and str(m.get("client_id")) == str(green_earth["id"])), None)
    if own_matter is not None:
        matter_number = own_matter.get("matter_number")
    else:
        max_num = 0
        for m in matters:
            match = NUM_RE.match(m.get("matter_number") or "")
            if match and match.group(1) == ep[:4]:
                max_num = max(max_num, int(match.group(2)))
        matter_number = f"{ep[:4]}-{(max_num + 1):03d}"

        description = "Environmental claim against Tech Innovations Inc."
        if conflict_found:
            description += (
                " CONFLICT FLAGGED: proposed opposing party Tech Innovations Inc matches the firm's "
                "conflict screen (existing client / contact / recent former-client matter) -- pending partner review."
            )
        g.push("matters", {
            "matter_number": matter_number,
            "matter_name": "Green Earth Foundation v. Tech Innovations Inc",
            "practice_area": "Environmental",
            "matter_type": "litigation",
            "status": "conflict_check" if conflict_found else "open",
            "client_id": green_earth["id"],
            "responsible_attorney": responsible,
            "supervising_partner": supervising,
            "team_members": [{"employee_id": responsible, "name": responsible, "role": "Lead Attorney"}],
            "jurisdiction": "CA-STATE",
            "court": "N/A",
            "opposing_counsel": opposing,
            "date_opened": ep + "T00:00:00.000Z",
            "description": description,
            "tags": ["environmental", "conflict-review"] if conflict_found else ["environmental"],
            "conflict_checked": True,
            "conflict_checked_at": ep + "T00:00:00.000Z",
        })

    # ---- Stage 4: intake audit note row -------------------------------------
    try:
        existing_ops = g.all("ops_reports")
    except RuntimeError:
        existing_ops = []
    for r in existing_ops:
        if r.get("report") == "intake_conflict_audit" and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    live_matters = g.all("matters")
    new_matter = next(m for m in live_matters if m.get("matter_number") == matter_number)
    note = (
        f"Conflict screen for opposing party {opposing}: {len(matches)} match(es) considered; "
        f"basis: " + "; ".join(f"{kind} {ref or ''} ({name})".strip() for kind, ref, name in matches)
        if matches else f"Conflict screen for opposing party {opposing}: no matches."
    )
    g.push("ops_reports", {
        "report": "intake_conflict_audit",
        "batch_code": batch,
        "matter_id": new_matter.get("id"),
        "matter_number": matter_number,
        "conflict_found": conflict_found,
        "matches_considered": len(matches),
        "note": note,
    })

    print(f"gold done in {g.steps} API calls")


if __name__ == "__main__":
    main()
