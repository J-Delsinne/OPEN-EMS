---
stepsCompleted:
  - step-01-document-discovery
  - step-02-prd-analysis
  - step-03-epic-coverage-validation
  - step-04-ux-alignment
  - step-05-epic-quality-review
  - step-06-final-assessment
documentsIncluded:
  - prd: "_bmad-output/planning-artifacts/prd.md"
  - architecture: "_bmad-output/planning-artifacts/architecture.md"
  - epics: "_bmad-output/planning-artifacts/epics.md"
  - ux: "_bmad-output/planning-artifacts/ux-design-specification.md"
---

# Implementation Readiness Assessment Report

**Date:** 2026-05-01
**Project:** OPEN-EMS

---

## Document Inventory

| Document Type | File | Size | Last Modified |
|---|---|---|---|
| PRD | `prd.md` | 54.9 KB | 2026-04-29 |
| Architecture | `architecture.md` | 78.6 KB | 2026-05-01 |
| Epics & Stories | `epics.md` | 194.0 KB | 2026-05-01 |
| UX Design | `ux-design-specification.md` | 125.4 KB | 2026-05-01 |

**Duplicates:** None  
**Missing Documents:** None  
**Status:** All required documents present and confirmed for assessment.

---

## PRD Analysis

### Functional Requirements

**Device Integration and Management**

- FR1: The installer can discover and connect to energy devices on the local network via Modbus TCP and OCPP 1.6, and connect to DSMR P1 smart meters as a real-time grid data source
- FR2: The installer can assign an energy role (inverter, battery, EV charger, grid meter) to each discovered device
- FR3: The system can detect whether a discovered device supports full or partial integration capability
- FR4: The system can operate in reduced-capability mode when a device's integration is partial, while continuing to enforce safety constraints using available data
- FR5: The system maintains a versioned capability profile per supported device model and firmware version, validated through read and write/control testing before listing as supported
- FR6: The installer can view the integration capability status and any active limitations for each connected device
- FR6b: The system does not expose unsupported or unvalidated device capabilities to the decision engine

**Energy Control and Decision Engine**

- FR7: The system continuously evaluates energy state on a fixed cycle and issues control actions within installer-defined safety boundaries
- FR8: The system enforces a configurable peak consumption limit by controlling battery discharge and EV charge rate
- FR9: The system controls battery charge and discharge state in accordance with installer-defined constraints and the active energy strategy
- FR10: The system manages EV charging session start, stop, and — where supported by the charger — dynamic charge rate adjustment
- FR11: The system applies a homeowner-selected energy strategy (Minimize Cost / Maximize Self-Consumption / Prioritize EV) to determine control priorities
- FR11b: The system resolves conflicting energy demands (e.g. EV charging vs. battery reserve vs. peak limit) using a deterministic priority model derived from the active strategy and installer-defined constraints
- FR12: The system respects a configurable EV charging window preference while permitting homeowner overrides bounded by active safety constraints
- FR13: The system transitions to a conservative fallback mode (control actions reduced or stopped) when device communication is degraded or energy data is incomplete, while maintaining safety constraints
- FR14: The system verifies command acknowledgment before assuming a control action has taken effect
- FR15: The system never silently ignores a constraint violation, command failure, or degraded state — all such conditions are logged and surfaced to the installer

**Installer Configuration and Management**

- FR16: The installer can configure per-site safety constraints (peak consumption limit, battery reserve floor) and the preferred EV charging window, which the decision engine treats as a scheduling preference
- FR17: The installer can validate device communication and constraint configuration before completing deployment
- FR18: The installer can view a system health indicator showing overall system status (healthy / degraded / error) and per-device connectivity state
- FR19: The installer can view current-month peak consumption data (highest 15-minute interval recorded) against the configured limit
- FR20: The installer can access a timestamped event log of control decisions, constraint enforcement events, command failures, degraded-mode transitions, and recovery events
- FR21: The installer can add documentation notes to the event log
- FR22: The installer can check for available platform updates and trigger an update with explicit approval, with release notes visible before confirming
- FR23: The installer can create and reset homeowner credentials
- FR24: The installer can export a site configuration backup and restore it to the same or a replacement system without exposing plaintext credentials

