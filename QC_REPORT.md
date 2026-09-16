# QC Report — law_firm_software

## v2 rewrite (26 tasks) — full 8-gate battery, image `rl-env/law_firm_software:2.0.1`

Every task below was rewritten for measured difficulty (distributed evidence, planted
near-misses, policy discovery, real multi-stage chains) and run through the extended
`tools/qc_task.py` battery against a fresh container of the rebuilt image: no-op baseline,
10-random-action baseline, lazy-hardcode baseline, `gold.py` + verify, verify re-run
(idempotent), **second gold run + verify (re-run idempotency)**, **`gold_alt.py`
(independent second solution) + verify**, and `max_steps` budget fit.

| Task | noop FAIL | random FAIL | hardcode FAIL | gold PASS | idem. PASS | 2nd-gold PASS | gold_alt PASS | gold calls | max_steps |
|---|---|---|---|---|---|---|---|---|---|
| 001_trust_ledger_reconciliation | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 1012 | 1547 |
| 002_privilege_clawback_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 26 | 40 |
| 003_hold_linkage_repair_and_ack_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 251 | 382 |
| 004_grant_portfolio_integrity_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 382 | 574 |
| 005_docket_deadline_risk_task_seeding | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 90 | 112 |
| 006_new_matter_intake_conflict_check | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 82 | 124 |
| 007_time_entry_invoice_conversion | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 1987 | 3044 |
| 008_billing_realization_utilization_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 73 | 110 |
| 009_grant_pipeline_risk_dashboard | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 29 | 40 |
| 010_ediscovery_review_queue_progress_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 122 | 178 |
| 011_matter_staffing_roster_reconciliation | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 129 | 197 |
| 012_firmwide_conflict_of_interest_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 21 | 32 |
| 013_sol_calendaring_gap_repair | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 302 | 470 |
| 014_custodian_collection_gap_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 24 | 29 |
| 015_client_lifetime_value_ar_aging_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 527 | 794 |
| 016_top_matter_profitability_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 231 | 482 |
| 017_grant_budget_burn_forecast | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 146 | 223 |
| 018_chain_of_custody_timeline_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 115 | 173 |
| 019_billing_disruption_window_detection | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 19 | 29 |
| 020_grant_coauthor_workload_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 102 | 118 |
| 021_invoice_aging_dispute_risk_report | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 512 | 670 |
| 022_firmwide_kpi_pack | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 94 | 140 |
| 023_firmwide_temporal_anomaly_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 30 | 46 |
| 024_firmwide_referential_integrity_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 114 | 173 |
| 025_billing_arrangement_compliance_audit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 278 | 389 |
| 026_firmwide_month_end_close_pack | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 273 | 482 |

**Result: 26 / 26 tasks passing all 8 gates on the post-reseed image.**

### Bugs found and fixed in this pass (v2 hardening round)

- **jsonb `::text` vs string refs (024)**: `matter_id::text` on a jsonb column renders
  `"PLACEHOLDER-1"` (with quotes), double-counting policy-exempt placeholders vs the Python
  path. Fixed to `#>> '{}'`; the `.format()` collision this introduced was resolved with
  `.replace()`.
- **Timestamp-vs-date comparisons (005)**: `due_date::text <= 'YYYY-MM-DD'` drops all rows on
  the window's final day; fixed to `::date` in verifier and gold_alt.
- **Live-vs-seed state confusion (007, 015)**: verifier SQL paths queried post-mutation state
  for seed-derived expectations (007's approved->invoiced flips; 015's invoice-only GROUP BY
  dropping zero-invoice clients). Fixed to compare post-run state against seed-derived
  expectations / LEFT JOIN from clients.
- **Idempotency reconstruction (001, 007)**: second gold runs must reproduce seed-derived
  expectations from a mutated live state — 001 reuses prior batch report rows; 007 marks batch
  invoices with `batch_code` and reconstructs the pre-batch universe from covered entries.
- **Alias mismatches**: 014 gold_alt (`hid`/`mid` vs `id`/`matter_id`), 020 gold_alt
  (`h` vs `hours`), 025 gold_alt (`client_id` vs `id`), 014 verifier (`hid`/`cid` vs
  `hold_id`/`custodian_contact_id`).
