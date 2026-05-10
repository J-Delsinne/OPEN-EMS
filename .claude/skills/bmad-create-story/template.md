# Story {{epic_num}}.{{story_num}}: {{story_title}}

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a {{role}},
I want {{action}},
so that {{benefit}}.

## Acceptance Criteria

1. [Add acceptance criteria from epics/PRD]

## Tasks / Subtasks

- [ ] Task 1 (AC: #)
  - [ ] Subtask 1.1
- [ ] Task 2 (AC: #)
  - [ ] Subtask 2.1

## Dev Notes

- Relevant architecture patterns and constraints
- Source tree components to touch
- Testing standards summary

### Project Structure Notes

- Alignment with unified project structure (paths, modules, naming)
- Detected conflicts or variances (with rationale)

### Orchestration Risk Analysis (A2-triggered — REQUIRED if applicable)

> **A1 enforcement contract (Epic 8 retro, 2026-05-10):** When this story matches one or more A2 triggers (T1–T7 in `SKILL.md`), all seven sections below MUST be present and substantive. Same enforcement class as missing acceptance criteria. `Status: ready-for-dev` cannot be set otherwise.
>
> **Skip this section ONLY if no A2 trigger matched** — and even then, leave a one-line note explaining which trigger evaluation was performed and why none matched, so the absence is auditable.

**A2 triggers matched:** {{list each, e.g. "T5: multi-adapter coordination — probes Modbus + OCPP + DSMR in one flow"}}

#### R1 — Composition-risk analysis

{{Enumerate every operational domain converging in this story. For each, identify the specific risk introduced by its convergence with the others. Cite review-finding precedents from prior stories where applicable.}}

#### R2 — State-transition table

{{For every state machine implemented or modified: states, allowed transitions, triggers, side-effects (audit, persistence, snapshot publish, log lines). Cover both nominal and degraded paths.}}

| State | Transitions out | Trigger | Side-effects |
|---|---|---|---|
| | | | |

#### R3 — Impossible-state analysis (with cold-start coverage)

**Forbidden state combinations and their structural invariants:**

1. {{forbidden combination → invariant that prevents it}}
2. ...

**Cold-start / startup-grace coverage (mandatory):**

{{What is the system's state from process boot until the first successful evaluation cycle? What is published, what is null/sentinel, what is loaded from DB, what relaxes temporarily, what marker signals normal operation begins?}}

#### R4 — Cancellation ownership map

| Async operation | CancelledError owner | Cancellation audit owner | Cleanup owner | Reporting field semantics under cancel |
|---|---|---|---|---|
| | | | | |

{{If this story does not introduce cancellation paths, document why explicitly.}}

#### R5 — Before-first-successful-cycle lifecycle review

{{Trace the sequence from process start to first successful normal cycle. What does the system publish, log, audit, or expose via API during this window? What invariants temporarily relax? What marker signals normal operation has begun?}}

#### R6 — Source-of-truth ownership per datum

| Datum | Single owner | Read path | Drift risk (if any) |
|---|---|---|---|
| | | | |

{{Explicitly call out any datum currently held in two places. Drift risk must be flagged or eliminated.}}

#### R7 — Deferred-findings triage

{{Scan `_bmad-output/implementation-artifacts/deferred-work.md` for items whose component or invariant overlaps this story. Cite each by its bracket reference.}}

| Finding (bracket ref) | Classification | Rationale |
|---|---|---|
| `[file:line]` | must-resolve / safe-during / acceptable-post | |

### References

- Cite all technical details with source paths and sections, e.g. [Source: docs/<file>.md#Section]

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
