# Law Firm Software — Practice Management Benchmark

> **Published by [Praesidium Compliance Systems Corporation](https://praesidiumsystems.ai)**
> · Built on **[Stackhouse](https://github.com/ArjavDesa912/stackhouse)** ([stackhousedb.com](https://stackhousedb.com))
> · **No LLM-as-judge anywhere** — all 26 tasks are graded by deterministic,
> fail-closed Python verifiers asserting exact database state, never by a
> model scoring the transcript.

A 26-task law-firm practice-management benchmark (matters, clients,
billing/trust accounting, e-discovery, grant management) running against a
**live Stackhouse BaaS** — packaged as one fully self-contained Docker
container (Postgres 15 + Stackhouse BaaS + prebuilt Vite frontend + OpenEnv
env server). No external services, no volumes, no API keys.

- **Contract**: OpenEnv gym (`/reset`, `/step`, `/state` on port 8000)
- **Image**: built from `proj/server/Dockerfile` (the unified bundle Dockerfile)
- **Action space**: one Stackhouse REST call per step
  (`{method, endpoint, payload, as_user}`)
- **Reward**: 1.0 iff the task's standalone fail-closed verifier exits 0
- **Episode isolation**: `reset()` restores a pristine template database
  (frozen at image build time) in ~1-3s and injects a fresh per-episode nonce
  (`EP-XXXXXXXX`) plus a +/-1-day-jittered episode date, so memorized answers
  go stale every episode
- **Task discovery**: `GET /tasks` (or the TaskProvider routes) lists all 26
  tasks; select one via `reset(task_id=...)`
- **Dependent-stage chains**: every task carries `horizon_tier >= 2` — a
  second stage reads the state the first stage actually mutated or wrote
  (never an independent recompute), so a wrong or skipped first stage
  silently corrupts the second stage's output instead of erroring. One task
  (`026_firmwide_month_end_close_pack`) chains 5 such stages plus a
  6-historical-month-end point-in-time reconstruction (`horizon_tier: 6`).
  Every aggregate is verified two independent ways (Python filter + SQL)
  before being compared to the agent's report.
- Seed: ~50,250 rows across 17 collections, correct-by-construction by
  default with a small, counted, taxonomy-tagged set of deliberate defects
  planted on top (see the public sample repo's `README.md`/`BUILD_NOTES.md`/
  `QC_REPORT.md` for the full defect table, QC transcripts, and every task's
  own `REDTEAM.md` for its specific dependency mechanism and a live
  adversarial attack transcript proving it isn't cosmetic).

**Difficulty is author-asserted and unmeasured** — no calibration run against
a frontier-model pool has been performed, so there is no claimed measured
pass rate for any task ahead of the eval results attached to this listing.