- **Dissolved hazards found by probing, fixed in seed.js**: 014's cross-matter trap was inert
  (all 5 trapped custodians' holds were suspended/released → forced active, issued 200d out);
  025's disbursement-only exemption had zero fixtures (planted INV-2026-0000 on the top-billed
  contingency client so it lands inside every `audit_top_clients` slice). Post-reseed probes:
  014 naive-matcher under-reports by 5; 025 disb-only count = 1.
- **`expect_cents` unit bug (007)**: verifier passed cents-valued expectations where dollars
  were expected — masked by an earlier-failing check until the dual-path fix surfaced it.

Dissolution-probe numbers per task (naive query vs correct rule, measured on the captured
snapshot) are in `outputs/dissolution_probes.py` and recorded in each task's `REDTEAM.md`.

### Phase-4 spot checks + canary coverage fix (v2)

`tools/phase4_attack.py` (partial-kill, out-of-scope canary mutation, verifier retry x5) was
re-run on the heavily-rewritten tasks. 007 and 013 passed all three attacks on the first pass.
024 and 025 initially **failed the canary attack**: their `check_canaries` lists omitted
read-source collections (`matters`, `clients`, `invoices`, ...), so mutating an unrelated
`matters` field post-gold still passed. A suite-wide audit found 19 tasks with the same class
of gap; every verifier's canary list was extended to cover all seeded collections outside its
`blast_radius` (ops_meta/ops_reports excluded — episode state / shared write sink). Re-run:
024 and 025 now correctly FAIL the canary attack, and all 19 re-verified through the full
8-gate battery (gold/gold_alt/idempotent/second-run all still PASS — the extended canaries
protect rows no gold touches).

---

## v1 history — 25-task suite + task 026 capstone (superseded by the v2 table above)

Every task below was run through `tools/qc_task.py` against a fresh container of
`rl-env/law_firm_software:latest` (the final, chaos-injected, ~50,250-row build). Each run
executes, per task: a no-op baseline (verifier immediately after reset), a 10-random-action
baseline, a lazy-hardcode baseline, `gold.py` against a fresh container, the verifier
immediately after gold, and the verifier again on the same unchanged state (idempotency).
Raw transcripts are in `qc_results/<task_id>.json`.

| Task | noop FAIL | random FAIL | hardcode FAIL | gold PASS | idempotent PASS | par_steps |
|---|---|---|---|---|---|---|
| 001_trust_ledger_reconciliation | ✓ | ✓ | ✓ | ✓ | ✓ | 566 |
| 002_privilege_clawback_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 32 |
| 003_hold_linkage_repair_and_ack_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | 49 |
| 004_grant_portfolio_integrity_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | 188 |
| 005_docket_deadline_risk_task_seeding | ✓ | ✓ | ✓ | ✓ | ✓ | 41 |
| 006_new_matter_intake_conflict_check | ✓ | ✓ | ✓ | ✓ | ✓ | 71 |
| 007_time_entry_invoice_conversion | ✓ | ✓ | ✓ | ✓ | ✓ | 190 |
| 008_billing_realization_utilization_report | ✓ | ✓ | ✓ | ✓ | ✓ | 69 |
| 009_grant_pipeline_risk_dashboard | ✓ | ✓ | ✓ | ✓ | ✓ | 7 |
| 010_ediscovery_review_queue_progress_report | ✓ | ✓ | ✓ | ✓ | ✓ | 840 |
| 011_matter_staffing_roster_reconciliation | ✓ | ✓ | ✓ | ✓ | ✓ | 75 |
| 012_firmwide_conflict_of_interest_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 13 |
| 013_sol_calendaring_gap_repair | ✓ | ✓ | ✓ | ✓ | ✓ | 16 |
| 014_custodian_collection_gap_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 17 |
| 015_client_lifetime_value_ar_aging_report | ✓ | ✓ | ✓ | ✓ | ✓ | 512 |
| 016_top_matter_profitability_report | ✓ | ✓ | ✓ | ✓ | ✓ | 95 |
| 017_grant_budget_burn_forecast | ✓ | ✓ | ✓ | ✓ | ✓ | 7 |
| 018_chain_of_custody_timeline_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 6 |
| 019_billing_disruption_window_detection | ✓ | ✓ | ✓ | ✓ | ✓ | 14 |
| 020_grant_coauthor_workload_report | ✓ | ✓ | ✓ | ✓ | ✓ | 69 |
| 021_invoice_aging_dispute_risk_report | ✓ | ✓ | ✓ | ✓ | ✓ | 392 |
| 022_firmwide_kpi_pack | ✓ | ✓ | ✓ | ✓ | ✓ | 85 |
| 023_firmwide_temporal_anomaly_sweep | ✓ | ✓ | ✓ | ✓ | ✓ | 21 |
| 024_firmwide_referential_integrity_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 161 |
| 025_billing_arrangement_compliance_audit | ✓ | ✓ | ✓ | ✓ | ✓ | 710 |

