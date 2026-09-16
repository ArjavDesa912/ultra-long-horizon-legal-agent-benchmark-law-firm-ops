# RECON — law_firm_software (Phase 0)

`SOFTWARE_DIR = example-for-baas-main/softwares/law_firm_software`
`OUTPUT_DIR = rl_envs/law_firm_software`
Bundle: `bundle/law_firm_software/` (unified Dockerfile, image `rl-env/law_firm_software:latest`).
Admin app login: `admin@lawfirm.com` / `Admin123!` (app-level, not used by tasks).
Verifier/gold login: `rl-admin@rl.local` / `RLVerifier2025!` (same convention as `veterinary_clinic_system`).
App port 3005 (frontend), Stackhouse BaaS port 9090. Collection prefix `law_firm_software_`.

Seed script: `example-for-baas-main/softwares/law_firm_software/scripts/seed.js` — hand-authored
fixed arrays (not procedurally scaled like vet clinic's hardmode pass), all dates computed as
`subtractDays(N)`/`addDays(N)` from `today = new Date()` **at image build time**. This is a
different anchoring scheme from `veterinary_clinic_system` (which bakes absolute fictional-2026
dates and needs a fixed calendar anchor) — see BUILD_NOTES.md FIX-1.

## Live row counts (from seed.js arrays, confirmed by reading the script; re-verify against
`GET /v1/tables` once the image builds)

| collection | rows | notes |
|---|---|---|
| clients | 5 | CLI-2026-001..005 |
| matters | 7 | 2026-001..007 (one, 006, is `suspended`/closed) |
| contacts | 6 | opposing_counsel, witness, expert, judge, court_officer |
| deadlines | 10 | |
| tasks | 12 | |
| time_entries | 16 | |
| invoices | 3 | INV-2026-001..003 |
| trust_transactions | 8 | |
| ediscovery_holds | 3 | HOLD-2026-001..003 |
| ediscovery_collections | 3 | COL-2026-001..003 |
| ediscovery_documents | 5 | DOC-001..005 |
| ediscovery_productions | 1 | PROD-2026-001 |
| grant_opportunities | 7 | OPP-2026-001..007 |
| grant_applications | 3 | APP-2026-001..003 |
| grant_awards | 1 | AWD-2026-001 |
| grant_reports | 1 | RPT-2026-001 |
| grant_expenses | 1 | |

No `employees` collection exists — `employee_id` (e.g. `EMP-001`) is a soft string reference
scattered across `matters.team_members`/`time_entries`/`deadlines`/`tasks`; the 5-person roster
(names/roles) lives only in the seed script, never pushed to Stackhouse. Tasks that need the
roster state it inline in the instruction (not a leaked *answer*, just a reference fact — same
as giving a contractor an org chart).

## Confirmed real defects mined from the seed data (grounding for Phase 1 hazards)

1. **`clients.trust_balance` is a stale cache.** Recomputing the running trust balance from
   `trust_transactions` (chronological, `balance_after`) disagrees with the stored
   `clients.trust_balance` field for 4 of 5 clients (001: 50000 vs true 46625; 002: 25000 vs
   22500; 004: 0 vs 10000; 005: 100000 vs 95000). Only client 003 (75000) is already correct.
2. **`invoices.trust_applied` never backfilled.** INV-2026-001 and INV-2026-002 were paid via a
   trust withdrawal (`trust_transactions.reference` names the invoice number explicitly), but
   `trust_applied` on both invoices is still 0.
3. **`ediscovery_collections.hold_id` is broken.** All 3 rows use placeholder strings
   (`'hold-001'`, `'hold-002'`) that don't match any real `ediscovery_holds.id` — the real ids
   are Stackhouse-assigned. Correct hold is derivable via matching `matter_id`.
4. **Legal hold custodian acknowledgement gaps.** HOLD-2026-001: 1/3 unacknowledged (David
   Engineer). HOLD-2026-003 (SEC investigation, issued 15 days ago): 0/2 acknowledged.
5. **Privilege clawback exposure.** Production PROD-2026-001 (bates `DEF000001`-`DEF000002`,
   status `finalised`, served to opposing counsel) includes `DEF000001` (DOC-001), which carries
   `privilege: 'attorney_client'` — a produced document that should never have left the firm.
   `DEF000002` (privilege `none`) is correctly in-range; `DEF000003` (DOC-005, privilege
   `work_product`) is outside the production's bates range and correctly excluded — a real
   boundary decoy.
