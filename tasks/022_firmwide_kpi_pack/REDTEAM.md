# 022_firmwide_kpi_pack — REDTEAM notes (v2 stub)

## Mission
Build the firm's enterprise KPI pack: one `enterprise_kpi` row whose seven figures each span a
different collection (matters, time_entries, invoices, clients, ediscovery_holds, ediscovery_documents,
grant_opportunities), then a ratios row derived from the pack row's own values, alert rows where a
ratio breaches its episode-row threshold, and a tie-out row that re-reads the batch's own alert and
ratio rows. Every definition lives in firm policy `KPI-PACK-01`; both alert thresholds are episode
knobs (`ar_alert_ratio`, `trust_alert_ratio`). The instruction prints no status set, no formula, no
threshold — the agent must read the policy row and apply it.

## Why this is hard / unique
- Widest integration in the suite: 7 collections feed one row, and a wrong stage-1 value compounds
  silently into the ratios, the alerts, and the tie-out (SEVERITY-01's reuse rule makes independent
  recomputation in the summarizing rows a policy violation the verifier enforces).
- **Trust-as-liability trap.** `total_trust_liability` is a client liability reported separately
  (KPI-CLIENT-01/KPI-PACK-01). The snapshot's trust total (10,080,508) is within ~4% of open AR
  (10,458,031), so folding trust into AR — or netting it against WIP — yields a plausible pack that
  is wrong by roughly the firm's entire trust balance.
- **Status-set drift hazard.** Every KPI's status set is a near-miss of a neighbouring collection's
  vocabulary, and the seed carries real rows in every adjacent class (522 written_off invoices,
  5971 written_off time_entries, 12 legacy `qc-complete` documents, 408 closed / 398 suspended
  matters, 62 non-active grant opportunities) — a drifted set produces plausible wrong numbers.
- Both alert thresholds are episode knobs (`ar_alert_ratio` 25–40, `trust_alert_ratio` 85–105 as
  integer percentages) against live ratios of ~0.32 and ~0.96, so the alert set genuinely depends on
  the episode row.

## Hazards planted (taxonomy §2a)
1. `mistake` — trust-as-liability confusion inflating/deflating open AR by the full trust total
   (definitional trap; the policy states the rule, the data makes the error plausible).
2. `copy_paste_drift` — status-set drift across the seven KPI definitions (planted background:
   522 written_off time_entries, 522 written_off invoices, 12 `qc-complete` documents, 408 closed +
   398 suspended matters, 96 terminal-status opportunities — snapshot-confirmed).

## Phase 4 — NOT YET RUN
The hacker-fixer loop runs in the QC session against `rl-env/law_firm_software:latest`:
1. No-op baseline — verifier must FAIL on pristine state.
2. Random-action baseline — 10 valid-API junk actions, verifier must FAIL.
3. Lazy-hardcode baseline — plausible static rows with a stale batch_code, verifier must FAIL.
4. Gold run + verifier PASS, verifier rerun (idempotent), gold second run + verifier PASS.
5. Canary/scope violation — mutate a row outside blast_radius after a correct gold run, must FAIL.
6. Partial-completion — kill gold.py partway, verifier must FAIL; retry/flake — verifier 5x identical.

## Dissolution probe & chain evidence

- Every KPI is dual-pathed (Python vs SQL aggregate) and the ratio/alert/tie-out rows are checked against the SAME live pack values, so a wrong stage-1 KPI silently corrupts later stages.
- Hazard coverage embedded in the verifier: each KPI's definition lives in the episode row / firm_policies, not the instruction.
