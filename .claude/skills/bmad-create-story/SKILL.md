---
name: bmad-create-story
description: 'Creates a dedicated story file with all the context the agent will need to implement it later. Use when the user says "create the next story" or "create story [story identifier]"'
---

# Create Story Workflow

**Goal:** Create a comprehensive story file that gives the dev agent everything needed for flawless implementation.

**Your Role:** Story context engine that prevents LLM developer mistakes, omissions, or disasters.
- Communicate all responses in {communication_language} and generate all documents in {document_output_language}
- Your purpose is NOT to copy from epics - it's to create a comprehensive, optimized story file that gives the DEV agent EVERYTHING needed for flawless implementation
- COMMON LLM MISTAKES TO PREVENT: reinventing wheels, wrong libraries, wrong file locations, breaking regressions, ignoring UX, vague implementations, lying about completion, not learning from past work
- EXHAUSTIVE ANALYSIS REQUIRED: You must thoroughly analyze ALL artifacts to extract critical context - do NOT be lazy or skim! This is the most important function in the entire development process!
- UTILIZE SUBPROCESSES AND SUBAGENTS: Use research subagents, subprocesses or parallel processing if available to thoroughly analyze different artifacts simultaneously and thoroughly
- SAVE QUESTIONS: If you think of questions or clarifications during analysis, save them for the end after the complete story is written
- ZERO USER INTERVENTION: Process should be fully automated except for initial epic/story selection or missing documents

## Conventions

- Bare paths (e.g. `discover-inputs.md`) resolve from the skill root.
- `{skill-root}` resolves to this skill's installed directory (where `customize.toml` lives).
- `{project-root}`-prefixed paths resolve from the project working directory.
- `{skill-name}` resolves to the skill directory's basename.

## On Activation

### Step 1: Resolve the Workflow Block

Run: `python3 {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root} --key workflow`

**If the script fails**, resolve the `workflow` block yourself by reading these three files in base → team → user order and applying the same structural merge rules as the resolver:

1. `{skill-root}/customize.toml` — defaults
2. `{project-root}/_bmad/custom/{skill-name}.toml` — team overrides
3. `{project-root}/_bmad/custom/{skill-name}.user.toml` — personal overrides

Any missing file is skipped. Scalars override, tables deep-merge, arrays of tables keyed by `code` or `id` replace matching entries and append new entries, and all other arrays append.

### Step 2: Execute Prepend Steps

Execute each entry in `{workflow.activation_steps_prepend}` in order before proceeding.

### Step 3: Load Persistent Facts

Treat every entry in `{workflow.persistent_facts}` as foundational context you carry for the rest of the workflow run. Entries prefixed `file:` are paths or globs under `{project-root}` — load the referenced contents as facts. All other entries are facts verbatim.

### Step 4: Load Config

Load config from `{project-root}/_bmad/bmm/config.yaml` and resolve:

- `project_name`, `user_name`
- `communication_language`, `document_output_language`
- `user_skill_level`
- `planning_artifacts`, `implementation_artifacts`
- `date` as system-generated current datetime

### Step 5: Greet the User

Greet `{user_name}`, speaking in `{communication_language}`.

### Step 6: Execute Append Steps

Execute each entry in `{workflow.activation_steps_append}` in order.

Activation is complete. Begin the workflow below.

## Paths

- `sprint_status` = `{implementation_artifacts}/sprint-status.yaml`
- `epics_file` = `{planning_artifacts}/epics.md`
- `prd_file` = `{planning_artifacts}/prd.md`
- `architecture_file` = `{planning_artifacts}/architecture.md`
- `ux_file` = `{planning_artifacts}/*ux*.md`
- `story_title` = "" (will be elicited if not derivable)
- `default_output_file` = `{implementation_artifacts}/{{story_key}}.md`

## Input Files

