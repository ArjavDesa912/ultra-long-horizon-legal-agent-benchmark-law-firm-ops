# 026_firmwide_month_end_close_pack — REDTEAM notes (v2 stub)

## Mission
A chained SEVEN-stage month-end close: (0) capture the opening control totals
(WIP / open AR / trust) and the billing candidates the close will bill, (1)
trust reconciliation per TRUST-REC-01, (2) the status-blind, boundary-delimited
trust-applied backfill, (3) bill the policy's top-N candidates and flip exactly
the entries billed, (4) reconstruct the AR aging as it stood at each of the 12
calendar month-ends preceding the episode month, (5) the close pack rollup,
(6) a tie-out of the opening controls against the final state (the WIP drop
must equal the billed value; the AR delta must include the new invoices).
Every rule lives in firm policy (TRUST-REC-01, BILL-GEN-01) or the episode row
(top_n, due_offset_days); the instruction narrates no formula, predicate, or
stage dependency.

## Why this is hard / unique
- **Real chain, silent far-from-cause failures.** Stage 5/6 are computed by the
  verifier from the SAME post-gold reads used for stages 1-3: skipping the
  stage-2 backfill doesn't error — it silently inflates the close pack's open
  AR and every aging bucket containing INV-2026-002 (whose unpaid drops to 0
  once its 2500 trust withdrawal is applied).
- **Interior-month correctness.** The aging is reconstructed at the 12 calendar
  month-ends preceding the episode month (~2600 of 3000 invoices in-window at
  the current episode date); an invoice issued after a month-end must not
  appear in that month-end's buckets. Documented approximation (no historical
  status/payment log exists): "as of" = issued-by-then + current status,
  post-stage-2 trust_applied, current amount_paid — stated in this file, never
  in the instruction.
- **Idempotency by construction.** The opening controls are captured once per
  batch (a re-run reuses them), a matter's approved entries drop to 0 once
  billed so it is excluded from a later recomputation, and the tie-out's
  billed_value is read from the captured candidates — re-running the close
  leaves identical state.
- **A zero-visible-effect hazard.** INV-2026-001 is already `paid`; its
  backfill has zero effect on any AR total, yet the status-blind rule requires
  it — a solution that only backfills AR-relevant invoices fails only on the
  direct per-invoice check.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `latent_organizational` — 44 clients' stored trust_balance drifted from the
   ledger (planted: 40, natural drift: 4, snapshot-confirmed); feeds stage 1
   and the close pack's trust total.
2. `lapse` — INV-2026-001 (paid) still needs its trust_applied backfilled
   (planted: 1, snapshot-confirmed); INV-2026-002 does move live numbers.
3. `copy_paste_drift` — the withdrawal reference 'Wire re INV-2026-0071
   settlement proceeds' contains INV-2026-007 as a bare substring; a
   non-boundary-delimited match mis-applies 4997 to INV-2026-007 (planted: 1
   trap reference; invoice INV-2026-0071 does not exist).
4. `decoy_boundary` — the aging buckets must exclude invoices issued after each
   month-end; reusing today's open-AR set overstates all 12 snapshots.
5. `decoy_boundary` — matters ranked below the episode row's top_n must not be
   invoiced or flipped (1914 matters carry approved value in the snapshot).

## Phase 4 — NOT YET RUN
The hacker-fixer loop runs in the QC session against `rl-env/law_firm_software:latest`:
1. No-op baseline — verifier must FAIL on pristine state.
2. Random-action baseline — 10 valid-API junk actions, verifier must FAIL.
3. Lazy-hardcode baseline — plausible static rows with a stale batch_code, verifier must FAIL.
4. Gold run + verifier PASS, verifier rerun (idempotent), gold second run + verifier PASS.
5. Canary/scope violation — mutate a row outside blast_radius after a correct gold run, must FAIL.
6. Partial-completion — kill gold.py partway, verifier must FAIL; retry/flake — verifier 5x identical;
   chain-break QC — break stage 2 (skip the backfill) and confirm the stage-4/5/6 numbers FAIL.

## Dissolution probe & chain evidence (snapshot-measured)

- 7 chained stages; close_pack and close_tieout rows are checked against the same post-mutation reads as stages 1-3 — a skipped or wrong early stage surfaces as a plausible wrong number downstream.
- **Haystack**: 2772 of 3000 invoices inside the 365d span.
- **Probe**: `outputs/dissolution_probes.py`.