**Homeowner Energy Interface**

- FR25: The homeowner can select an energy strategy from three options: Minimize Cost, Maximize Self-Consumption, or Prioritize EV
- FR26: The homeowner can view the current system state: battery charge level, solar production, and grid draw
- FR27: The homeowner can view the next scheduled EV charging session
- FR28: The homeowner can trigger an immediate EV charging session that overrides scheduled timing, while remaining subject to active safety constraints and dynamic charge rate adjustments if required
- FR29: The homeowner can view a simple system status indicator (plain-language, no technical detail) when the system is operating with reduced capability
- FR30 *(nice-to-have)*: The homeowner can view a weekly energy summary: peaks avoided, self-consumption ratio, and estimated cost savings

**System Observability and Reliability**

- FR31: The system automatically recovers to normal operation after a reboot or temporary network interruption without manual intervention
- FR32: The system detects stalled control loops or unresponsive subsystems and triggers automatic recovery (service restart or transition to fail-safe mode)
- FR33: The system logs all automatic recovery and watchdog-triggered events with timestamps, visible to the installer
- FR34: The system transitions to fail-safe mode (ceases issuing control commands; relies on device-level safety) when device communication is lost, and logs the degraded state

**Access Control and Security**

- FR35: Users authenticate with role-based local credentials before accessing any system functionality — Installer/Admin and Homeowner/User roles are distinct
- FR36: The Installer/Admin role has access to device configuration, constraint management, system logs, update management, and homeowner account management
- FR37: The Homeowner/User role has access only to strategy selection, system status, EV override, and degraded-mode indication
- FR38: The system enforces session expiry for both roles, with Installer/Admin sessions requiring re-authentication after extended inactivity
- FR39: The system serves the web interface over HTTPS; any HTTP access during development is explicitly marked as development-only

**External Data Integration**

- FR40 *(nice-to-have)*: The system can retrieve solar production forecasts from a configurable external API and use them to improve optimization decisions
- FR41 *(nice-to-have)*: The system can consume dynamic tariff data from a configurable external feed and use it for cost-optimization decisions
- FR42: The installer can view when the optimization layer is operating in degraded mode due to unavailability of external forecast or tariff data

**Total FRs: 40** (including FR6b, FR11b sub-items; FR30, FR40, FR41 are nice-to-haves)

---

### Non-Functional Requirements

**Performance**

- NFR1: Decision engine evaluation cycle target 5–15 seconds under normal operation; max 60 seconds under degraded connectivity before watchdog triggers
- NFR2: Control actions (EV charge rate, battery dispatch) applied within 30 seconds from decision to observable device state change
- NFR3: Web interface pages load and are interactive within 3 seconds on minimum target hardware (Raspberry Pi 4)
- NFR4: Device state data displayed in interfaces reflects actual device state within 30 seconds of a confirmed change
- NFR5: Event log queries covering the past 30 days return results within 5 seconds

**Reliability**

- NFR6: System operates without unhandled crashes or forced restarts for minimum 30 consecutive days under normal conditions
- NFR7: System must never violate configured safety constraints due to internal logic errors
- NFR8: System returns to full operational state within 2 minutes after clean or unexpected reboot
- NFR9: After temporary network interruption, system reconnects to devices and resumes normal operation within 60 seconds of network restoration
- NFR10: Stalled control loops detected and watchdog recovery triggered within 2 consecutive missed evaluation cycles

**Security**

- NFR11: Credentials stored using bcrypt or equivalent one-way hashing; no plaintext credentials stored at rest
- NFR12: Configuration backups must not contain plaintext credentials or secrets; secrets excluded or encrypted within export
- NFR13: All web interface traffic uses HTTPS in production; self-signed certificates acceptable for v1
- NFR14: Session tokens must use minimum 128 bits of entropy from a cryptographically secure random generator
- NFR15: Installer/Admin sessions expire after configurable inactivity period (recommended default: 4 hours)
- NFR16: Homeowner/User sessions expire after configurable inactivity period (recommended default: 7–30 days)
- NFR17: Local energy data must not be transmitted to external services except explicitly installer-configured optional API calls; all such calls must use HTTPS

**Integration**

