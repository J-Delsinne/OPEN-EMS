---
deferred_work_file: '{implementation_artifacts}/deferred-work.md'
---

# Step 4: Present and Act

## RULES

- YOU MUST ALWAYS SPEAK OUTPUT in your Agent communication style with the config `{communication_language}`
- When `{spec_file}` is set, always write findings to the story file before offering action choices.
- `decision-needed` findings must be resolved before handling `patch` findings.

## INSTRUCTIONS

### 1. Clean review shortcut

If zero findings remain after triage (all dismissed or none raised): state that and proceed to section 6 (Sprint Status Update).

### 2. Write findings to the story file

If `{spec_file}` exists and contains a Tasks/Subtasks section, append a `### Review Findings` subsection. Write all findings in this order:

1. **`decision-needed`** findings (unchecked):
   `- [ ] [Review][Decision] <Title> — <Detail>`

2. **`patch`** findings (unchecked):
   `- [ ] [Review][Patch] <Title> [<file>:<line>]`

3. **`defer`** findings (checked off, marked deferred):
   `- [x] [Review][Defer] <Title> [<file>:<line>] — deferred, pre-existing`

Also append each `defer` finding to `{deferred_work_file}` under a heading `## Deferred from: code review ({date})`. If `{spec_file}` is set, include its basename in the heading (e.g., `code review of story-3.3 (2026-03-18)`). One bullet per finding with description.

### 3. Present summary

Announce what was written:

> **Code review complete.** <D> `decision-needed`, <P> `patch`, <W> `defer`, <R> dismissed as noise.

If `{spec_file}` is set, add: `Findings written to the review findings section in {spec_file}.`
Otherwise add: `Findings are listed above. No story file was provided, so nothing was persisted.`

### 4. Resolve decision-needed findings

If `decision_needed` findings exist, present each one with its detail and the options available. The user must decide — the correct fix is ambiguous without their input. Walk through each finding (or batch related ones) and get the user's call. Once resolved, each becomes a `patch`, `defer`, or is dismissed.

If the user chooses to defer, ask: Quick one-line reason for deferring this item? (helps future reviews): — then append that reason to both the story file bullet and the `{deferred_work_file}` entry.

**HALT** — I am waiting for your numbered choice. Reply with only the number. Do not proceed until you select an option.

### 5. Handle `patch` findings

If `patch` findings exist (including any resolved from step 4), HALT. Ask the user:

If `{spec_file}` is set, present all three options:

> **How would you like to handle the `<P>` `patch` findings?**
> 1. **Apply every patch** — fix all of them now, no per-finding confirmation. Defer and decision-needed items are not touched.
> 2. **Leave as action items** — they are already in the story file
> 3. **Walk through each patch** — show details for each before deciding

If `{spec_file}` is **not** set, present only options 1 and 2 (omit "Leave as action items" — findings were not written to a file):

> **How would you like to handle the `<P>` `patch` findings?**
> 1. **Apply every patch** — fix all of them now, no per-finding confirmation. Defer and decision-needed items are not touched.
> 2. **Walk through each patch** — show details for each before deciding

**HALT** — I am waiting for your numbered choice. Reply with only the number. Do not proceed until you select an option.

- **Apply every patch**: Apply every patch finding without per-finding confirmation. Do not modify defer or decision-needed items. After all patches are applied, present a summary of changes made. If `{spec_file}` is set, check off the patch items in the story file (leave defer items as-is).
- **Leave as action items** (only when `{spec_file}` is set): Done — findings are already written to the story.
- **Walk through each patch**: Present each finding with full detail, diff context, and suggested fix. After walkthrough, re-offer the applicable options above.

  **HALT** — I am waiting for your numbered choice. Do not proceed until you select an option.

**✅ Code review actions complete**

- Decision-needed resolved: <D>
- Patches handled: <P>
- Deferred: <W>
- Dismissed: <R>

### 5b. Review-closure structural gate (Epic 9 retro action item A7)