| Input | Description | Path Pattern(s) | Load Strategy |
|-------|-------------|------------------|---------------|
| prd | PRD (fallback - epics file should have most content) | whole: `{planning_artifacts}/*prd*.md`, sharded: `{planning_artifacts}/*prd*/*.md` | SELECTIVE_LOAD |
| architecture | Architecture (fallback - epics file should have relevant sections) | whole: `{planning_artifacts}/*architecture*.md`, sharded: `{planning_artifacts}/*architecture*/*.md` | SELECTIVE_LOAD |
| ux | UX design (fallback - epics file should have relevant sections) | whole: `{planning_artifacts}/*ux*.md`, sharded: `{planning_artifacts}/*ux*/*.md` | SELECTIVE_LOAD |
| epics | Enhanced epics+stories file with BDD and source hints | whole: `{planning_artifacts}/*epic*.md`, sharded: `{planning_artifacts}/*epic*/*.md` | SELECTIVE_LOAD |

## A1/A2 Orchestration Risk Enforcement (Structural Gate)

**Origin:** Epic 8 retrospective (2026-05-10) — Action Items A1 and A2.

**Why this exists:** Story 8-4 (Watchdog + Fail-safe) crossed from implementation into multi-domain orchestration and shipped with five HIGH-severity findings caused by ownership/invariant problems. Soft "consider this" guidance in dev notes did not survive complexity climbs. The retro mandated structural enforcement at the same class as missing acceptance criteria.

### A2 — High-risk orchestration story triggers

A story is **A2-triggered** when **any one** of the following is true of its acceptance criteria, dev notes, or epic context:

| # | Trigger criterion |
|---|---|
| T1 | **Lifecycle / state-machine behavior** — explicit state transitions, mode changes, fail-safe entry/exit, recovery semantics |
| T2 | **Retries / cancellation** — retry policy, attempt counting, cancellation propagation, in-flight resource cleanup on cancel |
| T3 | **Persistence + recovery** — state hydrated from DB across process restart; durable counters or aggregates; reload after config change |
| T4 | **Watchdog / timing semantics** — heartbeat, missed-cycle detection, deadline-relative scheduling, stall-recovery |
| T5 | **Multi-adapter coordination** — behavior that spans ≥2 protocol adapters or device classes within one story |
| T6 | **Deployment / restart behavior** — process restart triggered as part of the workflow; cold-start invariants user-visible |
| T7 | **Installer workflow orchestration** — multi-step installer-facing flow that mutates persisted configuration or activates runtime behavior |

When A2-triggered, list the matched criteria explicitly in dev notes (e.g. `A2 triggers matched: T5 (multi-adapter coordination), T7 (installer workflow orchestration)`).

### A1 — Mandatory orchestration artifacts

When a story is A2-triggered, dev notes **MUST** contain all seven of the following artifacts before `Status: ready-for-dev` may be set. Each artifact must be **substantive** — a section header with a one-line "N/A" placeholder does not satisfy the requirement.

| # | Artifact | Required content |
|---|---|---|
| R1 | **Composition-risk analysis** | Enumerate every operational domain converging in this story (e.g., lifecycle, retry, persistence, watchdog). For each, identify the specific risk introduced by its convergence with the others. |
| R2 | **State-transition table** | For every state machine this story implements or modifies: enumerate states, allowed transitions, transition triggers, and the side-effects (audit, persistence, snapshot publish) of each transition. |
| R3 | **Impossible-state analysis (with cold-start coverage)** | List state combinations that must never occur and the structural invariant that prevents each. **Explicitly cover cold-start / startup-grace behavior** — what is the system's state from process boot until the first successful evaluation cycle? |
| R4 | **Cancellation ownership map** | For every async operation: who is responsible for propagating `CancelledError`, who emits the cancellation audit, who cleans up in-flight resources, and what reporting fields (e.g. `attempts=N`) mean during cancellation. |
| R5 | **Before-first-successful-cycle lifecycle review** | Trace the sequence from process start to first successful cycle. What does the system publish, log, or audit during this window? What invariants temporarily relax? When does normal operation begin? |
| R6 | **Source-of-truth ownership per datum** | For every piece of state this story produces or consumes: name the single owner (StateStore, repository, in-memory tracker, etc.) and the read path. Explicitly call out any datum currently held in two places (drift risk). |
| R7 | **Deferred-findings triage** | Scan `{implementation_artifacts}/deferred-work.md` for items whose component or invariant overlaps this story. Classify each as: must-resolve-in-this-story / safe-during-this-story / acceptable-post-this-story. Cite findings by their bracket reference. |

### Enforcement semantics