- NFR18: Modbus TCP and OCPP connection attempts must time out within 10 seconds; handled gracefully without blocking the control loop
- NFR19: Failed control commands may be retried a maximum of 2 times, provided retries do not risk violating safety constraints or causing oscillating behavior
- NFR20: DSMR P1 grid data older than 60 seconds treated as stale; triggers conservative control assumptions in decision engine
- NFR21: External API calls (solar forecast, tariff data) must time out within 10 seconds without affecting core control operation

**Data and Storage**

- NFR22: Energy state history retained locally for minimum 12 months
- NFR23: Event log entries retained locally for minimum 90 days
- NFR24: Event log entries related to failures, constraint enforcement, and recovery must not be pruned below minimum retention period
- NFR25: Configuration and event log data written with power-loss protection (atomic writes or journaling)
- NFR26: Platform updates must preserve all site configuration and energy history without data loss or silent migration failure
- NFR27: No energy consumption data or personal data transmitted externally in v1 outside explicitly configured optional API integrations

**Maintainability and Deployability**

- NFR28: Platform installation on fresh Raspberry Pi 4 or equivalent completable in under 30 minutes following documented procedure
- NFR29: Site configuration export and restore on compatible hardware completable in under 10 minutes
- NFR30: Installation process reproducible across sites using same documented steps and package versions — no site-specific system configuration required beyond device onboarding and constraint setup

**Total NFRs: 30**

---

### Additional Requirements / Constraints

- **Belgian tariff compliance:** Flanders capaciteitstarief (15-min peak billing) and Wallonia incentive tariff (dynamic pricing) are first-class requirements; the decision engine must natively support both billing structures
- **GDPR:** All v1 data stored locally with no cloud transmission; no user accounts transmitted to external services; formal GDPR assessment required before any remote/telemetry features in future versions
- **Liability:** OPEN-EMS operates as supervisory control layer only; device-level safety mechanisms (BMS, inverter protection) are not overridden; installer is responsible for constraint configuration
- **Hardware:** Edge deployment on Raspberry Pi 4+, NUC, or existing Linux host; no dedicated hardware product in v1; SSD strongly recommended for production
- **No built-in remote access:** Remote connectivity is installer-managed (VPN/port forwarding); OPEN-EMS provides no remote access infrastructure in v1
- **Containerized deployment:** Docker/Compose is a nice-to-have; direct host installation is the baseline and must always be supported
- **Protocol scope:** Modbus TCP, OCPP 1.6, DSMR P1 — no manufacturer cloud APIs in v1

---

---

## Epic Coverage Validation

### Coverage Matrix

