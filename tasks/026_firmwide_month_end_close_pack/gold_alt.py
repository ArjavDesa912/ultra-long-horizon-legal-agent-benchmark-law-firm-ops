#!/usr/bin/env python3
"""Independent second gold for 026_firmwide_month_end_close_pack (v2).

Materially different path from gold.py: the trust reconciliation is a SQL
DISTINCT ON, the payment-matching backfill is a single boundary-safe SQL join
(Postgres regex, not a Python substring scan), the billing candidates are
SQL-ranked, and every stage-4/5/6 number is a SQL aggregate. Writes still go
through the REST API (SQL is read-only in this env). Run against a FRESH
container."""
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import glib  # noqa: E402

INV_RE = re.compile(r"^INV-(\d{4})-(\d+)$")
MUTATED_REPORTS = ("ar_aging_history", "close_pack", "close_tieout")


def sql_retry(g, query, attempts=6):
    """Ride out transient BaaS 503s (pool churn right after episode reset)."""
    import time
    for i in range(attempts):
        try:
            return getattr(g, "sql")(query)
        except RuntimeError as e:
            if "503" not in str(e) or i == attempts - 1:
                raise
            time.sleep(2.0 * (i + 1))


def month_ends_before(ep_dt, n=12):
    out = []
    y, m = ep_dt.year, ep_dt.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append(date(y, m, monthrange(y, m)[1]))
    return out


