# 014_custodian_collection_gap_audit — REDTEAM notes (v2 stub)

## Mission
Run the custodian collection-gap audit per the firm's hold-acknowledgement
policy `HOLD-ACK-01`: for every ACTIVE hold in effect at least the episode row's
`gap_grace_days` before the episode date, every named custodian must have data
collected for that hold's OWN matter -- a collection under a different matter
never covers the custodian. Push one gap row per uncovered custodian, one
summary row with the total, and age the same gap rows into the firm's standard
under_30 / 30_90 / over_90 hold-aging buckets.

## Why this is hard / unique
- The scope rule is no longer narrated: which holds are audited is decided by
  HOLD-ACK-01 plus the episode row's `gap_grace_days` (15-45) -- released and
  suspended holds are out of scope, and freshly issued holds inside the grace
  window are excluded, so the gap set moves every episode.
- The coverage rule is per-MATTER: a custodian's collection under a DIFFERENT
  matter does not cover their own hold. The seed plants 5 cross-matter
  collections for the first 5 bulk gap custodians (COL-2026-XMAT-0..4) -- an
  agent matching "custodian has any collection anywhere" misses exactly those
  gaps whenever the custodians' own holds are in scope.
- The aging stage must derive from the SAME gap rows the batch pushed (read
  back, not recomputed) -- a wrong gap identification or day count silently
  mis-buckets.
- Read-only source collections: ediscovery_holds and ediscovery_collections
  must survive byte-identical; the fix lives entirely in ops_reports.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `lapse` — custodians of active holds with no covering collection for that
   matter (planted: 20 bulk holds with no collection at all + 4 hand-authored
   custodian gaps; the live in-scope gap set recomputes per episode -- 12 gap
   pairs at the minimum grace window in the current snapshot).
2. `scope_boundary` — 5 cross-matter collections (COL-2026-XMAT-0..4,
   snapshot-confirmed: 5) cover their custodians under a DIFFERENT matter; the
   naive any-collection matcher misses exactly those gaps.
3. `decision_pressure` — the grace window is an episode knob (15-45 days); the
   in-scope hold set and the gap set move every episode, so memorized gap
   lists go stale.

## Episode knobs (nonce.extra_fields)
- `gap_grace_days` (min 15, max 45) — read live by gold (`g.nonce(field=...)`)
  and verifier (`vlib.get_nonce(v.token, field=...)`); referenced by field name
  in the instruction, never by value.

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
`max_steps` (80) is a generous placeholder until that measurement lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Cross-matter trap**: 5 COL-2026-XMAT collections cover custodians bulk-cust-0..4 under a DIFFERENT matter than their active hold; a "custodian has any collection" matcher under-reports the gap set. Seed fix (post-QC round): holds[0..4] forced `active`, issued 200d before build so they clear any gap_grace_days in [15,45].
- **Chain**: gap rows -> summary count -> aging buckets over this batch's own rows.
- **Probe**: `outputs/dissolution_probes.py` -> XMAT=5; trap bite verified post-reseed.