| FR | PRD Requirement (summary) | Epic Coverage | Status |
|---|---|---|---|
| FR1 | Device discovery via Modbus TCP, OCPP 1.6, DSMR P1 | Epic 3 + 4 + 9 | ✓ Covered |
| FR2 | Energy role assignment per device | Epic 9 | ✓ Covered |
| FR3 | Detect full vs. partial integration capability | Epic 4 + 9 | ✓ Covered |
| FR4 | Operate in reduced-capability mode | Epic 4 + 9 | ✓ Covered |
| FR5 | Versioned capability profile per device model/firmware | Epic 4 | ✓ Covered |
| FR6 | Installer views integration capability status and limitations | Epic 9 | ✓ Covered |
| FR6b | Unvalidated capabilities not exposed to decision engine | Epic 4 | ✓ Covered |
| FR7 | Continuous evaluation cycle, control within safety boundaries | Epic 7 | ✓ Covered |
| FR8 | Enforce peak consumption limit via battery/EV control | Epic 7 | ✓ Covered |
| FR9 | Battery charge/discharge control within constraints and strategy | Epic 7 | ✓ Covered |
| FR10 | EV session start/stop and dynamic charge rate adjustment | Epic 7 | ✓ Covered |
| FR11 | Apply homeowner-selected energy strategy | Epic 7 | ✓ Covered |
| FR11b | Deterministic conflict resolution (constraints > optimization > convenience) | Epic 7 | ✓ Covered |
| FR12 | Respect EV charging window preference with homeowner override bounds | Epic 7 | ✓ Covered |
| FR13 | Conservative fallback mode on degraded communication or incomplete data | Epic 4 + 8 | ✓ Covered |
| FR14 | Command acknowledgment verification before assuming effect | Epic 8 | ✓ Covered |
| FR15 | No silent failures — all constraint/command/degraded events logged | Epic 6 | ✓ Covered |
| FR16 | Installer configures per-site constraints and EV window preference | Epic 9 | ✓ Covered |
| FR17 | Deployment validation before handoff | Epic 9 | ✓ Covered |
| FR18 | System health indicator (healthy/degraded/error + per-device) | Epic 11 | ✓ Covered |
| FR19 | Current-month peak consumption vs. configured limit | Epic 11 | ✓ Covered |
| FR20 | Timestamped event log (decisions, constraints, failures, recovery) | Epic 6 + 11 | ✓ Covered |
| FR21 | Installer notes on event log | Epic 6 + 11 | ✓ Covered |
| FR22 | Platform update management with installer approval | Epic 12 | ✓ Covered |
| FR23 | Homeowner credential creation and reset | Epic 11 | ✓ Covered |
| FR24 | Configuration backup and restore without exposing credentials | Epic 12 | ✓ Covered |
| FR25 | Homeowner strategy selector (Minimize Cost / Self-Consumption / Prioritize EV) | Epic 10 | ✓ Covered |
| FR26 | Homeowner current state: battery SOC, solar production, grid draw | Epic 10 | ✓ Covered |
| FR27 | Homeowner next scheduled EV session display | Epic 10 | ✓ Covered |
| FR28 | Single-tap EV override with safety constraint enforcement | Epic 10 | ✓ Covered |
| FR29 | Plain-language degraded-mode indicator for homeowner | Epic 10 + 11 | ✓ Covered |
| FR30 *(N2H)* | Homeowner weekly energy summary | Epic 10 | ✓ Covered |
| FR31 | Automatic recovery after reboot or network interruption | Epic 4 + 12 | ✓ Covered |
| FR32 | Watchdog stall detection and automatic recovery trigger | Epic 8 | ✓ Covered |
| FR33 | Recovery and watchdog events logged with timestamps | Epic 6 + 8 | ✓ Covered |
| FR34 | Fail-safe mode on device communication loss | Epic 8 | ✓ Covered |
| FR35 | Role-based local authentication (Installer/Admin + Homeowner/User) | Epic 2 | ✓ Covered |
| FR36 | Installer/Admin access scope enforcement | Epic 2 | ✓ Covered |
| FR37 | Homeowner/User access scope enforcement | Epic 2 | ✓ Covered |
| FR38 | Session expiry for both roles | Epic 2 | ✓ Covered |
| FR39 | HTTPS enforcement; HTTP dev-only | Epic 2 | ✓ Covered |
| FR40 *(N2H)* | Solar production forecast API integration | Epic 13 | ✓ Covered |
| FR41 *(N2H)* | Dynamic tariff data feed integration | Epic 13 | ✓ Covered |
| FR42 | Installer visibility into degraded optimization mode | Epic 13 | ✓ Covered |

### Missing Requirements

None. All PRD functional requirements are covered.

### Coverage Statistics

- Total PRD FRs: 40 (including FR6b, FR11b sub-items; FR30, FR40, FR41 are nice-to-haves)
- FRs covered in epics: 40
- **Coverage: 100%**

---

### PRD Completeness Assessment

The PRD is well-structured and thorough. Requirements are consistently numbered, categorized, and clearly distinguished between must-have and nice-to-have. User journeys are concrete and directly traceable to individual FRs. Compliance, liability, and security constraints are explicitly called out. The document provides sufficient detail to validate epic and story coverage. No ambiguous or missing requirement areas were detected.

---

## UX Alignment Assessment

### UX Document Status

**Found and complete.** `ux-design-specification.md` (125.4 KB, 2026-05-01) covers both user personas (Lien/Installer and Marc/Homeowner), all 5 PRD user journey flows, 17 custom components, a full design system (Tailwind pre-compiled + CSS custom properties + Jinja2 macros), accessibility strategy, real-time event contracts, action response contracts, failure handling patterns, and an explicit v1 scope boundary list. The spec was derived directly from the PRD and architecture documents.

---

### UX ↔ PRD Alignment