**This is the structural enforcement gate. It mirrors the A1/A2 enforcement codified in `bmad-create-story` on 2026-05-10. `Status: review → done` and any sprint-status update are mechanically blocked here unless every review finding satisfies one of three closure criteria. The gate fails closed — any parsing or verification failure halts the workflow, it never lets state through on uncertainty.**

#### Scope

This gate enforces `Status: review → done` transitions specifically. It runs ONLY when the user's section-5 choice signals intent to close the story.

Determine `{intended_status}`:

- User chose option 1 (**Apply every patch**) → `{intended_status} = done` → gate runs.
- User chose option 2 (**Leave as action items**) → `{intended_status} = in-progress` → SKIP this section. Proceed to section 6, which will set `{new_status} = in-progress`. The story stays open; the gate has nothing to enforce.
- User chose option 3 (**Walk through each**) → walk-through is a presentation tier that loops back to options 1 or 2. Apply this Scope decision to whichever terminal choice the walk-through resolves into.

Also skip this section if `{spec_file}` is not set (no story file to gate — there is nothing to enforce against).

Note on the carry-over sub-story pattern: if during section 5 the user wants to convert a `[Review][Patch]` item into a `[Review][Defer]` carry-over sub-story (the `9-Y-*` precedent), that re-classification MUST happen before reaching this section. The current section 5 does not have a built-in per-patch defer path; the user can either (a) leave as action items (option 2) and rerun the workflow after manually editing the story file to re-classify, or (b) apply the patch now and accept that any leftover work becomes a future review cycle. A future enhancement could add a per-patch defer choice to section 5's walk-through; until then the gate's "Deferred and linked into `deferred-work.md`" path is reached only via section 4's defer prompt (which fires on `decision-needed` resolutions) or via manual edits between review runs.

The gate reads `{spec_file}` and `{deferred_work_file}` from disk. It does NOT trust in-memory state from earlier sections — the failure mode this gate prevents is precisely the divergence between in-memory belief and on-disk reality. Story 9.4 (Epic 9) was marked `Status: done` while 25 review-finding checkboxes remained `[ ]` in the story file; Story 9.3 had a Status header / sprint-status disagreement that surfaced the same class of failure. The on-disk file is the audit trail; the gate enforces against that.

#### Closure criteria — every `[Review]` bullet in the story file must satisfy ONE

1. **Resolved** — bullet checkbox is `[x]`. The fix landed in code, or the item was applied during section 5's "Apply every patch" branch.
2. **Dismissed with rationale** — bullet appears in a section whose heading contains the word `Dismissed` (case-insensitive — e.g., "Dismissed as noise", "Dismissed (4)") OR carries an inline `Dismissed` annotation with a one-line rationale. Counts-only summaries (e.g., "Dismissed (Round 2 — 20 — counts only)") are accepted IFF the count is non-zero and the heading exists.
3. **Deferred and linked into `{deferred_work_file}`** — bullet appears in a section whose heading contains the word `Deferred` / `Defer` (case-insensitive) OR carries a `[Review][Defer]` tag, AND the bullet's citation resolves to an entry in `{deferred_work_file}`.

Any `[Review]` bullet that satisfies none of these three is **UNRESOLVED** and blocks closure.

#### Verification procedure

1. Read `{spec_file}` from disk. Locate every `### Review Findings` section, AND every "Round-N Patch Resolution" / "Round-N Re-review findings" subsection, AND every "Dev-note" subsection that lists review items. These are all canonical review-closure surfaces.

2. Enumerate every bullet matching `^- \[(x| )\] \[Review\]` across all these surfaces. For each bullet, capture:
   - **checkbox state** — `x` (checked) or ` ` (unchecked)
   - **sub-type tag** — `[Review][Decision]`, `[Review][Patch]`, `[Review][Defer]`, `[Review][Decision→Patched]`, or other `[Review][...]` variant
   - **containing section heading** — the most recent `###` / `####` / `#####` heading above the bullet
   - **bullet body** — from the title marker to the next sibling bullet at the same indent OR the next section break
   - **citation** — file path with optional line number (e.g., `src/foo.py:123` or `tests/bar.py`), AND any `9-Y-*` / `9-X-*` / `9-Z-*` carry-over story-name strings found in the body