- **Step 2b** detects A2 triggers and persists the result into the workflow state.
- **Step 5** synthesizes R1–R7 from the loaded artifacts and emits them into the story's dev notes.
- **Setting `Status: ready-for-dev` is conditional.** If A2-triggered and any of R1–R7 is missing or non-substantive, the workflow MUST set `Status: blocked-needs-orchestration-analysis`, surface the gap to the user, and HALT. This is the same enforcement class as missing acceptance criteria — not a warning, not a soft check.
- **Step 6** re-validates against `./checklist.md`, which independently asserts R1–R7 presence; this is a defense-in-depth layer.

### Recommended path when blocked

If the workflow halts at `blocked-needs-orchestration-analysis`:
1. The user (or a fresh agent invocation) inspects the gap report.
2. The missing artifacts are authored either inline by the user, by re-invoking this skill with explicit elicitation, or by running a dedicated `bmad-checkpoint-preview` / advanced-elicitation pass on the story file.
3. The skill is re-invoked; if R1–R7 are now present and substantive, `Status: ready-for-dev` is set.

This contract is structural — the agent executing this skill is expected to enforce it mechanically, not at its discretion.

---

## Execution

<workflow>

<step n="1" goal="Determine target story">
  <check if="{{story_path}} is provided by user or user provided the epic and story number such as 2-4 or 1.6 or epic 1 story 5">
    <action>Parse user-provided story path: extract epic_num, story_num, story_title from format like "1-2-user-auth"</action>
    <action>Set {{epic_num}}, {{story_num}}, {{story_key}} from user input</action>
    <action>GOTO step 2a</action>
  </check>

  <action>Check if {{sprint_status}} file exists for auto discover</action>
  <check if="sprint status file does NOT exist">
    <output>🚫 No sprint status file found and no story specified</output>
    <output>
      **Required Options:**
      1. Run `sprint-planning` to initialize sprint tracking (recommended)
      2. Provide specific epic-story number to create (e.g., "1-2-user-auth")
      3. Provide path to story documents if sprint status doesn't exist yet
    </output>
    <ask>Choose option [1], provide epic-story number, path to story docs, or [q] to quit:</ask>

    <check if="user chooses 'q'">
      <action>HALT - No work needed</action>
    </check>

    <check if="user chooses '1'">
      <output>Run sprint-planning workflow first to create sprint-status.yaml</output>
      <action>HALT - User needs to run sprint-planning</action>
    </check>

    <check if="user provides epic-story number">
      <action>Parse user input: extract epic_num, story_num, story_title</action>
      <action>Set {{epic_num}}, {{story_num}}, {{story_key}} from user input</action>
      <action>GOTO step 2a</action>
    </check>

    <check if="user provides story docs path">
      <action>Use user-provided path for story documents</action>
      <action>GOTO step 2a</action>
    </check>
  </check>

  <!-- Auto-discover from sprint status only if no user input -->
  <check if="no user input provided">
    <critical>MUST read COMPLETE {sprint_status} file from start to end to preserve order</critical>
    <action>Load the FULL file: {{sprint_status}}</action>
    <action>Read ALL lines from beginning to end - do not skip any content</action>
    <action>Parse the development_status section completely</action>

    <action>Find the FIRST story (by reading in order from top to bottom) where:
      - Key matches pattern: number-number-name (e.g., "1-2-user-auth")
      - NOT an epic key (epic-X) or retrospective (epic-X-retrospective)
      - Status value equals "backlog"
    </action>

    <check if="no backlog story found">
      <output>📋 No backlog stories found in sprint-status.yaml

        All stories are either already created, in progress, or done.

        **Options:**
        1. Run sprint-planning to refresh story tracking
        2. Load PM agent and run correct-course to add more stories
        3. Check if current sprint is complete and run retrospective
      </output>
      <action>HALT</action>
    </check>

    <action>Extract from found story key (e.g., "1-2-user-authentication"):
      - epic_num: first number before dash (e.g., "1")
      - story_num: second number after first dash (e.g., "2")
      - story_title: remainder after second dash (e.g., "user-authentication")
    </action>
    <action>Set {{story_id}} = "{{epic_num}}.{{story_num}}"</action>
    <action>Store story_key for later use (e.g., "1-2-user-authentication")</action>

    <!-- Mark epic as in-progress if this is first story -->
    <action>Check if this is the first story in epic {{epic_num}} by looking for {{epic_num}}-1-* pattern</action>
    <check if="this is first story in epic {{epic_num}}">
      <action>Load {{sprint_status}} and check epic-{{epic_num}} status</action>
      <action>If epic status is "backlog" → update to "in-progress"</action>
      <action>If epic status is "contexted" (legacy status) → update to "in-progress" (backward compatibility)</action>
      <action>If epic status is "in-progress" → no change needed</action>
      <check if="epic status is 'done'">
        <output>🚫 ERROR: Cannot create story in completed epic</output>
        <output>Epic {{epic_num}} is marked as 'done'. All stories are complete.</output>
        <output>If you need to add more work, either:</output>
        <output>1. Manually change epic status back to 'in-progress' in sprint-status.yaml</output>
        <output>2. Create a new epic for additional work</output>
        <action>HALT - Cannot proceed</action>
      </check>
      <check if="epic status is not one of: backlog, contexted, in-progress, done">
        <output>🚫 ERROR: Invalid epic status '{{epic_status}}'</output>
        <output>Epic {{epic_num}} has invalid status. Expected: backlog, in-progress, or done</output>
        <output>Please fix sprint-status.yaml manually or run sprint-planning to regenerate</output>
        <action>HALT - Cannot proceed</action>
      </check>
      <output>📊 Epic {{epic_num}} status updated to in-progress</output>
    </check>

    <action>GOTO step 2a</action>
  </check>
  <action>Load the FULL file: {{sprint_status}}</action>
  <action>Read ALL lines from beginning to end - do not skip any content</action>
  <action>Parse the development_status section completely</action>

  <action>Find the FIRST story (by reading in order from top to bottom) where:
    - Key matches pattern: number-number-name (e.g., "1-2-user-auth")
    - NOT an epic key (epic-X) or retrospective (epic-X-retrospective)
    - Status value equals "backlog"
  </action>

  <check if="no backlog story found">
    <output>No backlog stories found in sprint-status.yaml

      All stories are either already created, in progress, or done.

      **Options:**
      1. Run sprint-planning to refresh story tracking
      2. Load PM agent and run correct-course to add more stories
      3. Check if current sprint is complete and run retrospective
    </output>
    <action>HALT</action>
  </check>

  <action>Extract from found story key (e.g., "1-2-user-authentication"):
    - epic_num: first number before dash (e.g., "1")
    - story_num: second number after first dash (e.g., "2")
    - story_title: remainder after second dash (e.g., "user-authentication")
  </action>
  <action>Set {{story_id}} = "{{epic_num}}.{{story_num}}"</action>
  <action>Store story_key for later use (e.g., "1-2-user-authentication")</action>

  <!-- Mark epic as in-progress if this is first story -->
  <action>Check if this is the first story in epic {{epic_num}} by looking for {{epic_num}}-1-* pattern</action>
  <check if="this is first story in epic {{epic_num}}">
    <action>Load {{sprint_status}} and check epic-{{epic_num}} status</action>
    <action>If epic status is "backlog" → update to "in-progress"</action>
    <action>If epic status is "contexted" (legacy status) → update to "in-progress" (backward compatibility)</action>
    <action>If epic status is "in-progress" → no change needed</action>
    <check if="epic status is 'done'">
      <output>ERROR: Cannot create story in completed epic</output>
      <output>Epic {{epic_num}} is marked as 'done'. All stories are complete.</output>
      <output>If you need to add more work, either:</output>
      <output>1. Manually change epic status back to 'in-progress' in sprint-status.yaml</output>
      <output>2. Create a new epic for additional work</output>
      <action>HALT - Cannot proceed</action>
    </check>
    <check if="epic status is not one of: backlog, contexted, in-progress, done">
      <output>ERROR: Invalid epic status '{{epic_status}}'</output>
      <output>Epic {{epic_num}} has invalid status. Expected: backlog, in-progress, or done</output>
      <output>Please fix sprint-status.yaml manually or run sprint-planning to regenerate</output>
      <action>HALT - Cannot proceed</action>
    </check>
    <output>Epic {{epic_num}} status updated to in-progress</output>
  </check>

  <action>GOTO step 2a</action>