Strong alignment. Every PRD user journey is explicitly mapped in the UX spec (Journey Flows 1–5). All homeowner-facing FRs (FR25–FR30) and installer-facing FRs (FR1–FR24) are addressed in the component and interaction specifications. The UX spec respects the PRD's non-technical language principle, the single-tap EV override constraint, the one-page dashboard readability target, and the deployment validation pass/fail handoff ceremony.

**UX-introduced additions not in PRD (requires attention):**

| Item | Description | Gap type |
|---|---|---|
| Language configurability | UX spec requires `<html lang>` set to `nl` or `fr` based on installer-configured language at install time; language must be configurable and changeable by installer | New requirement — not in PRD, not in any epic |
| WCAG 2.1 Level AA | UX spec designates WCAG 2.1 AA as the formal accessibility compliance target ("European standard expected under EN 301 549") | Not in PRD NFRs; not in any epic acceptance criteria |
| Handoff checklist artifact | UX spec requires a printable one-page guide for installer to walk homeowners through certificate acceptance at first access | Deployment artifact requirement — not in PRD, not in any epic |

**Minor observations (no gap, noted for implementation):**

- `house_load_kw` included in the `metric_update` SSE payload — not explicitly in FR26, but consistent with it and unlikely to require additional effort
- EV card "No EV connected" empty state (button hidden, not disabled) — not explicitly in Epic 10 stories; needs to be handled as an acceptance criterion detail
- `action_id` in the action response contract matches `correlation_id` in AR16 `CommandResult` — minor naming difference; same concept, needs explicit mapping in Epic 10 implementation

---

### UX ↔ Architecture Alignment

Strong alignment. The UX spec was built on the architecture document and explicitly references its constraints:

| Architecture constraint | UX spec response |
|---|---|
| FastAPI + Jinja2 + HTMX + Alpine.js | Design system and all components target this stack explicitly |
| No frontend build pipeline at runtime | Tailwind pre-compiled to `static/open-ems.css`; vendored locally; no CDN |
| SSE for state push (AR11) | SSE as primary path; HTMX polling as silent fallback — same behavior, no visible difference |
| Role-filtered SSE payload (AR11) | UX spec confirms homeowner payload excludes installer diagnostics |
| StateStore (AR14) | "UI reads SystemOperatingMode from StateStore snapshots only; does not infer state from raw adapter data" |
| PolicyGuard + CommandResult (AR15, AR16) | EV override 4-state model correctly models the CommandResult async response cycle |
| No CDN / local-first (deployment context) | All assets served from `static/`; Alpine.js and HTMX pinned and committed |

**State model mapping note (implementation-critical):**
Architecture defines `SystemOperatingMode` as `normal / degraded / conservative / fail_safe` (AR8). UX spec defines global UI states as `NORMAL / DEGRADED / STALE / FAILED`. The implicit mapping (`conservative` → `DEGRADED`; `fail_safe` → `FAILED`; `STALE` is UI-only) is reasonable but never made explicit in either document or the epics. Epic 5 (State Aggregation) should formalize this mapping before implementation begins.

---

### Warnings

1. **⚠️ Language configurability not planned:** The UX spec introduces a Belgian multilingual language configuration requirement (nl/fr at install time) that has no corresponding FR, AR, or epic coverage. This is not a blocker for MVP but will surface during implementation of the setup wizard. Recommend adding it as a story in Epic 1 (Foundation) or Epic 9 (Setup Wizard) if it is in scope.

2. **⚠️ Accessibility compliance not formalized:** WCAG 2.1 AA is named in the UX spec as the compliance target but does not appear in the PRD NFRs or any epic acceptance criteria. Accessibility testing (axe-core, keyboard navigation, screen reader) is described in detail in the UX spec but has no implementation story to own it. Recommend adding an acceptance criterion to Epic 10 (Homeowner Dashboard) and Epic 9 (Installer Setup) for axe-core clean scans.

3. **⚠️ Handoff certificate guide has no delivery owner:** The UX spec identifies a printable homeowner first-access guide as a required deployment artifact. This is not in the PRD or any epic. If this is genuinely required for the installer handoff to succeed, it needs a delivery home — likely Epic 9 or a deployment documentation story in Epic 1.

