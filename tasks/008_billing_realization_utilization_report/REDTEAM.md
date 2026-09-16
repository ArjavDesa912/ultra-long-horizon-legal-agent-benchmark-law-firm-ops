# 008_billing_realization_utilization_report — REDTEAM notes (Phase 4 PENDING)

## Mission
Build the fee-earner realization report per KPI-REAL-01 (billed basis: approved+invoiced over ALL entries by amount, written_off in the denominator at full value, zero-amount employees realize 0.0), plus a FIRM rollup, then flag employees more than `realization_flag_gap` episode points below the firm rate.

## Why this is hard / unique
- The policy-chosen denominator (write-offs at full value) contradicts the natural instinct to exclude them — the exclusion basis yields a different firm rate and flag set (plausible wrong).
- Episode-knob flag threshold (10-20 pts) moves the flag set every episode.
- Zero-billable employees exercise the division guard.

## Hazards planted (task.json.hazards) — snapshot-confirmed
- mistake — 5971 written_off entries priced at full amount; excluding them from the denominator is the natural wrong basis.
- latent_organizational — EMP-004 books only zero-rate entries (division guard).
- decision_pressure — `realization_flag_gap` 10-20 moves the flag set per episode.

## Phase 4 — NOT YET RUN
This task has not been through the live hacker-fixer loop. A QC session with Docker access must run the 6 standard attacks (metadata inference, partial/prefix satisfaction, canary/scope violation, evaluation-function tampering, retry/flake, style/no-op-adjacent shortcut) against a real container before this task ships. Do not treat this task as done until this section is replaced with real results and `hardened_after_rounds` is recorded.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive denominator**: excluding written_off entries gives realization 25.09%; the KPI-REAL-01 billed basis includes them -> 20.05%. A 5-point swing that passes casual eyeballing.
- **Probe**: `outputs/dissolution_probes.py` -> incl-wo=20.05%, excl-wo=25.09%, written_off=5971.