3. Classify each bullet by checkbox + section heading + body:

   | Checkbox | Containing section heading contains... | Body contains `Dismissed` annotation | Body has `[Review][Defer]` tag | Classification |
   |---|---|---|---|---|
   | `x` | (any) | (any) | (any) | **Resolved** |
   | ` ` | `Dismissed` (case-insensitive) | (any) | (any) | **Dismissed** |
   | ` ` | (any) | yes | (any) | **Dismissed** |
   | ` ` | `Deferred` / `Defer` (case-insensitive) | no | (any) | **Deferred** (proceed to step 4) |
   | ` ` | (any) | no | yes | **Deferred** (proceed to step 4) |
   | ` ` | (none of the above) | no | no | **Unresolved** ← BLOCKS |

4. For every **Deferred** bullet, verify its citation resolves to an entry in `{deferred_work_file}`. Read `{deferred_work_file}` and look for any of:
   - exact `file:line` citation substring match, OR
   - `9-Y-*` / `9-X-*` / `9-Z-*` carry-over story-name string match, OR
   - a unique substring of the bullet's title (≥ 12 characters AND appearing exactly once in `{deferred_work_file}`).

   If a Deferred bullet's citation cannot be resolved, classify it as **Deferred-unverified** ← BLOCKS.

5. Compute tallies:
   - `resolved_count` = count of Resolved bullets
   - `dismissed_count` = count of Dismissed bullets
   - `deferred_count` = count of Deferred (verified) bullets
   - `unresolved_count` = count of Unresolved bullets
   - `deferred_unverified_count` = count of Deferred-unverified bullets

6. **Fail-closed conditions** — if ANY of the following are true, HALT and emit the blocking summary below. Do NOT proceed to section 6. Do NOT update the story file Status. Do NOT update `{sprint_status}`.
   - `unresolved_count > 0`
   - `deferred_unverified_count > 0`
   - `{spec_file}` cannot be read, is malformed, or no `### Review Findings` section can be located
   - `{deferred_work_file}` cannot be read AND at least one Deferred bullet exists
   - Bullet parsing fails for any reason (ambiguous section nesting, malformed checkbox markup, truncated body)
   - Any closure-criteria classification raises a tie (e.g., bullet is in BOTH a Dismissed section AND a Deferred section)

7. If none of the fail-closed conditions are true: gate passes. Proceed to section 6.

#### Blocking summary (emit on fail-closed)

> 🚫 **Review-closure gate BLOCKED (Epic 9 retro action item A7).**
>
> Story file: `{spec_file}`
> Deferred-work file: `{deferred_work_file}`
>
> **Counts:**
>   Resolved `[x]`:           `<resolved_count>`
>   Dismissed:                `<dismissed_count>`
>   Deferred (verified):      `<deferred_count>`
>   Unresolved `[ ]`:         `<unresolved_count>`
>   Deferred (unverified):    `<deferred_unverified_count>`
>
> **Unresolved items (block `Status: review → done`):**
> - `<bullet 1 title>` — file: `<citation>`, section: `<heading>`
> - `<bullet 2 title>` — file: `<citation>`, section: `<heading>`
> - ...
>
> **Deferred items missing from `{deferred_work_file}` (block `Status: review → done`):**
> - `<bullet title>` — looked for citation: `<citation>`
> - `<bullet title>` — looked for carry-over name: `<name>`
> - ...
>
> **Parsing failures (block `Status: review → done`):**
> - `<description of failure>`
>
> **To unblock, each blocking item must satisfy ONE:**
> 1. **Resolve in code** and flip the checkbox to `[x]`.
> 2. **Dismiss with rationale** — move the bullet to a section titled `Dismissed` (or similar) OR add an inline `Dismissed: <one-line reason>` annotation.
> 3. **Defer to `{deferred_work_file}`** — move the bullet to a `Deferred` section, ensure the bullet carries a `[Review][Defer]` tag, AND add an entry in `{deferred_work_file}` whose citation matches the bullet (by `file:line`, `9-Y-*` style carry-over name, or unique title substring).
>
> The `Status: done` transition and the `sprint-status.yaml` update are blocked until ALL blocking items above are addressed. Rerun `bmad-code-review` after correction.

