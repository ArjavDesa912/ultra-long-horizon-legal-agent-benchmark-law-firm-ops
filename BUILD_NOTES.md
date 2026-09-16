# Build notes — law_firm_software bundle

Status: **25-task suite complete, QC'd, and Phase-4 hacker-fixer tested — every task
individually attacked.** Following `RL_ENV_FACTORY_PROMPT.md` Phases 0-5, `N_TASKS` raised
from the default 10 to 25 per operator direction, every task calibrated hard (no easy/medium
tier in this pass). All 25 pass the full QC battery against the final ~50,250-row,
chaos-injected build (`rl-env/law_firm_software:latest`), confirmed via both individual runs
and one full-suite sweep — see `QC_REPORT.md` and `qc_results/*.json`. A full Phase 4
hacker-fixer pass ran canary/scope-violation, partial/prefix-satisfaction, and retry/flake x5
attacks against **all 25 tasks individually** (not a representative sample), plus a
suite-wide metadata-inference/evaluation-tampering check, finding and fixing 2 real exploits
(task 004's overly-strict `grant_reports` coverage check, task 007's unbounded
7881-API-call blast radius at full seed scale) before the final clean sweep — every one of the
25 tasks correctly rejected every attack. See `QC_REPORT.md`'s Phase 4 section and each task's
own `REDTEAM.md` for the specific transcript and `hardened_after_rounds` count.

## Pass 2 fixes (this session, before task authoring)

1. **`env.py` nonce anchor was wrong for this app.** Copied verbatim from
   `veterinary_clinic_system`'s scaffold, it hardcoded a fixed calendar
   anchor (`2026-09-30`) appropriate for vet clinic's absolute fictional
   dates. This app's `seed.js` computes every date as
   `addDays`/`subtractDays(N)` from real wall-clock `today` *at image build
   time* — the correct anchor is `datetime.now(timezone.utc)` at grading
   time, not a hardcoded date. Fixed: `_inject_nonce` now uses real "now",
   jittered ±1 day (not vet clinic's ±3 — no month-end slack to jitter
   within here).
2. **Collection prefix: manifest lies.** `/rl/manifest.json` inside the
   image claims `"collection_prefix": "law_firm_software_"`, but `GET
   /v1/tables` against a fresh container shows the live tables carry **no
   prefix at all** (`clients`, `matters`, `invoices`, ... not
   `law_firm_software_clients`). This is the exact "trust live observations
   over manifests" failure mode `RL_ENV_FACTORY_PROMPT.md` warns about for
   vet clinic's doubled-underscore incident — caught by actually querying
   `/v1/tables`, not by reading the manifest. Fixed in three places:
   `env.py` gained an explicit `collection_prefix` constructor arg
   (defaulting to `""` for this app, no longer re-derived as
   `f"{app_slug}_"`), and `tools/glib.py`/`tools/vlib.py`'s `PREFIX` constant
   changed to `""`. `bundle/law_firm_software/capture_snapshot.py`'s BaaS
   readiness probe also checked for a prefixed table name and hung for its
   full 240s timeout every time — fixed to check for the real unprefixed
   `clients` table.
3. **Seed script pushed 3 collections with a field literally named `date`.**
   `time_entries.date`, `trust_transactions.date`, `grant_expenses.date` (all
   named in the product spec) are rejected outright by Stackhouse's push
   validation ("Invalid identifier: Identifier 'date' is a SQL reserved
   keyword" — the same class of restriction `veterinary_clinic_system`hit
   with a field named `key`, but that one only affected query filters; this
   one silently dropped **every** push to these 3 collections, confirmed via
   the actual build log, not assumed). Compound field names like
   `issued_date`/`due_date` are unaffected — only the bare identifier `date`
   trips it. Minimal fix in the SOURCE seed script
   (`example-for-baas-main/softwares/law_firm_software/scripts/seed.js`,
   Hard Rule 1): renamed to `entry_date` (time_entries), `tx_date`
   (trust_transactions), `expense_date` (grant_expenses). Nested `date` keys
   inside a JSONB blob (`invoices.line_items[].date`) are unaffected by this
   rule and were left alone.
