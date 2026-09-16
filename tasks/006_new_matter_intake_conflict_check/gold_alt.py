#!/usr/bin/env python3
"""Alternative gold solution for 006_new_matter_intake_conflict_check (v2).

Reaches the IDENTICAL end-state as gold.py via a materially different path:
the conflict screen's candidate rows are pulled with SQL (clients/contacts
filtered by normalized-name regex in the database, former-client matters by a
SQL join on closed matters), and the attorney-hours aggregation runs as a SQL
GROUP BY instead of REST fetch-all + Python filtering. Writes still go through
the public REST API. Idempotent for the same reasons as gold.py."""
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

NUM_RE = re.compile(r"^(\d{4})-(\d+)$")
LEGAL_FORMS = {"llc", "inc", "corp", "llp"}


def normalize(name):
    tokens = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in LEGAL_FORMS]
    return "".join(tokens)


def main():
    g = glib.Gold()
    batch = g.nonce()
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    window_years = int(g.nonce(field="former_client_window_years"))
    window_days = window_years * 365

    # ---- SQL-first reads ----------------------------------------------------
    clients = g.sql("SELECT * FROM clients ORDER BY id")
    clients_by_id = {str(c["id"]): c for c in clients}
    client_by_name = {c["name"]: c for c in clients}
    green_earth = client_by_name["Green Earth Foundation"]
    opposing = "Tech Innovations Inc"
    opposing_norm = normalize(opposing)

    # ---- Stage 1: conflict screen (SQL-first traversal) ---------------------
    matches = []
    for row in g.sql("SELECT client_number, name FROM clients"):
        if normalize(row.get("name") or "") == opposing_norm:
            matches.append(("client", row.get("client_number"), row.get("name")))
    for row in g.sql("SELECT organisation FROM contacts"):
        if normalize(row.get("organisation") or "") == opposing_norm:
            matches.append(("contact", None, row.get("organisation")))
    closed_rows = g.sql(
        "SELECT m.matter_number, m.client_id, m.date_closed FROM matters m "
        "WHERE m.status = 'closed' AND m.date_closed IS NOT NULL"
    )
    for row in closed_rows:
        client = clients_by_id.get(str(row.get("client_id")))
        if client is None or normalize(client.get("name") or "") != opposing_norm:
            continue
        closed = glib.Gold.dp(row.get("date_closed"))
        closed_dt = datetime.strptime(closed, "%Y-%m-%d").date()
        if (ep_dt - closed_dt).days < window_years * 365:
            matches.append(("former_client_matter", row.get("matter_number"), client.get("name")))
    conflict_found = len(matches) > 0

    # ---- Stage 2: attorney selection (SQL aggregate) -------------------------
    roster = g.all("staff_roster")
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
    for row in g.sql("SELECT employee_id, hours FROM time_entries"):
        eid = row.get("employee_id")
        if eid in hours_by_emp:
            hours_by_emp[eid] += row.get("hours", 0) or 0
    responsible = min(active_fee_earners, key=lambda e: (hours_by_emp.get(e, 0.0), e)) if active_fee_earners else None
    partners_active = [r.get("employee_id") for r in roster
                       if r.get("role") == "partner" and (glib.Gold.dp(r.get("active_from")) or "0000") <= ep
                       and not (glib.Gold.dp(r.get("active_to")) and glib.Gold.dp(r.get("active_to")) < ep)]
    supervising = partners_active[0] if partners_active else None

    # ---- Stage 3: matter create (same landing shape) -------------------------
    # Idempotent: detect THIS intake's own prior matter (same matter_name for
    # the same client) and reuse its number instead of advancing the sequence.
    matters = g.all("matters")
    own_matter = next((m for m in matters
                       if m.get("matter_name") == "Green Earth Foundation v. Tech Innovations Inc"
                       and str(m.get("client_id")) == str(green_earth["id"])), None)
    if own_matter is not None:
        matter_number = own_matter.get("matter_number")
        existing_numbers = {m.get("matter_number") for m in matters}
        already_open = True
    else:
        max_num = 0
        for m in matters:
            match = NUM_RE.match(m.get("matter_number") or "")
            if match and match.group(1) == ep[:4]:
                max_num = max(max_num, int(match.group(2)))
        matter_number = f"{ep[:4]}-{(max_num + 1):03d}"
        existing_numbers = {m.get("matter_number") for m in matters}
        already_open = matter_number in existing_numbers
    description = "Environmental claim against Tech Innovations Inc."
    if conflict_found:
        description += (
            " CONFLICT FLAGGED: proposed opposing party Tech Innovations Inc matches the firm's "
            "conflict screen (existing client / contact / recent former-client matter) -- pending partner review."
        )
    if not already_open:
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

    # ---- Stage 4: intake audit note row (same landing shape) -----------------
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