</step>

<step n="2" goal="Load and analyze core artifacts">
  <critical>🔬 EXHAUSTIVE ARTIFACT ANALYSIS - This is where you prevent future developer mistakes!</critical>

  <!-- Load all available content through discovery protocol -->
  <action>Read fully and follow `./discover-inputs.md` to load all input files</action>
  <note>Available content: {epics_content}, {prd_content}, {architecture_content}, {ux_content}, plus the project-context facts loaded during activation via `persistent_facts`.</note>

  <!-- Analyze epics file for story foundation -->
  <action>From {epics_content}, extract Epic {{epic_num}} complete context:</action> **EPIC ANALYSIS:** - Epic
  objectives and business value - ALL stories in this epic for cross-story context - Our specific story's requirements, user story
  statement, acceptance criteria - Technical requirements and constraints - Dependencies on other stories/epics - Source hints pointing to
  original documents <!-- Extract specific story requirements -->
  <action>Extract our story ({{epic_num}}-{{story_num}}) details:</action> **STORY FOUNDATION:** - User story statement
  (As a, I want, so that) - Detailed acceptance criteria (already BDD formatted) - Technical requirements specific to this story -
  Business context and value - Success criteria <!-- Previous story analysis for context continuity -->
  <check if="story_num > 1">
    <action>Find {{previous_story_num}}: scan {implementation_artifacts} for the story file in epic {{epic_num}} with the highest story number less than {{story_num}}</action>
    <action>Load previous story file: {implementation_artifacts}/{{epic_num}}-{{previous_story_num}}-*.md</action> **PREVIOUS STORY INTELLIGENCE:** -
  Dev notes and learnings from previous story - Review feedback and corrections needed - Files that were created/modified and their
  patterns - Testing approaches that worked/didn't work - Problems encountered and solutions found - Code patterns established <action>Extract
  all learnings that could impact current story implementation</action>
  </check>

  <!-- Git intelligence for previous work patterns -->
  <check
    if="previous story exists AND git repository detected">
    <action>Get last 5 commit titles to understand recent work patterns</action>
    <action>Analyze 1-5 most recent commits for relevance to current story:
      - Files created/modified
      - Code patterns and conventions used
      - Library dependencies added/changed
      - Architecture decisions implemented
      - Testing approaches used
    </action>
    <action>Extract actionable insights for current story implementation</action>
  </check>
