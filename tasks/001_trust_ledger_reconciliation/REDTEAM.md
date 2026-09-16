# 001_trust_ledger_reconciliation — REDTEAM notes (v2)

## Mission
Run the firm's monthly three-way trust reconciliation per firm policy `TRUST-REC-01`
(seeded in `firm_policies`): recompute every client's true sub-ledger balance from the
`trust_transactions` ledger, correct the stale stored `clients.trust_balance`, backfill
`invoices.trust_applied` from the trust withdrawals that paid them (boundary-delimited
reference matching), push the three-way reconciliation report (whose third leg is the
PRE-correction stored-balance total), and classify exposure tiers using the episode
row's `exposure_tier_cutoff`.

## Why this is hard / unique
- The instruction no longer prints the algorithm (v1 leaked tie-breaks, row shapes and
  the tier cutoff). The agent must read `TRUST-REC-01` and discover: the date-then-id
  ordering rule, the zero-transaction rule, the boundary-delimited reference rule, the
  report scope (clients with ledger activity or a stored balance), and the tier cutoff
  living in the episode row (`ops_meta`, `exposure_tier_cutoff`, 2000-8000).
- Boundary trap: a planted withdrawal's reference contains `INV-2026-0071` — a bare
  substring match mis-applies it to invoice `INV-2026-007` (a plausible wrong value the
  verifier explicitly rejects). Both INV-2026-007 and INV-2026-071 must stay untouched.
- The reconciliation rollup's third leg is the PRE-correction stored-balance total, not
  a recomputation — an agent that "fixes balances first, then reports" loses the third
  leg and writes a plausible wrong `stored_total`/`discrepancy_cents`.
- Stage 4 tiers off the CORRECTED balances, not the seed values — a wrong stage-1
  correction silently mis-tiers clients far from the cause.
- Read-only source ledger: `trust_transactions` must survive byte-identical; the fix
  lives entirely in the derived caches (`clients`, `invoices`) and `ops_reports`.
- Dual-path: per-client balances, transaction counts, report row counts, the tie-out
  sum, and the tier distribution are each computed via raw-row filtering AND an
  independent SQL statement; both paths must agree with each other and with the
  written rows.

## Hazards planted
1. `lapse` — 40 bulk clients' stored `trust_balance` drifted from their ledger
   (planted: 40, snapshot-confirmed plan; per-rebuild drift put the current snapshot at
   44 — the verifier recomputes from the seed snapshot, never hardcodes).
2. `copy_paste_drift` — withdrawal reference contains `INV-2026-0071`; a bare-substring
   match mis-applies it to invoice INV-2026-007 (planted: 6 trap-reference rows in the
   current snapshot; boundary-delimited matching leaves both INV-2026-007 and
   INV-2026-071 unchanged).
3. `normalization_of_deviance` — the ledger is the source of truth and must survive
   byte-identical while ~500 cached client balances and 3000 invoice rows around it get
   corrected/backfilled; bulk `trust_transactions` never reference an invoice by design,
   so the backfill stays bounded to the hand-authored pair (INV-2026-001/002).

## Episode knobs (nonce.extra_fields)
- `exposure_tier_cutoff` (min 2000, max 8000) — read live by gold (`g.nonce(field=...)`)
  and verifier (`vlib.get_nonce(v.token, field=...)`); referenced by field name in the
  instruction, never by value.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate a collection outside blast_radius) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC session.
`par_steps`/`max_steps`: `par_steps` is null pending the live gold measurement in the
QC session; `max_steps` is a generous placeholder (2600) until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: `withdrawal.reference LIKE '%<invoice_number>%'` matches 4 invoice/withdrawal pairs; the boundary-delimited token rule matches 2. The INV-2026-0071 trap withdrawals (2 rows) silently inflate trust_applied for INV-2026-007/-071 under the naive rule.
- **Haystack**: 3000 trust transactions; 4 reference dangling client ids that must be skipped without error.
- **Chain**: client trust_balance corrections (stage 1) feed the reconciliation report rows (stage 2); a wrong match set corrupts `trust_applied` and the audit rows. Verified: gold second-run + idempotent gates pass; random/hardcode/noop all FAIL.
- **Probe**: `outputs/dissolution_probes.py` -> naive pairs=4, boundary pairs=2, dangling-tx=4.
