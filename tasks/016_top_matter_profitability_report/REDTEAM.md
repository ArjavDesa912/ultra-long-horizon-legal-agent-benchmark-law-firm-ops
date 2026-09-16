# 016_top_matter_profitability_report — REDTEAM notes (v2)

Mission: rank all ~2000 matters by the policy-defined logged value (KPI-REAL-01's realization
basis decides the denominator — and with it the entire top-N), write exactly the top-N rows (N
from the episode row's `top_n`), flag the sub-threshold slice of those rows, and open a
partner-review task per flag assigned to the roster-derived supervising partner. A
billing-integrity anomaly class (billed with zero logged time) is part of the written contract.

## Why this is hard / unique

- **The ranking metric is policy-defined.** KPI-REAL-01's realization basis ("all entries, by
  amount") makes written_off entries count in the denominator at their full amount — 5,971
  entries worth 10,881,050 in the snapshot. Excluding them (or valuing them at 0) yields a
  plausible but different top-N; the verifier recomputes the ranking from the snapshot and
  FAILs any other denominator.
- **Blast radius +tasks.** Stage 3 writes `tasks` rows — the only report task in this batch that
  writes outside ops_reports. The assignee is derived from `staff_roster` (the only active
  partner), never from the instruction.
- **Read-back discipline.** The alert stage must reuse the stage-1 rows (SEVERITY-01); the
  verifier cross-checks each alert's realization_pct against the agent's own written row.
- **The anomaly class is empty in this build** (0 matters billed with zero logged time —
  snapshot-confirmed). The verifier still asserts the exact expected set (empty) and the
  diagnosis category on any row an agent hallucinates — inventing anomalies FAILs.
- Full sort over 2002 matters, bounded write set (N rows + alerts + tasks).

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| mistake | written_off entries carry full-amount values; the policy's realization denominator counts every entry status — excluding written_off (or valuing it at 0) yields a plausible wrong ranking and alert set | 5,971 written_off entries, 10,881,050 at stake (snapshot-confirmed) |
| lapse | matters billed with no logged time require the division guard and an anomaly row with the taxonomy diagnosis — the class is empty in this build; the verifier recomputes the expected set, so hallucinating anomalies FAILs | planted class: 0 (snapshot-confirmed); verifier recomputes per rebuild |
| mistake | supervising partner must come from staff_roster (only active partner, EMP-001); EMP-006 is departed (active_to past) and must never receive a review task | 6 roster rows, 1 active partner (snapshot-confirmed) |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate a source collection outside blast_radius) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (rank by collected_value; assign tasks to EMP-006) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive denominator**: dropping written_off (or any status) from the logged-value basis shifts the top-N ranking; the anomaly class is empty by design so hallucinated anomalies FAIL.
- **Haystack**: 30000 time entries across 5 statuses (~6000 each).
- **Probe**: `outputs/dissolution_probes.py`.