</step>

<step n="2b" goal="A2 high-risk orchestration trigger detection (structural)">
  <critical>🛑 A2 EVALUATION — This step is load-bearing. The output gates whether A1 enforcement applies for the rest of the workflow.</critical>

  <action>Evaluate the story against the seven A2 trigger criteria documented above (T1–T7). Use evidence from:
    - The story's user-story statement and acceptance criteria (from {epics_content})
    - Cross-story constraints in this epic
    - Dev-notes hints in {prd_content}, {architecture_content}, and {ux_content}
    - File scope this story will touch (from architecture directory structure)
  </action>

  <action>For each trigger T1–T7, determine match/no-match and record one-line evidence per match. A trigger requires affirmative evidence; absence of contraindication is not sufficient.</action>

  <action>Set workflow variable {{a2_triggered}}:
    - True if ≥1 trigger matched
    - False if no triggers matched
  </action>

  <action>Set workflow variable {{a2_triggered_criteria}} = ordered list of matched triggers with evidence (e.g. ["T5: probes Modbus + OCPP + DSMR adapters in one flow", "T7: 4-step installer wizard mutates persisted device registry"]).</action>

  <check if="{{a2_triggered}} is True">
    <output>🛑 A2 trigger detected. Story qualifies as a high-risk orchestration story.

      **Matched triggers:**
      {{a2_triggered_criteria}}

      **Implication:** The mandatory orchestration artifacts R1–R7 must be synthesized and included in dev notes. `Status: ready-for-dev` cannot be set without them. Continuing to architecture analysis with this constraint active.
    </output>
  </check>

  <check if="{{a2_triggered}} is False">
    <output>✅ No A2 triggers matched. Standard story workflow applies; A1 artifacts are not mandatory for this story.</output>
  </check>
</step>