4. **`ediscovery_documents` tripped a schema-churn rate limit.** Each
   hand-authored document had ~22 fields; pushing a **first-ever** row for a
   dynamic-schema table that needs to create more than 20 new columns in one
   shot exceeds Stackhouse's "20 new columns per 60s rolling window per
   table" limit outright — confirmed via the build log ("Rate limited:
   Table 'ediscovery_documents' has added 0 new columns in the last 60s;
   limit is 20"), which silently dropped 3 of the 5 hand-authored documents
   (including the one the privilege-clawback task's whole hazard depends
   on). Fixed by trimming the seeded field set to 16 (dropped
   `date_modified`, `date_received`, `recipients`, `tags`, `notes`,
   `hash_md5` — none used by any authored task) for both the hand-authored
   rows and every bulk-generated document (see below), so schema growth for
   this table stops at 16 columns, comfortably under the limit, and every
   later push (hand-authored or bulk) reuses those same columns with zero
   further schema churn.

All 4 fixes are logged here per Hard Rule 1; none change business logic or
seeded row *content*, only what makes the seed actually load and what the
host-side tooling assumes about live table names.

## Pass 2: seed volume scale-up (50k+ rows firm-wide)

Per operator direction, appended a **BULK VOLUME SCALE-UP** section at the
end of `seed.js` (after every hand-authored fixture section, so no existing
row's id or content changes) that procedurally generates ~50,150 additional
rows across all 17 collections via a `pushMany()` helper (`Promise.allSettled`
in batches of 40 for throughput — the whole bulk pass completed in the
seeder Docker stage in ~55s total, well within budget). Because this runs
*after* every collection's schema is already fully established by the
hand-authored rows, bulk pushes introduce **zero new columns**, so the
`ediscovery_documents` rate-limit above never re-triggers during bulk
seeding regardless of volume.

Approximate allocation (hand-authored + bulk = total): clients 5+495=500,
matters 7+1993=2000, contacts 6+794=800, deadlines 10+2990=3000,
tasks 12+2988=3000, time_entries 17+29983=30000, invoices 3+2997=3000,
trust_transactions 8+3992=4000, ediscovery_holds 3+97=100,
ediscovery_collections 3+197=200, ediscovery_documents 5+1995=2000,
ediscovery_productions 1+99=100, grant_opportunities 7+293=300,
grant_applications 3+297=300, grant_awards 1+149=150, grant_reports
1+299=300, grant_expenses 1+499=500. Total ≈ 50,250 rows.

Bulk rows use plain `Math.random()` (no fixed seed) — like this app's
existing date-anchoring, every image rebuild produces different bulk data;
`capture_snapshot.py` must be re-run after every rebuild (already true).
Bulk `trust_transactions.reference` is a fixed non-matching string
("Bulk-generated transaction..."), so task 001's invoice-backfill part
stays bounded to the 2 original hand-authored matches regardless of bulk
volume — deliberate, so that mission doesn't balloon into thousands of
false substring hits. Cross-references inside the e-discovery/grants bulk
rows (`hold_id`, `collection_id`, `opportunity_id`, `award_id`,
`application_id`) are soft placeholder strings that don't resolve to real
bulk ids, matching the *existing* pattern in the hand-authored data (the
original `ediscovery_collections.hold_id` bug task 003 repairs) rather than
introducing a new inconsistency class.

**Consequence for task design**: any task whose blast radius is "every
row matching X" must be re-checked against the new scale — a mission that
was a precise 2-4-row fix against the 93-row v1 seed can become a
firm-wide sweep touching hundreds of rows against the 50k-row seed. Where
that inflates a task's write cost past a reasonable per-episode step
budget, the task is rescoped to a bounded, named target (a specific matter,
client, or date window) discovered via full-dataset querying, rather than
processing the entire collection — see each task's own notes for how it
was scoped.

## What this pass did

- Selected `law_firm_software` (Stackhouse app at
  `example-for-baas-main/softwares/law_firm_software`, no nested `-app`
  folder, port 3005, `node scripts/seed.js`, admin `admin@lawfirm.com` /
  `Admin123!`) as the next software to bundle, following the same recipe as
  `rl_envs/veterinary_clinic_system` / `bundle/veterinary_clinic_system`.
- Cloned `bundle/veterinary_clinic_system/` -> `bundle/law_firm_software/`
  and mechanically renamed every `veterinary_clinic_system` / port `5110` /
  `admin@pawsclinic.com` / `Demo123!` reference to the law-firm equivalents
  (Dockerfile ARGs, server/, grading/, rl/, hud/, openenv.yaml,
  capture_snapshot.py, pyproject.toml, README.md). Did not copy
  `bundle/veterinary_clinic_system/prime/` or `outputs/` — those are
  generated/staged artifacts (a 41MB Prime remote-build staging copy and
  local eval outputs), not source.
- Scaffolded `rl_envs/law_firm_software/`: `env.py` (Gymnasium wrapper,
  fully generic — only the `app_slug` default differs), `tools/vlib.py` +
  `tools/glib.py` (generic Stackhouse REST helpers, `PREFIX` updated),
  `deploy/prime_intellect.yaml`. `tasks/` and `_expectations/` are empty —
  the Dockerfile's grading-suite `COPY` needs the directories to exist, but
  there are no gold solutions, verifiers, or seed snapshot yet.
- Fixed one drift from the vet-clinic template while adapting the
  Dockerfile: its `APP_DIR` ARG pointed at a nonexistent nested
  `law_firm_software-app/` folder (copied from vet clinic's layout,
  which does have one) and its `SEED_CMD` ARG used vet clinic's
  `node src/scripts/seed.js` path. Corrected to `example-for-baas-main/softwares/law_firm_software`
  and `node scripts/seed.js` to match this app's actual layout
  (`example-for-baas-main/rl-env/apps.json`).

## Explicitly not done in this pass

- Phase 0 recon (`RECON.md`: live collection inventory, seed row counts,
  Stackhouse endpoints actually used) — not started.
- Phase 1-3: the 10 tasks (task.json/gold.py/verifier.py/REDTEAM.md each),
  web-search uniqueness reports, QC transcripts, reward-hacking red-team
  tests.
- `QC_REPORT.md`, `test_env.py`.
- HUD/Prime Intellect publish (`publish.ps1`'s later stages) — the adapter
  files were copied over for parity but nothing was pushed anywhere.
- Open-source `release/` sample (mirrors `release/veterinary-clinic-system-sample`).

Follow `RL_ENV_FACTORY_PROMPT.md` (SOFTWARE_DIR =
`example-for-baas-main/softwares/law_firm_software`, OUTPUT_DIR =
`rl_envs/law_firm_software`, N_TASKS = 10) for the next pass.

## Verification done this pass

See the bottom of this file once the image build below finishes — updated
in place with the actual `docker build` / healthcheck outcome.

## Pass 3: hardmode capstone (task 026)

Added `026_firmwide_month_end_close_pack` per `RL_ENV_HARDMODE_PROMPT.md` §1e (dependent-stage
chains) after the 25-task pass above shipped. No seed/schema/image changes — it reuses the
existing 50k-row build and reuses tasks 001/007's exact rules for its first three stages, so
no new `tools/glib.py`/`tools/vlib.py` primitives were needed. Full design rationale, the
stage-dependency map, and its own Phase 4 transcript are in
`tasks/026_firmwide_month_end_close_pack/REDTEAM.md`; QC summary in `QC_REPORT.md`'s "Hardmode
QC — task 026" section. `hud/gen_tasks.py` was re-run to pick it up in the bundle's HUD taskset.

## Pass 4: uniform horizon_tier upgrade (tasks 001-025)

Operator feedback: a suite can't be called long-horizon/dependent-stage if only 1 of 26 tasks
(026) actually has that structure. Extended every one of tasks 001-025 with a genuine second
stage per §1e's rule -- stage 2 reads the state stage 1 actually mutated or wrote (never an
independent recompute from the original seed) -- calibrated per task's own domain rather than
forcing every task into 026's 5-stage shape. No seed/schema/image changes; all additive to
`tasks/<id>/{task.json,gold.py,verifier.py,REDTEAM.md}`. Full per-task design, the specific
dependency mechanism, and a live "dependency-break" attack transcript for each are in that task's
own `REDTEAM.md`; the summary table is in `QC_REPORT.md`'s "Dependent-stage-chain upgrade" section
and the design write-up is in `README.md`'s "Dependent-stage chains" section.

Also fixed in the same pass, unrelated to the horizon upgrade itself: task 006's `task.json`
instruction had drifted from its own `gold.py`/`verifier.py` (a stale scenario describing fields
the verifier would never accept); and `tools/qc_task.py`'s idempotency-category second-gold-run
check compared `task.get("category") == "idempotency"` against a field that is always a list, so
it silently never ran for any task (only 005 carries that tag) -- fixed to `"idempotency" in
task.get("category")`, confirmed working.

`hud/gen_tasks.py` does not need re-running for this pass (no tasks added or removed, only
existing task files changed) — but `bundle/law_firm_software`'s stale-reference comments (e.g.
`verify_embedded.py`'s "16 gold API calls" for task 013, now 25) still need a sync pass before
the next publish, since every task's real `par_steps` roughly doubled or more.

## Pass 5: v2 difficulty rewrite + dissolution-driven reseed (image 2.0.1)

All 26 tasks rewritten for measured difficulty (recipe leakage removed, distributed evidence,
planted near-misses, policy discovery via firm_policies/firm_controls/staff_roster, real
multi-stage chains). Two seed fixes driven by `outputs/dissolution_probes.py` measuring
naive-vs-correct deltas on the captured snapshot:

1. `ediscovery_holds` bulk specs 0..4 forced `status="active"`, `issued_date=subtractDays(200)`
   -- the task-014 cross-matter trap was inert because every trapped custodian's own hold
   rolled suspended/released (out of scope for any gap_grace_days in [15,45]). Post-reseed the
   naive "custodian has any collection" matcher under-reports the gap set by 5.
2. One disbursement-only invoice `INV-2026-0000` (fees_total=0, disbursements_total=250000,
   status=sent) planted on whichever contingency client leads the non-draft billed ranking --
   guaranteeing it lands inside every audit_top_clients slice for task 025's BILL-EXPOSE-01
   exemption. No disbursement-only invoices existed before; the hazard had zero fixtures.

Rebuild path: `bundle/law_firm_software/publish.ps1 -SkipPrime -SkipHud -Version 2.0.1`
(docker build -> capture_snapshot.py -> bake-in rebuild -> 5-gate verify_embedded on task 013
+ OpenEnv /health probe -- all green). Full 26-task 8-gate QC re-run against the new image:
26/26 PASS (see QC_REPORT.md v2 table; raw transcripts in `qc_results/`).

Verifier/gold correctness fixes folded into this image: jsonb `#>> {}` normalization for
string-vs-int references (024, 001), `::date` boundary comparisons (005), live-vs-seed state
paths (007 verifier rewrite + batch_code-marked idempotent gold, 015 client-driven LEFT JOIN),
expect_cents unit fixes (007), SQL alias mismatches (014/020/025 gold_alt, 014 verifier), and
001's second-run reuse of prior batch report rows.
