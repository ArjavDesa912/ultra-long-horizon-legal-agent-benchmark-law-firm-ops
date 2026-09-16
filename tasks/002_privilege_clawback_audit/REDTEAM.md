# 002_privilege_clawback_audit — REDTEAM notes (v2)

## Mission
Audit the firm's finalised and served e-discovery productions for privileged material
that left the firm, per the seeded clawback procedure `PRIV-CLAWBACK-01`: derive
production membership from the numeric Bates range (no stored link exists), log each
policy violation to `privilege_log` with its privilege classification, roll the log up
per matter, and push a firm-wide summary row.

## Why this is hard / unique
- Membership is derived, not stored: which documents a production covers is computed
  from the Bates prefix + numeric range, not a foreign key on the document.
- The covered/none 502(d) distinction, the draft-production exclusion, and the
  attorney_client vs work_product classification all live in the seeded policy — the
  instruction names only the symptom ("privileged material left the firm").
- Three near-miss classes each fail a different naive solution: flagging covered
  productions (over-reports by 4), logging draft-range documents, and privilege-only
  scanning with no range check (over-reports by 338 in the current snapshot).
- Read-only source records: the violation is logged externally, never by editing the
  document or production (mirrors real clawback practice).
- Dual-path: in-range and violation sets are recomputed via raw-row filtering AND an
  independent SQL join (Bates prefix/suffix split via SQL regex) inside the verifier.

## Hazards planted
1. `mistake` — 7 in-range privileged documents on uncovered finalised/served
   productions (planted: 6 bulk + the hand-authored DEF000001; snapshot-confirmed: 7).
   Each must be logged with its privilege classification in the reason.
2. `violation` — 4 in-range privileged documents sit on productions with
   `clawback_order_status='covered'` (planted, snapshot-confirmed: 4); PRIV-CLAWBACK-01
   says covered disclosures are not violations — flagging any = FAIL.
3. `lapse` — 37 draft productions in the current snapshot (snapshot-confirmed) are
   excluded by policy; logging any draft-range document = FAIL.
4. `slip` — 338 filler documents carry privilege != none but sit OUTSIDE every
   production Bates range (snapshot-confirmed: 338); a privilege-only scan with no
   range check over-reports by exactly that many.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate ediscovery_documents/productions) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC session.
`par_steps` is null pending the live gold measurement; `max_steps` (220) is a generous
placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: privileged documents inside a finalised/served production's bates range flags 11 rows; the clawback-order-covered exclusion drops 4, leaving 7 true violations.
- **Chain**: per-violation flag rows -> summary row -> log entries all derive from the same violation set; a naive over-report propagates to every stage.
- **Probe**: `outputs/dissolution_probes.py` -> violations=7, covered-near-miss=4.
