#!/usr/bin/env python3
"""Dissolution probes for law_firm_software v2 (host-side, snapshot-based).

For every task: the naive "single most obvious query" result set is measured
against the snapshot and compared to the correct rule set. A task's hazard is
dissolved iff naive == correct (the obvious approach produces the right answer
or the target set is trivially isolated). Each printed line is the evidence
recorded in the task's REDTEAM.md.

NOTE: episode-knob-dependent quantities (ops_meta nonce fields) are not in the
snapshot; where a knob gates the cut, the probe measures the STRUCTURAL hazard
(status/vocab/linkage discrimination) that the knob rides on.
"""
import json
import re
from collections import Counter, defaultdict
from datetime import datetime

SNAP = r"D:\Arjav\Code Repos\rl envs\rl_envs\law_firm_software\_expectations\seed_snapshot.json"
snap = json.load(open(SNAP, encoding="utf-8"))
C = snap["collections"]


def rows(c):
    return C[c]["rows"]


def dp(v):
    return str(v)[:10] if v not in (None, "") else None


def dt(v):
    s = dp(v)
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def cents(v):
    from decimal import Decimal
    return int(Decimal(str(v or 0)) * 100)


def norm(name):
    s = str(name or "").lower()
    s = re.sub(r"\b(llc|inc|corp|llp)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


matters = rows("matters")
clients = rows("clients")
invoices = rows("invoices")
deadlines = rows("deadlines")
tasks_ = rows("tasks")
txs = rows("trust_transactions")
contacts = rows("contacts")
docs = rows("ediscovery_documents")
prods = rows("ediscovery_productions")
holds = rows("ediscovery_holds")
cols = rows("ediscovery_collections")
apps = rows("grant_applications")
greports = rows("grant_reports")
awards = rows("grant_awards")
opps = rows("grant_opportunities")
entries = rows("time_entries")
roster = rows("staff_roster")
controls = rows("firm_controls")
reminders = rows("hold_reminders")
policies = rows("firm_policies")

matters_by_id = {str(m["id"]): m for m in matters}
client_by_id = {str(c["id"]): c for c in clients}
client_ids = set(client_by_id)
matter_ids = set(matters_by_id)
EP = datetime.strptime(snap["captured_at"][:10], "%Y-%m-%d").date()

print("=== 001 trust ledger reconciliation ===")
# naive: invoice_number appears ANYWHERE in withdrawal reference
# correct: boundary-delimited token match (non-alnum or boundary both sides)
def boundary(reference, number):
    for m in re.finditer(re.escape(number), reference or ""):
        before = reference[m.start() - 1] if m.start() > 0 else ""
        after = reference[m.end()] if m.end() < len(reference) else ""
        if not before.isalnum() and not after.isalnum():
            return True
    return False

wd = [t for t in txs if t.get("type") == "withdrawal"]
naive_pairs = sum(1 for inv in invoices for t in wd
                  if inv.get("invoice_number") and inv["invoice_number"] in (t.get("reference") or ""))
corr_pairs = sum(1 for inv in invoices for t in wd
                 if inv.get("invoice_number") and boundary(t.get("reference"), inv["invoice_number"]))
trap = [t for t in wd if "INV-2026-0071" in (t.get("reference") or "")]
print(f"naive substring pairs={naive_pairs}  boundary pairs={corr_pairs}  "
      f"trap refs citing INV-2026-0071={len(trap)}  dangling-tx rows="
      f"{sum(1 for t in txs if str(t.get('client_id')) not in client_ids)}")

print("=== 002 privilege clawback ===")
prod_by_prefix = {p.get("bates_prefix"): p for p in prods}
viol = cov = 0
for d in docs:
    b = str(d.get("bates_number") or "")
    if not b or d.get("privilege", "none") == "none":
        continue
    pre = "".join(ch for ch in b if ch.isalpha())
    num = b[len(pre):]
    p = prod_by_prefix.get(pre)
    if not p or p.get("status") not in ("finalised", "served"):
        continue
    if not (int(p.get("bates_start", 0)) <= int(num) <= int(p.get("bates_end", -1))):
        continue
    if p.get("clawback_order_status") == "covered":
        cov += 1
    else:
        viol += 1
print(f"violations={viol} covered-near-miss={cov} "
      f"(naive any-privileged-doc matching over-reports by {cov})")

print("=== 003 hold linkage + ack sweep ===")
broken = [c for c in cols if c.get("hold_id") is not None
          and str(c.get("hold_id")) not in {str(h["id"]) for h in holds}]
print(f"pre-seeded reminders={len(reminders)}  broken collection hold_id refs={len(broken)}  "
      f"active holds={sum(1 for h in holds if h.get('status') == 'active')}")

print("=== 004 grant portfolio integrity ===")
print(f"waived reports={sum(1 for r in greports if r.get('status') == 'waived')}  "
      f"awards with multi-entry schedule={sum(1 for a in awards if len(a.get('reporting_schedule') or []) > 1)}  "
      f"reports dangling award={sum(1 for r in greports if str(r.get('award_id')) not in {str(a['id']) for a in awards})}")

print("=== 005 docket deadline risk ===")
upcoming = [d for d in deadlines if d.get("status") == "upcoming"]
non_open_upcoming = [d for d in upcoming
                     if (matters_by_id.get(str(d.get("matter_id"))) or {}).get("status") != "open"]
prep_rows = [d for d in deadlines if (d.get("title") or "").startswith("Prepare for: ")]
print(f"upcoming deadlines={len(upcoming)}  on non-open matters (naive over-seeds)={len(non_open_upcoming)}  "
      f"stale prep rows in deadlines={len(prep_rows)}")
print("  NOTE: window/lead/urgent thresholds are episode knobs; structural hazard = matter-status + update-in-place")

print("=== 006 new matter intake conflict ===")
# naive: exact-name contact screen; correct: normalized-org match incl. dba variants
print(f"contacts={len(contacts)}  contacts w/ organisation={sum(1 for c in contacts if c.get('organisation'))}  "
      f"distinct normalized orgs matching a client={sum(1 for k in {norm(c.get('organisation')) for c in contacts} if k and k in {norm(c.get('name')) for c in clients})}")

print("=== 007 time entry invoice conversion ===")
appr = [t for t in entries if t.get("status") == "approved"]
zero_rate = [t for t in appr if not (t.get("rate") or 0)]
written_off = [t for t in entries if t.get("status") == "written_off"]
print(f"approved={len(appr)}  zero-rate approved (naive bills them)={len(zero_rate)}  "
      f"written_off (naive flip = corruption)={len(written_off)}")

print("=== 008 realization/utilization ===")
tot_amt = sum(cents(t.get("amount")) for t in entries)
wo_amt = sum(cents(t.get("amount")) for t in written_off)
inv_amt = sum(cents(t.get("amount")) for t in entries if t.get("status") == "invoiced")
naive_basis = tot_amt - wo_amt
if tot_amt and naive_basis:
    print(f"realization incl-wo={round(100.0*inv_amt/tot_amt,2)}%  excl-wo(naive)={round(100.0*inv_amt/naive_basis,2)}%  "
          f"written_off entries={len(written_off)}")

print("=== 009 grant pipeline risk ===")
print(f"applications={len(apps)} by_status={dict(Counter(a.get('status') for a in apps))}")
print(f"decision_date set={sum(1 for a in apps if a.get('decision_date'))}  submitted set={sum(1 for a in apps if a.get('submitted_date'))}")

print("=== 010 review queue progress ===")
sc = Counter(x.get("review_status") for x in docs)
legacy = [x for x in docs if x.get("review_status") == "qc-complete"]
print(f"status vocab={dict(sc)}")
print(f"legacy 'qc-complete' docs (naive raw-vocab count misses them)={len(legacy)}")

print("=== 011 staffing roster ===")
active_roster = {r["employee_id"] for r in roster
                 if r.get("active_from") and dp(r["active_from"]) <= EP.isoformat()
                 and (r.get("active_to") is None or dp(r["active_to"]) >= EP.isoformat())}
dep = [r for r in roster if r.get("active_to") and dp(r["active_to"]) < EP.isoformat()]
dep_ids = {r["employee_id"] for r in dep}
dep_assign = sum(1 for d in deadlines if d.get("assigned_to") in dep_ids) + \
    sum(1 for t in tasks_ if t.get("assigned_to") in dep_ids)
print(f"departed staff={sorted(dep_ids)}  departed-staff assignments (naive appends them)={dep_assign}")

print("=== 012 conflict of interest ===")
cbn = defaultdict(list)
for c in clients:
    cbn[norm(c.get("name"))].append(c)
expected = set()
naive = set()
for ct in contacts:
    k = norm(ct.get("organisation"))
    if not k:
        continue
    for matched in cbn.get(k, []):
        for mid in ct.get("matter_ids") or []:
            m = matters_by_id.get(str(mid))
            if m is None:
                continue
            naive.add((str(ct["id"]), str(matched["id"]), str(mid)))
            mc = client_by_id.get(str(m.get("client_id")))
            if mc and norm(mc.get("name")) != norm(matched.get("name")):
                expected.add((str(ct["id"]), str(matched["id"]), str(mid)))
extra = {k[0] for k in naive} - {e[0] for e in expected}
print(f"true conflicts={len(expected)}  naive extra contacts (name-only matching)={len(extra)}")

print("=== 013 SOL calendaring ===")
cand = [m for m in matters if m.get("matter_type") == "litigation" and m.get("status") == "open" and m.get("statute_of_limitations")]
sbm = defaultdict(list)
for d in deadlines:
    if d.get("type") == "statute":
        sbm[str(d.get("matter_id"))].append(d)
gaps = [m for m in cand if str(m["id"]) not in sbm]
buf = nc = 0
for m in cand:
    for d in sbm.get(str(m["id"]), []):
        if dp(d["due_date"]) < dp(m["statute_of_limitations"]):
            buf += 1
        else:
            nc += 1
print(f"candidates={len(cand)} gaps={len(gaps)} compliant_buffers={buf} noncompliant={nc}")
print(f"  naive 'SOL IS NOT NULL' returns {len(cand)} rows -- field populated at bulk density")

print("=== 014 custodian collection gap ===")
xmat = [c for c in cols if str(c.get("collection_id") or "").startswith("COL-2026-XMAT")]
cov_naive = defaultdict(set)
for c in cols:
    if c.get("custodian_id") is not None:
        cov_naive[str(c["custodian_id"])].add(str(c.get("matter_id")))
cov_pm = defaultdict(set)
for c in cols:
    if c.get("matter_id") is not None:
        cov_pm[str(c["matter_id"])].add(str(c.get("custodian_id")))
traps = 0
for h in holds:
    if h.get("status") != "active":
        continue
    for cust in (h.get("custodians") or []):
        cid = str(cust.get("contact_id"))
        if cid in cov_naive and str(h.get("matter_id")) not in cov_naive[cid]:
            traps += 1
print(f"cross-matter mis-scoped collections={len(xmat)}  naive-matcher traps (under-reported gaps)={traps}")

print("=== 015 client LTV + AR aging ===")
dangling_inv = [i for i in invoices if str(i.get("client_id")) not in client_ids]
drafts = [i for i in invoices if i.get("status") == "draft"]
print(f"dangling-client invoices (naive attributes value to them)={len(dangling_inv)}  "
      f"drafts (naive counts toward LTV)={len(drafts)}")

print("=== 016 top matter profitability ===")
print(f"time_entries={len(entries)} statuses={dict(Counter(t.get('status') for t in entries))}")
print("  naive denominator drops written_off -> shifts top-N ranking; verifier recomputes")

print("=== 017 grant budget burn ===")
stale = 0
exp_by_award = defaultdict(int)
for e in rows("grant_expenses"):
    exp_by_award[str(e.get("award_id"))] += cents(e.get("amount"))
for a in awards:
    if cents(a.get("total_spent")) != exp_by_award.get(str(a["id"]), 0):
        stale += 1
print(f"awards={len(awards)}  stale cached total_spent={stale}  (naive trusts cache; correct recomputes)")

print("=== 018 chain of custody ===")
n_with_custody = sum(1 for c in cols if c.get("chain_of_custody"))
print(f"collections with chain_of_custody events={n_with_custody} of {len(cols)}  "
      f"empty-chain collections={len(cols) - n_with_custody}")

print("=== 019 billing disruption ===")
tot = Counter(); bad = Counter()
for inv in invoices:
    iss = dp(inv.get("issued_date"))
    if not iss:
        continue
    age = (EP - datetime.strptime(iss, "%Y-%m-%d").date()).days
    if age < 0:
        continue
    k = age // 30
    tot[k] += 1
    if inv.get("status") in ("overdue", "disputed", "written_off"):
        bad[k] += 1
rates = {k: 100.0 * bad[k] / tot[k] for k in tot}
med = sorted(rates.values())[len(rates) // 2] if rates else 0
mean = sum(rates.values()) / len(rates) if rates else 0
print(f"age buckets={len(tot)}  median bad-rate={round(med,1)}  mean bad-rate={round(mean,1)}  "
      f"buckets above mean (naive flag)={sum(1 for r in rates.values() if r > mean)}")

print("=== 020 grant coauthor workload ===")
print(f"apps led by departed EMP-006={sum(1 for a in apps if a.get('lead_author') == 'EMP-006')}  "
      f"co-author entries by EMP-006={sum(1 for a in apps if 'EMP-006' in (a.get('co_authors') or []))}")

print("=== 021 invoice aging dispute risk ===")
disputed = [i for i in invoices if i.get("status") == "disputed"]
open_inv = [i for i in invoices if i.get("status") in ("sent", "overdue", "disputed")]
print(f"open invoices={len(open_inv)}  disputed (naive excludes from aging)={len(disputed)}")

print("=== 022 firmwide KPI pack ===")
print("multi-figure pack; each KPI dual-pathed in verifier (see verifier.py).")

print("=== 023 temporal anomaly ===")
inv_bad = sum(1 for i in invoices if dp(i.get("due_date")) and dp(i.get("issued_date")) and dp(i["due_date"]) < dp(i["issued_date"]))
mat_bad = sum(1 for m in matters if m.get("date_closed") and dp(m["date_closed"]) < dp(m.get("date_opened")))
doc_bad = sum(1 for x in docs if x.get("reviewed_at") and x.get("date_created") and dp(x["reviewed_at"]) < dp(x["date_created"]))
grant_le = sum(1 for a in apps if a.get("decision_date") and a.get("submitted_date") and dp(a["decision_date"]) <= dp(a["submitted_date"]))
grant_lt = sum(1 for a in apps if a.get("decision_date") and a.get("submitted_date") and dp(a["decision_date"]) < dp(a["submitted_date"]))
print({"invoice_due<issued": inv_bad, "matter_closed<opened": mat_bad,
       "doc_reviewed<created": doc_bad,
       "grant decision <= submitted (correct)": grant_le,
       "grant decision < submitted (naive strict)": grant_lt})

print("=== 024 referential integrity ===")
ph = sum(1 for coll, f in [("deadlines", "matter_id"), ("tasks", "matter_id"), ("time_entries", "matter_id"),
                           ("invoices", "client_id"), ("trust_transactions", "client_id")]
         for r in rows(coll) if str(r.get(f) or "").startswith("PLACEHOLDER-"))
print({"deadlines_dangling": sum(1 for d in deadlines if d.get("matter_id") is not None and str(d["matter_id"]) not in matter_ids and not str(d["matter_id"]).startswith("PLACEHOLDER-")),
       "tasks_dangling": sum(1 for t in tasks_ if t.get("matter_id") is not None and str(t["matter_id"]) not in matter_ids and not str(t["matter_id"]).startswith("PLACEHOLDER-")),
       "te_dangling": sum(1 for t in entries if t.get("matter_id") is not None and str(t["matter_id"]) not in matter_ids and not str(t["matter_id"]).startswith("PLACEHOLDER-")),
       "inv_dangling": sum(1 for i in invoices if i.get("client_id") is not None and str(i["client_id"]) not in client_ids and not str(i["client_id"]).startswith("PLACEHOLDER-")),
       "tx_dangling": sum(1 for t in txs if t.get("client_id") is not None and str(t["client_id"]) not in client_ids and not str(t["client_id"]).startswith("PLACEHOLDER-")),
       "te_inactive_emp": sum(1 for t in entries if t.get("employee_id") is not None and t["employee_id"] not in active_roster and not str(t["employee_id"]).startswith("PLACEHOLDER-")),
       "placeholders (naive counts as violations)": ph})

print("=== 025 billing arrangement ===")
cont = {str(c["id"]) for c in clients if c.get("preferred_billing") == "contingency"}
nd = [i for i in invoices if str(i.get("client_id")) in cont and i.get("status") != "draft"]
disb_only = [i for i in nd if (i.get("fees_total") or 0) == 0 and (i.get("disbursements_total") or 0) > 0]
print(f"contingency clients={len(cont)}  non-draft invoices to them={len(nd)}  "
      f"disbursement-only among them (naive flags)={len(disb_only)}")
print("  NOTE: audit_top_clients is an episode knob; structural hazard = draft/disb-only exclusions + ranked slice")

print("=== 026 month-end close pack ===")
ages = [(EP - datetime.strptime(dp(i["issued_date"]), "%Y-%m-%d").date()).days
        for i in invoices if dp(i.get("issued_date"))]
print(f"invoices within 365d span={sum(1 for a in ages if a <= 365)} of {len(ages)}  "
      f"7 chained stages (see verifier docstring)")
