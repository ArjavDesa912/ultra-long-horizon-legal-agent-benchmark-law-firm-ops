# 018_chain_of_custody_timeline_audit — REDTEAM notes (v2)

Mission: audit every hold-linked collection's chain_of_custody array against the firm's five
custody rules (policy CUSTODY-01): non-decreasing `when`; a preservation action no later than
the earliest collection action (copy/export exempt); every entry carrying actor, action and
when (the actor of record is `actor` on bulk rows, `who` on the hand-authored rows); no
adjacent gap beyond the episode row's `custody_gap_days`; plus the trail's span. Then a
defect summary by read-back, and the defective set ranked by span.

## Why this is hard / unique

- **Five rules, four planted defect classes, distributed by index** across the ~80 hold-linked
  collections' custody arrays (snapshot-confirmed: 8 shuffled-timestamp / 7 over-gap /
  7 missing-preservation / 7 missing-actor, 51 clean including the 3 hand-authored collections
  whose actor lives under `who`).
- **The gap verdict depends on the live episode knob** (`custody_gap_days`, 100–150): the
  seed's clean trails sit exactly at a 100-day adjacent gap, so a hardcoded tolerance either
  under- or over-flags. The verifier recomputes the verdict from the live knob.
- **Two actor spellings** (`actor` vs `who`) — an agent matching only one spelling misclassifies
  the hand-authored collections as incomplete.
- **Empty arrays get no row** — flagging the ~125 empty-array collections FAILs the row count.
- The only task in the suite auditing a doubly-nested JSON array against a multi-rule policy.

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| slip | custody trails with transposed preservation/collection timestamps (non-chronological AND preservation after earliest collection) | planted: 8 (snapshot-confirmed) |
| lapse | custody trails whose final processing step drifted far past the collection step — adjacent gap beyond the episode knob's tolerance | planted: 7 (snapshot-confirmed; verdict recomputed from the live knob) |
| lapse | custody trails with no preservation entry at all (trail starts at collection) | planted: 7 (snapshot-confirmed) |
| slip | custody trails with an entry whose actor was left empty (under either spelling) | planted: 7 (snapshot-confirmed) |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate ediscovery_collections) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (audit only the first CUSTODY-01 rule; flag empty arrays) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Haystack**: 80 of 205 collections carry custody trails with a planted defect mix (shuffled timestamps, over-gap, missing preservation, missing actor); 125 empty-chain collections must NOT be flagged.
- **Naive**: skipping any CUSTODY-01 rule, flagging empty arrays, misreading who/actor spellings, or hardcoding the gap tolerance (episode knob) all FAIL.
- **Probe**: `outputs/dissolution_probes.py` -> custody=80, empty=125.