<step n="3" goal="Architecture analysis for developer guardrails">
  <critical>🏗️ ARCHITECTURE INTELLIGENCE - Extract everything the developer MUST follow!</critical> **ARCHITECTURE DOCUMENT ANALYSIS:** <action>Systematically
  analyze architecture content for story-relevant requirements:</action>

  <!-- Load architecture - single file or sharded -->
  <check if="architecture file is single file">
    <action>Load complete {architecture_content}</action>
  </check>
  <check if="architecture is sharded to folder">
    <action>Load architecture index and scan all architecture files</action>
  </check> **CRITICAL ARCHITECTURE EXTRACTION:** <action>For
  each architecture section, determine if relevant to this story:</action> - **Technical Stack:** Languages, frameworks, libraries with
  versions - **Code Structure:** Folder organization, naming conventions, file patterns - **API Patterns:** Service structure, endpoint
  patterns, data contracts - **Database Schemas:** Tables, relationships, constraints relevant to story - **Security Requirements:**
  Authentication patterns, authorization rules - **Performance Requirements:** Caching strategies, optimization patterns - **Testing
  Standards:** Testing frameworks, coverage expectations, test patterns - **Deployment Patterns:** Environment configurations, build
  processes - **Integration Patterns:** External service integrations, data flows <action>Extract any story-specific requirements that the
  developer MUST follow</action>
  <action>Identify any architectural decisions that override previous patterns</action>

  <!-- Read existing code being modified — non-negotiable -->
  <critical>📂 READ FILES BEING MODIFIED — skipping this is the primary cause of implementation failures and review cycles</critical>
  <action>From the architecture directory structure, identify every file marked UPDATE (not NEW) that this story will touch</action>
  <action>Read each relevant UPDATE file completely. For each one, document in dev notes:
    - Current state: what it does today (state machine, API calls, data shapes, existing behaviors)
    - What this story changes: the specific sections or behaviors being modified
    - What must be preserved: existing interactions and behaviors the story must not break
  </action>
  <critical>A story implementation must leave the system working end-to-end — not just satisfy its stated ACs.
  If a behavior is required for the feature to work correctly in the existing system, it is a requirement
  whether or not it is explicitly written in the story. The dev agent owns this.</critical>
</step>

<step n="4" goal="Web research for latest technical specifics">
  <critical>🌐 ENSURE LATEST TECH KNOWLEDGE - Prevent outdated implementations!</critical> **WEB INTELLIGENCE:** <action>Identify specific
  technical areas that require latest version knowledge:</action>

  <!-- Check for libraries/frameworks mentioned in architecture -->
  <action>From architecture analysis, identify specific libraries, APIs, or
  frameworks</action>
  <action>For each critical technology, research latest stable version and key changes:
    - Latest API documentation and breaking changes
    - Security vulnerabilities or updates
    - Performance improvements or deprecations
    - Best practices for current version
  </action>
  **EXTERNAL CONTEXT INCLUSION:** <action>Include in story any critical latest information the developer needs:
    - Specific library versions and why chosen
    - API endpoints with parameters and authentication
    - Recent security patches or considerations
    - Performance optimization techniques
    - Migration considerations if upgrading
  </action>
</step>