4. **ℹ️ SystemOperatingMode → UI state mapping is implicit:** The translation between backend engine operating modes and UI global states needs to be made explicit in Epic 5 (State Aggregation and Real-Time Distribution). This is low risk but should be addressed before Epics 7/8 and 10/11 are implemented to avoid inconsistent frontend behavior.

---

## Epic Quality Review

### Standards Applied

Epics validated against: user value delivery, epic independence (no forward dependencies), story user-centric framing, acceptance criteria quality (BDD format + testability), database table creation timing, and greenfield project setup patterns.

---

### 🔴 Critical Violations

**Finding 1: Epics 3–8 are technical milestone epics with no direct user-deliverable outcome**

Epics 3 (Protocol Adapters), 4 (Device Abstraction Layer), 5 (State Aggregation), 6 (Observability and Event Logging), 7 (Decision Engine), and 8 (Control Execution and Safety Enforcement) are purely technical infrastructure epics. No installable, demonstrable user-facing capability exists until Epic 9. The "user outcome" statements for these epics describe developer outcomes, not user outcomes.

*Example:* Epic 5 user outcome: "Any component reads a consistent, immutable view of the system from a single source." This is a system invariant — not a user-visible capability.

**Assessment:** This is a genuine structural concern by create-epics-and-stories standards. However, for an IoT control platform where reliability is the primary success criterion and the control loop must function correctly before any UI is useful, a sequential infrastructure-first sequence is a defensible deliberate design choice. The concern is noted but assessed as **accepted risk for this product type** — the team should be aware that 8 epics of technical work deliver zero user-visible outcomes, creating a long runway before any installer or homeowner can validate the system.

**Recommendation:** Consider adding interim validation milestones — e.g., a "smoke test harness" story in Epic 8 that lets a developer simulate a full decision cycle end-to-end on test hardware. This would provide early integration confidence before Epic 9.

---

### 🟠 Major Issues

**Finding 2: "As a developer" user story persona used throughout Epics 3–8**

Every story in Epics 3–8 uses "As a developer" as the persona. Best practices require user-centric framing even for technical stories. The "developer" is not an end user of OPEN-EMS — the installer and homeowner are.

*Examples:*
- Story 3.1: "As a developer, I want a typed interface contract..."
- Story 5.1: "As a developer, I want a StateStore..."
- Story 7.1: "As a developer, I want the decision engine to derive SystemOperatingMode..."

**Impact:** Low practical impact — the technical intent is clear and ACs are detailed. However, the framing obscures which PRD requirements are being served and makes it harder to assess value delivery.

**Recommendation:** Reframe developer stories with installer or system outcomes. Example: "As an installer, I want the system to communicate reliably with all supported device protocols..." drives the same technical work with a user-visible framing.

---

**Finding 3: Story 3.1 ("Define protocol adapter contract and raw protocol types") delivers only design/typing, no runtime behavior**

Story 3.1 is purely a type definition and interface contract story. All its acceptance criteria are structural ("Given the adapters package is initialized, When types are imported, Then..."). No runtime behavior is tested or delivered. This is a developer scaffolding story, not a user story.

**Impact:** Minor — the contract is necessary and establishing it explicitly is good engineering practice. But as an independent story, it delivers nothing testable in isolation beyond mypy checks.

**Recommendation:** Acceptable to keep as-is given the architecture's strict type-checking emphasis (mypy strict mode). Alternatively, merge Story 3.1 into Story 3.2 (Modbus adapter) where the contract is first exercised in a meaningful way.

---

**Finding 4: Epic 5 FR coverage is limited to architectural requirements (AR14, AR11, NFR-P4) — no FRs covered**

Epic 5 covers no Functional Requirements from the PRD directly. It is entirely an architectural requirement implementation. At the end of Epic 5, no FR is directly satisfied by Epic 5 alone.

**Impact:** Not a coverage gap (Epic 5 is a prerequisite for FRs satisfied by later epics), but it means Epic 5 represents a pure infrastructure investment with no user validation checkpoint.

**Recommendation:** Acceptable as designed. Note this in the development plan so the team is aware that Epics 1–8 collectively satisfy FRs only via the output they enable in Epics 9–12.

---

### 🟡 Minor Concerns

