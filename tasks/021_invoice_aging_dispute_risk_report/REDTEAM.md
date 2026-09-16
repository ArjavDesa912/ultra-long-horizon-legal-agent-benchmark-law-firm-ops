# 021_invoice_aging_dispute_risk_report — REDTEAM notes (v2)

Mission: the firm's AR aging and dispute-risk report — open invoices (policy KPI-CLIENT-01's
open-AR set, unpaid balance positive) aged in whole days from due_date to the episode date into
the standard buckets (current / 1-30 / 31-60 / 61-90 / 90+), with disputed invoices aged inside
their bucket AND separately risk-flagged when their unpaid balance exceeds the episode row's
dispute_risk_threshold; a dollar-weighted 90+ concentration row read back from the bucket rows;
and the aging reconstructed as of the six calendar month-ends preceding the episode month.

## Why this is hard / unique

- **The disputed-in-aging interaction is policy-defined**: disputed invoices ARE aged in their
  due-date bucket AND risk-flagged — assuming they are either excluded from aging or excluded
  from flagging produces plausible wrong bucket totals (449 disputed invoices with positive
  unpaid balance in the snapshot).
- **The flag threshold lives in the episode row** (`dispute_risk_threshold`, 2000-8000): the
  snapshot's disputed-unpaid balances run ~536-14,995, so the flag set moves between 403 / 312 /
  212 as the knob moves — a hardcoded threshold is wrong on most episodes.
- **Dollar-weighted concentration**: pct_in_90_plus must use unpaid totals, not invoice counts.
- **Point-in-time aging at six month-ends**: an invoice issued after a close date must not
  appear in that close date's buckets (interior-month correctness), and ages are measured to
  the close date, not the episode date.
- Boundary cases at scale: the 'current' bucket legitimately holds open invoices not yet past
  due (snapshot: 107), including overdue-status invoices whose due date is still ahead.

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| decoy_boundary | disputed invoices must be aged in their bucket AND flagged — either exclusion is a plausible wrong total | 449 disputed invoices with positive unpaid balance (snapshot-confirmed) |
| decoy_boundary | disputed invoices under the episode threshold are aged but not flagged; the threshold is episode-randomized | unpaid balances ~536-14,995; flag set 403/312/212 at 2000/5000/8000 (snapshot-confirmed) |
| decoy_boundary | the 'current' bucket includes open invoices not yet past due at the episode date | 107 invoices (snapshot-confirmed) |
| mistake | count-based instead of dollar-based 90+ concentration | 5 buckets, 90+ holds ~77% of unpaid dollars vs ~76% of counts (snapshot-confirmed) |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate invoices) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (exclude disputed from aging; count-based concentration) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive aging**: excluding the 449 disputed invoices from the aging report (they are still open AR) under-counts every bucket; sub-threshold disputes must not be flagged; concentration must be dollar-weighted, not count-weighted.
- **Probe**: `outputs/dissolution_probes.py` -> open=1563, disputed=449.
