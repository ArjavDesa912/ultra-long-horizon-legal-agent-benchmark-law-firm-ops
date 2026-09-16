# 004_grant_portfolio_integrity_sweep — REDTEAM notes (v2)

## Mission
Run the grant portfolio integrity sweep per the seeded policy `GRANT-REPORT-01`: repair
grant applications whose client disagrees with their matter's real client, reconcile each
award's authoritative `reporting_schedule` against the live `grant_reports` tracking rows
(create missing, correct drifted due_dates, never touch waived), and reconstruct —
point-in-time — which schedule entries were already due and unmet at each of the last four
quarter-ends.

## Why this is hard / unique
- Three independent rule sources fuse into one mission: the application-to-matter client
  match (referential integrity), the schedule-vs-tracking reconciliation (create missing /
  correct drifted / skip waived), and the historical reconstruction whose definition
  ("owed as of a date" = only schedule entries due on/before that date) lives in the
  policy, not the instruction.
- `reporting_schedule` is an embedded JSON array on the award; matching it against
  `grant_reports` requires correct (award_id, report_type) pairing across 150 awards.
- The point-in-time stage is genuinely dependent: an early error in the schedule
  reconciliation (stage 2) silently corrupts the historical counts (stage 5) — far from
  the cause.
- Idempotency: the verifier's expectations derive from the SEED snapshot, and the
  integrity summary re-derives from live end-state (never a run-local delta), so a
  second gold run reports identical numbers.

## Hazards planted
1. `copy_paste_drift` — `grant_applications.client_id` disagreeing with the linked
   matter's client: 2 hand-authored (APP-2026-001/003) + 15 bulk planted
   (snapshot-confirmed total: 17); APP-2026-002 is already correct and must stay so.
2. `lapse` — AWD-2026-001's progress report due_date drifted from its schedule entry
   (+5d vs +90d) and 40 bulk awards carry two-entry schedules with missing reports
   (snapshot-confirmed: 41 awards with multi-entry schedules) — both the create and the
   correct paths are exercised.
3. `normalization_of_deviance` — 8 bulk grant_reports carry status='waived' (planted,
   snapshot-confirmed: 8); GRANT-REPORT-01 forbids recreating or re-dating waived
   reports — touching any = FAIL.
4. `latent_organizational` — the stage-5 point-in-time reconstruction uses only schedule
   entries due on/before each quarter-end; an early error in the stage-2 reconciliation
   silently corrupts the historical unmet counts.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate grant_awards/opportunities/expenses) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC session.
`par_steps` is null pending the live gold measurement; `max_steps` (1000) is a generous
placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: applying each award's current `reporting_schedule` to every report misses the effective-dated history and touches waived rows.
- **Hazards**: 8 waived reports (normalization_of_deviance — must stay untouched), 41 awards with multi-entry schedules, 300 reports on dangling award ids.
- **Chain**: point-in-time reconstruction feeds the mismatch report; wrong schedule windows shift every downstream count.
- **Probe**: `outputs/dissolution_probes.py`.