**Finding 5: Stories defining types/contracts alongside behavior in the same story (mixed concerns)**

Stories 4.1 ("Define domain device state model and DeviceAdapter protocol") and 7.5 ("Define EvaluationResult contract with decision reasons and cycle timing") mix type definition with behavioral testing. The type definitions are not independently valuable without the behaviors built on them.

**Assessment:** Acceptable in context. Both stories include meaningful testable invariants (sign convention tests in 4.1, intent conflict resolution tests in 7.5). Not a significant concern.

---

**Finding 6: Epic 2 story 2.5 multi-device session concurrency policy is explicitly "Option A (multiple sessions per user)" without record of Option B being rejected**

Story 2.5 labels its session concurrency policy "Option A" with no documentation of what Option B was or why A was chosen. This creates an implicit decision record gap.

**Impact:** Trivial — the policy is clearly specified in the ACs. But labeling it "Option A" suggests an alternative was considered without documenting the tradeoff.

**Recommendation:** Remove the "Option A" label or add a one-line rationale in the story (e.g., "Multiple sessions permitted — installer access from multiple devices simultaneously is a valid use case").

---

**Finding 7: Story 12.3 persona is "As a developer" despite having significant installer-facing outcomes**

Story 12.3 covers startup recovery, data retention, and the `/health/status` endpoint — all of which have direct installer visibility. The "As a developer" framing undersells the user value.

**Recommendation:** Reframe as "As an installer, I want the system to recover automatically after a reboot and maintain energy history reliably, so that I don't need to intervene manually after power failures."

---

### ✅ Best Practices Compliance Checklist

| Check | Epic 1 | Epics 2–8 | Epics 9–13 |
|---|---|---|---|
| Epic delivers user value | ✓ Partial (NFR-M1 installer value) | ⚠️ Developer value only | ✓ |
| Epic can function independently | ✓ | ✓ No forward deps | ✓ |
| Stories appropriately sized | ✓ | ✓ | ✓ |
| No forward dependencies | ✓ | ✓ | ✓ |
| DB tables created when needed | ✓ | ✓ Alembic migrations | ✓ |
| Clear acceptance criteria (BDD) | ✓ | ✓ | ✓ |
| Traceability to FRs maintained | ✓ (ARs) | ✓ | ✓ |
| Greenfield setup story present | ✓ Story 1.1 | N/A | N/A |
| CI/CD pipeline story present | ✓ Story 1.6 | N/A | N/A |

**Overall quality assessment:** The epic and story structure is detailed, precise, and well-engineered. Acceptance criteria are specific, testable, and use proper BDD format throughout. The primary concern is the concentration of technical infrastructure in Epics 1–8 with no user-visible delivery until Epic 9. This is a calculated trade-off for a reliability-first platform product — it should be a conscious team decision with clear go/no-go criteria before committing to the full 8-epic runway.

---

## Summary and Recommendations

### Overall Readiness Status

## ✅ READY WITH CONDITIONS

The planning artifacts for OPEN-EMS are comprehensive, well-aligned, and implementation-ready. The PRD is complete with 40 FRs and 30 NFRs fully extracted and verified. All FRs achieve **100% coverage** across 13 epics. The UX design specification is thorough and technically aligned with the architecture. Epic and story quality is high, with specific, testable BDD acceptance criteria throughout.

Three conditions must be resolved before or in parallel with early implementation work. They are not blockers for starting Epic 1, but will become implementation blockers if left unaddressed.

---

### Issues Summary

| # | Severity | Source | Finding |
|---|---|---|---|
| 1 | ⚠️ Condition | UX Alignment | Language configurability (nl/fr) not planned in any epic |
| 2 | ⚠️ Condition | UX Alignment | WCAG 2.1 Level AA not in NFRs or epic ACs |
| 3 | ⚠️ Condition | UX Alignment | Installer handoff certificate guide has no delivery owner |
| 4 | ℹ️ Note | UX Alignment | SystemOperatingMode → UI state mapping is implicit |
| 5 | 🟠 Design | Epic Quality | Epics 3–8 are technical milestone epics (accepted risk for product type) |
| 6 | 🟠 Style | Epic Quality | "As a developer" persona in Epics 3–8 |
| 7 | 🟡 Minor | Epic Quality | Story 3.1 is a type definition story only (no runtime behavior) |
| 8 | 🟡 Minor | Epic Quality | Epic 5 covers no FRs directly |
| 9 | 🟡 Minor | Epic Quality | "Option A" session policy label undocumented |
| 10 | 🟡 Minor | Epic Quality | Story 12.3 uses developer framing for installer-visible features |

