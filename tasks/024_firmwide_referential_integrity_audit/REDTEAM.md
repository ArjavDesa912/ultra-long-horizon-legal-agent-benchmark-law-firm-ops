# 024_firmwide_referential_integrity_audit — REDTEAM notes (v2 stub)

## Mission
Run the firm's ENABLED referential-integrity controls and report by rule. The
firm_controls registry carries 7 referential controls — CTL-REF-01..06 enabled,
CTL-REF-07 dormant — and the integrity policy REF-INTEG-01 defines (a) the
placeholder exemption (references starting with PLACEHOLDER- are intentional
scaffolding, never violations) and (b) that employee-style fields resolve
against staff_roster rows ACTIVE at the episode date. The agent discovers the
scope, the exemption, and the employee rule from data; nothing is narrated in
the instruction.

## Why this is hard / unique
- **Scope discovery.** The registry mixes enabled and dormant controls; the
  dormant CTL-REF-07 sits on 300 dangling bulk grant_reports that would
  dominate the total if reported. Reporting it (or folding it in) FAILs.
- **Exemption discovery.** REF-INTEG-01 exempts PLACEHOLDER- references; the
  seed plants 6 placeholder deadlines, 4 placeholder tasks and 3 placeholder
  time_entries among the dangling rows, so a naive anti-join over-reports by 13.
- **Roster-active resolution.** CTL-REF-06 resolves employee-style fields
  against roster rows active at the episode date (the departed EMP-006 is on
  the roster but inactive; the 4 planted violations reference EMP-606, which is
  not on the roster at all).
- **Real dangling plants.** 8 deadlines, 6 tasks, 7 time_entries (matter), 5
  invoices and 4 trust transactions carry unresolvable references
  (snapshot-confirmed) — the sweep is no longer trivially clean, and the
  by-rule counts must match both a raw-row pass and a SQL anti-join.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `scope_boundary` — dormant CTL-REF-07 over grant_reports: planted 300
   dangling rows (snapshot-confirmed); reporting it FAILs.
2. `mistake` — 13 PLACEHOLDER- references (6 deadlines + 4 tasks + 3
   time_entries) are exempt; counting them FAILs.
3. `lapse` — the planted dangling rows themselves: 8 deadlines + 6 tasks +
   7 time_entries + 5 invoices + 4 trust transactions (snapshot-confirmed),
   plus 4 time_entries referencing the non-roster EMP-606.

## Phase 4 — NOT YET RUN
The hacker-fixer loop runs in the QC session against `rl-env/law_firm_software:latest`:
1. No-op baseline — verifier must FAIL on pristine state.
2. Random-action baseline — 10 valid-API junk actions, verifier must FAIL.
3. Lazy-hardcode baseline — plausible static rows with a stale batch_code, verifier must FAIL.
4. Gold run + verifier PASS, verifier rerun (idempotent), gold second run + verifier PASS.
5. Canary/scope violation — mutate a row outside blast_radius after a correct gold run, must FAIL.
6. Partial-completion — kill gold.py partway, verifier must FAIL; retry/flake — verifier 5x identical.

## Dissolution probe & chain evidence (snapshot-measured)

- **Placeholder exemption**: 13 PLACEHOLDER-* references are policy-exempt; a naive dangling-join counts them as violations (over-report by 13 across 5 controls).
- **True violations**: deadlines=8, tasks=6, time_entries=7, invoices=5, trust_txs=4, inactive-emp=4.
- **Chain**: per-control findings -> summary; wrong exemption or join corrupts every figure. Verified: PASS on all 8 gates (fresh-container run).
- **Probe**: `outputs/dissolution_probes.py`.