6. **Grant application client/matter mismatch.** APP-2026-001 and APP-2026-003 both link to
   matter 2026-001 (client CLI-2026-001/Acme) but were seeded with unrelated `client_id`s
   (CLI-2026-003, CLI-2026-004). APP-2026-002 is internally consistent.
7. **Grant reporting schedule drift.** `grant_awards.AWD-2026-001.reporting_schedule` declares 3
   required reports (progress +90d, financial +180d, final +390d); `grant_reports` has only one
   row (progress), and its `due_date` (+5d) doesn't match the schedule's own +90d for that type.
8. **E-discovery collection-vs-review-queue gap.** `ediscovery_collections` claim thousands of
   collected items (2453 / 156 / 567) but only 5 `ediscovery_documents` rows exist total across
   all three collections — a realistic processing backlog, not an error to fix, but a real KPI
   to report.

## Fix required before task design (Hard Rule 1 — minimal, logged in BUILD_NOTES.md)

`env.py`'s `_inject_nonce` hardcoded a `veterinary_clinic_system`-specific fixed calendar anchor
(`2026-09-30`) copied over during scaffolding. Since this app's seed dates are relative offsets
from real wall-clock build time (not absolute fictional dates), the anchor must be real
`datetime.now(timezone.utc)` at grading time instead, jittered ±1 day (not ±3 — there's no
month-end slack to jitter within here). Fixed in this pass.

## Stackhouse endpoints used (per `STACKHOUSE_LLM_PROMPT.md`, same API as vet clinic)

`POST /v1/auth/login`, `GET /v1/query/{c}`, `GET /v1/query/{c}/{id}`, `POST /v1/push/{c}`,
`POST /v1/update/{c}/{id}`, `POST /v1/delete/{c}/{id}`, `POST /v1/sql/query` (read-only for
verifiers). `GET /v1/tables` for the live collection inventory.

## Scale-up pass (same session) — corrections to the above

Two facts above turned out to be wrong once actually queried against a live container, both
logged in full in BUILD_NOTES.md:

- **Collection prefix is `""`, not `law_firm_software_`.** `GET /v1/tables` against a fresh
  container shows unprefixed table names (`clients`, `matters`, ...); `/rl/manifest.json`
  inside the image claims the vet-clinic-style prefix and is simply wrong for this app. Every
  task's raw SQL and `tools/{glib,vlib}.py`'s `PREFIX` constant were fixed to `""` accordingly.
- **`time_entries.date`, `trust_transactions.date`, `grant_expenses.date` never actually
  seeded** — Stackhouse rejects the bare identifier `date` as a push field (a SQL reserved
  keyword at the push-validation layer), silently dropping every row to those 3 collections.
  Confirmed via the build log, not assumed. Fixed in the SOURCE seed script by renaming to
  `entry_date`/`tx_date`/`expense_date`.

Per operator direction, the seed was then scaled from the ~93-row v1 fixture set to ~50,250
rows (500 clients, 2000 matters, 800 contacts, 3000 deadlines, 3000 tasks, 30000 time_entries,
3000 invoices, ~4000 trust_transactions, 100 e-discovery holds, 200 collections, 2000
documents, 100 productions, 300 grant opportunities, 300 applications, 150 awards, 300 reports,
500 expenses) via a bulk-generation section appended to `seed.js` after every hand-authored
fixture. See BUILD_NOTES.md for the exact allocation, the planted-chaos design (client
trust_balance, grant application client_id, e-discovery hold_id/collection-gap, privilege
clawback, matter staffing gaps, a §5a-style billing-disruption window, contingency billing
violations — each a counted, taxonomy-tagged subset, not incidental noise from independent
randomization), and three real seed-generation bugs this pass caught and fixed by re-verifying
counts against the actual captured snapshot rather than trusting the generator code.

All 8 "confirmed real defects" listed above from the original ~93-row seed remain individually
true and unchanged at the original ids (the bulk pass only appends, never touches an existing
row) — they're the exact hazards tasks 001-004/012 target.
