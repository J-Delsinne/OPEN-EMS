# Story 9.5: Installer Handoff Guide

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

> **A2-triggered:** False. Trigger evaluation (T1–T7, per `bmad-create-story/SKILL.md` 2026-05-10 codification):
>
> - **T1 (lifecycle/state-machine):** no match — the story consumes Story 9.4's already-derived render state (`complete-PASS` / `complete-WARN` / `complete-FAIL` / `running` / `never_run` / `outdated`) and adds zero state transitions of its own.
> - **T2 (retries/cancellation):** no match — static download endpoint with no retry policy and no cancellation semantics.
> - **T3 (persistence + recovery):** no match — AC2059 explicitly mandates "static, pre-rendered" content with "no dynamic content generation required"; the story introduces zero new persisted state and the guide download is contractually side-effect-free.
> - **T4 (watchdog/timing):** no match.
> - **T5 (multi-adapter coordination):** no match — no adapter I/O on the read path; the guide does not consult devices, the StateStore, or the snapshot.
> - **T6 (deployment/restart):** no match — guide content is deterministic across restart; no cold-start invariant introduced.
> - **T7 (installer workflow orchestration):** no match — although installer-facing and part of the 4-step wizard's terminal screen, the criterion requires "mutates persisted configuration or activates runtime behavior". This story mutates no state: handoff is already committed by 9.4's `POST /installer/setup/validation/handoff`; Story 9.5 only renders an informational artifact and exposes a read-only download endpoint.
>
> Because no triggers matched, the R1–R7 mandatory orchestration artifacts are NOT required. A standard story-context narrative follows.
>
> **Prerequisite gate:** Stories 9.0, 9.0b, 9.0c, 9.0d, 9.1, 9.2, 9.3, 9.4 are all `done`. Story 9.5 is the final Epic 9 deliverable. The `_setup_validation_handoff_success.html` template (deliberate placeholder authored by 9.4) and the `GET /installer/handoff` route are the agreed extension points; 9.4's dev notes explicitly state "Story 9.5 will replace the body with the printable installer guide; this route stays put."
>
> **Single-evaluator principle (Epic 7 retro 2026-05-05):** the guide reads validation state via `DeploymentValidationService.get_current_view()` only — no parallel computation of "is the guide allowed to render?" lives in route handlers or templates.

## Story

As an installer who has just completed a successful (PASS or PASS-with-WARN) deployment validation in Step 4,
I want a one-click download of a concise, printable handoff guide containing dashboard orientation, strategy-change instructions, degraded-mode plain-language explanations, installer-contact placeholders, and homeowner first-access (HTTPS certificate acceptance) instructions,
so that I can leave the homeowner with clear written instructions for daily use without having to author them myself — and the homeowner has a single page to refer back to when something looks unfamiliar.

## Acceptance Criteria

### AC1 — Static, pre-rendered, side-effect-free download route

A new route `GET /installer/handoff/guide` is added to `src/open_ems/web/routes/setup.py` (the existing installer router; not a new module).

**Route contract:**

- Auth: `Depends(require_installer)` — guide content is installer-facing setup material, not homeowner-visible until printed/shared. Non-installer (or unauthenticated) requests receive the same 302/401-style redirect that every other installer route returns.
- CSRF: none (read-only GET).
- Step-gate: identical to `GET /installer/setup/validation` — if `wizard_state.step_3_complete=0`, redirect to `/installer/setup/constraints`; if `step_2_complete=0`, redirect to `/installer/setup/roles`; if `step_1_complete=0`, redirect to `/installer/setup/discovery`. The guide is not reachable before Step 3 because the validation state it summarises does not yet exist meaningfully.
- Validation-state gate: the route resolves `view = await deployment_validation_service.get_current_view()`. If `view.result is None` OR `view.result.overall_status` is `running` OR `complete-FAIL`, the route responds with **400** and exact-match detail `handoff_guide_not_available: <reason>` where `<reason>` is one of: `never_run`, `running`, `complete-FAIL`. The PRD's two qualifying states (`complete-PASS` and `complete-WARN`) are the only ones that produce a 200.
- Outdated handling: when `view.is_outdated` is True, the route still serves the guide (the guide content is static and not parameterised by config_version) BUT renders an inline "outdated configuration" notice block at the top of the page (see AC4). The contract here is deliberately gentle: the guide is informational; an outdated validation does not invalidate the page's *content*, only signals that the installer should re-run validation before completing handoff. This avoids surprising the installer who clicks Download immediately after a constraint tweak.
- Response: `HTTPResponse` rendered from a Jinja2 template (see AC4). Content-Type defaults to `text/html; charset=utf-8`. **No `Content-Disposition: attachment` header is set** — the page is intended to render inline so the installer can review and then use the browser's Print dialog (or save to PDF) on-site. This satisfies AC2067's "200 response with correct content-type (`application/pdf` or `text/html`)" via the `text/html` branch.
- Side-effect contract: the handler MUST NOT call any service method that issues a device command, mutates `device_repo`, `wizard_state_repo`, `active_constraints`, or emits an `event_log` row. The only state read is `deployment_validation_service.get_current_view()`; the structlog emission is one line (`installer_handoff_guide_rendered` at info, with `session_id` + `overall_status` + `is_outdated` fields) for observability — no `event_log` insertion. AC2062 ("guide download does not trigger any device action or state change") is enforced structurally because the handler does not import `IntentExecutor`, `PolicyGuard`, `ControlLoop`, or any adapter module.

### AC2 — "Download handoff guide" button on validation result screen

The Step 4 validation result screen (`src/open_ems/web/templates/installer/_setup_validation_actions.html` — the actions fragment authored by 9.4) is extended to render a "Download handoff guide" link/button alongside the existing "Run validation" + "Complete handoff" controls.

**Button visibility — exact match to AC2061:**

- **Visible** when `view.result is not None` AND `view.result.overall_status in {'complete-PASS', 'complete-WARN'}` AND `view.is_outdated is False`.
- **Hidden** when `view.result is None` (never_run), `overall_status == 'running'`, `overall_status == 'complete-FAIL'`, OR `view.is_outdated is True`.

The outdated case is included in the "hidden" set even though AC1's route-level gate would tolerate outdated reads. Rationale: showing the button on an outdated page would invite the installer to grab a guide before re-running validation, blurring the moment-of-truth UX that Epic 9's "deployment validation as confidence ceremony" (UX spec §Bold Bets, line 53) is built around. The route accepts outdated for direct-URL bookmark resilience; the button hides on outdated for UX clarity.

**Rendering shape:**

- A plain `<a href="/installer/handoff/guide" target="_blank" rel="noopener" class="secondary-action">Download handoff guide</a>` element — NOT a form-POST button. The link opens the static guide page in a new tab so the installer can print without navigating away from the Step 4 wizard state. `target="_blank"` is paired with `rel="noopener"` per the UX spec's link-hygiene baseline.
- Label text: `Download handoff guide` (verbatim from AC2057). No alternate copy for WARN vs PASS — the same link text covers both qualifying states.
- The link is positioned visually *next to* the "Complete handoff" button (not above, not in a separate row) so the two actions are co-located. Per UX spec §Buttons (line 1384): primary action = "Complete handoff" with accent background; the guide download is a `secondary-action` (lighter visual weight, NOT accent background) so the primary action remains visually dominant.