**Result: 25 / 25 tasks passing (100%).**

## Bugs found and fixed during this QC pass (not just "ran once, shipped")

1. **Task 004** (`grant_portfolio_integrity_sweep`): verifier initially asserted exact-set
   equality between live `grant_reports` (award_id, report_type) pairs and the schedule-derived
   expected set. Bulk seed data intentionally includes `grant_reports` rows tied to
   non-resolving placeholder `award_id`s (volume filler unrelated to any real award), which the
   exact-equality check wrongly flagged as "extra" rows. Fixed to a subset check (every required
   pair present) plus a per-pair correctness check, since real-world messiness (unrelated
   report rows existing) shouldn't fail an audit that only cares about *required* coverage.
2. **Task 007** (`time_entry_invoice_conversion`): the original "invoice every matter with
   approved time" design measured **7881 API calls** for gold.py at 50k-row scale — nearly
   every one of ~2000 matters had at least one approved entry, an unbounded blast radius that
   isn't a reasonable single-episode mission. Rescoped to the top-15 matters by approved value
   (found via a full-firm ranking, same pattern as task 016), dropping gold to 190 calls.
3. **Seed generator bugs** (all fixed before this QC pass began, see BUILD_NOTES.md for detail):
   a digit-bearing Bates prefix that broke the letters/digits regex split used by task 002's
   verifier: (in-range document count went from an expected ~675 to 2); trust_transaction dates
   generated independently of the sequential balance-computation order, which let sorting by
   date pick a different "latest" transaction than intended (corrupted-client count went from a
   planned 40 to an observed 317); and a hold-to-matter assignment that didn't guarantee
   uniqueness (found via post-build snapshot inspection, fixed to a without-replacement sample).
   None of these were caught by reading the generator code — only by re-querying the actual
   captured snapshot after each rebuild and comparing to the intended counts.

## `max_steps`/`par_steps` methodology

`tools/qc_task.py`'s gold run talks to Stackhouse directly via `glib.Gold` and bypasses
`env.py`'s Gymnasium step counting entirely — a task can pass this QC battery with a
`max_steps` far too low for a real agent driving the same mission through `env.step()`. Every
task's `par_steps` above is the actual `g.steps` count gold.py reported on its final passing
run against the final seed; `max_steps` in each `task.json` is `ceil(par_steps * 1.5)`
(floor 20), not a round-number guess.

## Phase 4 — hacker-fixer loop (complete, all 25 tasks, live container)

Every one of the 25 tasks was individually attacked with the three attacks the standard
battery above doesn't already cover, plus the two suite-wide checks:

