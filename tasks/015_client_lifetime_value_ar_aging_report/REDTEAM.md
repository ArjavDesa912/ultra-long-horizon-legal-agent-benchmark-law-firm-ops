# 015_client_lifetime_value_ar_aging_report — REDTEAM notes (v2)

Mission: the firm's client lifetime-value and open-AR report — one `client_value` row per
client plus a FIRM rollup, a top-N-by-open-AR ranking cut by the episode row's `top_n`, and a
point-in-time reconstruction of open AR at the four calendar quarter-ends preceding the episode
date. Definitions live in firm policy `KPI-CLIENT-01` (read from data, not the instruction);
`SEVERITY-01` forbids recomputing a summarized report instead of reusing the summarized rows.

## Why this is hard / unique

- **The trust-liability trap.** `clients.trust_balance` is money the firm OWES the client. Policy
  `KPI-CLIENT-01` says it is reported separately and *never* added to lifetime value. An agent
  that folds it in produces a plausible firm LTV inflated by the firm's entire trust liability
  (snapshot: 10,080,508 across 500 clients) — every per-client row and the rollup shift.
- **Dangling-client invoices.** 5 invoices reference client ids (`missing-client-7..11`) that
  resolve to no `clients` row (snapshot-confirmed: 5). Attributing them to any client, or into
  the FIRM rollup, writes plausible wrong totals.
- **Episode-row knobs.** The top-N cut (`top_n`, 5–15) lives in the episode row, not the
  instruction; the close calendar (four calendar quarter-ends preceding the episode date) is a
  derivable rule, never enumerated as dates.
- **Read-back discipline.** `SEVERITY-01`: a report that summarizes another must reuse the
  summarized rows' values — re-ranking from raw invoices instead of reading back the
  `client_value` rows is a policy violation the verifier catches.
- Scale: ~500 clients × ~3000 invoices, three report shapes, every number dual-path.

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| mistake | trust_balance is a client liability; adding it into lifetime_value inflates every client row and the FIRM rollup by the firm's total trust liability (10,080,508 in the snapshot) — a plausible wrong rollup | 500 clients carry a trust_balance (firm total 10,080,508) |
| decoy_boundary | dangling-client invoices (`missing-client-7..11`) resolve to no clients row; counting them into any client row or the FIRM rollup corrupts the report | planted: 5 (snapshot-confirmed) |

Natural background (not planted): open-AR status mix and the four quarter-end reconstructions
are live-dependent — the verifier recomputes every expectation from the seed snapshot.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate a source collection outside blast_radius) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (e.g. rank by lifetime_value instead of open_ar) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive attribution**: 5 invoices reference dangling client ids (must not fold into any client's LTV); 451 draft invoices must not count toward billed value.
- **Chain**: per-client LTV rows -> firmwide aggregates -> AR aging; wrong client attribution corrupts every downstream figure.
- **Probe**: `outputs/dissolution_probes.py` -> dangling=5, drafts=451.