#### Guidance — stories that intentionally spawn carry-over sub-stories

A review can legitimately surface findings that should NOT be applied within the source story but instead need a dedicated follow-up story (the `9-Y-*` precedent from Story 9.3's round-2 review, which produced six carry-over sub-stories `9-Y-a` through `9-Y-f`).

To close the source story without violating this gate when carry-overs are intentional:

1. **Name the carry-over** using the convention `<source-story>-Y-<letter>-<slug>` (e.g., `9-Y-a-e2e-fail-abort-coverage`). For prep-style follow-ups that are not review-driven carry-overs, the convention is `<source-story>-X-<slug>` (e.g., `9-X-wire-adapter-map-into-policy-guard-and-control-loop`).
2. **Enter the carry-over in `{deferred_work_file}`** under a heading like `## Carry-over sub-stories from: code review of <source-story> (<date>)`. The entry MUST carry inline labels in the format `[severity — source — classification]` where:
   - **severity** is HIGH / MEDIUM / LOW
   - **source** identifies the originating review finding (e.g., `from R2P3`)
   - **classification** is one of: `must-before-next-epic` / `safe-during-next-epic` / `acceptable-post-next-epic` (or named after the next epic explicitly, e.g., `must-before-epic-10`)
3. **In the source story's review finding bullet**, mark the item as deferred — `[Review][Defer]` tag, body cites the carry-over name (e.g., `Carry to 9-Y-a-e2e-fail-abort-coverage`).
4. Re-run the gate. It will now see the bullet as **Deferred** (criterion 3), verify the carry-over name appears in `{deferred_work_file}`, and pass.

Epic 9 retro action item R3 produced exactly this pattern: each `9-Y-*` entry in `{deferred_work_file}` carries `[severity — source — classification]` so it is self-contained, and the corresponding bullets in 9-3's story file are deferred-and-linked.

#### Historical context

Epic 9 retro (2026-05-11) identified a class of failure where `bmad-code-review` allowed `Status: review → done` transitions while review-finding checkboxes in the story file remained `[ ]` and the items had not been entered in `{deferred_work_file}`. The verifiable evidence:

- Story 9.4 was marked `Status: done` with 25 unchecked `[Review]` bullets (D3, D4, P7–P17, P20–P29). 23 were verified during the Epic 9 retro R1 audit to have been silently applied in code without flipping the checkboxes; 2 (P18, P19) were genuinely not implemented and had to be moved to `{deferred_work_file}` retroactively.
- Story 9.3 had `Status: in-progress` in the story file but `done` in `sprint-status.yaml` because the `bmad-code-review` workflow's automatic Status revert (when Round-2 patches remained as carry-over sub-stories) was not propagated to `sprint-status.yaml`. The carry-overs (`9-Y-a` through `9-Y-f`) were named in `{deferred_work_file}` but not yet classified.

This gate is the codified prevention. It matches the A1/A2 enforcement pattern that was codified for `bmad-create-story` on 2026-05-10: same enforcement class (structural, fail-closed), same auditability (on-disk file is the audit trail), same precedent of moving discipline from human procedure to workflow machinery.

The first proof-of-enforcement story this gate runs against is Story `9-Y-a` (FAIL-abort E2E coverage), per Jordan's Epic 9 retro instructions.

### 6. Update story status and sync sprint tracking

Skip this section if `{spec_file}` is not set.

**Hard precondition:** Section 5b determined `{intended_status}` and either ran the gate (if `done`) or skipped (if `in-progress`). One of three states applies here:

- 5b ran AND passed → `{intended_status} = done`, gate verified all closure criteria. Proceed.
- 5b was skipped (user chose option 2, or walk-through left action items) → `{intended_status} = in-progress`. Proceed.
- 5b ran AND blocked → the workflow halted at 5b; this section is not entered. If you find yourself in section 6 after a blocking summary was emitted, this is a workflow violation — abort and report it to the user.

If you find yourself in section 6 without having executed 5b at all (e.g., direct re-entry, resumed session, partial-state recovery): return to 5b and execute it from scratch before any of the work below. The gate is the audit-trail authority; section 6 trusts it but does not bypass it.

#### Set `{new_status}`

`{new_status} = {intended_status}` (as determined by section 5b's scope subsection).

When `{new_status} = done`, section 5b's verification has already proven:
- `unresolved_count == 0`
- `deferred_unverified_count == 0`
- Every `[Review]` bullet is Resolved, Dismissed, or Deferred-and-verified.

When `{new_status} = in-progress`, no closure verification was required. The story stays open with action items.

#### Update the story file

Update the story file `Status:` section to `{new_status}`. Append a one-line dated note immediately below the Status header in the format:

```
_Status set to {new_status} by bmad-code-review on {date} after review-closure gate {gate_outcome}: {resolved_count} resolved, {dismissed_count} dismissed, {deferred_count} deferred-and-verified._
```

Where `{gate_outcome}` is `"passed (5b)"` when `{new_status} = done`, or `"skipped (in-progress; action items remain)"` when `{new_status} = in-progress` (in the in-progress case, omit the resolved/dismissed/deferred counts, which were not computed).

Save the story file.

#### Sync sprint-status.yaml

The `{spec_file}` `Status:` header and the `sprint-status.yaml` `development_status[{story_key}]` entry MUST agree at the end of this step. The Epic 9 retro identified the failure mode where they diverged (Story 9.3); the gate-and-sync flow below prevents recurrence.

If `{story_key}` is not set, skip this subsection and warn the user that sprint status was not synced because no story key was available. The story file Status was updated; manual sprint-status reconciliation may be needed.

If `{sprint_status}` file exists:

1. Load the FULL `{sprint_status}` file.
2. Find the `development_status` entry matching `{story_key}`.
3. If found:
   - Update `development_status[{story_key}]` to `{new_status}`.
   - Update `last_updated` to current date.
   - Save the file, preserving ALL comments and structure including STATUS DEFINITIONS.
4. If `{story_key}` not found in sprint status: warn the user that the story file was updated but sprint-status sync failed. The consistency triangle (file Status ↔ sprint-status ↔ review-checkboxes) is broken until this is reconciled.

If `{sprint_status}` file does not exist, note that story status was updated in the story file only.

#### Consistency check (post-sync)

After both writes complete, re-read `{spec_file}`'s `Status:` header and `{sprint_status}`'s `development_status[{story_key}]` from disk. They MUST be identical. If they are not, emit a warning and instruct the user to investigate — this would indicate a write failure or a concurrent modification.

#### Completion summary

> **Review Complete!**
>
> **Story Status:** `{new_status}`
> **Issues Fixed:** <fixed_count>
> **Action Items Created:** <action_count>
> **Deferred:** <W>
> **Dismissed:** <R>

### 7. Next steps

Present the user with follow-up options:

> **What would you like to do next?**
> 1. **Start the next story** — run `dev-story` to pick up the next `ready-for-dev` story
> 2. **Re-run code review** — address findings and review again
> 3. **Done** — end the workflow

**HALT** — I am waiting for your choice. Do not proceed until the user selects an option.

## On Complete

Run: `python3 {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root} --key workflow.on_complete`

If the resolved `workflow.on_complete` is non-empty, follow it as the final terminal instruction before exiting.