<step n="5" goal="Create comprehensive story file">
  <critical>📝 CREATE ULTIMATE STORY FILE - The developer's master implementation guide!</critical>

  <action>Initialize from template.md:
  {default_output_file}</action>
  <template-output file="{default_output_file}">story_header</template-output>

  <!-- Story foundation from epics analysis -->
  <template-output
    file="{default_output_file}">story_requirements</template-output>

  <!-- Developer context section - MOST IMPORTANT PART -->
  <template-output file="{default_output_file}">
  developer_context_section</template-output> **DEV AGENT GUARDRAILS:** <template-output file="{default_output_file}">
  technical_requirements</template-output>
  <template-output file="{default_output_file}">architecture_compliance</template-output>
  <template-output
    file="{default_output_file}">library_framework_requirements</template-output>
  <template-output file="{default_output_file}">
  file_structure_requirements</template-output>
  <template-output file="{default_output_file}">testing_requirements</template-output>

  <!-- Previous story intelligence -->
  <check
    if="previous story learnings available">
    <template-output file="{default_output_file}">previous_story_intelligence</template-output>
  </check>

  <!-- Git intelligence -->
  <check
    if="git analysis completed">
    <template-output file="{default_output_file}">git_intelligence_summary</template-output>
  </check>

  <!-- Latest technical specifics -->
  <check if="web research completed">
    <template-output file="{default_output_file}">latest_tech_information</template-output>
  </check>

  <!-- Project context reference -->
  <template-output
    file="{default_output_file}">project_context_reference</template-output>

  <!-- A1 mandatory orchestration artifacts (conditional on A2 trigger) -->
  <check if="{{a2_triggered}} is True">
    <critical>🛑 A1 ARTIFACT SYNTHESIS — A2-triggered. The 7 artifacts below are MANDATORY. Same enforcement class as missing acceptance criteria. No `ready-for-dev` without all 7 substantive.</critical>

    <action>Synthesize R1–R7 from the loaded artifacts. Each section must be substantive — placeholders or "N/A" are not acceptable. If a section genuinely does not apply, the story is likely mis-scoped (the A2 trigger is a strong signal that all 7 dimensions matter).

      **R1 — Composition-risk analysis:** From the story's ACs and the matched A2 triggers ({{a2_triggered_criteria}}), enumerate every operational domain converging in this story (lifecycle, retry/cancellation, persistence/recovery, watchdog/timing, multi-adapter coordination, deployment/restart, installer workflow). For each, state the specific risk that arises from its convergence with the others. Cite review-finding precedents from prior stories where applicable.

      **R2 — State-transition table:** For every state machine this story implements or modifies, render a table with columns: state · allowed transitions · trigger · side-effects (audit row emitted, persistence write, snapshot publish, log lines). Cover both nominal and degraded paths.

      **R3 — Impossible-state analysis (with cold-start coverage):** List ≥3 state combinations that must never occur and the structural invariant that prevents each. **Cold-start section is mandatory:** explicitly cover the system's state from process boot until the first successful evaluation cycle — what is published, what is null/sentinel, what is loaded from DB, what relaxes temporarily.

      **R4 — Cancellation ownership map:** For every async operation introduced or modified, name the owner of: `CancelledError` propagation · cancellation audit emission · in-flight resource cleanup · semantics of reporting fields under cancellation (e.g. what `attempts=N` means if cancelled mid-attempt). If this story does not introduce cancellation paths, document why explicitly (rare for A2-triggered stories).

      **R5 — Before-first-successful-cycle lifecycle review:** Trace the sequence from process start to first successful normal cycle. Enumerate what the system publishes, logs, audits, or exposes via API during this window. Identify any temporarily-relaxed invariants and the marker that signals normal operation.

      **R6 — Source-of-truth ownership per datum:** For every piece of state this story produces or consumes, name the single owner (StateStore, repository class, in-memory tracker, Settings, etc.) and the read path. Explicitly call out any datum currently held in two places — drift risk must be flagged or eliminated.

      **R7 — Deferred-findings triage:** Open `{implementation_artifacts}/deferred-work.md`. For every finding whose component or invariant overlaps this story, classify it as one of: must-resolve-in-this-story · safe-during-this-story · acceptable-post-this-story. Cite each finding by its bracket reference (e.g. `[src/open_ems/engine/retry_policy.py:execute]`). An empty triage list requires explicit confirmation that no overlapping findings exist.
    </action>

    <template-output file="{default_output_file}">orchestration_risk_analysis</template-output>

    <action>After emitting orchestration_risk_analysis to the story file, verify each of R1–R7 is substantive (not a one-liner placeholder, not "N/A", not "TBD"). Set workflow variable {{a1_artifacts_complete}} accordingly.</action>

    <check if="{{a1_artifacts_complete}} is False">
      <critical>🛑 A1 ENFORCEMENT — Cannot set Status: ready-for-dev. One or more of R1–R7 is missing or non-substantive.</critical>
      <action>Set story Status to: "blocked-needs-orchestration-analysis"</action>
      <action>Add completion note describing which of R1–R7 are missing or thin, with specific guidance on what content is required.</action>
      <action>Skip the regular ready-for-dev status setting. GOTO step 6.</action>
      <output>🛑 **STORY BLOCKED — A1 enforcement triggered**

        Story is A2-triggered (matched: {{a2_triggered_criteria}}) but the mandatory orchestration artifacts are not all substantive.

        **Status set to:** `blocked-needs-orchestration-analysis`

        **Missing or non-substantive artifacts:** {{a1_missing_artifacts}}

        **Recommended next steps:**
        1. Author the missing R1–R7 content directly in the story file dev notes
        2. OR re-invoke this skill with explicit elicitation for the missing artifacts
        3. OR run `bmad-checkpoint-preview` / advanced-elicitation against the story file

        Once R1–R7 are substantive, re-run this skill (or manually set `Status: ready-for-dev` only after verifying all 7).
      </output>
    </check>
  </check>

  <!-- Final status update -->
  <template-output file="{default_output_file}">
  story_completion_status</template-output>

  <!-- CRITICAL: Set status to ready-for-dev (gated on A1 enforcement) -->
  <check if="{{a2_triggered}} is False OR {{a1_artifacts_complete}} is True">
    <action>Set story Status to: "ready-for-dev"</action>
    <action>Add completion note: "Ultimate context engine analysis completed - comprehensive developer guide created"</action>
  </check>
