---
name: Epic 7 Retrospective — Decision Engine
description: Retro done 2026-05-05; single-evaluator principle, ActionType enum gate, PartialIntervalTracker as full BMAD story, deferred findings triage rule for Epic 8
type: project
---

Epic 7 retrospective completed 2026-05-05. All 5 stories done, 736 tests, zero open findings.

**Key decisions and constraints for Epic 8:**

Single-evaluator principle: `evaluate_cycle()` is the sole generator of control intents. Every other Epic 8 component either provides input or enforces safety — no competing decisions.

Deferred findings triage rule (Jordan): If a deferred engine issue can produce an invalid intent, misleading reason, unsafe dispatch, or ambiguous watchdog behavior → prerequisite fix or explicit Epic 8 AC. PolicyGuard must not compensate for weak decision-engine invariants.

**Why:** Deferred action_type strings crossed the runtime-risk threshold. ActionType enum with exhaustive dispatch (P2) is a hard gate before Story 8.2.

**How to apply:** Before any Epic 8 story is created, verify P1 and P2 are complete as full BMAD stories. Their outputs directly determine Story 8.1 and 8.2 ACs.

**Critical path before Story 8.1:**
- P1: PartialIntervalTracker contract + implementation (full BMAD story, Winston + Jordan) — includes finalize-before-evaluate invariant: PeakContext only from active interval
- P2: ActionType enum + exhaustive dispatch (full BMAD story, Amelia) — unknown member raises, not silent hold

**A5 (prev A3/B4):** CSRF/HTMX reminder still deferred to first authenticated UI story in Epic 10 or 11 (Paige).
**A4 (prev A2):** Adapter scaling counter at 1 of 2 (Winston monitors).
**A3:** Jordan to update Story 7.5 file from `Status: review` to `Status: done`.