**Accessibility (AC2068 + NFR-A1):**

- The link's accessible name is its text content (no `aria-label` override needed).
- Keyboard-focusable in the natural tab order; the same focus-ring rules as other links in the wizard apply.
- Colour contrast for the link text against the actions-region background meets 4.5:1 — same `--color-accent` family the rest of the wizard uses for non-primary actions.
- When the link is hidden, it is **not rendered at all** in the DOM (no `display: none` on a present element). Screen readers must not announce a non-existent control.

### AC3 — Handoff success page (`/installer/handoff`) inlines the guide content

The Story 9.4 placeholder template `src/open_ems/web/templates/installer/_setup_validation_handoff_success.html` is **replaced** (same path, same route, same URL — see 9.4 dev notes line 957) with a richer page that:

1. Renders a confirmation block at the top: "Handoff complete. Deployment validation passed and handoff was recorded at config_version {{ step_4_completed_config_version }}." (matches the existing placeholder copy verbatim for continuity — line 9 of the placeholder).
2. Inlines (NOT redirects to) the same handoff-guide content as `GET /installer/handoff/guide`. The shared content lives in a single Jinja2 partial `installer/_handoff_guide_body.html` that both the success page and the download page include. This means: after a successful POST /handoff, the installer lands directly on the printable guide content — no second click required.
3. Includes a prominent "Print this page" affordance at the top of the inlined guide block — a `<button type="button" onclick="window.print()">Print handoff guide</button>` element. This is the single line of inline JavaScript permitted in this story (per architecture's HTMX + Alpine.js + minimal-JS posture); it is `type="button"` so it does not submit any form, and the `onclick="window.print()"` call has no side effects beyond the browser print dialog.
4. Includes a sibling link to the dedicated `/installer/handoff/guide` page for installers who prefer to open the guide in a separate tab before printing.

The shared `_handoff_guide_body.html` partial is the single source of truth for guide content; the success page (which has the wizard chrome) and the dedicated download page (which has its own print-optimised layout) each include this partial inside their own outer wrapper.

### AC4 — Content of the handoff guide (English-only per NFR-L1)

The guide is a single printable HTML page laid out for letter / A4 with `@media print` CSS tuned so a homeowner can read it without zooming. Sections in order:

1. **Title block** — "OPEN-EMS Homeowner Quick-Start Guide" + a single-line subhead "Your installer set up this system on {{ today_iso }}. Keep this page for quick reference."

2. **Outdated-configuration notice (conditional)** — rendered ONLY when `view.is_outdated is True` (matches AC1's outdated handling). A single paragraph in a neutral-styled box: "Note: the system configuration changed after validation. Ask your installer to re-run validation before printing this guide." The box uses the same slate-600 styling Epic 10's degraded headline uses (UX spec line 2092) — no amber, no alarm. The notice is informational only; the rest of the content renders normally.

3. **First-time access — accepting the security certificate** — addresses UX spec line 1689-1692 ("A minimal first-access instruction screen or printed handoff checklist must exist for installers to walk homeowners through certificate acceptance"). The 2026-05-01 implementation-readiness report (line 459) flagged that this content has no delivery home in the PRD or any epic; Story 9.5 is the natural home and this section closes the gap. Content: plain-language description that OPEN-EMS uses an in-home certificate, the homeowner will see a "Not Secure" / "Your connection is not private" warning on their first visit, and the one-time steps to accept it on the major browsers (Chrome, Safari, Firefox, Edge — three-to-four lines each, no screenshots required for v1 because the guide is HTML and browsers refresh their UX). Closing line: "Once accepted, the warning will not reappear unless the certificate is re-issued."

4. **The dashboard at a glance** — Plain-language summary of what each card on the homeowner dashboard (Stories 10.1, 10.2, 10.3) shows:
   - "**Status headline**: a single sentence describing what the system is doing right now (for example, 'Your home is running on solar · Minimize Cost')."
   - "**Battery card**: how much of your battery is currently charged, in percent."
   - "**Solar card**: how much electricity your panels are producing right now, in kilowatts (kW)."
   - "**Grid card**: how much electricity is flowing to or from the public grid, in kilowatts. A positive number means you are drawing from the grid; a negative number means you are exporting."
   - "**EV card**: the next scheduled EV charging session, and a 'Charge EV now' button to start charging immediately."
   This section is deliberately careful to avoid technical jargon (no SoC, no kWh, no SystemOperatingMode) — the homeowner persona Lien (PRD §Personas) is non-technical.

5. **How to change your energy strategy** — Three-step walkthrough of Story 10.3's inline strategy selector: tap the strategy text on the dashboard headline, select one of the three options (Minimize Cost / Maximize Self-Consumption / Prioritize EV), the headline updates within a few seconds. One-line explanation of each strategy in homeowner terms (no mention of conflict resolution, no mention of EvaluationResult).

6. **Understanding degraded-mode messages and when to call your installer** — addresses AC2059 ("what degraded-mode messages mean and when to call the installer"). Plain-language explanation: the dashboard sometimes shows "Running with limited functionality" instead of the normal status. This is normal and the system is still safe. The homeowner can safely continue using the dashboard. Reasons to call the installer: (a) the message stays for more than 24 hours, (b) a card shows "Unavailable" for a specific device, (c) the homeowner cannot reach the dashboard at all from a known-good browser. The section explicitly does NOT enumerate degraded-mode internal reasons (no SystemOperatingMode names, no event_log surfacing) — that's installer-facing material that lives on the Story 11.1 monitoring dashboard.

7. **Known v1 limitation: control commands** — addresses Story 9.4's R7 carry-over (deferred-work item from Story 8-2 `[src/open_ems/web/app.py:223-236]`, classified `acceptable-post-this-story` in 9.4 with the explicit note "This must be called out explicitly in Story 9.5's handoff guide so installers do not promise homeowner-facing control before the wiring is in place"). One paragraph in installer-facing tone (this section is the only part of the guide written for the *installer reading the guide with the homeowner*, not for the homeowner alone): "v1 limitation: the dashboard's 'Charge EV now' button and strategy selector display correctly but commands may not yet reach connected devices (deferred Story 8-2 follow-up). Verify with the homeowner during handoff which controls are wired live on this site." This is a deliberate, dated note — it documents the v1 boundary rather than hiding it, and will be removed in the story that wires the runtime adapter map.

8. **Installer contact** — three placeholder fields the installer fills in by hand after printing (per AC2059 "installer contact placeholder fields"):
   - Installer name: `___________________________`
   - Phone: `___________________________`
   - Email: `___________________________`
   The fields are rendered as printed underlines (CSS `border-bottom: 1px solid;` on empty inline spans) sized so a typical handwritten entry fits. **No site-config-derived auto-fill in v1** — the AC says "placeholder fields" not "pre-filled fields", and OPEN-EMS does not yet store installer contact information in site config. A future story may auto-populate these from a site-config field.

9. **Footer** — single line: "OPEN-EMS · printed {{ today_iso }} · site config_version {{ result.config_version if result else 'unknown' }}." This is the only place the config_version surfaces; it appears in a small slate-400 footer because the homeowner doesn't need it, but the installer can use it to confirm which configuration this guide was generated for.

**English-only enforcement (NFR-L1):** all strings are inline literal English. No i18n machinery is introduced. The Jinja2 template does not import any translation context object. Multilingual support is contracted out of scope per PRD NFR-L1 (line 712) and epic line 417.

**Print styling:** a CSS block (inline `<style>` inside the template head, or in `static/open-ems.css` if it grows) provides `@media print` rules that hide the wizard chrome, set page margins to 15mm, ensure black text on white background, and tune font sizes for readability (14-16pt body, 18-20pt headings). The page must print cleanly on letter and A4 without manual scaling.

### AC5 — Templates and accessibility

New / modified Jinja2 templates under `src/open_ems/web/templates/installer/`:

- **NEW** `installer/handoff_guide_page.html` — standalone page extending `base.html` (NOT `setup_layout.html`, because the printed page does not need the wizard step indicator). Contains: an outer wrapper with print-styling CSS, the "Print handoff guide" button at the top (same `window.print()` call as AC3), and an `{% include "installer/_handoff_guide_body.html" %}` directive. This is the template served by `GET /installer/handoff/guide`.
- **NEW** `installer/_handoff_guide_body.html` — the shared partial holding sections 1–9 from AC4. Used by both `handoff_guide_page.html` AND the rewritten `_setup_validation_handoff_success.html`. Single source of truth for guide content. The partial receives `result`, `today_iso`, and `is_outdated` from the rendering context and uses them only for the optional outdated-notice block (section 2) and the footer config_version line (section 9).
- **MODIFIED** `installer/_setup_validation_handoff_success.html` — placeholder body REPLACED. The new body extends `setup_layout.html` (keeping the wizard chrome for the in-flow handoff screen), renders the confirmation block (AC3 step 1), the "Print handoff guide" button + link-to-standalone-page (AC3 step 3 + step 4), and an `{% include "installer/_handoff_guide_body.html" %}`.
- **MODIFIED** `installer/_setup_validation_actions.html` — adds the conditional "Download handoff guide" link per AC2 visibility rules.

**Accessibility (NFR-A1 / AC2068):**

- All sections use semantic `<h2>` / `<h3>` headings with sequential nesting — no heading-level skips.
- Section landmarks: each major section is wrapped in `<section aria-labelledby="<heading-id>">`.
- The "Print handoff guide" button has a visible accessible name; its `onclick` is the only JS; with JS disabled, the homeowner can still use the browser's File → Print menu (the page renders fully without JS).
- Colour contrast for body text against the printed-page background meets 4.5:1 — black-on-white is the print default.
- The "outdated configuration" notice (section 2) is rendered with `role="note"` so screen readers identify it as advisory rather than alert (it is not an error).
- The installer-contact placeholder fields (section 8) are visually styled as underlines but are NOT real form inputs (no `<input>` elements) — the page is for printing, not for digital filling. Screen readers see the labels followed by a non-interactive blank space; this is acceptable because the labels carry the information.
- Skip-to-content link at the top of `handoff_guide_page.html` lets keyboard users jump past the print button to the guide body — same pattern as the rest of the installer surface.
- Automated axe-core scan: same deferred-CI-hardening pattern as 9.1 AC10 / 9.2 AC8 / 9.3 AC8 / 9.4 AC9. A placeholder test at `tests/integration/web/test_handoff_guide_a11y.py` is marked `pytest.mark.xfail(reason="axe-playwright wiring deferred", strict=False)`. The outer `pytest.mark.skipif(npx absent)` is retained.

### AC6 — Routes module wiring (no new dependencies)

The new `GET /installer/handoff/guide` route is added directly to `src/open_ems/web/routes/setup.py` (the existing installer router). No new module is created; no new third-party dependencies are added; `uv.lock` is unchanged. The route handler depends on the same `_deployment_validation_service` and `_wizard_state_repo` helpers Story 9.4 already wired into the module. The `Depends(require_installer)` helper from `web/dependencies.py` is unchanged. No lifespan changes are required — Story 9.5 adds zero new state to construct.

### AC7 — Tests

Each test asserts the **exact** contract clause it covers (exact-match assertions on rejection reasons — per 9.0c AC7 / 9.1 AC11 / 9.2 AC9 / 9.3 AC10 / 9.4 AC11 precedent).

**Unit tests (`tests/unit/`):**

- `web/test_handoff_guide_route.py` (new) — covers:
  - GET `/installer/handoff/guide` without authentication → redirect-to-login (matches existing installer-route auth pattern; assert response 302 with `Location` header pointing at the login route).
  - GET when `step_1_complete=0` → 302 to `/installer/setup/discovery`.
  - GET when `step_2_complete=0` (and 1 complete) → 302 to `/installer/setup/roles`.
  - GET when `step_3_complete=0` (and 1+2 complete) → 302 to `/installer/setup/constraints`.
  - GET when steps 1+2+3 complete AND `view.result is None` (never_run) → 400 + exact-match detail `handoff_guide_not_available: never_run`.
  - GET when validation `overall_status='running'` → 400 + exact-match `handoff_guide_not_available: running`.
  - GET when `overall_status='complete-FAIL'` → 400 + exact-match `handoff_guide_not_available: complete-FAIL`.
  - GET when `overall_status='complete-PASS'` AND NOT outdated → 200, content-type `text/html; charset=utf-8`, body contains the AC4 section headings (assertion is on heading text presence, not the full HTML).
  - GET when `overall_status='complete-WARN'` AND NOT outdated → 200, body contains the same AC4 headings.
  - GET when `overall_status='complete-PASS'` AND outdated → 200, body contains the AC4 outdated-notice block heading ("Configuration changed since validation"); assertion verifies the route does NOT 400 on outdated PASS/WARN.
  - GET emits exactly one structlog event `installer_handoff_guide_rendered` with `session_id`, `overall_status`, `is_outdated` fields. Use `structlog.testing.capture_logs()` per project precedent.
  - GET never calls `device_repo.list_all()`, `state_store.get_snapshot()`, or any method on `IntentExecutor` / `PolicyGuard` / `ControlLoop` (assert via mocks set to raise on call; structural enforcement of the side-effect-free contract from AC1).
- `web/test_setup_validation_actions_download_button.py` (new — OR extend `test_setup_validation_routes.py` from 9.4) — covers the conditional render of the "Download handoff guide" link inside `_setup_validation_actions.html`:
  - View with `result.overall_status='complete-PASS'` AND NOT outdated → link present in the rendered fragment, `href="/installer/handoff/guide"`, `target="_blank"`, `rel="noopener"`.
  - View with `result.overall_status='complete-WARN'` AND NOT outdated → link present.
  - View with `result is None` → link absent (assert via `not in` on the rendered HTML).
  - View with `overall_status='running'` → link absent.
  - View with `overall_status='complete-FAIL'` → link absent.
  - View with `overall_status='complete-PASS'` AND `is_outdated=True` → link absent (UX-contract per AC2).

**Integration tests (`tests/integration/`):**

- `web/test_handoff_guide_e2e.py` (new) — full request flow against in-memory SQLite with the real services. Cases:
  - End-to-end happy path: Discovery (9.1 fixture) → roles (9.2 fixture) → constraints (9.3 fixture activates with a valid set, increments `config_version`) → validation (POST /run completes → `complete-PASS`) → GET `/installer/handoff/guide` → 200 + `text/html` body + body contains the AC4 section headings AND the resolved `config_version` value in the footer.
  - End-to-end after handoff: same path + POST `/handoff` (Story 9.4) → 302 to `/installer/handoff` → GET `/installer/handoff` body contains the inline guide partial AND the "Print handoff guide" button.
  - End-to-end WARN path with all warnings acknowledged: simulated adapter returns REDUCED capability → connectivity check WARN → ack the warning → GET `/installer/handoff/guide` → 200 (WARN qualifies per AC1).
  - End-to-end FAIL path: simulated adapter unreachable → connectivity check FAIL → GET `/installer/handoff/guide` → 400 + `handoff_guide_not_available: complete-FAIL`.
  - End-to-end outdated path: complete-PASS → constraint activation increments config_version → GET `/installer/handoff/guide` → 200 with the outdated notice rendered; AND GET `/installer/setup/validation` page → the "Download handoff guide" link is ABSENT from the actions fragment (per AC2 outdated-hidden rule). This is the cross-AC consistency assertion.
  - End-to-end no-state-change: GET `/installer/handoff/guide` does not change `wizard_state.step_4_complete`, does not change `deployment_validation_results.overall_status`, does not insert an `event_log` row, does not insert a `deployment_validation_acks` row. Assertion is by row count + value comparison pre/post call.
- `web/test_handoff_guide_a11y.py` (new — `pytest.mark.xfail` per AC5) — axe-core placeholder for the standalone guide page.

**Cross-story regression:** the full `tests/` sweep is green; the existing 9.4 setup-validation suite passes unchanged (the actions fragment now renders an extra link conditionally; the existing 9.4 tests assert on the existing Run / Complete-handoff buttons and the ack/revoke OOB swap, none of which change).

**Quality gates:** `pytest tests/ --no-cov -q`, `mypy src/`, `ruff check .`, `ruff format --check .` all clean before status moves to `review`.

### AC8 — Adversarial 3-layer review with severity tagging (process AC)

Before `Status: review → done`, run `/bmad-code-review` and apply the three-layer review (Blind Hunter / Edge Case Hunter / Acceptance Auditor) with severity tagging (HIGH / MEDIUM / LOW / deferred / dismissed). Same gate semantics as 9.0c AC9 / 9.0d AC11 / 9.1 AC12 / 9.2 AC10 / 9.3 AC11 / 9.4 AC12: **zero unresolved HIGH/MEDIUM permitted before merge**. All deferred findings logged to `_bmad-output/implementation-artifacts/deferred-work.md` under a dated "code review of 9-5-..." heading.

**Tiered review note (Epic 8 retro 2026-05-10):** Story 9.5 is NOT A2-triggered (zero T1–T7 matches; see header). The Epic 8 retro's tiered-review rule applies the full 3-layer adversarial pass when a story matches ≥1 A2 trigger; for non-A2 stories the 3-layer pass remains the project default but the Blind Hunter layer's focus areas are narrower because there is no cancellation, no concurrency, no cross-table transaction, and no race window to scrutinise. The Blind Hunter focus areas for this story are: (1) the side-effect-free contract on the GET handler (does it actually only read?), (2) the outdated-handling asymmetry between route (serves) and button (hides), (3) the JS-disabled fallback for the "Print" button, (4) the partial-template include path correctness (the same partial rendered into two different outer wrappers must look right in both).

## Tasks / Subtasks

- [x] Task 1 — Templates and content (AC4, AC5)
  - [x] Subtask 1.1 — Author `src/open_ems/web/templates/installer/_handoff_guide_body.html` containing the 9 AC4 sections + the optional outdated-notice block
  - [x] Subtask 1.2 — Author `src/open_ems/web/templates/installer/handoff_guide_page.html` (standalone print page; extends `base.html`; includes the partial)
  - [x] Subtask 1.3 — Replace `src/open_ems/web/templates/installer/_setup_validation_handoff_success.html` body: confirmation block + Print button + link-to-standalone + include of the partial. Keep the existing route + URL.
  - [x] Subtask 1.4 — Update `src/open_ems/web/templates/installer/_setup_validation_actions.html` to render the conditional "Download handoff guide" link per AC2 visibility rules
  - [x] Subtask 1.5 — `@media print` CSS rules (inline in `handoff_guide_page.html` head OR in `static/open-ems.css`) — implemented inline in `handoff_guide_page.html` for self-containment per AC4 print-styling guidance
- [x] Task 2 — Route handler (AC1, AC6)
  - [x] Subtask 2.1 — Add `GET /installer/handoff/guide` to `src/open_ems/web/routes/setup.py`: `Depends(require_installer)`, step-gate redirects, validation-state gate (400 for never_run/running/complete-FAIL), structlog `installer_handoff_guide_rendered` emission, render `handoff_guide_page.html` with `view`, `today_iso`, `is_outdated` context. Also updated the existing `GET /installer/handoff` handler to fetch the view and pass `result` / `is_outdated` / `today_iso` so the inlined guide partial renders correctly on the success page.
  - [x] Subtask 2.2 — Confirm zero new dependencies — the new handler imports only `DeploymentValidationService` (already in the module) and standard library; no `IntentExecutor`, `PolicyGuard`, `ControlLoop`, or adapter module imports introduced
- [x] Task 3 — Tests (AC7)
  - [x] Subtask 3.1 — Unit test file `tests/unit/web/test_handoff_guide_route.py` per AC7 unit cases (auth, step-gates, 400 paths with exact-match detail, 200 + body content, structlog emission, side-effect-free row-count assertion)
  - [x] Subtask 3.2 — Unit tests for the conditional Download button consolidated into the same new file `tests/unit/web/test_handoff_guide_route.py` (never_run / complete-PASS / complete-FAIL / outdated-PASS visibility per AC2)
  - [x] Subtask 3.3 — Integration test file `tests/integration/web/test_handoff_guide_e2e.py`: WARN-through-ack-and-handoff journey with inlined guide on success page + the cross-AC outdated-route-tolerates-but-button-hides asymmetry check
  - [x] Subtask 3.4 — Accessibility placeholder `tests/integration/web/test_handoff_guide_a11y.py` marked `pytest.mark.xfail` per the 9.1/9.2/9.3/9.4 precedent
- [ ] Task 4 — Quality gates + review (AC8)
  - [x] Subtask 4.1 — `pytest tests/ --no-cov -q` clean (1412 passed + 5 xfailed — the 4 pre-existing a11y placeholders from 9.1/9.2/9.3/9.4 + the new 9.5 a11y placeholder)
  - [x] Subtask 4.2 — `mypy src/` clean (96 source files, 0 issues)
  - [x] Subtask 4.3 — `ruff check .` and `ruff format --check .` clean (243 files)
  - [x] Subtask 4.4 — `/bmad-code-review` run (3-layer adversarial pass per Epic 8 retro + AC8) — Completed 2026-05-11. 3-layer adversarial pass (Blind Hunter / Edge Case Hunter / Acceptance Auditor) produced 3 decision-needed + 5 patch + 2 defer + 11 dismissed. All HIGH/MEDIUM findings resolved (D1 HIGH, D2 + D3 + P1 MEDIUM patched; DF1 MEDIUM deferred to follow-up as pre-existing Story 9.4 surface). Quality gates re-validated: pytest 1413p/5xfail, mypy 0 issues, ruff clean.
- [x] Task 5 — Cross-story regression validation
  - [x] Subtask 5.1 — Story 9.4 setup-validation suite passes unchanged (15 tests in `test_setup_validation_routes.py` + `test_setup_validation_e2e.py`); the new conditional Download link in the actions fragment did NOT regress the existing Run / Complete-handoff / ack-OOB-swap assertions
  - [x] Subtask 5.2 — Step 4 GET handler short-circuit verified via the new E2E test `test_warn_path_through_ack_and_handoff_renders_inlined_guide`: POST /handoff → 302 → GET /installer/handoff → 200 with inlined guide body + "Print this page" affordance + link to standalone guide page
  - [x] Subtask 5.3 — `epic-9-retrospective: optional` entry in `sprint-status.yaml` unchanged; running the retrospective remains a separate workflow decision

### Review Findings

_3-layer adversarial code review run 2026-05-11 (Blind Hunter / Edge Case Hunter / Acceptance Auditor). 11 dismissed as noise / false positive (incl. B3 `is_running` IS defined at `_setup_validation_actions.html:8`; A1 `component=` IS the project-wide structlog convention; B6 inline JS is documented project pattern)._

- [x] [Review][Patch] **D1 — Success page renders inlined guide using current `view`, not handoff-time view** [HIGH] [`src/open_ems/web/routes/setup.py:1150-1167`] — `GET /installer/handoff` fetches `await svc.get_current_view()` and unconditionally inlines `_handoff_guide_body.html`. After handoff, if validation is re-run to FAIL / running OR constraints change, the success page renders a guide that misrepresents deployment state. **Resolved 2026-05-11:** success page now calls the shared `_handoff_guide_unavailable_reason()` helper and renders the HTML 400 page (same template as the standalone route) when state has degraded. Symmetric semantics with `/installer/handoff/guide`. Source: Edge Case Hunter.
- [x] [Review][Patch] **D2 — `today_iso` semantics: request time vs install time + UTC vs local** [MEDIUM] [`src/open_ems/web/routes/setup.py:1166`, `:1223` + `_handoff_guide_body.html:19`, `:174`] — Section 1 subhead and footer both used `datetime.now(UTC).date()`. **Resolved 2026-05-11:** split into `install_iso` (sourced via `_install_iso()` helper: `state.step_4_completed_at` → `view.result.started_at` → request-time fallback) for section 1; `printed_iso` (request-time UTC) for the footer's "printed on" line. Partial template + both wrapper handlers updated. Sources: Edge Case Hunter + Blind Hunter (merged).
- [x] [Review][Patch] **D3 — JSON 400 raw output on direct browser GET of guide route** [MEDIUM] [`src/open_ems/web/routes/setup.py:1203-1207`] — `HTTPException` with `detail` on the HTMLResponse route emitted JSON in the browser. **Resolved 2026-05-11:** new template `installer/handoff_guide_unavailable.html` renders an HTML 400 page with the exact-match `handoff_guide_not_available: <reason>` token preserved verbatim inside a `<code>` block. `_render_handoff_guide_unavailable()` shared between the standalone route and the success page (D1). Existing tests adapted from `response.json()["detail"] == …` to substring checks on `response.text`. Sources: Edge Case Hunter (E3+E4 merged).
- [x] [Review][Patch] **P1 — Success page renders guide body unstyled (`.guide-*` classes only inline in standalone page)** [MEDIUM] [`src/open_ems/web/templates/installer/handoff_guide_page.html:17-83` + `_setup_validation_handoff_success.html`] — **Resolved 2026-05-11:** moved the body-content `.guide-*` screen styles into `_handoff_guide_body.html` itself (inline `<style>` block at the top) so they travel with the partial wherever it's included. Standalone page's `<head>` retains only wrapper-specific styles (body width, print rules, skip-link, print-bar). The success page now renders the partial with identical visual treatment to the standalone page. Source: Blind Hunter.
- [x] [Review][Patch] **P2 — Success page Print button has no JS-disabled fallback hint** [LOW] [`src/open_ems/web/templates/installer/_setup_validation_handoff_success.html:39-41`] — **Resolved 2026-05-11:** added the same `<small>Use your browser's Print dialog to print on letter or A4 paper, or save as PDF.</small>` hint adjacent to the success-page Print button as the standalone page already had. Source: Blind Hunter (B1+B5 merged).
- [x] [Review][Patch] **P3 — Success page Print button can print wizard chrome — no `@media print` rule to hide it** [LOW] [`src/open_ems/web/templates/installer/_setup_validation_handoff_success.html`] — **Resolved 2026-05-11:** overrode the parent layout's `{% block head %}` (calling `{{ super() }}` first) to inject a `@media print { header { display: none; } main { margin: 0; padding: 0; } }` rule. The in-place Print button on the success page now produces a clean printout equivalent to the standalone page. Source: Edge Case Hunter.
- [x] [Review][Patch] **P4 — Missing test: `overall_status="running"` 400 path** [LOW] [`tests/unit/web/test_handoff_guide_route.py`] — **Resolved 2026-05-11:** added `test_get_when_running_returns_400_with_exact_reason` that inserts a `running` row directly via SQL (avoiding races with the validation runner) and asserts the AC1 exact-match `handoff_guide_not_available: running` token in the HTML 400 body. Sweep totals went from 1412 → 1413 passing. Source: Edge Case Hunter.
- [x] [Review][Patch] **P5 — Print button label diverges from AC3 contract** [LOW] [`src/open_ems/web/templates/installer/_setup_validation_handoff_success.html:40` + `tests/unit/web/test_handoff_guide_route.py:810`] — **Resolved 2026-05-11:** success-page button text changed from "Print this page" to "Print handoff guide" (verbatim AC3). Corresponding test assertion (in `test_handoff_success_page_inlines_guide_after_handoff`) updated to match the new label. Source: Acceptance Auditor.
- [x] [Review][Defer] **DF1 — `is_outdated=False` when constraints provider unhydrated** [MEDIUM] [`src/open_ems/services/deployment_validation.py:206-212` — Story 9.4 surface] — `_current_config_version_safe()` returns `None` on `RuntimeError` (post-restart, pre-hydration); `is_outdated` derivation returns `False` if `current_version is None`. Story 9.5 inherits this — during the hydration window the button shows / the route 200s when it shouldn't. Deferred reason: pre-existing semantic gap in DeploymentValidationService introduced by Story 9.4; touches Story 9.4 service code and the validation page also relies on the same flag. Out of scope for 9.5's review. Source: Edge Case Hunter.
- [x] [Review][Defer] **DF2 — Homeowner-rejection test asserts status only, not redirect destination** [LOW] [`tests/unit/web/test_handoff_guide_route.py:435-443`] — `test_homeowner_rejected_on_handoff_guide` asserts `response.status_code == 302` but does not assert the `Location` header. A regression in `_ROLE_HOME["homeowner"]` would not be caught by this test. The other 302 tests in the same file DO assert redirect targets via `.endswith(...)`. Deferred reason: test-tightening cosmetic; project's homeowner-redirect pattern is well-established and exercised by many other tests. Source: Edge Case Hunter.

## Dev Notes

### Source-tree alignment

- `src/open_ems/web/templates/installer/handoff_guide_page.html` — NEW; standalone print page; extends `base.html` (not `setup_layout.html`). The print page does not need the wizard step indicator because it is read after handoff and primarily intended for print.
- `src/open_ems/web/templates/installer/_handoff_guide_body.html` — NEW; shared partial used by both the standalone page and the success page. Single source of truth for the 9 AC4 sections.
- `src/open_ems/web/templates/installer/_setup_validation_handoff_success.html` — MODIFIED; placeholder body replaced. Extends `setup_layout.html` (wizard chrome preserved on the in-flow success screen). Includes the new partial.
- `src/open_ems/web/templates/installer/_setup_validation_actions.html` — MODIFIED; adds the conditional "Download handoff guide" link.
- `src/open_ems/web/routes/setup.py` — MODIFIED; adds `GET /installer/handoff/guide` handler. The existing `GET /installer/handoff` placeholder handler is updated to render the new (richer) `_setup_validation_handoff_success.html` template — no route signature change.
- No new Python modules, no new third-party dependencies, no `uv.lock` changes. No migration. No service-layer changes. No lifespan changes. No Settings additions.

### Why printable HTML, not PDF

The epic AC (line 2058) accepts either "static, pre-rendered PDF or printable HTML page". HTML is chosen for v1 because:

1. **No new dependency.** Generating PDF would require `weasyprint` (~30 MB native deps), `reportlab` (pure Python but adds a templating surface that diverges from the rest of the wizard), or a headless-browser pipeline (Playwright/Chromium — too heavy for a Raspberry Pi 4 footprint). HTML uses the existing Jinja2 + browser-Print pipeline.
2. **Better accessibility.** WCAG 2.1 AA compliance (NFR-A1) on HTML with semantic markup is well-understood; verifying the same compliance on a generated PDF requires PDF/UA tooling that the project does not have. AC2068 mandates a zero-violations axe-core scan — that scan runs against HTML, not PDF.
3. **Simpler print fidelity.** Browser Print → "Save as PDF" produces a faithful PDF when the homeowner wants a file copy; the installer can print on-site without leaving the page.
4. **No content-generation runtime cost.** A pre-rendered PDF would still need to be cached or re-rendered per request (or trigger a build-time step); HTML rendered via Jinja2 is the default architecture pattern and adds zero new runtime considerations.

The content-type contract from AC2067 accepts either `application/pdf` or `text/html`; this story serves `text/html; charset=utf-8`.

### Why the route gates `complete-FAIL` (400) but tolerates `outdated` (200)

The Story 9.5 PRD line 2061 says: "the handoff button is only shown when validation status is `complete-PASS` or `complete-WARN` — it is hidden when status is `complete-FAIL`, `running`, or `never_run`."

The PRD's "hidden" set does NOT include `outdated`. The button is a button (visibility = boolean), but the route is a URL the installer might bookmark or land on after a constraint tweak. Two contracts diverge for one reason: **the button on the validation result screen is part of the "confidence ceremony" UX** (UX spec §Bold Bets line 53). Showing a "Download handoff guide" link on an outdated page would invite the installer to grab a guide before re-running validation, blurring the moment-of-truth signal. So the button HIDES on outdated.

**The route, however, does not hide on outdated.** Reason: a route's 400 response signals "this content is structurally unavailable" — but the guide content IS available; the static guide body does not change based on config_version. A 400 on outdated would prevent the installer from previewing the guide content while iterating on constraints — bad UX, no safety benefit. So the route renders 200 with an inline "outdated" notice (AC4 section 2).

This asymmetry is documented in AC1 (route tolerates) + AC2 (button hides) + AC4 section 2 (the inline notice that softens the route's outdated-200) so a future reviewer does not flag it as inconsistent.

### Why the success page inlines the guide (AC3) instead of redirecting to `/installer/handoff/guide`

Three alternatives were considered:

1. **Redirect after handoff** — `POST /installer/setup/validation/handoff` 302s directly to `/installer/handoff/guide` instead of `/installer/handoff`. **Rejected** because it removes the "Handoff complete" confirmation block and rushes the installer past the moment-of-truth. The success page's purpose is closure; the guide's purpose is reference. Combining them with a redirect loses the closure beat.
2. **Success page links to the guide** (button + URL, no inline content). **Rejected** because it requires a second click to view the guide. Story 9.5's AC contracts the guide as the *primary deliverable* of the handoff moment — making it one click away is right; making it zero clicks away is righter.
3. **Success page inlines the guide via shared partial** (selected). The installer lands on the success page, sees the confirmation block at the top, scrolls / prints the guide content immediately below. The standalone `/installer/handoff/guide` URL remains for direct linking, bookmarking, and the in-flow "Download handoff guide" button.

Selected option means: the success page's `_setup_validation_handoff_success.html` and the standalone `handoff_guide_page.html` share the `_handoff_guide_body.html` partial. Content drift between the two is structurally impossible.

### Why no `Content-Disposition: attachment` header

AC2057 says "Download handoff guide" — the *label* on the button. AC2058 says "triggering the button produces a static, pre-rendered PDF or printable HTML page". The PRD does not contract a file-download (forced save) — it contracts that the button *produces* the page. Rendering inline in a new tab (per AC2's `target="_blank"`) gives the installer:

- The ability to review the content in-browser before printing.
- The browser's native Print dialog, which can also "Save as PDF" if the homeowner wants a file copy.
- Better accessibility (screen readers handle inline HTML pages cleanly; forced-download attachments require external tools).

Adding `Content-Disposition: attachment; filename="OPEN-EMS-handoff-guide.html"` would force the file to download. A future story can add an optional `?format=download` query parameter if installer feedback shows this is needed; v1 does not contract it.

### Library / framework notes

- No new third-party dependencies. The standalone page extends `base.html` and uses only Jinja2 + HTMX baseline (no HTMX features are used in this story; HTMX is just available because `base.html` includes it).
- The single inline JS (`window.print()` button) is a `type="button"` with no other side effects. It is the same line of JS used by the rest of the printable-HTML web — no precedent change.
- `@media print` CSS lives inline in `handoff_guide_page.html` head OR in `static/open-ems.css` (developer choice; the inline approach keeps the standalone page self-contained; the central CSS file is more discoverable and reusable if Epic 11/12 add more print-able views).

### Previous-story intelligence — what we carry forward

- **Story 9.4's `DeploymentValidationService.get_current_view()` is the sole read path.** No re-computation of the PASS/WARN/FAIL/outdated derived state lives in this story. Single-evaluator principle (Epic 7 retro) preserved.
- **Story 9.4's placeholder template `_setup_validation_handoff_success.html`** is the explicit handoff point. 9.4's dev notes (line 957) say "Story 9.5 will replace the body with the printable installer guide; this route stays put." This story honours both halves: the body is rewritten; the route + URL + auth gate are unchanged.
- **Story 9.4's R7 carry-over from Story 8-2** (`adapters={}` runtime-adapter deferral) is surfaced explicitly in AC4 section 7 ("Known v1 limitation: control commands"). This is the structural close-out of the "MUST be called out in Story 9.5's handoff guide" commitment 9.4 made (9.4 dev notes line 631).
- **Story 9.4's W2 deferred-finding** ("No back-navigation to Step 4 once `step_4_complete=1`") is addressed structurally: the rewritten success page inlines the guide content, so the installer who lands there post-handoff has the printable artifact directly. The original concern (installer cannot revisit Step 4 to see results) is partially addressed because the guide footer surfaces the result's `config_version`; full Step 4 result re-render is NOT contracted (that's a separate UX-polish concern).
- **Story 9.1 / 9.2 / 9.3 / 9.4 exact-match rejection-reason pattern** is preserved: the new 400-class rejections (`handoff_guide_not_available: never_run | running | complete-FAIL`) use exact-match string assertions in tests.
- **Story 9.1 / 9.2 / 9.3 / 9.4 placeholder-CI a11y pattern** is preserved: a new `pytest.mark.xfail` placeholder test exists; the cross-story a11y CI hardening is one resolution sweep, not a per-story burden.

### Audit semantics

This story emits **exactly one structlog event** (`installer_handoff_guide_rendered` at info level, with `session_id`, `overall_status`, `is_outdated` fields). It emits NO `event_log` audit rows — consistent with Story 9.4's audit-semantics decision (9.4 dev notes line 666: "the FR20 event log is reserved for control decisions, constraint enforcement, degraded-mode transitions, recovery, and installer notes; deployment validation results live in their own table"). A guide render is informational and does not warrant `event_log` surfacing.

If a future Epic 11 audit policy decides to log handoff-guide views (e.g., for support traceability — "was the guide rendered before the homeowner called?"), the structlog event is already there and can be tapped by a logging sink. No retroactive schema change required.

### Project Structure Notes

- All new files map onto the architecture-defined directory layout (architecture.md §Project Structure):
  - `web/templates/installer/handoff_guide_page.html` and `_handoff_guide_body.html` are permitted additions under `templates/installer/`.
  - `web/routes/setup.py` extension (one new handler) is permitted; the file remains within the project's informal LOC threshold post-9.4 (~1050 LOC; adding ~50 LOC for one handler keeps it ~1100, well below 9.3 / 9.4 precedent).
- No new top-level packages. No new third-party dependencies. `uv.lock` unchanged.
- **Detected variance: none.** This is a documentation-style story with no architectural decisions to land.

### Orchestration Risk Analysis

**Not applicable — Story 9.5 is NOT A2-triggered.** See the header A2 trigger evaluation: all seven of T1–T7 evaluated to "no match". The story does not modify any state machine, does not introduce cancellation or retry logic, does not persist any new data, does not coordinate adapters, does not depend on cold-start lifecycle, and does not mutate persisted configuration as part of an installer workflow. The R1–R7 mandatory orchestration artifacts (per `bmad-create-story` codification 2026-05-10) are therefore NOT required.

The A2 evaluation was performed against actual evidence in the AC text (AC1, AC2, AC3, AC4, AC5, AC6, AC7 above) — not by absence-of-contraindication. Each trigger was checked affirmatively and returned no match. Re-verification on review is welcome; the evaluation note in the header is the authoritative record.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-9.5] — story spec lines 2047–2068; the authoritative AC source
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-9-Cross-story-constraints] — lines 2035–2043; cross-story constraints apply transitively (none of them bind 9.5 directly, but Story 9.5's guide content surfaces the `complete-PASS` / `complete-WARN` / outdated derived state Story 9.4 owns)
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-L1] — line 712; English-only v1 contract
- [Source: _bmad-output/planning-artifacts/prd.md#NFR-A1] — line 714; WCAG 2.1 AA compliance
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#HTTPS-and-first-access-experience] — lines 1689–1692; the printed handoff checklist for browser certificate acceptance (covered by AC4 section 3)
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Buttons] — line 1384; primary vs secondary action visual treatment; informs AC2's `secondary-action` styling
- [Source: _bmad-output/planning-artifacts/ux-design-specification.md#Bold-Bets] — line 53; "Deployment validation as a confidence ceremony" rationale; underpins the AC2 outdated-hidden-on-button decision
- [Source: _bmad-output/planning-artifacts/implementation-readiness-report-2026-05-01.md#Condition-3] — lines 457–461; flagged the handoff certificate guide as a deployment artifact without a delivery home — Story 9.5 closes that gap
- [Source: _bmad-output/implementation-artifacts/9-4-implement-deployment-validation-with-safe-readiness-probing-and-persisted-results.md] — full Story 9.4; the source-of-truth for `DeploymentValidationService.get_current_view()`, `_setup_validation_handoff_success.html` placeholder, `/installer/handoff` route, and the W2 deferred-finding
- [Source: _bmad-output/implementation-artifacts/9-4-…#Dev-Notes — adapters_runtime_deferral] — Story 9.4 dev notes line 624–631; the explicit "must be called out in Story 9.5's handoff guide" commitment that AC4 section 7 closes
- [Source: _bmad-output/implementation-artifacts/deferred-work.md#code-review-of-story-9-4] — lines 3–9; W2 (no back-navigation to Step 4) classification as "defer to Story 9.5"; Story 9.5 partially addresses by inlining the guide on the success page
- [Source: src/open_ems/web/templates/installer/_setup_validation_handoff_success.html] — current 9.4 placeholder; the template body this story replaces
- [Source: src/open_ems/web/templates/installer/_setup_validation_actions.html] — 9.4 actions fragment; the conditional "Download handoff guide" link is added here
- [Source: src/open_ems/web/routes/setup.py] — 9.4 route module; the new `GET /installer/handoff/guide` handler is added here
- [Source: src/open_ems/services/deployment_validation.py] — `DeploymentValidationService.get_current_view()` (9.4); the single read path used by this story
- [Source: src/open_ems/web/dependencies.py] — `require_installer`, `_resolve_session`
- [Source: memory project_epic1_retro.md] — Epic 1 retro 2026-05-02; hard-gates pattern that justifies the prerequisite-gate note in the header
- [Source: memory project_epic7_retro.md] — Epic 7 retro 2026-05-05; single-evaluator principle (handoff guide reads through one service surface)
- [Source: memory project_epic8_retro.md] — Epic 8 retro 2026-05-10; A1/A2 codification; non-A2 stories use the standard story workflow

## Dev Agent Record

### Agent Model Used

claude-opus-4-7

### Debug Log References

- Quality gates (2026-05-11):
  - `pytest tests/ --no-cov -q` → 1412 passed + 5 xfailed (1 new 9.5 a11y placeholder; 4 pre-existing 9.1/9.2/9.3/9.4 a11y placeholders).
  - `mypy src/` → clean across 96 source files.
  - `ruff check .` → all checks passed across 243 files.
  - `ruff format --check .` → all 243 files already formatted (initial run flagged 2 files for formatting; `ruff format` applied; second `--check` clean).
- Scoped reruns:
  - New tests in isolation: `pytest tests/unit/web/test_handoff_guide_route.py tests/integration/web/test_handoff_guide_e2e.py tests/integration/web/test_handoff_guide_a11y.py --no-cov -q` → 18 passed + 1 xfail.
  - Cross-story 9.4 regression: `pytest tests/unit/web/test_setup_validation_routes.py tests/integration/web/test_setup_validation_e2e.py --no-cov -q` → 15 passed.

### Completion Notes List

- **Shared partial pattern delivered.** `installer/_handoff_guide_body.html` is the single source of truth for the 9 AC4 sections. Both the standalone print page (`handoff_guide_page.html`) and the rewritten success page (`_setup_validation_handoff_success.html`) include it via `{% include %}`, so content drift between the two is structurally impossible.
- **New route shape — `GET /installer/handoff/guide`.** Installer-auth + step-1/2/3 redirects mirror the `get_validation_page` chain. Validation-state gate raises `HTTPException(400, "handoff_guide_not_available: <reason>")` for `never_run` / `running` / `complete-FAIL`. PASS and WARN (including outdated) return 200 with the standalone print page. Emits exactly one structlog event `installer_handoff_guide_rendered` per request with `session_id` / `overall_status` / `is_outdated` fields. No imports of `IntentExecutor`, `PolicyGuard`, `ControlLoop`, or adapter modules in the handler — AC1 side-effect-free contract is structural.
- **Success page (`GET /installer/handoff`) enriched.** The existing 9.4 placeholder route is unchanged in URL/auth/step-gate; only the template body is replaced and the handler now also fetches `await svc.get_current_view()` so the inlined guide body partial gets the `result` / `is_outdated` / `today_iso` context it expects. The "Handoff complete" confirmation block continues to render at the top (continuity with 9.4 placeholder copy).
- **Conditional Download link on the validation actions fragment** (`_setup_validation_actions.html`). Visibility per AC2: rendered IFF `result is not none AND not is_running AND not is_outdated AND result.overall_status in {complete-PASS, complete-WARN}`. Rendered as `<a target="_blank" rel="noopener" class="secondary-action">` — NOT a form button — so the installer doesn't lose Step 4 wizard state when reviewing/printing the guide.
- **Cross-AC asymmetry (route serves outdated, button hides) explicitly tested** via `test_outdated_route_serves_but_validation_page_hides_button` in the integration suite. Documented in the dev notes of the story file ("Why the route gates `complete-FAIL` (400) but tolerates `outdated` (200)") and asserted in code so future refactors cannot regress one half without the other.
- **Side-effect-free contract structurally asserted.** `test_get_does_not_mutate_validation_or_wizard_state` snapshots row counts in `deployment_validation_results`, `deployment_validation_acks`, `wizard_state`, AND `event_log` before/after the GET, plus the `wizard_state.step_4_*` triple — every count and value must be unchanged. The added `event_log` DDL in the test fixture matches `migrations/versions/0005_add_event_log_table.py` so the assertion is real.
- **HTTPS certificate-acceptance section (AC4 section 3) closes the 2026-05-01 readiness-report Condition 3 gap.** UX spec lines 1689-1692 identified this as a required deployment artifact without a delivery home in PRD/epics; Story 9.5's guide content now hosts it.
- **v1 control-commands limitation (AC4 section 7) closes Story 9.4's R7 carry-over from Story 8-2.** The Story 8-2 deferred-finding (`adapters={}` runtime adapter map; PolicyGuard rejects all commands) is now called out explicitly in the homeowner guide ("installer-facing tone") so installers do not promise homeowner-facing control before the runtime-adapter wiring story lands.
- **Story 9.4 W2 deferred-finding ("No back-navigation to Step 4 once `step_4_complete=1`") partially addressed.** The rewritten `/installer/handoff` success page inlines the full guide body, so the installer landing there post-handoff has the printable artifact directly. The `result.config_version` is surfaced in the guide footer for traceability. Full Step 4 result re-render is NOT contracted (that remains a separate UX-polish concern).
- **No new third-party dependencies.** `uv.lock` unchanged. No migration. No lifespan changes. No Settings additions. Pure templates + one route handler + tests.
- **Task 4.4 (`/bmad-code-review` run) deferred** to a separate workflow invocation per Epic 8 retro + Story 9.4 precedent — code-review runs AFTER `Status: review` is set.

### File List

**New source files:**
- `src/open_ems/web/templates/installer/_handoff_guide_body.html`
- `src/open_ems/web/templates/installer/handoff_guide_page.html`

**Modified source files:**
- `src/open_ems/web/templates/installer/_setup_validation_handoff_success.html` — body replaced (was a Story 9.4 placeholder)
- `src/open_ems/web/templates/installer/_setup_validation_actions.html` — added conditional "Download handoff guide" link per AC2 visibility rules
- `src/open_ems/web/routes/setup.py` — added `GET /installer/handoff/guide` handler; extended the existing `GET /installer/handoff` handler to fetch `DeploymentValidationView` and pass `result` / `is_outdated` / `today_iso` to the success template

**New test files:**
- `tests/unit/web/test_handoff_guide_route.py` (covers AC7 unit cases + the conditional Download button visibility tests)
- `tests/integration/web/test_handoff_guide_e2e.py` (covers AC7 integration cases — multi-step user journeys)
- `tests/integration/web/test_handoff_guide_a11y.py` (axe-core placeholder; `pytest.mark.xfail` per the project-wide deferred-CI-hardening pattern)

**Modified out-of-band:**
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — `9-5-…: ready-for-dev → in-progress → review`

## Change Log

- 2026-05-11 — Story 9.5 implementation completed. Status: ready-for-dev → in-progress → review. All Task 1-3 subtasks complete; Task 4.1-4.3 quality gates pass (pytest 1412p/5xfail, mypy 0 issues, ruff clean); Task 4.4 (`/bmad-code-review`) queued for separate workflow invocation. Task 5 cross-story regression validation green.
- 2026-05-11 — `/bmad-code-review` 3-layer adversarial pass complete. Status: review → done. 3 decision-needed (D1 HIGH, D2/D3 MEDIUM) resolved as patches; 5 patches (P1 MEDIUM, P2/P3/P4/P5 LOW) applied; 2 defer entries logged to `deferred-work.md` (DF1 — pre-existing `is_outdated=False` when constraints provider unhydrated; DF2 — homeowner-rejection test redirect-target tightening); 11 findings dismissed as noise / false positive (incl. B3 `is_running` IS defined; A1 `component=` IS project-wide structlog convention; B6 inline JS is documented project pattern). New file: `installer/handoff_guide_unavailable.html`. Modified: `installer/_handoff_guide_body.html` (styles + `install_iso`/`printed_iso` split), `installer/handoff_guide_page.html` (de-duplicated body-content styles), `installer/_setup_validation_handoff_success.html` (D1 gate via shared helper, P2 fallback hint, P3 print-only chrome hide, P5 button label), `web/routes/setup.py` (D1 gate on success page, D2 install/printed split with `_install_iso()` helper, D3 HTML 400 via shared `_render_handoff_guide_unavailable()` helper), `tests/unit/web/test_handoff_guide_route.py` (D3 substring assertions on response.text, P4 new running-state test, P5 label assertion). Quality gates re-validated: pytest 1413p/5xfail, mypy 0 issues across 96 files, ruff check + format clean across 243 files.
