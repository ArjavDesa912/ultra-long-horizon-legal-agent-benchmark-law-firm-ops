# 019_billing_disruption_window_detection — REDTEAM notes (v2)

Mission: discover, from live data and under the in-force effective-dated policy version, the
firm-wide billing disruption the seed planted across the historical window (~340-400 days before
the episode date runs ~72-90% bad-status rate against a ~46-54% baseline everywhere else), by
aging every invoice into 30-day buckets, keeping only buckets above the episode row's
min_bucket_size, and flagging the buckets whose bad_rate breaches the live severity multiplier
times the median across considered buckets. Stage 2 derives each disrupted bucket's severity
multiple by read-back from the stage-1 rows (SEVERITY-01).

## Why this is hard / unique

- **Two effective-dated DISRUPT-01 versions exist** (v1 2024-01-01: fixed 1.3x; v2 effective
  2026-01-01, superseding: multiplier = episode row's severity_multiplier_tenths / 10, median
  over considered buckets only). Applying the superseded version FAILs on 2 of every 3 episodes.
- **Both knobs live in the episode row** (`severity_multiplier_tenths` 12-15, `min_bucket_size`
  50-120) — the instruction names them, never their values; the snapshot's bucket sizes
  (~72-246) mean a 120 floor drops several baseline buckets and shifts the median.
- **The disruption is background chaos (§5a), not a row-level plant**: no invoice is "wrong";
  the signal only exists in aggregate, so the agent must compute the median and threshold from
  live data to find the window.
- Small-sample guard: buckets below min_bucket_size are excluded from consideration entirely
  (not counted, not in the median, never flagged).

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| decision_pressure_context | a real, dated firm-wide payment disruption spans invoices issued ~340-400 days before the episode date (economic-downturn-shaped background chaos, not a per-row bug); the buckets covering that window run ~72-90% bad-status rate vs a ~46-54% baseline everywhere else | planted window: 3 buckets at the 1.3x default, 2-3 depending on the live episode multiplier (snapshot-confirmed) |
| mistake | the superseded DISRUPT-01 version (fixed 1.3x) vs the live version (episode multiplier / 10): applying the wrong one yields a plausible wrong disrupted set | 2 effective-dated policy rows, 1 live at the episode date (snapshot-confirmed) |
| lapse | the min_bucket_size guard: buckets below the episode knob are excluded from consideration, the median and the flag set | 14 buckets at snapshot, sizes ~72-246 (snapshot-confirmed) |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate invoices) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (hardcode 1.3x; skip the min-bucket guard) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive threshold**: flagging buckets above the MEAN bad-rate (54.7%, 3 buckets) instead of the superseded/live DISRUPT-01 rule (median 49.1% x episode multiplier); the min_bucket_size guard must apply first.
- **Chain**: stage-1 bucket rows -> severity read back from the written rows; recomputed severity instead of read-back FAILs.
- **Probe**: `outputs/dissolution_probes.py` -> buckets=14, median=49.1, mean=54.7.