- **Canary/scope violation** (all 25): mutate a field on a collection outside the task's
  `blast_radius` after a correct gold run (`clients.notes` for the 15 tasks whose blast radius
  doesn't include `clients`; a task-specific victim for the first 10). **25/25 correctly
  FAILed** with the expected canary-violation message.
- **Partial/prefix satisfaction** (all 25): kill `gold.py` partway through its run (kill time
  scaled to each task's own measured runtime), confirm the verifier still FAILs against the
  incomplete state. **25/25 correctly FAILed.**
- **Retry/flake exploitation** (all 25): run the verifier 5x against identical post-gold state.
  **25/25 produced 5/5 identical PASS.**
- **Metadata inference / evaluation-function tampering** (suite-wide, shared infrastructure,
  not per-task): confirmed `expect_equal`/`expect_cents` in the shared `tools/vlib.py` cap
  FAIL-message detail to `"label: mismatch"` by default (full detail gated behind a host-only
  `VERIFIER_DEBUG=1`); grading files (`gold.py`/`verifier.py`/`task.json`/
  `seed_snapshot.json`) are root-owned `chmod 400` inside the built image; and — checked live
  via a safe, non-exploit read-only SQL introspection query
  (`SELECT rolsuper FROM pg_roles WHERE rolname = current_user`) rather than an actual
  file-read attempt — the Stackhouse SQL role (`stackhouse_app`) is confirmed
  `rolsuper = false`, which by default blocks the `pg_read_file`/`COPY ... TO PROGRAM`/
  `lo_import` functions Postgres reserves for superusers, closing the path that would otherwise
  let an agent read `verifier.py` straight off disk via `POST /v1/sql/query`.
- **Style/no-op-adjacent shortcut**: covered by the lazy-hardcode baseline in the standard
  battery, run for all 25.

**Two real exploits were found and fixed** during this pass (both detailed in the "Bugs found"
section above): task 004's exact-set-equality check wrongly failing on intentional bulk filler
rows, and task 007's unbounded blast radius (7881 steps) at full seed scale. Both were found,
fixed, and re-confirmed clean before the results above were recorded. Every task's own
`REDTEAM.md` records its specific attack transcript (message text, not just pass/fail) and its
`hardened_after_rounds` count. Full raw results: `qc_results/*.json` (standard battery) and
the individually-attacked transcripts embedded in each task's `REDTEAM.md`.

## Not yet done (open items, tracked in BUILD_NOTES.md)

- `gold_alt.py` (a second, independently-coded solution per task) was not written for this
  pass — the base `RL_ENV_FACTORY_PROMPT.md` schema doesn't require it (that's a hardmode-v2
  addition); Phase 2's equivalence requirement was satisfied by dual-path verification inside
  each verifier instead, not by a second gold script.
- This is black-box adversarial testing against the standard reward-hacking threat model
  (`QC_METHODOLOGY.md`), not a formal correctness proof — a sufficiently novel attack outside
  that threat model (the 6 canonical categories: no-op/random, hardcode, delete-to-force,
  duplicate-to-satisfy-count, tamper-verifier, poll-to-infer, bypass-via-SQL, crash-grader,
  retry-until-flaky-pass, cross-episode) is not ruled out by construction. "Unbreakable" here
  means "survives every attack in that model," not "provably unbreakable."

## Dependent-stage-chain upgrade — tasks 001-025 (`horizon_tier` 1 -> 2, added after task 026)

After task 026 shipped as a standalone capstone, every one of tasks 001-025 was individually
extended with a second, genuinely dependent stage (see README.md's "Dependent-stage chains"
section for the design rationale and the pattern used per task). Each task was re-run through the
full `tools/qc_task.py` battery, `tools/phase4_attack.py` (partial-completion, canary/scope
violation, retry x5), and one live "dependency-break" attack targeting the specific new stage-2
link, against the same `rl-env/law_firm_software:latest` image. All 25 pass:

| Task | QC battery | Phase4 (partial/canary/retry) | Dependency-break attack | par_steps (was) |
|---|---|---|---|---|
| 001 | PASS | 3/3 | Stale local copy reused for stage 2 -- FAIL (correct) | 1067 (566) |
| 002 | PASS | 3/3 | Firm-wide total instead of per-matter -- FAIL (correct) | 54 (32) |
| 003 | PASS | 3/3 | Counted by stale pre-repair hold_id -- FAIL (correct) | 152 (49) |
| 004 | PASS | 3/3 | Grouped from pre-fix reports snapshot -- FAIL (correct) | 339 (188) |
| 005 | PASS | 3/3 + idempotency 2nd-gold-run (newly working) | Loose stage-2 filter (missing matter-status check) -- FAIL (correct) | 61 (41) |
| 006 | PASS | 3/3 | Hardcoded EMP-001 instead of the real attorney -- FAIL (correct) | 78 (71) |
| 007 | PASS | 3/3 | Stale local copy, next-wave re-ranked off it -- FAIL (correct) | 202 (190) |
| 008 | PASS | 3/3 | Hardcoded 50% firm benchmark -- FAIL (correct) | 71 (69) |
| 009 | PASS | 3/3 | Iterated ALL opportunities, not just at-risk -- FAIL (correct) | 21 (7) |
| 010 | PASS | 3/3 | Sorted descending (wrong end of the queue) -- FAIL (correct) | 847 (840) |
| 011 | PASS | 3/3 | Reported full roster instead of just additions -- FAIL (correct) | 133 (75) |
| 012 | PASS | 3/3 (canary attack caught via stage 2, not the hash) | (incidental, see task REDTEAM) | 20 (13) |
| 013 | PASS | 3/3 | (relied on code-construction; see task REDTEAM) | 25 (16) |
| 014 | PASS | 3/3 | Swapped under_30/over_90 bucket labels -- FAIL (correct) | 21 (17) |
| 015 | PASS | 3/3 | Ranked by lifetime_value, not open_ar -- FAIL (correct) | 524 (512) |
| 016 | PASS | 3/3 | Flipped comparison direction (high not low) -- FAIL (correct) | 116 (95) |
| 017 | PASS | 3/3 | Hardcoded risk_tier='critical' -- FAIL (correct) | 9 (7) |
| 018 | PASS | 3/3 | Sorted ascending, not descending -- FAIL (correct) | 10 (6) |
| 019 | PASS | 3/3 | Forgot the division by median -- FAIL (correct) | 19 (14) |
| 020 | PASS | 3/3 | Ranked by billable_hours, not share_pct -- FAIL (correct) | 75 (69) |
| 021 | PASS | 3/3 | Count-based instead of dollar-based ratio -- FAIL (correct) | 394 (392) |
| 022 | PASS | 3/3 | Inverted the ratio (wip/ar not ar/wip) -- FAIL (correct) | 87 (85) |
| 023 | PASS | 3/3 | min() instead of max() for worst_rule -- FAIL (correct) | 23 (21) |
| 024 | PASS | 3/3 | Reverse-alphabetical tie-break -- FAIL (correct) | 163 (161) |
| 025 | PASS | 3/3 | total_exposure computed from count, not dollars -- FAIL (correct) | 849 (710) |

**3 design corrections and 2 unrelated pre-existing bugs found and fixed** during this pass (all
detailed in the affected task's `REDTEAM.md` and summarized in README.md's "Dependent-stage
chains" section): tasks 002/018/023-024 needed a stage-2 redesign or tie-break fix after live data
showed the first design was vacuous or imprecisely specified; task 006's `task.json` instruction
had drifted from its own gold/verifier code (unrelated to the horizon upgrade); and
`tools/qc_task.py`'s idempotency-category second-gold-run check had a `list == str` bug that
silently disabled it for the suite's only idempotency-tagged task (005), now fixed and confirmed
working.

## Hardmode QC — task 026 (dependent-stage-chain capstone, added after the 25-task pass)

`026_firmwide_month_end_close_pack` (`horizon_tier: 6`, `RL_ENV_HARDMODE_PROMPT.md` §1e) was run
through the same `tools/qc_task.py` battery, **plus** two `gold_alt.py` (independent second
solution) and two hazard-targeted attacks beyond the standard three, against the same live
`rl-env/law_firm_software:latest` image:

| Check | Result |
|---|---|
| no-op baseline | FAIL (correct) |
| 10-random-action baseline | FAIL (correct) |
| lazy-hardcode baseline | FAIL (correct) |
| gold.py -> verify | PASS (278 measured API calls) |
| verify re-run (idempotent) | PASS |
| gold_alt.py (SQL-first path) -> verify | PASS (238 measured API calls) |
| Partial-completion (gold.py killed ~1.5s into its ~5.3s run) | FAIL (correct): `client 1 trust_balance: mismatch` |
| Canary/scope violation (`matters` mutated post-gold) | FAIL (correct): `canary violated: matters was modified outside the task's blast radius` |
| Retry/flake (verifier x5 on identical state) | 5/5 identical PASS |
| Hazard-targeted: skip `trust_applied` backfill on already-`paid` invoices | FAIL (correct): `invoice INV-2026-001 trust_applied: mismatch` |
| Hazard-targeted: drop stage 4's issued_date scoping (reuse today's full open-AR set for every historical month-end) | FAIL (correct): `2026-08-31/current invoice_count: mismatch` |

`par_steps`=278, `max_steps`=417, both measured (not guessed) from the live gold run. Full
transcript and the stage-dependency map (which stage's breakage trips which downstream
assertion) are in `tasks/026_firmwide_month_end_close_pack/REDTEAM.md`.