def main():
    g = glib.Gold()
    batch = g.nonce()
    top_n = int(g.nonce(field="top_n"))
    due_offset = int(g.nonce(field="due_offset_days"))
    ep = glib.Gold.dp(g.nonce(field="episode_date"))
    ep_dt = datetime.strptime(ep, "%Y-%m-%d").date()
    due = (ep_dt + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    try:
        existing = g.all("ops_reports")
    except RuntimeError:
        existing = []
    for r in existing:
        if r.get("report") in MUTATED_REPORTS and r.get("batch_code") == batch:
            g.delete("ops_reports", r["id"])

    # ---- STAGE 0: capture the opening state (push-once, SQL aggregates) ----
    if not any(r.get("report") == "close_controls" and r.get("batch_code") == batch
               for r in existing):
        wip_control = sql_retry(g, 
            "SELECT COALESCE(SUM(amount), 0) AS n FROM time_entries "
            "WHERE status IN ('draft','submitted','approved')")[0]["n"]
        ar_control = sql_retry(g, 
            "SELECT COALESCE(SUM(total - amount_paid - trust_applied), 0) AS n FROM invoices "
            "WHERE status IN ('sent','overdue','disputed') "
            "AND (total - amount_paid - trust_applied) > 0")[0]["n"]
        trust_control = sql_retry(g, "SELECT COALESCE(SUM(trust_balance), 0) AS n FROM clients")[0]["n"]
        ranked = sql_retry(g, 
            "SELECT matter_id, SUM(amount) AS approved_value FROM time_entries "
            "WHERE status = 'approved' GROUP BY matter_id HAVING SUM(amount) > 0"
        )
        matters_meta = {str(r["id"]): r["matter_number"] for r in sql_retry(g, "SELECT id, matter_number FROM matters")}
        ordered = sorted(((float(r["approved_value"]), str(r["matter_id"])) for r in ranked),
                         key=lambda t: (-t[0], matters_meta.get(t[1], "")))
        candidates = [{"matter_id": mid, "approved_value": val} for val, mid in ordered[:top_n]]
        g.push("ops_reports", {
            "report": "close_controls", "batch_code": batch,
            "wip_control": wip_control, "ar_control": ar_control,
            "trust_control": trust_control, "billing_candidates": candidates,
        })
    controls_row = next(r for r in g.all("ops_reports")
                        if r.get("report") == "close_controls" and r.get("batch_code") == batch)
    candidates = controls_row.get("billing_candidates") or []

    # ---- STAGE 1: trust reconciliation (SQL DISTINCT ON) -------------------
    latest = sql_retry(g, 
        "SELECT DISTINCT ON (client_id) client_id, balance_after FROM trust_transactions "
        "ORDER BY client_id, tx_date DESC, id DESC"
    )
    true_balance = {str(r["client_id"]): r["balance_after"] for r in latest}
    for c in g.all("clients"):
        cid = str(c["id"])
        want = true_balance.get(cid, 0)
        if c.get("trust_balance") != want:
            g.update("clients", c["id"], {"trust_balance": want})

    # ---- STAGE 2: trust-applied backfill (boundary-safe SQL join) ----------
    # Token-extraction form of the same boundary rule (TRUST-REC-01): pull the
    # INV-YYYY-NNN tokens out of each withdrawal reference, then hash-join on
    # exact invoice_number. Equivalent to the pairwise boundary regex but O(rows)
    # instead of a 20k x 3k nested loop that starves the BaaS connection pool.
    matches = sql_retry(g,
        "SELECT i.id AS invoice_id, MAX(tok.amount) AS best FROM invoices i "
        "JOIN (SELECT t.amount AS amount, (regexp_matches(t.reference, "
        "'(^|[^A-Za-z0-9])(INV-[0-9]{4}-[0-9]+)($|[^A-Za-z0-9])', 'g'))[2] AS inv_num "
        "FROM trust_transactions t WHERE t.type = 'withdrawal' "
        "AND t.reference IS NOT NULL) tok "
        "ON tok.inv_num = i.invoice_number "
        "WHERE i.invoice_number IS NOT NULL "
        "GROUP BY i.id"
    )
    backfill = {str(r["invoice_id"]): r["best"] for r in matches}
    for inv in g.all("invoices"):
        want = backfill.get(str(inv["id"]))
        if want is not None and inv.get("trust_applied") != want:
            g.update("invoices", inv["id"], {"trust_applied": want})

    # ---- STAGE 3: bill the candidates (SQL-ranked, per-matter SQL) ---------
    invoices_all = g.all("invoices")
    max_year, max_num = "2026", 0
    for inv in invoices_all:
        m = INV_RE.match(inv.get("invoice_number") or "")
        if m:
            max_year = m.group(1)
            max_num = max(max_num, int(m.group(2)))
    matters_full = {str(m["id"]): m for m in g.all("matters")}
    due = (datetime.strptime(ep, "%Y-%m-%d") + timedelta(days=due_offset)).strftime("%Y-%m-%d")

    billed_value = 0
    new_invoices = []
    for cand in sorted(candidates, key=lambda c: matters_meta.get(str(c["matter_id"]), "")):
        mid = str(cand["matter_id"])
        # BILL-GEN-01: zero-rate approved entries are skipped.
        # matter_id is a jsonb column: the literal must be quoted (jsonb = '14'
        # casts to jsonb numeric equality); an unquoted int has no operator and
        # the BaaS surfaces the planner error as a misleading HTTP 503.
        entries = sql_retry(g,
            f"SELECT id, entry_date, description, hours, rate, amount FROM time_entries "
            f"WHERE matter_id = '{int(mid)}' AND status = 'approved' AND rate > 0 ORDER BY id::text"
        )
        if not entries:
            continue  # already billed by an earlier run of this close
        matter = matters_full.get(mid) or {}
        max_num += 1
        line_items = [
            {"entry_id": te["id"], "date": te.get("entry_date"),
             "description": te.get("description"), "hours": te.get("hours"),
             "rate": te.get("rate"), "amount": te.get("amount")}
            for te in entries
        ]
        fees_total = sum(te.get("amount", 0) or 0 for te in entries)
        g.push("invoices", {
            "invoice_number": f"INV-{max_year}-{max_num:03d}",
            "matter_id": mid, "client_id": matter.get("client_id"),
            "line_items": line_items, "fees_total": fees_total,
            "disbursements_total": 0, "tax": 0, "total": fees_total,
            "amount_paid": 0, "trust_applied": 0, "status": "sent",
            "issued_date": ep + "T00:00:00.000Z", "due_date": due + "T00:00:00.000Z",
        })
        billed_value += fees_total
        new_invoices.append({"total": fees_total, "amount_paid": 0,
                             "trust_applied": 0, "status": "sent",
                             "issued": ep, "due": due})
        for te in entries:
            g.update("time_entries", te["id"], {"status": "invoiced"})

    # ---- STAGE 4: point-in-time AR aging (per-month-end SQL aggregate) -----
    # The stage-3 invoices are all issued at the episode date, after every
    # historical month-end by construction, so the live table state already
    # excludes them from every historical snapshot.
    for month_end in month_ends_before(ep_dt):
        me = month_end.strftime("%Y-%m-%d")
        rows = sql_retry(g, 
            "SELECT CASE WHEN d.age <= 0 THEN 'current' WHEN d.age <= 30 THEN '1-30' "
            "WHEN d.age <= 60 THEN '31-60' WHEN d.age <= 90 THEN '61-90' ELSE '90+' END "
            "AS bucket, COUNT(*) AS n, SUM(d.unpaid) AS total FROM ("
            "  SELECT i.id, ('" + me + "'::date - i.due_date::date) AS age, "
            "         (i.total - i.amount_paid - i.trust_applied) AS unpaid "
            "  FROM invoices i WHERE i.issued_date::date <= '" + me + "'::date "
            "  AND i.status IN ('sent','overdue','disputed')"
            ") d WHERE d.unpaid > 0 GROUP BY 1"
        )
        for r in rows:
            g.push("ops_reports", {
                "report": "ar_aging_history", "batch_code": batch, "month_end": me,
                "bucket": r["bucket"], "invoice_count": int(r["n"]),
                "unpaid_total": r["total"],
            })

    # ---- STAGE 5: close pack rollup (SQL aggregates) -----------------------
    total_trust_liability = sql_retry(g, "SELECT COALESCE(SUM(trust_balance), 0) AS n FROM clients")[0]["n"]
    total_wip_value = sql_retry(g, 
        "SELECT COALESCE(SUM(amount), 0) AS n FROM time_entries "
        "WHERE status IN ('draft','submitted','approved')")[0]["n"]
    total_open_ar = sql_retry(g, 
        "SELECT COALESCE(SUM(total - amount_paid - trust_applied), 0) AS n FROM invoices "
        "WHERE status IN ('sent','overdue','disputed') "
        "AND (total - amount_paid - trust_applied) > 0")[0]["n"]
    g.push("ops_reports", {
        "report": "close_pack", "batch_code": batch,
        "total_trust_liability": total_trust_liability,
        "invoices_generated": len(candidates),
        "total_wip_value": total_wip_value,
        "total_open_ar": total_open_ar,
    })

    # ---- STAGE 6: tie-out against the captured opening controls ------------
    wip_drop = (controls_row.get("wip_control") or 0) - total_wip_value
    ar_delta = total_open_ar - (controls_row.get("ar_control") or 0)
    billed = sum(cand.get("approved_value", 0) or 0 for cand in candidates)
    g.push("ops_reports", {
        "report": "close_tieout", "batch_code": batch,
        "wip_drop": wip_drop, "billed_value": billed,
        "ar_delta": ar_delta,
        "ties_out": glib.Gold.cents(wip_drop) == glib.Gold.cents(billed),
    })

    print(f"gold_alt done in {g.steps} API calls")


if __name__ == "__main__":
    main()
