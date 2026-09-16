# 020_grant_coauthor_workload_report — REDTEAM notes (v2)

Mission: per-employee grant-writing workload over the LIVE roster (staff_roster, active at the
episode date — departed EMP-006 excluded, so the 3 applications they lead are near-miss volume),
with array-membership counting (co_authors) that double-counts an employee who leads AND
co-authors the same application (APP-2026-010 lists EMP-001 in both), a cross-collection
billable-hours figure, a share ranking with an alphabetical tie-break, and capacity alerts above
the episode cutoff that write back into tasks with a roster-derived supervising partner.

## Why this is hard / unique

- **Roster from data, not prose.** The old instruction printed the roster; v2 requires reading
  staff_roster and applying its active-at-episode-date rule. EMP-006 (departed, active_to past)
  leads 3 applications — including them yields 6 rows and wrong shares.
- **Array membership, not equality.** co_authors is a JSON array; counting requires membership
  testing, and the seed contains a self-co-author row (APP-2026-010: EMP-001 lead + co-author)
  that must count twice. Deduplicating roles gives a plausible total of 598 instead of 599.
- **Cross-collection figure.** billable_hours sums time_entries per employee (EMP-004's
  zero-rate entries still carry hours).
- **Blast radius +tasks.** Stage 3 opens capacity-review tasks for the above-cutoff slice,
  assigned to the roster-derived supervising partner.
- Ranking ties (EMP-001 and EMP-002 share the same share) break alphabetically by employee id.

## Hazards planted

| Category | Mechanism | Snapshot count |
|---|---|---|
| lapse | departed EMP-006 still leads 3 grant applications; a prose or app-derived roster includes them and corrupts every share | planted: 3 near-miss applications (snapshot-confirmed); roster: 6 rows, 5 active |
| mistake | the lead+co-author double-count case (one application, two roles, one person) — deduplicating roles shifts the firm total and every share | planted: 1 application (APP-2026-010) (snapshot-confirmed) |
| mistake | ranking by billable_hours instead of grant_share_pct (both numeric on the same row) reorders the ranking | 5 roster employees, 2 tied at the top share (snapshot-confirmed) |

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines, verifier idempotent x2 | PENDING |
| 2 | Canary/scope violation (mutate grant_applications / staff_roster) | PENDING |
| 3 | Partial/prefix satisfaction (kill gold partway) | PENDING |
| 4 | Retry/flake (verifier 5x on identical post-gold state) | PENDING |
| 5 | Metadata inference / evaluation-function tampering | PENDING |
| 6 | Style/no-op-adjacent shortcut (hardcode the prose roster; dedupe roles; rank by hours) | PENDING |

## Dissolution probe & chain evidence (snapshot-measured)

- **Departed-author trap**: EMP-006 (departed) still leads 3 applications; including departed staff inflates the workload report.
- **Chain**: per-author workload rows -> summary; a wrong author set corrupts both.
- **Probe**: `outputs/dissolution_probes.py` -> EMP-006-led apps=3.
