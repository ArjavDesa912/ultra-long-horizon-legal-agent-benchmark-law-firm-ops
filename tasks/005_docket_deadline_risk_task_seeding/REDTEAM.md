# 005_docket_deadline_risk_task_seeding — REDTEAM notes (v2)

## Mission
Run the firm's docket risk sweep per the seeded policy `DOCKET-01`: every deadline the
policy admits into the risk window (status, matter state, episode row's
`docket_window_days`) gets exactly one preparation task with policy-derived due date
(`prep_lead_days`) and priority (`urgent_window_hours`), with an existing prep task for
the same deadline UPDATED in place rather than duplicated — then report per-matter and
FIRM summary rows derived from the sweep.

## Why this is hard / unique
- Three episode-row knobs (`docket_window_days` 7-14, `urgent_window_hours` 24-72,
  `prep_lead_days` 1-3) move the qualifying set and every derived due date/priority each
  episode — memorized constants go stale by design.
- The stale-prep trap requires UPDATE-in-place semantics: the planted prep row for
  'Claim Construction Hearing' (due_date already past, deadline still upcoming) must be
  brought to the policy-derived state, not duplicated and not deleted. An agent that only
  checks title existence leaves the stale date in place; one that always creates
  duplicates fails the exact-set check.
- The pre-existing prep row lives among the deadlines (pre-migration data quirk) — the
  agent must discover where prep rows actually live rather than assuming the tasks
  collection.
- Two independent filters (deadline status, matter status) must both apply; matter
  2026-006 is suspended with 2 completed deadlines, exercising both at once.
- Idempotency is real: rerunning must not duplicate a prep task for a deadline already
  handled.

## Hazards planted
1. `mistake` — stale prep task 'Prepare for: Claim Construction Hearing' (planted: 1,
   snapshot-confirmed) must be UPDATED in place to the policy-derived due date/priority
   when its deadline qualifies; leaving it stale or duplicating it = FAIL.
2. `slip` — window/urgency/lead knobs (7-14 days, 24-72 hours, 1-3 days) move the
   qualifying set and every derived value each episode; snapshot background: 885 upcoming
   deadlines on open matters, of which only the in-window slice qualifies.
3. `lapse` — matter 2026-006 is suspended with 2 already-completed deadlines (planted,
   snapshot-confirmed); both filters must independently exclude them.

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
`par_steps` is null pending the live gold measurement; `max_steps` (260) is a generous
placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: `status='upcoming' AND due_date in window` returns deadlines on suspended/closed matters too — 570 of 1455 upcoming deadlines sit on non-open matters; the matter-status filter is the discriminating rule.
- **Update-in-place hazard**: 1 stale prep row must be updated, never duplicated; a create-only agent leaves a duplicate + stale row.
- **Chain**: qualifying set -> prep task upserts -> risk summary; a wrong set corrupts all three.
- **Probe**: `outputs/dissolution_probes.py` -> upcoming=1455, non-open=570, stale prep rows=1. Window/lead/urgent are episode knobs.
