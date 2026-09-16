# 007_time_entry_invoice_conversion — REDTEAM notes (v2)

## Mission
Run the firm's priority invoice generation batch per the seeded policy `BILL-GEN-01`:
snapshot the firm's work-in-progress before touching anything, bill the top-N matters by
approved value (N and the payment-due offset come from the episode row), number the
invoices continuing the live max INV-YYYY-NNN sequence with the lowest matter number
first, flip exactly the entries each invoice covers, then report the realization impact
(before vs after) and an exception row for the ranked-out matters.

## Why this is hard / unique
- Four dependent stages: the WIP snapshot must capture per-matter approved value BEFORE
  any mutation; the realization-impact row's after-value is driven by the batch's own
  exact flip set (a wrong flip corrupts it silently, far from the cause); the exception
  row accounts for the ranked-out matters.
- Two episode knobs (`top_n` 10-20, `due_offset_days` 21-45) move the cut and every date
  each episode — memorized constants go stale by design.
- Three matter states must be told apart per policy: has billable approved entries (get
  an invoice), all-already-invoiced (excluded by the ranking itself), zero-rate approved
  entries (skipped) — plus written_off work priced at full amount in the data that must
  never be billed.
- Numbering continues the highest existing INV-YYYY-NNN with the lowest matter number
  billed first; the verifier derives the whole expected set from the SEED snapshot, so it
  stays idempotent across reruns (live state has no 'approved' rows left after a correct
  run).

## Hazards planted
1. `slip` — top-N cut boundary: matters ranked just outside the episode row's top_n
   (10-20) carry real approved work and must NOT be billed; snapshot background: 1914
   matters carry approved value, so an unbounded "bill everything" run balloons into
   thousands of invoices.
2. `violation` — written_off entries are priced at full amount in the data
   (snapshot-confirmed: 5971 written_off entries); BILL-GEN-01 forbids billing them —
   billing them or counting them as approved value produces plausible wrong invoices.
3. `lapse` — zero-rate approved entries (snapshot-confirmed: 1256) must be skipped per
   policy; including them flips non-billable entries and corrupts line items and the
   realization delta.
4. `copy_paste_drift` — numbering continues the highest existing INV-YYYY-NNN with the
   lowest matter number billed first; reusing a memorized max or ordering by matter id
   instead of matter_number silently mis-numbers the batch.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate matters/clients outside blast_radius) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC session.
`par_steps` is null pending the live gold measurement; `max_steps` (3400) is a generous
placeholder until that measurement lands (the WIP snapshot alone pushes ~1900 rows).

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: billing every approved entry (or the wrong top-N) — 1256 of 6103 approved entries are zero-rate and must be skipped; 5971 written_off entries must never flip.
- **Chain**: WIP snapshot (stage 1) -> invoice generation + flips (stage 2) -> realization impact derives from the flip set (stage 3) -> exceptions from the cut (stage 4). A wrong flip set silently corrupts `realization_after_pct`.
- **Idempotency**: batch invoices carry `batch_code`; re-runs reconstruct the pre-batch universe from covered entries. Verified: gold + second-run + idempotent + alt-gold all PASS.
- **Probe**: `outputs/dissolution_probes.py` -> approved=6103, zero-rate=1256, written_off=5971.