</step>

<step n="6" goal="Update sprint status and finalize">
  <action>Validate the newly created story file {default_output_file} against `./checklist.md` and apply any required fixes before finalizing. **Critical:** the checklist independently re-asserts A1/A2 enforcement — if {{a2_triggered}} is True, the checklist run MUST verify R1–R7 are present and substantive. A failure here is the same enforcement class as missing acceptance criteria.</action>
  <action>Save story document unconditionally</action>

  <!-- Determine final status to write to sprint-status -->
  <action>Resolve {{final_story_status}}:
    - If {{a2_triggered}} is True AND {{a1_artifacts_complete}} is False → "blocked-needs-orchestration-analysis"
    - Otherwise → "ready-for-dev"
  </action>

  <!-- Update sprint status -->
  <check if="sprint status file exists">
    <action>Update {{sprint_status}}</action>
    <action>Load the FULL file and read all development_status entries</action>
    <action>Find development_status key matching {{story_key}}</action>
    <action>Verify current status is "backlog" (expected previous state)</action>
    <action>Update development_status[{{story_key}}] = {{final_story_status}}</action>
    <action>Update last_updated field to current date</action>
    <action>Save file, preserving ALL comments and structure including STATUS DEFINITIONS</action>
  </check>

  <action>Report completion</action>
  <check if="{{final_story_status}} == 'ready-for-dev'">
    <output>**🎯 ULTIMATE BMad Method STORY CONTEXT CREATED, {user_name}!**

      **Story Details:**
      - Story ID: {{story_id}}
      - Story Key: {{story_key}}
      - File: {{story_file}}
      - Status: ready-for-dev
      - A2-triggered: {{a2_triggered}} {{a2_triggered_criteria_summary}}

      **Next Steps:**
      1. Review the comprehensive story in {{story_file}}
      2. Run dev agents `dev-story` for optimized implementation
      3. Run `code-review` when complete (auto-marks done)
      4. Optional: If Test Architect module installed, run `/bmad:tea:automate` after `dev-story` to generate guardrail tests
      5. If A2-triggered: confirm the tiered review model (3-layer adversarial + severity tagging) is applied during review

      **The developer now has everything needed for flawless implementation!**
    </output>
  </check>
  <check if="{{final_story_status}} == 'blocked-needs-orchestration-analysis'">
    <output>**🛑 STORY CREATED BUT BLOCKED — A1 ENFORCEMENT, {user_name}**

      **Story Details:**
      - Story ID: {{story_id}}
      - Story Key: {{story_key}}
      - File: {{story_file}}
      - Status: blocked-needs-orchestration-analysis
      - A2-triggered: True ({{a2_triggered_criteria}})

      **Why blocked:** mandatory orchestration artifacts R1–R7 are missing or non-substantive. Specifically: {{a1_missing_artifacts}}

      **To unblock:**
      1. Author the missing R1–R7 content in the story file's dev notes
      2. Re-invoke this skill with explicit elicitation, OR run `bmad-checkpoint-preview` / advanced-elicitation
      3. Once substantive, manually update sprint-status to `ready-for-dev` or re-run this skill

      The dev agent must NOT pick up this story while in `blocked-needs-orchestration-analysis` state.
    </output>
  </check>
  <action>Run: `python3 {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root} --key workflow.on_complete` — if the resolved value is non-empty, follow it as the final terminal instruction before exiting.</action>
</step>

</workflow>