**Total: 3 conditions + 7 informational/quality items**

---

### Critical Conditions Requiring Action Before Phase 4

**Condition 1 — Language configurability (nl/fr)**

The UX specification requires the web interface language (Dutch/French) to be configurable at install time for Belgian multilingual deployments. This has no corresponding FR, AR, NFR, or epic story. This will become an implementation blocker when Epic 9 (Installer Setup Wizard) is implemented.

*Action:* Decide whether this is in v1 scope. If yes, add a story to Epic 1 or Epic 9 and add NFR for language configurability. If no, explicitly document it as v2 scope in the PRD and UX spec.

**Condition 2 — WCAG 2.1 Level AA accessibility**

The UX spec designates WCAG 2.1 AA as the compliance target (required under EN 301 549 for Belgian B2B products). This standard is not referenced in the PRD NFRs and no epic acceptance criteria include accessibility validation requirements. Without explicit ACs, accessibility is likely to be skipped under time pressure.

*Action:* Add a single NFR to the PRD ("The web interface must comply with WCAG 2.1 Level AA"). Add an acceptance criterion to Story 10.1 (homeowner dashboard) and Story 9.1 (device discovery step) requiring axe-core clean scan with zero critical violations. Add keyboard navigation and screen reader AC to Stories 10.2 (EV override) and 9.4 (deployment validation).

**Condition 3 — Installer handoff certificate guide**

The UX spec requires a printable one-page guide for installers to walk homeowners through the browser certificate acceptance experience at first access. This is identified as a required deployment artifact but has no delivery home in the PRD, epics, or any story.

*Action:* Add a documentation/deployment story to Epic 9 ("Produce installer handoff checklist covering first-access certificate acceptance"). Low effort, high first-impression impact for homeowner onboarding.

---

### Recommended Next Steps

1. **Address the 3 conditions above** — each can be resolved in under an hour of planning work (one PRD update + 3–4 new ACs + 1 new story).

2. **Formalize the SystemOperatingMode → UI state mapping** — add an explicit mapping table to Epic 5's `Story 5.1` ACs (or the system state model docstring) documenting that `conservative → DEGRADED` and `fail_safe → FAILED` in the UI layer. This prevents silent inconsistency in Epic 10 and Epic 11 implementations.

3. **Define an Epic 1–8 go/no-go checkpoint** — before investing in Epics 3–8, establish a clear integration test milestone (e.g., "end-to-end simulated control cycle on test hardware") that validates the infrastructure stack works together. Without this, 6–9 weeks of infrastructure work could be completed before a full-system integration failure is discovered.

4. **Consider an interim validation milestone at Epic 8** — add a story at the end of Epic 8 that exercises the full decision pipeline (simulated adapter → decision engine → PolicyGuard → command dispatch → event log) end-to-end against test hardware. This de-risks the infrastructure epics before Epic 9 depends on them working.

5. **Resolve the Story 3.1 design concern** — either keep it as-is (low risk, good type-safety foundation) or merge into Story 3.2 if the team prefers fewer micro-stories during Epic 3 implementation.

---

### Final Note

This assessment reviewed 4 planning documents totaling approximately 459 KB of planning content. **10 issues** were found across 4 categories. The 3 conditions (language configurability, accessibility compliance, handoff guide) are the only items that must be resolved before proceeding — they are all lightweight to address. The 7 quality observations are informational and can be addressed opportunistically during implementation.

The OPEN-EMS planning artifacts represent a high-quality, production-ready plan for a complex IoT platform. The level of specification detail — particularly the explicit AR items, the system state model, the dual-mechanism peak tracking design, and the phased epic structure — is well above average and reflects careful architectural thinking.

**Assessor:** Claude (BMAD Implementation Readiness workflow)
**Date:** 2026-05-01
**Documents reviewed:** prd.md, architecture.md, epics.md, ux-design-specification.md
