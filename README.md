# Ultra-Long-Horizon Legal Agent Benchmark — Law Firm Ops

> ### 📌 Read this first
>
> **Published by [Praesidium Compliance Systems Corporation](https://praesidiumsystems.ai)**
> · Built on **[Stackhouse](https://github.com/ArjavDesa912/stackhouse)** ([stackhousedb.com](https://stackhousedb.com))
> · [This gym on GitHub](https://github.com/ArjavDesa912/ultra-long-horizon-legal-agent-benchmark-law-firm-ops)
> · [Live on Prime Intellect](https://app.primeintellect.ai/dashboard/environments/praesidiumsystems/ultra-long-horizon-legal-agent-benchmark-law-firm-ops)
>
> **No LLM-as-judge, anywhere.** Every verifier is plain, deterministic Python —
> exact-value assertions, cents-normalized money comparisons, canary SHA-256
> hashing, row-level diffing against a host-side seed snapshot. No model ever
> scores the agent's work, so there's nothing for a policy to talk its way past.
> Grading is 100% on final database state.
>
> This repo is a public sample from a catalog of **100+ RL environments —
> each with its own task suite — already in our build pipeline, shipping
> this quarter, engineered to the same reward-hacking-resistant
> standard proven below.** Every task in this sample clears a full
> reward-hacking QC battery before it ships — no-op fails, random-action
> fails, hardcoded-guess fails, gold passes, twice, deterministically — and
> that is the bar every environment coming out of the pipeline is held to, not
> just this one.
>
> **Contact us:** `arjav.desai@praesidiumsystems.ai` · `sam.heidler@praesidiumsystems.ai`

This is a companion public sample to our
[veterinary-clinic ops benchmark](https://github.com/ArjavDesa912/Long-Horizon-Medical-Agent-Benchmark-Veterinary-Clinic-Ops):
a law-firm practice-management system (matters, clients, billing/trust
accounting, e-discovery, grant management) doubling as an ultra-long-horizon
legal-ops agent benchmark.

## Why "ultra" long-horizon

The first version of this suite was calibrated like a normal benchmark —
and a frontier model (`openai/gpt-5.6-sol`) cleared our designated
"difficult" task in 8 turns / ~3 minutes. v2 is a from-scratch rewrite of all
26 tasks against the same app and platform, aimed at closing that ceiling:

- **Every task carries a genuine dependent-stage chain** (`horizon_tier >= 2`;
  one task is `horizon_tier: 6`): a later stage reads the state an earlier
  stage actually mutated or wrote — never an independent recompute from seed —
  so a wrong or skipped stage silently corrupts downstream values instead of
  erroring out.
- **Policy discovery, not recipe-following.** v1 instructions leaked most of
  the derivation recipe. v2 instructions name the *policy/report contract*;
  the thresholds, exemptions, and scoping rules live in seeded `firm_policies`,
  `firm_controls`, and `staff_roster` collections the agent has to find and
  read.
- **Planted near-misses defeat the single most obvious query** — measured, not
  asserted. `outputs/` dissolution probes compute the delta between the naive
  query and the correct rule on the captured seed snapshot (e.g. task 001's
  naive substring match finds 7 candidate pairs vs 2 real; task 014's naive
  "custodian has any collection" matcher under-reports the gap set by 5;
  task 012's name-only conflict scan over-reports by 3). Per-task numbers are
  in each task's `REDTEAM.md`.
- **Dual-path verification is the norm, not a one-off.** Every verifier that
  asserts an aggregate computes it two independent ways — a raw-row Python
  filter and a separate SQL `GROUP BY`/`SUM` — and requires both to agree
  before either is compared to the agent's report.
- **Every task ships two independently authored reference solutions**
  (`gold.py` + `gold_alt.py`), so a verifier that happens to fit one
  solution's incidental behavior (row iteration order, tie-breaking) gets
  caught by the other.

**Calibrated result:** `openai/gpt-5.6-sol` failed on all **5 measured
episodes** — every rollout ran to the model's own declared completion, and
the deterministic verifier rejected every one. This is the same model that
solved the v1 "difficult" task in 8 turns / ~3 minutes. Public traces on the
[environment leaderboard](https://app.primeintellect.ai/dashboard/evaluations/t1ntmvsonkbdpgvyekwau5fa);
full transcripts: `outputs/evals/`.

## What this is

A Gymnasium-compatible RL environment wrapping a real, self-contained web app
(law-firm React frontend + a Stackhouse backend-as-a-service + Postgres 15,
pre-seeded to ~50,250 rows across 21 collections and running in Docker), with
a **26-task suite** of state-change missions: multi-step, stateful,
API-driven work a billing clerk, conflicts analyst, e-discovery coordinator,
or legal-ops manager at a real firm would actually be paid to do — not
"insert a row with field foo=bar" toy tasks.

The agent acts by issuing structured REST calls (`GET`/`POST`) against the
app's live API — no source code is written, no GUI is driven by pixels.

## Why this is hard to cheat

Every task in this suite is a **state-change mission** graded on the final
database state, not on which endpoints were called. The design defends against
the standard ways a policy tries to claim reward without doing the work:

| Attack | Defense |
|---|---|
| No-op / random actions | Baselines score 0 — proven by executable test, not assumed |
| Hard-coded / memorized answers | Per-episode random nonce + jittered episode date make static answers stale on every reset |
| Delete/overwrite unrelated rows to force a pass | Canary hash check over every seeded collection outside the task's declared blast radius |
| Duplicate/stuff rows to fake a count | Exact-count + uniqueness assertions |
| Fudge one buggy aggregate computation | Every aggregate is asserted two independent ways (Python filter + SQL `GROUP BY`) before it's ever compared to the agent's report |
| Poll the grader to infer the answer | Fail-closed grading, first-failing-assertion detail cap (no full expected-value dump) |
| Flaky/lucky pass | Verifiers are deterministic; every task's QC required two consecutive identical verifier runs |
| Cross-episode contamination | Fresh `--rm` container per episode, no volumes, free ports |
| Persuade/game a fuzzy judge | No LLM-as-judge anywhere in the grading path — every verifier is plain Python asserting exact DB state (see `tools/vlib.py`) |

**Result: 26/26 tasks pass the full 8-gate QC battery** on the rebuilt image —
no-op fails, random-action fails, hardcoded-guess fails, the reference
solution passes, the verifier is idempotent, a *second* gold run still passes,
and the independently-authored `gold_alt.py` passes (see `QC_REPORT.md`,
`qc_results/`, and each task's `REDTEAM.md`).

## Task suite

Every task is **hard** difficulty, all 26 ship completely open — mission spec,
both reference solutions, grader, and red-team notes:

| # | task | category |
|---|---|---|
| 001 | trust_ledger_reconciliation | repair, aggregation |
| 002 | privilege_clawback_audit | repair, aggregation |
| 003 | hold_linkage_repair_and_ack_sweep | repair, workflow |
| 004 | grant_portfolio_integrity_sweep | repair, workflow |
| 005 | docket_deadline_risk_task_seeding | workflow, idempotency |
| 006 | new_matter_intake_conflict_check | create |
| 007 | time_entry_invoice_conversion | workflow, update |
| 008 | billing_realization_utilization_report | aggregation |
| 009 | grant_pipeline_risk_dashboard | aggregation |
| 010 | ediscovery_review_queue_progress_report | aggregation |
| 011 | matter_staffing_roster_reconciliation | repair, aggregation |
| 012 | firmwide_conflict_of_interest_audit | aggregation, repair |
| 013 | sol_calendaring_gap_repair | repair, create |
| 014 | custodian_collection_gap_audit | aggregation, workflow |
| 015 | client_lifetime_value_ar_aging_report | aggregation |
| 016 | top_matter_profitability_report | aggregation |
| 017 | grant_budget_burn_forecast | aggregation |
| 018 | chain_of_custody_timeline_audit | aggregation |
| 019 | billing_disruption_window_detection | aggregation |
| 020 | grant_coauthor_workload_report | aggregation |
| 021 | invoice_aging_dispute_risk_report | aggregation |
| 022 | firmwide_kpi_pack | aggregation |
| 023 | firmwide_temporal_anomaly_sweep | aggregation |
| 024 | firmwide_referential_integrity_audit | aggregation |
| 025 | billing_arrangement_compliance_audit | aggregation |
| 026 | firmwide_month_end_close_pack | repair, workflow, aggregation |

## Layout

```
env.py                 # Gymnasium wrapper: container-per-episode, nonce injection, external verifier
test_env.py             # smoke test: reset -> gold.py -> verifier PASS (via env reward path)
RECON.md                 # live inventory of the app's API surface (collections, fields, routes)
BUILD_NOTES.md           # source fixes, image build log, snapshot procedure, reseed rationale
QC_REPORT.md             # aggregate QC results across all 26 tasks
deploy/prime_intellect.yaml
tools/
  glib.py                # shared gold-solution library (stdlib-only)
  vlib.py                # shared verifier library (stdlib-only, read-only, fail-closed)
  qc_task.py              # per-task QC battery (noop/random/hardcode fail, gold + gold_alt pass, idempotent)
  phase4_attack.py        # partial-kill / canary-violation / retry-flake attacker
  capture_snapshot.py     # seed-snapshot capture for canary hashing
tasks/
  <id>/task.json         # mission spec
  <id>/REDTEAM.md         # reward-hacking threat-model notes + measured dissolution-probe numbers
  <id>/gold.py            # reference solution
  <id>/gold_alt.py        # second, independently authored reference solution
  <id>/verifier.py        # standalone PASS/FAIL grader
qc_results/               # per-task QC transcripts, all 26 tasks
outputs/evals/            # gpt-5.6-sol calibration transcripts (12 rollouts, avg reward 0.0)
adapters/
  hud/                    # HUD platform adapter (env.py/tasks.py/Dockerfile.hud)
  prime-intellect/        # Prime Intellect Environments Hub adapter (verifiers spec)
```

## Also available on HUD and Prime Intellect

`adapters/hud/` and `adapters/prime-intellect/` wire the suite into each
platform's native format (HUD's `Environment`/task-template SDK; Prime
Intellect's `verifiers` spec). The published environment is
`praesidiumsystems/law-firm-software` on Prime Intellect.

## Run it

The Docker image (`rl-env/law_firm_software:latest`) contains the seeded app +
Stackhouse backend + OpenEnv gym server. The image itself isn't hosted from
this repo yet; contact us for a pull token or a copy.

```bash
pip install gymnasium
docker pull <registry>/rl-env/law_firm_software:latest   # ask us for access
python test_env.py 013_sol_calendaring_gap_repair        # smoke test
```

```python
from env import StackhouseEnv

env = StackhouseEnv(app_slug="law_firm_software", task_id="013_sol_calendaring_gap_repair")
obs, info = env.reset()          # boots fresh container, injects episode nonce
obs, reward, terminated, truncated, info = env.step({
    "method": "GET",
    "endpoint": "/v1/query/matters?limit=5",
    "payload": None,
    "as_user": "verifier",
})
env.close()
```

Rewards are computed by shelling out to the task's standalone `verifier.py`
against the episode's own mapped BaaS port — 1.0 iff the verifier exits 0.

## Where this fits in the catalog

This environment is an **ultra-long-horizon, tool-use agent environment**: the
policy only ever emits structured API calls against a live backend and is
graded on resulting database state — no source code is written or edited, and
no GUI is driven by pixels/screenshots. The dependent-stage chains mean
episodes run hundreds of real API calls deep, with failure surfacing far from
its root cause. It sits in the same family as agentic
tool-use/backend-automation benchmarks, distinct from:

- **Code environments** (SWE-bench-style: the agent edits a codebase, graded
  by a test suite) — a separate category in our roadmap.
- **Computer-use environments** (OSWorld-style: the agent drives a real GUI
  via mouse/keyboard/screenshots) — also a separate category we're building.

## Get a custom environment

We build these to order: pick a domain, we ship a Docker-packaged app +
verified, reward-hacking-resistant tasks against it, QC'd exactly
like this one — and we have **100+ of these environments in the pipeline,
shipping this quarter**, this one included. Contact **Praesidium
Compliance Systems Corporation**:

- Email — `arjav.desai@praesidiumsystems.ai` or `sam.heidler@praesidiumsystems.ai`
- Web — [praesidiumsystems.ai](https://praesidiumsystems.ai)
- This gym — [GitHub](https://github.com/ArjavDesa912/ultra-long-horizon-legal-agent-benchmark-law-firm-ops) · [Prime Intellect](https://app.primeintellect.ai/dashboard/environments/praesidiumsystems/ultra-long-horizon-legal-agent-benchmark-law-firm-ops)
- Stack — [Stackhouse on GitHub](https://github.com/ArjavDesa912/stackhouse) · [stackhousedb.com](https://stackhousedb.com)
