#!/usr/bin/env python3
"""
Capture the host-side seed snapshot for the law_firm_software env.

Runs against a FRESH container (no episode actions taken) and writes
_expectations/seed_snapshot.json containing, per collection: row count,
canonical sha256 (the canary hash), and the full original row set.

This file lives host-side only. It must never be baked into the image.

Usage:
    STACKHOUSE_API_URL=http://127.0.0.1:<mapped_baas_port> python tools/capture_snapshot.py

Re-run after every image rebuild so expectations track the seed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vlib  # noqa: E402

OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_expectations"))
OUT_PATH = os.path.join(OUT_DIR, "seed_snapshot.json")

COLLECTIONS = [
    "clients", "matters", "contacts", "deadlines", "tasks", "time_entries",
    "invoices", "trust_transactions",
    "ediscovery_holds", "ediscovery_collections", "ediscovery_documents", "ediscovery_productions",
    "grant_opportunities", "grant_applications", "grant_awards", "grant_reports", "grant_expenses",
    # Firm-governance reference data (v2): read-only for every task, always canaried
    "firm_policies", "staff_roster", "firm_controls",
    # Pre-seeded reminder history (task 003's dedupe/format hazards live here)
    "hold_reminders",
]


def main():
    from datetime import datetime, timezone

    token = vlib.login("rl-admin@rl.local", "RLVerifier2025!")
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "stackhouse_url": vlib.STACKHOUSE_API_URL,
        "collections": {},
    }
    total = 0
    for coll in COLLECTIONS:
        rows = vlib.fetch_all(token, coll)
        snapshot["collections"][f"{vlib.PREFIX}{coll}"] = {
            "count": len(rows),
            "sha256": vlib.sha(rows),
            "rows": rows,
        }
        total += len(rows)
        print(f"  {coll}: {len(rows)} rows  sha256={vlib.sha(rows)[:12]}...")
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=1, default=str)
    os.replace(tmp, OUT_PATH)
    print(f"\nSnapshot written: {OUT_PATH} ({len(COLLECTIONS)} collections, {total} rows)")


if __name__ == "__main__":
    main()
