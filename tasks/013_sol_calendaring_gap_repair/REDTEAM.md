# 013_sol_calendaring_gap_repair — REDTEAM notes (Phase 4 PENDING)

## Mission

Run the firm's statute-of-limitations calendaring audit per policy SOL-01: every open litigation matter with an SOL value must carry exactly one statute-type deadline dated AT the SOL. Gaps get created; deadlines docketed on/after the SOL get re-dated (with a diagnosis row classifying the cause as a mistake); compliant lead-time buffers stay byte-identical. A risk ranking over every candidate matter's post-repair statute deadline is derived from the episode row's urgency cutoff.

## Why this is hard / unique

- The candidate set is a ~149-matter population (density-planted SOL values on bulk open litigation matters) — findable only by a filtered query, not the v1 2-row hand fixture (dissolution probe: `statute_of_limitations IS NOT NULL` now returns 149+ rows, not 2).
- Three outcome classes must be distinguished from policy: gap (create), compliant lead-time buffer (leave byte-identical), non-compliant late docketing (re-date + diagnose). The buffer-vs-gap distinction is a judgment against live data.
- The urgency threshold is an episode-row knob (`urgency_cutoff_days` 20-45) — memorized answers go stale every episode.
- Stage 4 derives from the post-repair state: a wrong stage-1/3 repair silently corrupts the ranking.

## Hazards planted (task.json.hazards) — snapshot-confirmed

- lapse — 112 candidate matters with NO statute deadline (gap set; snapshot-confirmed; verifier recomputes per rebuild).
- mistake — 11 statute deadlines dated on/after the SOL (4 planted "Docketed late" + natural background); must be re-dated, not skipped or duplicated.
- normalization_of_deviance — 31 compliant lead-time buffers that must remain byte-identical; an agent that "normalizes" every statute deadline to the SOL corrupts them.

## Phase 4 — NOT YET RUN

This task has not been through the live hacker-fixer loop. A QC session with Docker access must run the 6 standard attacks (metadata inference, partial/prefix satisfaction, canary/scope violation, evaluation-function tampering, retry/flake, style/no-op-adjacent shortcut) against a real container before this task ships. Do not treat this task as done until this section is replaced with real results and `hardened_after_rounds` is recorded.

## Dissolution probe & chain evidence (snapshot-measured)

- **Haystack density**: `statute_of_limitations IS NOT NULL` returns 149 open litigation matters — the needle is not a 2-row fixture.
- **Three-way split**: 112 gaps (create), 31 compliant lead-time buffers (leave byte-identical), 11 non-compliant (re-date + diagnose). A "repair everything" or "create missing only" agent corrupts buffers or skips re-dating.
- **Chain**: post-repair statute set -> risk ranking over episode knob `urgency_cutoff_days`; a wrong repair corrupts the ranking.
- **Probe**: `outputs/dissolution_probes.py`.
