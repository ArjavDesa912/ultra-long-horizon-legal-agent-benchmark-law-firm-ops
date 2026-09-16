# 010_ediscovery_review_queue_progress_report — REDTEAM notes (v2 stub)

## Mission
Build the e-discovery review-queue progress report per the firm's review-status
vocabulary (`REVIEW-STATUS-01`): scope = matters with an active hold OR a
finalised/served production; one `ediscovery_progress` row per in-scope matter
(document totals, reviewed share with the policy's normalization, privileged
count, collected-item total, and the collected-vs-loaded backlog); then a
bottom-N priority queue (N = the episode row's `priority_queue_size`) ranked by
lowest reviewed share with the firm's identifier-order tie-break, derived from
this batch's own rows.

## Why this is hard / unique
- The scope is no longer narrated: matters qualify via hold/production
  activity (active hold OR finalised/served production), not "every matter
  with documents" — 683 of the snapshot's document-bearing matters sit OUTSIDE
  the scope and must not get rows (snapshot-confirmed matter set).
- The review vocabulary is no longer narrated: REVIEW-STATUS-01's synonym map
  decides that legacy `qc-complete` counts as qc_complete (reviewed) and that
  unrecognized spellings count as unreviewed — an agent counting raw values
  writes plausible wrong percents.
- The queue cut is an episode knob (`priority_queue_size` 3-8) and the ranking
  is derived from the rows the agent just wrote — a wrong stage-1 percent
  silently re-ranks the queue far from the cause.
- In-scope matters with zero documents still get progress rows (backlog =
  collected items); an agent that only reports matters with documents
  under-reports the scoped set by 31 rows in the current snapshot.

## Hazards planted (taxonomy §2a + snapshot counts)
1. `integration_fault` — 12 documents carry the legacy hyphenated
   `qc-complete` review_status from the pre-migration review tool (planted: 12,
   snapshot-confirmed); REVIEW-STATUS-01 normalizes it to qc_complete, so they
   count as reviewed — an agent counting raw values under-reports those
   matters' reviewed counts (per-rebuild drift may place them in or out of
   scope; the verifier enforces normalization either way).
2. `latent_organizational` — the in-scope matter set is defined by hold/
   production activity, not by document presence (snapshot-confirmed: 98
   in-scope matters, 683 document-bearing matters out of scope, 31 in-scope
   matters with zero documents); auditing every matter with documents
   over-reports the queue.
3. `decision_pressure` — the priority-queue size is an episode knob (3-8);
   the bottom set's membership and order move every episode, so memorized
   rankings go stale.

## Episode knobs (nonce.extra_fields)
- `priority_queue_size` (min 3, max 8) — read live by gold
  (`g.nonce(field=...)`) and verifier (`vlib.get_nonce(v.token, field=...)`);
  referenced by field name in the instruction, never by value.

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
`max_steps` (200, from the already-v2 task.json) stays a placeholder until the
measured gold run lands.

## Dissolution probe & chain evidence (snapshot-measured)

- **Naive vocab**: counting `review_status='reviewed'` only misses the 12 legacy `qc-complete` rows (hyphen spelling) that must normalize into the reviewed bucket.
- **Haystack**: 2000 documents across 5 spellings; naive raw-value count disagrees with the normalized count.
- **Probe**: `outputs/dissolution_probes.py` -> vocab {reviewed:496, unreviewed:508, qc_complete:495, in_review:489, qc-complete:12}.
