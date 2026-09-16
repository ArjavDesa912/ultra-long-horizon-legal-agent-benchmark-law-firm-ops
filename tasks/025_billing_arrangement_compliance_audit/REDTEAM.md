# 025_billing_arrangement_compliance_audit — REDTEAM notes (v2 stub)

## Mission
Audit the firm's billing-arrangement compliance per firm policy BILL-ARR-01:
the arrangement matrix (contingency clients may only receive draft invoices;
flat/hourly any status), the disbursement-only exception, and the audit's
bounded scope — the top `audit_top_clients` (episode knob, 20–30) contingency
clients ranked by total non-draft billed value. One violation row per violating
invoice with a diagnosis naming the bypassed arrangement, then per-client
exposure rows, alert rows above the episode row's `exposure_alert_threshold`,
and one summary row.

## Why this is hard / unique
- **The scope is a ranked slice, not a filter the instruction states.** The
  agent must derive from BILL-ARR-01 that only the top `audit_top_clients`
  contingency clients (by total non-draft billed value) are in scope — auditing
  all 113 contingency clients over-reports several-fold and FAILs.
- **Near-miss invoice shapes the policy explicitly permits.** Draft invoices to
  contingency clients (99 in the snapshot), disbursement-only invoices, and
  identically-shaped invoices to fixed/hourly/retainer clients must all stay
  unflagged; each class is a plausible false positive.
- **Diagnosis is graded.** Each violation row's note must classify the cause —
  the bypassed contingency fee agreement — not just flag the invoice.
- The exposure-alert threshold is an episode knob (60000–75000), so the alert
  set moves per episode; the exposure sums are dual-path asserted.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `scope_boundary` — the ranked slice: auditing beyond the top-N contingency
   clients (113 exist, 557 non-draft invoices to them) FAILs.
2. `mistake` — permitted shapes: 99 draft invoices to contingency clients and
   the disbursement-only class (0 in this snapshot) must stay unflagged;
   non-contingency clients carry identically-shaped invoices.
3. `violation` — the planted violations: 1 hand-authored (CLI-2026-002 /
   INV-2026-002) plus the bulk population — 167 violating invoices in the
   top-20 slice, 202 in top-25, 234 in top-30 (snapshot-confirmed).

## Phase 4 — NOT YET RUN
The hacker-fixer loop runs in the QC session against `rl-env/law_firm_software:latest`:
1. No-op baseline — verifier must FAIL on pristine state.
2. Random-action baseline — 10 valid-API junk actions, verifier must FAIL.
3. Lazy-hardcode baseline — plausible static rows with a stale batch_code, verifier must FAIL.
4. Gold run + verifier PASS, verifier rerun (idempotent), gold second run + verifier PASS.
5. Canary/scope violation — mutate a row outside blast_radius after a correct gold run, must FAIL.
6. Partial-completion — kill gold.py partway, verifier must FAIL; retry/flake — verifier 5x identical.

## Dissolution probe & chain evidence (snapshot-measured)

- **Ranked slice**: violations are only in-scope inside the episode's audit_top_clients slice of the contingency ranking; a firmwide pass over-counts.
- **Disbursement-only exemption**: a planted INV-2026-0000 (fees=0, disb=250000) sits on the TOP-billed contingency client — inside every slice — and must NOT be flagged (BILL-EXPOSE-01). Drafts (451) never count.
- **Probe**: `outputs/dissolution_probes.py` -> contingency=113, non-draft=557, disb-only planted=1 (post-reseed).
