# 006_new_matter_intake_conflict_check — REDTEAM notes (v2)

## Mission
Open the matter 'Green Earth Foundation v. Tech Innovations Inc' per the seeded intake
procedure `INTAKE-01`: run the policy's conflict screen (normalized-name matching over
clients and contact organisations, plus the former-client window bounded by the episode
row's `former_client_window_years`), pick the responsible attorney and supervising
partner from the live roster and time-entry history, create the matter with the
policy-derived number and status, and record the intake audit row.

## Why this is hard / unique
- The v1 instruction handed the agent the roster, the numbering rule, the description
  template, and the conflict answer; v2 deletes all of it. The agent must read INTAKE-01
  and discover: the normalized-name match (contact 'TechInnovations LLC' normalizes to
  the same name as client 'Tech Innovations Inc' — the policy's normalization decides it
  IS a match), the former-client window edge (matter 2026-008 closed ~400d ago is inside
  any window; 2026-009 closed ~2600d ago is outside every window up to 6 years), and the
  attorney selection from live hours among active partner/associate roster rows.
- The roster lives in `staff_roster` (EMP-006 is a departed counsel; EMP-003/EMP-004 are
  wrong roles) — the near-miss web fails a naive solution in several ways.
- The matter's status (conflict_check vs open) and description note derive from the
  screen outcome, not from a template in the instruction.

## Hazards planted
1. `mistake` — normalized-name near-miss: contact 'TechInnovations LLC' (planted: 1;
   specified in the v2 seed plan, absent from the current snapshot capture — see
   ASSUMPTIONS) normalizes to the same name as client 'Tech Innovations Inc'; the
   policy's normalization decides it IS a match. A name-only exact-match screen misses it.
2. `decision_pressure` — the proposed opposing party is itself a current client
   (CLI-2026-003) and a former client on matter 2026-008 (closed ~400d, inside every
   window), while matter 2026-009 (closed ~2600d) is outside every window up to 6 years
   (planted: 2 window-edge matters, snapshot-confirmed); opening 'open' instead of
   'conflict_check', or citing 2026-009 as a conflict, = FAIL.
3. `copy_paste_drift` — attorney selection: fewest total hours among active
   partner/associate roster rows; EMP-006 (counsel, departed) and EMP-003/EMP-004 (wrong
   roles) are near-misses; the pick moves with live time_entries each rebuild.

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate clients/contacts/time_entries) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC session.
`par_steps` is null pending the live gold measurement; `max_steps` (160) is a generous
placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive query**: exact-name contact matching misses the normalized dba-variant contact; a name-only screen under-reports the conflict.
- **Near-misses**: a window-edge matter and an outside-window matter must NOT be cited — boundary discrimination on the conflict window.
- **Probe**: `outputs/dissolution_probes.py` -> 803 contacts (all carry organisation), 3 normalized orgs collide with client names.
