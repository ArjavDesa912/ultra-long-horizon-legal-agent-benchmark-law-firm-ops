# 023_firmwide_temporal_anomaly_sweep — REDTEAM notes (v2 stub)

## Mission
Run the firmwide temporal-impossibility sweep: read the firm_controls registry,
run exactly the ENABLED temporal controls (CTL-TMP-01..04) each against the
collection its row scopes, count violating rows per control, push one finding
row per control, then a summary row reusing those findings, then a severity row
per SEVERITY-01 (worst rule with alphabetical tie-break; overall_severity
critical iff the total exceeds the episode row's `severity_critical_count`).
Two further temporal controls (CTL-TMP-05/06) sit in the registry DORMANT with
large natural violation counts and must stay unreported.

## Why this is hard / unique
- The rule set is no longer narrated in the instruction: which rules run, which
  collections they scope, and what each comparison asserts are all derivable
  only from the registry rows (and REF-INTEG-01's "enabled flag" rule).
- **Semantics trap.** CTL-TMP-04 is ON-OR-BEFORE while CTL-TMP-01/02/03 are
  strictly-before. The snapshot plants 2 equal-date grant decisions alongside 2
  strict ones, so a naive copy of the strict comparison yields 2 instead of 4 —
  plausible, wrong, and rejected.
- **Dormant-control trap.** CTL-TMP-05/06 are disabled but their natural
  violation counts are large (426 deadline-before-matter-opened, 739
  invoice-issued-before-matter-opened in the snapshot) — a sweep that "runs
  everything in the registry" over-reports and FAILs.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `scope_boundary` — 2 dormant temporal controls with natural counts 426/739
   (planted: 0, natural background, snapshot-confirmed); reporting either FAILs.
2. `mistake` — CTL-TMP-04 on-or-before vs strictly-before: 2 equal-date grant
   decisions + 2 strict ones (planted: 2 equal + 2 strict, snapshot-confirmed);
   the naive copy yields 2 instead of 4.

## Phase 4 — NOT YET RUN
The hacker-fixer loop runs in the QC session against `rl-env/law_firm_software:latest`:
1. No-op baseline — verifier must FAIL on pristine state.
2. Random-action baseline — 10 valid-API junk actions, verifier must FAIL.
3. Lazy-hardcode baseline — plausible static rows with a stale batch_code, verifier must FAIL.
4. Gold run + verifier PASS, verifier rerun (idempotent), gold second run + verifier PASS.
5. Canary/scope violation — mutate a row outside blast_radius after a correct gold run, must FAIL.
6. Partial-completion — kill gold.py partway, verifier must FAIL; retry/flake — verifier 5x identical.

## Dissolution probe & chain evidence (snapshot-measured)

- **Boundary trap**: the grant control is on-or-before (decision_date <= submitted_date counts: 4 anomalies); a uniformly strict `<` rule misses 2 and reports a plausible-but-wrong count.
- **Hazards**: 7 due<issued invoices, 5 closed<opened matters, 354 reviewed<created documents (bulk density).
- **Probe**: `outputs/dissolution_probes.py`.
