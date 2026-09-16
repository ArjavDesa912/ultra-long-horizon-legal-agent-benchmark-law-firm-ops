# 017_grant_budget_burn_forecast — REDTEAM notes (v2)

Mission: a multi-award grant burn forecast (every award with status active or reporting_due —
70 in the snapshot) run as-of the episode date, recomputing each award's spend from the grant
expense ledger rather than trusting the awards collection's cached `total_spent`, applying the
firm's per-award burn branch from policy BURN-01 (trailing 90-day window for awards older than
180 days from start, lifetime spend over elapsed days otherwise), and summarizing the portfolio
(tier rows + firmwide months of runway) by read-back.

## Why this is hard / unique

- **The branch is the difficulty.** BURN-01 prescribes the trailing 90-day window when the award
  is older than 180 days from start_date, lifetime spend over elapsed days (minimum 1)
  otherwise — one correct end-state per award, and the branch decision is per award, not global.
- **Stale cache on 69 of 70 in-scope awards.** The awards collection carries its own cached
  `total_spent`; it disagrees with the expense-ledger recompute on 69 of the 70 in-scope awards
  (snapshot-confirmed). Only AWD-2026-001's cache is accurate (5000 = its single resolving
  expense). Trusting the cache writes 69 plausible wrong rows.
- **The expense ledger mostly dangles.** 499 of 500 grant_expenses rows reference `AWD-BULK-*`
  ids that resolve to no award; spend only resolves when expenses are joined on the award's
  `award_id` string. A row-id join silently reports zero spend everywhere.
- **The branch bites on the only spend-bearing award** (AWD-2026-001: 25 days old → lifetime
  branch, daily 200, 125 days of runway, projected exhaustion before its end date → critical).
- Firmwide runway aggregates the per-award remaining budget over the per-award daily rates —
  derived from the same rows the agent just wrote.

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| mistake | cached `total_spent` disagrees with the expense-ledger recompute for in-scope awards | planted: 69 of 70 in-scope awards (snapshot-confirmed) |
| integration_fault | grant_expenses rows reference `AWD-BULK-*` award ids that resolve to no award row; spend only resolves via the award_id string join | 499 of 500 expense rows dangle; 1 resolves (snapshot-confirmed) |
| mistake | applying one burn branch everywhere (or the wrong window boundary) changes the daily rate, exhaustion date and risk tier for aged awards | branch boundary: 180 days from start (policy BURN-01); snapshot: 1 in-scope award with resolving spend, 25 days old, lifetime branch, critical |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate grant_awards / grant_expenses) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (trust cached total_spent; join expenses on row id) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Cached-field trap**: `total_spent` is stale on all 150 awards; trusting it produces 150 wrong burn rates. Correct path recomputes from grant_expenses.
- **Chain**: stage-1 spend rows feed stage-2 severity tiers (SEVERITY-01 requires reusing stage-1 values).
- **Probe**: `outputs/dissolution_probes.py` -> stale cached total_spent=150/150.
