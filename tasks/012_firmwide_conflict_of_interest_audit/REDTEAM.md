# 012_firmwide_conflict_of_interest_audit — REDTEAM notes (v2 stub)

## Mission
Run the firmwide conflict-of-interest audit per the firm's intake conflict-
screen policy `INTAKE-01`: a conflict exists when a contact's organisation
matches a client's name under the policy's normalization (lowercase,
alphanumeric only, legal-form words stripped) AND the contact is linked to a
matter whose own client is a DIFFERENT client. Push one conflict row per
conflict with a diagnosis note, one severity row per conflict (high iff the
conflicted matter's status is open), and one summary row (the only row when the
sweep finds nothing).

## Why this is hard / unique
- The conflict definition is no longer narrated: the agent must apply INTAKE-01's
  NORMALIZED name matching (lowercase, alphanumeric only, legal-form words
  stripped) to contact organisations vs the client book -- an exact-name match
  alone is the wrong rule.
- The different-client condition is the discriminator: 3 same-org same-client
  near-miss contacts (General Counsel Tech -> matters 2026-003/007, Green Earth
  Liaison -> 2026-004, State AG Liaison -> matter 2026-005) match a client name
  exactly but their linked matters belong to that same client -- flagging any of
  them over-reports by exactly 3 (snapshot-confirmed) and FAILs.
- The former-client matters (2026-008 closed ~400d, inside any 2-6y window;
  2026-009 closed ~2600d, outside) are matters, not contacts: applying the
  former-client window as the audit's flag condition mis-flags; they are
  near-miss volume for this sweep.
- Stage 2 reads the conflict rows back and looks up each conflicted matter's
  CURRENT status live -- a wrong stage-1 contact/matter identification silently
  targets the wrong matter's status.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `latent_organizational` — exactly 1 true conflict exists in production data
   (planted: 1, snapshot-confirmed): contact Mary Johnson, organisation
   'Tech Innovations Inc', linked to matter 2026-001 whose client is Acme
   Corporation -- the firm is litigating against its own client's company.
2. `scope_boundary` — 3 same-org same-client near-miss contacts (4 contact-
   matter pairs, snapshot-confirmed) must NOT be flagged; name-only matching
   over-reports by exactly 3 (planted: 3, snapshot-confirmed).
3. `mistake` — former-client matters 2026-008 (closed ~400d, inside any 2-6y
   window) and 2026-009 (closed ~2600d, outside) are planted near-misses
   (snapshot-confirmed pair); the audit's flag rule is the organisation match,
   not the former-client window.

## Episode knobs (nonce.extra_fields)
- none required (the conflict rule is policy-fixed; the former-client window
  belongs to task 006's intake, not this audit).

## Phase 4 — NOT YET RUN

| # | Attack | Result |
|---|---|---|
| 1 | No-op / random / lazy-hardcode baselines | pending |
| 2 | Partial/prefix satisfaction (kill gold mid-run) | pending |
| 3 | Canary/scope violation (mutate a collection outside blast_radius) | pending |
| 4 | Retry/flake (verifier run 5x on identical post-gold state) | pending |
| 5 | Metadata inference / evaluation-function tampering | pending |
| 6 | Style/no-op-adjacent shortcut (lazy-hardcode baseline) | pending |

Phase 4 (hacker-fixer loop against a live container) is deferred to the QC
session. `par_steps` is null pending the live gold measurement there;
`max_steps` (60) is a generous placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive org match**: without the different-client rule, normalized-org matching flags 4 contacts; the correct set is 1 true conflict (severity split by matter status).
- **Near-misses**: exactly 3 same-org same-client contacts must NOT be flagged.
- **Probe**: `outputs/dissolution_probes.py` -> true=1, naive extras=3. Verified live: PASS on all 8 gates.
