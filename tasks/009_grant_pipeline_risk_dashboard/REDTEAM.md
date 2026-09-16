# 009_grant_pipeline_risk_dashboard — REDTEAM notes (Phase 4 PENDING)

## Mission

Build the grant pipeline risk dashboard: opportunities still in play with decision deadlines inside the episode row's `opp_window_days`, their summed max award, required reports due in the same window not yet submitted/accepted, and awards ending within `award_end_window_days` — then one detail row per at-risk opportunity derived from the same counted set.

## Why this is hard / unique

- Two different windows on two different collections, both episode-randomized (25-35 / 50-70 days) — memorized counts go stale.
- Status semantics differ per collection (opportunities exclude awarded/declined/withdrawn; reports exclude only submitted/accepted) — policy GRANT-REPORT-01/SEVERITY-01 governs; assuming symmetric semantics miscounts.
- Stage-2 detail rows must equal stage-1's counted set; recomputing with a shifted window silently diverges.

## Hazards planted (task.json.hazards) — snapshot-confirmed

- slip — inclusive window edges on both ends; boundary opportunities/reports move with the knob.
- mistake — report statuses draft/revision_required still count as due (not "done"); assuming submitted==accepted semantics undercounts.
- latent_organizational — detail stage must reuse the dashboard's counted set; a recompute with a different window diverges silently.

## Phase 4 — NOT YET RUN

This task has not been through the live hacker-fixer loop. A QC session with Docker access must run the 6 standard attacks (metadata inference, partial/prefix satisfaction, canary/scope violation, evaluation-function tampering, retry/flake, style/no-op-adjacent shortcut) against a real container before this task ships. Do not treat this task as done until this section is replaced with real results and `hardened_after_rounds` is recorded.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive window**: exclusive date edges and per-collection status semantics shift every window figure; dual-path asserts each.
- **Chain**: stage-2 risk rows derive from stage-1 window figures; wrong windows corrupt downstream silently.
- **Probe**: `outputs/dissolution_probes.py` -> 300 applications across 6 statuses; window edges are episode knobs.
