---
stepsCompleted: ["step-01-init", "step-02-discovery", "step-02b-vision", "step-02c-executive-summary", "step-03-success", "step-04-journeys", "step-05-domain", "step-06-innovation-skipped", "step-07-project-type", "step-08-scoping", "step-09-functional", "step-10-nonfunctional", "step-11-polish"]
releaseMode: phased
inputDocuments:
  - "_bmad-output/planning-artifacts/prfaq-OPEN-EMS.md"
  - "_bmad-output/planning-artifacts/prfaq-OPEN-EMS-distillate.md"
workflowType: 'prd'
classification:
  projectType: "edge-application + iot-orchestration-platform + web-app"
  domain: "energy"
  complexity: "high (v1 intentionally scoped to reduce implementation complexity)"
  projectContext: "greenfield"
---

# Product Requirements Document - OPEN-EMS

**Author:** Jordan
**Date:** 2026-04-29

## Executive Summary

OPEN-EMS is a local-first energy orchestration platform for residential energy installers. It enables installers to deploy, configure, and operate multi-brand energy systems — solar inverters, home batteries, EV chargers, and smart meters — as a single, coordinated system, without requiring per-installation custom integrations, vendor lock-in, or cloud dependency.

OPEN-EMS acts as the coordination layer between independently operating energy devices, turning a fragmented setup into a single, predictable system.

The platform runs as an edge application on standard Linux hardware (Raspberry Pi, NUC, or existing home server). A role-based device abstraction model assigns each physical device a defined energy role. A rule-based, deterministic decision engine continuously balances energy flows, enforces peak limits, manages battery cycles, and schedules EV charging sessions — always operating within installer-defined safety boundaries. A basic forecasting layer (solar production and consumption trends) improves optimization outcomes without ever overriding hard system limits.

**Primary customer:** Independent residential energy installer managing 5–50 installations per year. The installer selects the platform, configures each site, and defines the operational boundaries of the system. They interact with OPEN-EMS primarily at deployment time and for occasional monitoring.

**Secondary user:** Homeowner — daily operator. Sets an energy strategy (minimize cost / maximize self-consumption / prioritize EV), views current system state and weekly summaries, and can trigger temporary overrides. Does not manage devices or technical parameters.

**Launch market:** Belgium — Flanders capaciteitstarief (15-min peak billing, live 2023) and Wallonia incentive tariff (dynamic pricing default for digital meter holders, live Jan 2026) create measurable, bill-printed financial incentives for coordinated home energy management.

**v1 success definition:** An installer deploys OPEN-EMS on a real client site, walks away, and the system operates without unexpected behavior, broken integrations, or per-site custom fixes. Stability and predictability in field conditions are the primary proof points. Feature completeness is not.

---

### What Makes It Special

The residential energy space has smart devices without coordination. Every installer workaround — Home Assistant automations, EVCC per-site configs, manual peak monitoring — is a symptom of a missing professional coordination layer.

OPEN-EMS closes that gap with three things the existing alternatives don't combine:

1. **A structured energy domain model.** Devices are assigned roles (inverter, battery, EV charger, grid meter) with defined capabilities and constraints — not generic smart home entities. This makes decision logic consistent and repeatable across every installation, regardless of brand mix.

2. **A deterministic decision engine.** Energy control is rule-based and conservative by default. Forecasting improves outcomes within hard limits — it never overrides them. Behavior is predictable and auditable: the system never silently breaks a constraint.

3. **A professional deployment model.** Installers configure once per site using a structured flow, deploy consistently, and manage their portfolio without writing code or maintaining automations. The platform is designed to scale across a professional installer's book of work, not just a single home.

Competitive position: not more capable than a fully custom Home Assistant setup built by an expert — but significantly more reliable, maintainable, and practical for installers deploying across tens of projects. gridX is the enterprise equivalent; OPEN-EMS is the SME-accessible version. EVCC covers EV/solar; OPEN-EMS covers full system orchestration including battery dispatch and peak management.

**Homeowner design principle:** The system should feel like a reliable autopilot. The homeowner interface answers "what is it doing and why?" — it does not ask the homeowner to make technical decisions. Transparent and reassuring; complexity is handled invisibly.

---

## Project Classification

| Attribute | Value |
|---|---|
| Project type | Edge application + IoT orchestration platform + web application |
| Domain | Energy (HEMS — Home Energy Management System) |
| Overall complexity | High |
| v1 implementation complexity | Intentionally reduced — limited device set, rule-based engine, local-only operation |
| Project context | Greenfield |
| Launch market | Belgium (Flanders + Wallonia) |
| Team size | 3 (1–2 backend engineers + 1 integration-focused developer) |
| v1 timeline | 6–9 months |

## Success Criteria

### User Success

**Installer (primary):**
- Deploys the system on a client site without requiring per-installation custom fixes
- Walks away from a deployment with confidence the system will operate correctly without follow-up intervention
- Generates measurably fewer support calls post-deployment compared to previous integration approach
- Uses the platform on a second installation without retraining or platform-level troubleshooting

**Homeowner (secondary):**
- System operates autonomously under normal conditions — no user actions required during daily operation
- Homeowner can describe at a high level what the system is doing and why, using the interface alone
- No confusion-driven support calls are generated during normal operation
- Homeowner remains comfortable leaving the system in automatic mode at all times

### Business Success

Measured at the end of the v1 pilot phase (months 1–3 post-deployment):

- 20–50 stable active installations running without requiring manual intervention from the OPEN-EMS team
- 5–10 installers have used the platform across more than one installation (repeat adoption signal)
- Measurable reduction in per-installation setup time compared to the installer's previous approach
- Majority of pilot installers respond positively to "I would use this again" qualitative check
- Zero installations requiring platform-level rollback or emergency intervention

### Technical Success

A v1 deployment is considered technically stable when all of the following hold:

- No unhandled crashes or forced restarts for at least 30 consecutive days under normal operating conditions
- The decision engine respects all defined safety constraints 100% of the time — no peak limit violations or unsafe battery control caused by system logic
- Device integrations remain stable with no unexpected disconnects requiring manual intervention more than once per 7-day period per device
- System recovers automatically from reboots or temporary network interruptions without manual reconfiguration
- No per-installation code changes or custom logic are required after initial deployment

**Governing principle:** The system must behave predictably under real-world conditions. Any unexplained or non-deterministic behavior is considered a failure.

### Measurable Outcomes

Supporting validation metrics (collected during pilot phase; not gating criteria for v1 completion):

- Measurable reduction or avoidance of monthly peak consumption events compared to pre-deployment baseline
- Improved self-consumption ratio for PV installations
- Reduction in manual interventions by homeowner (e.g. manual EV charging or scheduling)
- Reduction in average installer setup time per deployment compared to prior approach

**Governing principle:** Primary homeowner success is experiential — "I trust it and don't need to think about it." Metrics are validation, not the primary goal.

---

## Product Scope

### MVP — v1

Strictly local. Single installation. Buildable by a team of 3 in 6–9 months.

**Runtime and deployment:**
- Edge application on standard Linux hardware (Raspberry Pi 4+, NUC, or existing Linux host)
- No cloud dependency for core operation; local-only data storage

**Device support (launch set):**
- Inverters: Fronius Gen24 series, Huawei SUN2000 series, Growatt hybrid (selected models)
- Batteries: BYD HVS and HVM
- EV chargers: Wallbox Pulsar series, go-e Charger series
- Smart meters: DSMR-compatible meters
- Protocol coverage: Modbus TCP, OCPP

**Core engine:**
- Rule-based, deterministic decision engine: peak limiting, energy balancing, battery charge/discharge control, EV charging session scheduling
- Basic forecasting layer: consumption trend tracking; solar production forecast integration *(nice-to-have)*
- Installer-defined safety constraints applied unconditionally

**Configuration interface (installer-facing):**
- Device discovery and role assignment
- Safety constraint and boundary setup per site
- Strategy configuration for homeowner

**Homeowner interface:**
- Strategy selection: Minimize Cost / Maximize Self-Consumption / Prioritize EV
- Current system state: battery level, PV production, grid draw
- Weekly summary: peaks avoided, estimated savings *(nice-to-have)*
- Temporary override: "Charge EV now"
- Web-based, mobile-responsive; no native app

**Monitoring:**
- Local dashboard: single-installation view, real-time status, basic event log
- Basic event log includes system decisions and constraint enforcement events for debugging and transparency

### Growth — v2 (outline only)

- Multi-site remote monitoring and fleet management dashboard
- Remote configuration push to deployed installations
- Improved forecasting models (extended horizon, higher accuracy)
- Extended device compatibility library

### Vision (pointer only)

Full cloud management tier; mobile applications; pre-configured hardware bundles; installer certification and partner program. Not designed for in this document.

**Governing principle:** Future scope must not expand v1 requirements — it may only inform extensibility decisions where necessary.

## User Journeys

### Journey 1 — Installer: First Deployment (Primary Success Path)

**Persona: Lien**, independent energy installer, Belgium. Approximately 20 installations per year. Currently manages multi-brand setups through a combination of manufacturer apps and ad-hoc Home Assistant automations. Every new installation is a slightly different custom project.

**Opening scene:** Lien arrives at a new client site in Ghent: Fronius Gen24 inverter, BYD HVM battery, Wallbox Pulsar EV charger, DSMR-compatible smart meter. The client is on the Flanders capaciteitstarief and is frustrated — six months after installation, his electricity bill hasn't dropped the way he expected. Three separate apps. No coordination. Lien has seen this before.

**Rising action:** She connects her laptop to the site LAN and opens the OPEN-EMS installer interface. Device discovery finds all four devices via Modbus TCP and DSMR. She assigns each its energy role: inverter, battery, EV charger, grid meter. She sets the site-level constraints: peak consumption limit (aligned with the client's tariff threshold), battery reserve floor (minimum SOC the system will never discharge below), and the preferred EV charging window (22:00–06:00), used by the decision engine as a constraint, not a fixed schedule. She selects "Minimize Cost" as the homeowner's default strategy. She reviews the constraint summary screen — all limits confirmed.

**Climax:** She triggers a deployment validation run. A validation check confirms device communication, constraint configuration, and control responsiveness. No errors. No warnings.

**Resolution:** Lien closes the installer interface, hands the homeowner a simple one-page summary, and shows him the homeowner web interface on his phone. He sees the battery at 76%, PV producing, grid draw near zero. The strategy reads "Minimize Cost." He nods. Lien drives away. She receives no call that evening — or the day after.

**Requirements this journey surfaces:**
- Device discovery via Modbus TCP, OCPP, DSMR
- Energy role assignment per device
- Per-site constraint configuration: peak limit, battery reserve floor, preferred EV charging window
- Default strategy selection by installer
- Pre-handoff deployment validation with explicit pass/fail result
- Homeowner interface accessible immediately post-deployment: real-time state, active strategy

---

### Journey 2 — Installer: Device Integration Issue During Setup (Edge Case)

**Persona: Lien**, different site — Huawei SUN2000 inverter, BYD HVS battery, go-e Charger.

**Opening scene:** Device discovery completes. Battery and charger connect cleanly. The Huawei inverter is detected but OPEN-EMS flags it immediately: "Inverter [Huawei SUN2000] — Modbus read access restricted. Firmware version 3.11. PV production data unavailable. Forecasting accuracy will be reduced."

**Rising action:** Lien checks the inverter firmware. It's an older version with a known Modbus access limitation. Upgrading on-site today carries client risk she doesn't want to take. OPEN-EMS presents two paths: continue in reduced capability mode (peak limiting and battery control remain fully active; forecasting runs on consumption data only, without solar input) or halt and investigate. She chooses reduced capability.

**Climax:** Configuration completes. The system runs — battery is controlled, EV charging is scheduled, the peak limit is enforced using meter data. A persistent "Reduced capability" indicator remains visible in both the installer and homeowner interfaces, with a one-line explanation: "Solar production data unavailable — inverter firmware update recommended." Lien adds a note to the event log: firmware version, date, planned follow-up.

**Resolution:** She briefs the homeowner: the battery and charger are working intelligently, but the solar optimization is limited until the firmware is updated. The homeowner understands. The system runs stably. Lien schedules the firmware update for the following week. No custom workaround. No silent failure.

**Requirements this journey surfaces:**
- Device capability detection during discovery — distinguishes full vs. partial integration
- Graceful degradation: system operates in reduced mode when a device's capability is constrained
- Clear, non-alarmist warnings at setup time — actionable, not just error codes
- Persistent capability status indicators in installer and homeowner interfaces
- Event log with installer note field for documentation

---

### Journey 3 — Installer: Post-Deployment Operations Check

**Persona: Lien**, three weeks after Journey 1 deployment. Office, end of month.

**Opening scene:** The client is on the Flanders capaciteitstarief. This is billing week — the highest 15-minute consumption peak recorded this month becomes the basis for the monthly capacity charge. Lien wants to verify the system has been managing peaks correctly. She's not on-site. She connects via the VPN she configured at installation time.

**Rising action:** She opens the OPEN-EMS local dashboard. The system shows 21 days uptime, no forced restarts. Current state: battery at 62%, PV producing 2.1 kW, grid draw 0 W. She opens the event log: 47 decision events over the past seven days — battery charge/discharge cycles, one EV session that was delayed 22 minutes to avoid a near-peak window, two constraint enforcement events flagged for review. Device communication warnings are also visible — one temporary Modbus disconnect on the battery that resolved automatically after 40 seconds.

**Climax:** She checks the peak tracker. Highest 15-minute consumption this month: 3.2 kW. Configured threshold: 5.0 kW. Well within bounds. The two flagged constraint events were both correctly handled — no violations. She reads the brief decision explanations attached to each event. Everything is explainable.

**Resolution:** She closes the dashboard. Nothing to do. She moves on. This is exactly what a stable installation looks like.

*Implied support scenario: if a homeowner noticed something unusual and contacted Lien, this is the journey she would use to investigate. The event log provides the explanation before she picks up the phone.*

**Requirements this journey surfaces:**
- Uptime and stability indicator (days running, last restart)
- Real-time device state: battery SOC, PV production, grid draw
- Event log: decision events with timestamps and brief natural-language explanations
- Device communication warnings (e.g. temporary disconnects) logged and visible
- Peak tracker: current-month highest 15-min consumption vs. configured limit
- Constraint enforcement events flagged and explainable
- Dashboard accessible via local LAN; remote access via installer-configured method (VPN, port forwarding) — OPEN-EMS provides no built-in remote connectivity in v1

---

### Journey 4 — Homeowner: Normal Week (Secondary Success Path)

**Persona: Marc**, 48, Ghent. Retired teacher, not technically oriented. Solar, battery, and EV charger installed four months ago. Previously had three manufacturer apps — understood none of them. OPEN-EMS deployed by Lien.

**Opening scene:** Tuesday morning. Marc makes coffee, glances at his phone. He opens the OPEN-EMS web app (bookmarked on his home screen). Battery: 81%. PV: just starting to produce. EV: charging session scheduled for tonight at 22:30. He reads this in under ten seconds.

**Rising action:** He taps "This week." Weekly summary: two peak events avoided this month, self-consumption at 74%, three EV charging sessions completed automatically, estimated savings vs. unmanaged baseline approximately €18. He briefly considers switching strategy from "Minimize Cost" to "Prioritize EV" ahead of a long drive Friday — then sees the car is already scheduled for tonight and leaves it as-is.

**Climax:** Nothing else happens. Marc goes to work. The battery charges from solar during the afternoon. The EV session runs at 22:30 as scheduled. No notifications. No decisions required.

**Resolution:** Sunday. Marc shows his wife the summary. "Saved us about €20 this week, automatically." She says "okay." He checks once more, sees everything green, puts his phone down. He has not opened the Fronius app, the BYD app, or the Wallbox app in three weeks.

**Requirements this journey surfaces:**
- Homeowner dashboard readable in under 10 seconds: battery SOC, PV production, grid draw, next EV session
- Weekly summary: peaks avoided, self-consumption ratio, EV sessions completed, estimated savings (€)
- Active strategy display with one-tap switching
- All language non-technical — no register names, no Modbus values, no error codes

---

### Journey 5 — Homeowner: Urgent EV Override (Edge Case)

**Persona: Marc**, unexpected Wednesday evening.

**Opening scene:** Marc gets a call — he needs to drive his daughter to the airport at 06:00 Thursday. The EV is at 31%. OPEN-EMS has the next session scheduled for Thursday night. That won't work.

**Rising action:** Marc opens the app. He sees: "EV charge scheduled: Thu 23:15." He taps the single override button — "Charge EV now." No multi-step confirmation flow — a single-tap action with immediate feedback. Charging is initiated immediately, subject to system constraints. A brief notice appears below the button: "Charging now. Peak limit still active — charge rate may be adjusted if other loads are high."

**Climax:** The session runs. The decision engine monitors total grid draw in real-time. Around 21:40, the combined load from the EV charger and household consumption approaches the configured peak limit. The engine reduces the EV charge rate from 11 kW to 7 kW for 18 minutes until household load drops. Marc sees none of this — he sees "Charging" and a progress bar. The peak limit is never breached.

**Resolution:** By midnight the EV is at 82%. Marc drives to the airport at 06:00. The system automatically returns to its scheduled optimization mode the next morning. Marc did not call Lien. He did not open a second app. He tapped one button.

**Requirements this journey surfaces:**
- Single-tap override: "Charge EV now" accessible from main homeowner dashboard
- No multi-step confirmation flow — single-tap action with immediate feedback
- Brief, plain-language consequence notice (peak limit still active)
- Decision engine enforces peak constraints during manual overrides via charge rate reduction, not session cancellation
- Automatic return to scheduled optimization mode after override session completes
- Homeowner never exposed to underlying rate reduction logic — progress and status only

---

### Journey Requirements Summary

| Capability | Journeys |
|---|---|
| Device discovery — Modbus TCP, OCPP, DSMR | J1, J2 |
| Energy role assignment per device | J1 |
| Per-site constraint configuration (peak limit, battery reserve, preferred EV window) | J1 |
| Default strategy selection by installer | J1 |
| Deployment validation — connectivity and control readiness | J1 |
| Device capability detection (full vs. partial integration) | J2 |
| Graceful degradation in reduced capability mode | J2 |
| Clear, actionable setup warnings | J2 |
| Persistent capability status indicator | J2 |
| Event log: decision explanations, constraint events, installer notes | J2, J3 |
| Device communication warnings logged and visible | J3 |
| Uptime and stability indicator | J3 |
| Real-time device state (SOC, PV, grid draw) | J1, J3, J4 |
| Peak tracker (current month vs. configured limit) | J3 |
| Dashboard via local LAN; remote access installer-managed only | J3 |
| Homeowner weekly summary (peaks, self-consumption, savings) *(nice-to-have)* | J4 |
| Strategy display and one-tap switching | J4 |
| Non-technical UI language throughout | J4, J5 |
| EV schedule visibility (next session) | J4, J5 |
| Single-tap EV override — immediate feedback, no multi-step flow | J5 |
| Peak constraint enforcement during override (rate reduction) | J5 |
| Automatic return to scheduled mode post-override | J5 |

## Domain-Specific Requirements

### Compliance and Regulatory

**Belgian tariff integration (launch market):**
- Flanders capaciteitstarief: decision engine must track and act on 15-minute peak consumption windows; peak limit constraint is a first-class system parameter, not a configurable optional feature
- Wallonia incentive tariff: dynamic pricing signals must be consumable by the decision engine as input for cost-optimization decisions; tariff data source must be configurable per site
- Both tariff structures require that the system records and exposes peak consumption data for homeowner transparency and installer validation

**Data privacy (GDPR):**
- Energy consumption data constitutes personal data under GDPR; all v1 data is stored locally with no cloud transmission, which minimizes exposure
- No user accounts are transmitted to external services in v1; local-only storage is the primary GDPR mitigation
- If any remote access or telemetry is added in future versions, a formal GDPR data processing assessment is required before release

**Liability and legal framing:**
- OPEN-EMS operates as a supervisory control layer; device-level safety mechanisms (battery BMS, inverter protection circuits, charger safety functions) remain at the device and are not overridden by OPEN-EMS
- The installer is responsible for overall system configuration and constraint setup; OPEN-EMS enforces the constraints the installer defines, it does not define safe values independently
- Formal legal and insurance review is required before transitioning from pilot to commercial scaling — this is a scheduled cost, not an open question
- CE hardware compliance is not required in v1 (no dedicated hardware product)

### Technical Constraints

**Real-time control requirements:**
- Decision engine evaluation cycle target: 5–15 seconds under normal operation (well within 15-minute peak window)
- Control actions must be idempotent and safe under repeated execution due to polling-based control loop
- Device control commands (battery charge/discharge, EV charge rate adjustment) must complete within a latency that preserves system coherence — command acknowledgment and state feedback must be explicitly verified before the system assumes a control action has taken effect
- The system must remain operational and enforce constraints during periods of degraded device communication (e.g. intermittent Modbus connectivity)
- When operating with partial data, the decision engine must switch to conservative control assumptions to prevent unsafe or peak-inducing behavior

**Device protocol variance:**
- Modbus TCP register maps are inconsistently implemented across manufacturers and firmware versions; the device abstraction layer must isolate protocol-specific behavior from the decision engine
- OCPP implementations vary in compliance level across EV charger brands; the integration layer must handle non-conformant behavior gracefully
- Control command behavior (e.g. battery charge setpoint, EV charge rate cap) must be validated per device model and firmware version, and only exposed to the decision engine after passing integration validation tests — assumed behavior is not sufficient

**Safety constraint enforcement:**
- Safety constraints (peak limit, battery reserve floor, charge/discharge limits) must be enforced by the engine at all times during normal operation. Changes to constraint values are only allowed through explicit installer reconfiguration workflows that require validation before taking effect.
- Constraint violations caused by system logic constitute a critical failure (see Technical Success criteria)
- The system must fail safe: when device communication is lost, the default behavior is to transition to a safe fallback mode — stop issuing new control commands and rely on device-level safety mechanisms, while logging the degraded state

**Operational continuity:**
- The system must recover automatically from power loss, OS reboot, or network interruption without requiring manual reconfiguration
- Core decision logic must not depend on external network services — tariff data and solar forecasts improve optimization but their unavailability must not halt core operation
- All control decisions, constraint enforcement actions, and degraded-mode transitions must be logged with timestamp and concise human-readable explanations for traceability
- A watchdog mechanism must detect stalled control loops or unresponsive subsystems and trigger safe recovery (restart or fail-safe mode)
- Any automatic restart or watchdog-triggered recovery must be logged and visible to the installer

### Integration Requirements

**Device protocols (v1):**
- Modbus TCP: inverter and battery read/write; register maps maintained per supported device model
- OCPP 1.6 (minimum): EV charger session control and status
- DSMR P1 (Dutch Smart Meter Requirements): real-time grid consumption and production data from smart meter

**External data sources (v1, enhancement only):**
- Solar production forecast API: used by the forecasting layer; system operates in degraded optimization mode when unavailable
- Dynamic tariff data feed (where applicable): used for cost-optimization decisions; system falls back to time-of-use assumptions when unavailable
- Both sources are consumed by the optimization layer only; core constraint enforcement does not depend on them
- When unavailable, the system must explicitly indicate degraded optimization mode in the installer interface

### Domain-Specific Risks

| Risk | Mitigation |
|---|---|
| Device firmware update breaks Modbus register behavior | Per-device versioned register maps; integration test required before firmware version listed as supported |
| Manufacturer restricts or changes proprietary API | Prefer open protocols (Modbus TCP, OCPP) over manufacturer APIs; abstraction layer isolates impact to single device integration |
| System issues control command to device in invalid state | Per-device capability validation before command dispatch; command acknowledgment required; stale-state protection |
| Homeowner override causes peak breach | Override actions always evaluated against active constraints; charge rate reduction preferred over session cancellation |
| Local data loss on hardware failure | Local time-series data store with periodic snapshots; loss of historical data does not affect real-time operation |
| System operates on stale consumption data during meter disconnect | Meter disconnect logged as device communication warning; engine falls back to conservative defaults |
| Control loop stalls silently | Watchdog detects stalled subsystems and triggers restart or fail-safe mode; stall event logged and visible to installer |

## Edge Application + IoT Orchestration + Web Application Specific Requirements

### Project-Type Overview

OPEN-EMS is a Linux-based edge application that runs as a persistent local service on installer-provided hardware. It orchestrates energy devices via network protocols, serves a role-based web interface to two distinct user types, and operates autonomously between human interactions. The system is deployed once per site, is not cloud-connected in v1, and must operate reliably over extended periods without human supervision.

### Hardware Requirements

**Minimum recommended specification (v1):**

| Component | Requirement |
|---|---|
| Compute | Raspberry Pi 4 (4 GB RAM) or equivalent; Intel NUC or existing Linux server supported |
| RAM | 4 GB minimum |
| Storage | SSD strongly recommended for production; microSD acceptable for development/testing only |
| Network | Wired Ethernet strongly recommended; Wi-Fi not recommended for production |
| OS | Linux-based (Debian/Ubuntu variants preferred for package ecosystem) |
| Power | UPS or stable power supply recommended for production deployments |

**Rationale:** The 5–15 second decision cycle does not require high compute, but persistent local data storage, time-series logging, web UI serving, and multi-protocol device communication require stable I/O and reliable storage. MicroSD card failure is a known failure mode for always-on Raspberry Pi deployments.

**Hardware responsibility:** Hardware procurement and setup is the installer's responsibility. OPEN-EMS provides installation documentation; it does not ship hardware in v1.

### Connectivity and Protocol Architecture

**Device communication (outbound from OPEN-EMS to devices):**
- Modbus TCP: polling-based read/write to inverters and batteries; each supported device model has a versioned register map maintained in the integration layer
- OCPP 1.6: OPEN-EMS acts as OCPP Central System; EV chargers connect as OCPP Charge Points; session control via OCPP-supported commands such as RemoteStartTransaction, RemoteStopTransaction, and charging profile / limit mechanisms where supported by the charger
- DSMR P1: serial or TCP-connected smart meter; read-only real-time energy consumption and production data

**All device communication is on the local network.** OPEN-EMS does not initiate outbound connections to device manufacturer clouds in v1.

**External data (inbound, optional):**
- Solar production forecast: HTTP API call to external forecast provider (e.g. Solcast or equivalent); called on a scheduled interval; failure is graceful
- Dynamic tariff data: HTTP API or local file-based feed; configurable per site; failure triggers fallback to static time-of-use assumptions

**No built-in remote access:** OPEN-EMS does not provide or manage remote connectivity. Installer-managed VPN, port forwarding, or secure tunnel is the supported pattern for remote access. This is explicitly out of OPEN-EMS scope in v1.

### Security Model

**Authentication:**
- Local authentication only — no cloud identity provider, no external account dependency
- Two roles with separate credentials:
  - **Installer/Admin:** full access — device configuration, constraint setup, validation, system logs, platform updates, homeowner account management
  - **Homeowner/User:** limited access — strategy selection, current status, weekly summary, temporary EV override
- Credentials stored locally with appropriate hashing (bcrypt or equivalent); no plaintext storage
- Installer creates and resets homeowner credentials during initial setup and on-site visits

**Session management:**
- Web interface sessions are token-based with configurable expiry
- No persistent login assumed on homeowner devices — session should survive reasonable inactivity but not indefinitely
- Installer sessions should require re-authentication after extended inactivity given the elevated access level

**Network security:**
- HTTPS strongly recommended for the web interface even on local networks; self-signed certificate acceptable for v1. If HTTP is supported during early development, it must be explicitly marked as development-only.
- No open unauthenticated endpoints; all API routes require valid session token
- Local network is not treated as a trusted boundary — authentication is always required

**Configuration backup and recovery:**
- Installer/Admin must be able to export and restore local configuration backups
- Backup export must not expose plaintext credentials or secrets
- Secrets must be stored separately from human-readable configuration where practical

**Explicitly out of scope for v1:** multi-factor authentication, SSO, LDAP/AD integration, certificate rotation automation.

### Web Interface Architecture

**Single web application with role-based views:**
- One locally-served web application; one running service; one URL
- Role is determined at login; view and available actions are constrained by role
- No UI elements from the installer view leak into the homeowner view

**Installer/Admin view — access to:**
- Device discovery and role assignment
- Per-site constraint configuration and validation
- System status, uptime, and connection health
- Event log and decision audit trail
- Platform update management
- Homeowner account creation and credential reset
- Degraded capability and warning indicators

**Homeowner/User view — access to:**
- Energy strategy selection (Minimize Cost / Maximize Self-Consumption / Prioritize EV)
- Current system state: battery SOC, PV production, grid draw, next EV session
- Weekly summary: peaks avoided, self-consumption ratio, estimated savings
- Temporary override: "Charge EV now"
- No device-level data, no configuration parameters, no system logs

**Interface delivery:**
- Server-side rendered or lightweight SPA — no heavy frontend framework required; simplicity preferred over developer experience features
- Mobile-responsive; no native app in v1
- Served over HTTPS from the local OPEN-EMS runtime
- Supported browsers: current versions of Chrome, Edge, Safari, and Firefox
- Interface must remain usable on mobile screen sizes, but desktop/tablet is the preferred installer setup experience

### Update Mechanism

**Model: semi-manual with installer approval**

- OPEN-EMS checks for available updates on demand or on a scheduled interval
- Available updates are surfaced in the installer/admin interface with version number and brief release notes
- Installer explicitly triggers the update — no automatic unattended updates in v1
- Update process must preserve all site configuration and local data; data migration is part of the update process where required
- System displays update status and confirms completion; installer confirms the system returned to operational state post-update
- Rollback capability: desirable but not mandatory for v1; post-update recovery path must be documented even if automated rollback is not implemented

**Rationale:** Automatic updates on a system actively controlling energy devices are unacceptable in v1. Installer approval keeps responsibility and control unambiguous. An update that silently changes constraint behavior or breaks a device integration must be a visible, installer-approved event.

### Implementation Considerations

- The OPEN-EMS runtime, web server, decision engine, and device integration layer all run as a single local service (or tightly coupled set of services) on the edge hardware — no distributed deployment in v1
- Systemd service management recommended for process supervision and auto-start on boot
- Local persistent storage for configuration, energy history, and event logs. Database choice is deferred to architecture phase, but v1 should prioritize simplicity, backupability, and low maintenance over high-scale performance.
- Configuration stored in a structured local format (YAML or equivalent) — human-readable and recoverable from backup
- Containerized deployment should be supported in v1 where practical, but the system must also support direct host installation. Final packaging decision should be made in the architecture phase.

## Project Scoping

### Strategy and Philosophy

**Approach:** Phased delivery — v1 establishes a reliable, field-deployable single-site system; v2 adds fleet management and remote capabilities; long-term vision is defined as a pointer only and does not influence v1 architecture beyond necessary extensibility.

**v1 philosophy:** Reliability-first platform MVP. v1 does not need to be feature-complete. It needs to prove that an installer can deploy a stable, constraint-respecting energy management system and trust it to operate without intervention. Every scoping decision is evaluated against that bar.

**Team:** 3 people — 1–2 backend engineers, 1 integration-focused developer. 6–9 month timeline.

**Guiding scoping principle:** If timeline pressure occurs, reduce device compatibility breadth before reducing system reliability, safety constraints, or core control functionality. A system that controls fewer devices correctly is better than a system that attempts more devices unreliably.

---

### v1 Must-Have Capabilities

All five user journeys are fully supported by this capability set.

**Device layer:**
- Device discovery via Modbus TCP, OCPP 1.6, and DSMR P1
- Energy role assignment per device (inverter, battery, EV charger, grid meter)
- Device capability detection — full vs. partial integration, with graceful degradation
- Per-device versioned register maps and capability profiles (integration-validated before listing as supported)

**Decision engine:**
- Rule-based, deterministic engine: peak limiting, battery charge/discharge control, EV charging session management and dynamic charge rate control (where supported by the charger)
- Installer-defined safety constraints enforced unconditionally (peak limit, battery reserve floor, EV charging window as a preference constraint — can be temporarily overridden by homeowner actions, but still bounded by safety constraints)
- Conservative fallback mode when device communication is degraded or data is partial
- Command acknowledgment verification before assuming control action has taken effect
- Idempotent control actions safe under repeated execution
- The system must never silently ignore a constraint, command failure, or degraded state; all such conditions must be logged and surfaced in the installer interface

**Installer interface (Admin view):**
- Device setup flow: discovery, role assignment, constraint configuration, deployment validation
- Basic peak tracking: current-month highest 15-minute consumption vs. configured limit — numeric display or log-based view (not a graph UI in v1)
- System health and status indicator: overall system state (healthy / degraded / error), per-device connectivity status, degraded mode indication
- Event log: control decisions, constraint enforcement events, degraded-mode transitions — timestamped with concise human-readable explanations
- Homeowner account creation and credential reset
- Platform update management (semi-manual, installer-approved)

**Homeowner interface (User view):**
- Strategy selection: Minimize Cost / Maximize Self-Consumption / Prioritize EV
- Real-time system state: battery SOC, PV production, grid draw, next EV session
- Single-tap EV override: "Charge EV now" — immediate, with plain-language constraint notice
- Basic degraded-mode indication when system is operating with reduced capabilities (plain-language, non-technical)

**Operational infrastructure:**
- Role-based local authentication: Installer/Admin and Homeowner/User — separate credentials, locally stored
- Watchdog: detects stalled control loops, triggers restart or fail-safe mode, logs recovery events
- Fail-safe mode: stops issuing control commands on device communication loss, relies on device-level safety, logs degraded state
- Configuration backup/export: reliable export and restore of site configuration; CLI-based or minimal UI-based implementation acceptable in v1, as long as the process is documented and repeatable by installers; backup must not expose plaintext credentials
- Automatic recovery from reboot or network interruption without manual reconfiguration
- All control decisions and system events logged with timestamp and explanation
- Installation process must be reproducible across sites (documented setup steps, version consistency, and minimal environment-specific configuration)

---

### v1 Nice-to-Have Capabilities

Deliver if time permits within the 6–9 month window. None of these are required for the system to be trusted and field-deployable.

- **Homeowner weekly summary:** peaks avoided, self-consumption ratio, estimated savings (€)
- **Solar forecast integration:** improves optimization quality; system runs correctly without it
- **Dynamic tariff feed:** improves cost-optimization decisions; system falls back to static time-of-use assumptions when unavailable
- **Containerized deployment option (Docker/Compose):** simplifies installer repeatability; direct host installation is fully supported in v1 and is the fallback

---

### Risk Mitigation Strategy

**Technical risk — integration validation (primary):**
Device control across 7+ supported models and multiple firmware versions is the highest-probability source of schedule slip. Reading data is tractable; writing control commands across inconsistent protocol implementations is where real-world testing reveals edge cases.

Mitigation:
- Integration validation (including read + write/control verification) is a gating requirement before any device is listed as supported — assumed behavior is explicitly not sufficient
- If timeline pressure builds, reduce the supported device set (e.g. prioritize the most commonly deployed Belgian combinations) rather than shipping unvalidated integrations
- Never cut or simplify core safety constraint enforcement, fail-safe logic, or the decision engine to recover timeline

**Market risk — installer adoption:**
v1 is validated through a controlled pilot (5–10 installers, 1–2 installations each). The risk is that pilot feedback reveals a fundamental UX or workflow mismatch before wider rollout.

Mitigation:
- Pilot installers are selected from personal/professional network — known quantities with direct communication
- Direct support involvement during first deployments allows real-time issue detection
- Success criteria are explicitly defined (see Success Criteria section) — pilot outcome is measurable, not subjective

**Resource risk — small team, broad integration surface:**
A team of 3 covering backend, integration, and frontend across 6–9 months is achievable but leaves no buffer for scope expansion.

Mitigation:
- Device set reduction is the primary lever if resource pressure emerges
- Nice-to-have features are explicitly sequenced as post-launch — no ambiguity about what can be cut
- Architecture decisions (single local service, deferred containerization, simple storage layer) are deliberately chosen to minimize operational complexity for the team

## Functional Requirements

### Device Integration and Management

- **FR1:** The installer can discover and connect to energy devices on the local network via Modbus TCP and OCPP 1.6, and connect to DSMR P1 smart meters as a real-time grid data source
- **FR2:** The installer can assign an energy role (inverter, battery, EV charger, grid meter) to each discovered device
- **FR3:** The system can detect whether a discovered device supports full or partial integration capability
- **FR4:** The system can operate in reduced-capability mode when a device's integration is partial, while continuing to enforce safety constraints using available data
- **FR5:** The system maintains a versioned capability profile per supported device model and firmware version, validated through read and write/control testing before listing as supported
- **FR6:** The installer can view the integration capability status and any active limitations for each connected device
- **FR6b:** The system does not expose unsupported or unvalidated device capabilities to the decision engine

### Energy Control and Decision Engine

- **FR7:** The system continuously evaluates energy state on a fixed cycle and issues control actions within installer-defined safety boundaries
- **FR8:** The system enforces a configurable peak consumption limit by controlling battery discharge and EV charge rate
- **FR9:** The system controls battery charge and discharge state in accordance with installer-defined constraints and the active energy strategy
- **FR10:** The system manages EV charging session start, stop, and — where supported by the charger — dynamic charge rate adjustment
- **FR11:** The system applies a homeowner-selected energy strategy (Minimize Cost / Maximize Self-Consumption / Prioritize EV) to determine control priorities
- **FR11b:** The system resolves conflicting energy demands (e.g. EV charging vs. battery reserve vs. peak limit) using a deterministic priority model derived from the active strategy and installer-defined constraints
- **FR12:** The system respects a configurable EV charging window preference while permitting homeowner overrides bounded by active safety constraints
- **FR13:** The system transitions to a conservative fallback mode in which control actions are reduced or stopped based on available data while maintaining safety constraints, when device communication is degraded or energy data is incomplete
- **FR14:** The system verifies command acknowledgment before assuming a control action has taken effect
- **FR15:** The system never silently ignores a constraint violation, command failure, or degraded state — all such conditions are logged and surfaced to the installer

### Installer Configuration and Management

- **FR16:** The installer can configure per-site safety constraints (peak consumption limit, battery reserve floor) and the preferred EV charging window, which the decision engine treats as a scheduling preference rather than a hard limit
- **FR17:** The installer can validate device communication and constraint configuration before completing deployment
- **FR18:** The installer can view a system health indicator showing overall system status (healthy / degraded / error) and per-device connectivity state
- **FR19:** The installer can view current-month peak consumption data (highest 15-minute interval recorded) against the configured limit
- **FR20:** The installer can access a timestamped event log of control decisions, constraint enforcement events, command failures, degraded-mode transitions, and recovery events
- **FR21:** The installer can add documentation notes to the event log
- **FR22:** The installer can check for available platform updates and trigger an update with explicit approval, with release notes visible before confirming
- **FR23:** The installer can create and reset homeowner credentials
- **FR24:** The installer can export a site configuration backup and restore it to the same or a replacement system with compatible hardware and supported device integrations, without exposing plaintext credentials

### Homeowner Energy Interface

- **FR25:** The homeowner can select an energy strategy from three options: Minimize Cost, Maximize Self-Consumption, or Prioritize EV
- **FR26:** The homeowner can view the current system state: battery charge level, solar production, and grid draw
- **FR27:** The homeowner can view the next scheduled EV charging session
- **FR28:** The homeowner can trigger an immediate EV charging session that overrides scheduled timing, while remaining subject to active safety constraints and dynamic charge rate adjustments if required
- **FR29:** The homeowner can view a simple system status indicator (e.g. "Running normally" / "Limited functionality") providing a plain-language indication when the system is operating with reduced capability, with no technical detail exposed
- **FR30:** *(nice-to-have)* The homeowner can view a weekly energy summary: peaks avoided, self-consumption ratio, and estimated cost savings

### System Observability and Reliability

- **FR31:** The system automatically recovers to normal operation after a reboot or temporary network interruption without manual intervention
- **FR32:** The system detects stalled control loops or unresponsive subsystems and triggers automatic recovery (service restart or transition to fail-safe mode) depending on failure type
- **FR33:** The system logs all automatic recovery and watchdog-triggered events with timestamps, visible to the installer
- **FR34:** The system transitions to fail-safe mode (ceases issuing control commands; relies on device-level safety) when device communication is lost, and logs the degraded state

### Access Control and Security

- **FR35:** Users authenticate with role-based local credentials before accessing any system functionality — Installer/Admin and Homeowner/User roles are distinct
- **FR36:** The Installer/Admin role has access to device configuration, constraint management, system logs, update management, and homeowner account management
- **FR37:** The Homeowner/User role has access only to strategy selection, system status, EV override, and degraded-mode indication
- **FR38:** The system enforces session expiry for both roles, with Installer/Admin sessions requiring re-authentication after extended inactivity
- **FR39:** The system serves the web interface over HTTPS; any HTTP access during development is explicitly marked as development-only

### External Data Integration

- **FR40:** *(nice-to-have)* The system can retrieve solar production forecasts from a configurable external API and use them to improve optimization decisions
- **FR41:** *(nice-to-have)* The system can consume dynamic tariff data from a configurable external feed and use it for cost-optimization decisions
- **FR42:** The installer can view when the optimization layer is operating in degraded mode due to unavailability of external forecast or tariff data

*(FR42 is must-have — installer visibility into degraded optimization is required for trust, even when the external integrations themselves are nice-to-have.)*

## Non-Functional Requirements

### Performance

- The decision engine evaluation cycle must execute at a target interval of 5–15 seconds under normal conditions; under degraded device connectivity, the cycle may extend but must complete within 60 seconds before triggering watchdog recovery
- Control actions (e.g. EV charge rate adjustment, battery dispatch) must be applied within 30 seconds from decision to observable device state change under normal conditions
- Web interface pages must load and be interactive within 3 seconds on the minimum target hardware (Raspberry Pi 4 or equivalent) under normal local network conditions
- Device state data displayed in the installer and homeowner interfaces must reflect actual device state within 30 seconds of a confirmed device state change
- Event log queries covering the past 30 days must return results within 5 seconds

### Reliability

- The system must operate without unhandled crashes or forced restarts for a minimum of 30 consecutive days under normal operating conditions
- Under no circumstances may the system violate configured safety constraints due to internal logic errors
- After a clean or unexpected reboot (e.g. power loss), the system must return to full operational state — all device connections re-established, decision engine running — within 2 minutes
- After a temporary network interruption, the system must reconnect to devices and resume normal control operation within 60 seconds of network restoration
- Stalled control loops must be detected and watchdog recovery triggered within 2 consecutive missed evaluation cycles

### Security

- Credentials must be stored using bcrypt or equivalent one-way hashing; no plaintext credentials stored at rest
- Configuration backups must not contain plaintext credentials or secrets; secrets must be excluded from or encrypted within backup exports
- All web interface traffic must use HTTPS in production; self-signed certificates are acceptable for v1
- Session tokens must use a minimum of 128 bits of entropy using a cryptographically secure random generator
- Installer/Admin sessions must expire after a configurable inactivity period (recommended default: 4 hours)
- Homeowner/User sessions must expire after a configurable inactivity period (recommended default: 7–30 days depending on installer configuration)
- Local energy data must not be transmitted to external services in v1 except for explicitly installer-configured optional API calls (solar forecast, tariff feed); all such calls must use HTTPS

### Integration

- Modbus TCP and OCPP connection attempts must time out within 10 seconds; timeouts must be handled gracefully without blocking the control loop
- Failed control commands may be retried a limited number of times (default: 2), provided retries do not risk violating safety constraints or causing oscillating behavior
- DSMR P1 grid data older than 60 seconds must be treated as stale; stale meter data must cause the decision engine to switch to conservative control assumptions
- External API calls (solar forecast, dynamic tariff) must time out within 10 seconds; timeout must be handled gracefully without affecting core control operation

### Data and Storage

- Energy state history must be retained locally for a minimum of 12 months
- Event log entries must be retained locally for a minimum of 90 days
- Event log entries related to failures, constraint enforcement, and recovery events must not be pruned below the minimum retention period under any circumstances
- Configuration and event log data must be written in a way that prevents corruption in case of sudden power loss (e.g. atomic writes or journaling)
- Platform updates must preserve all site configuration and energy history without data loss or silent migration failure
- No energy consumption data or personal data may be transmitted externally in v1 outside of explicitly configured optional API integrations

### Maintainability and Deployability

- Platform installation on a fresh Raspberry Pi 4 or equivalent must be completable in under 30 minutes following the documented installation procedure
- Site configuration export and restore on compatible hardware must be completable in under 10 minutes following the documented restore procedure
- The installation process must be reproducible across sites using the same documented steps and package versions — no site-specific system configuration required beyond device onboarding and constraint setup

### Language and Accessibility

- **NFR-L1**: The system UI shall be English-only in v1. No multilingual support is required for the initial release.
- **NFR-L2**: In v2, the system shall support a multilingual UI (Dutch and French) implemented via a translation framework (e.g. GNU gettext or equivalent). All user-visible strings in v1 must be defined in a way that does not preclude later extraction and translation.
- **NFR-A1**: The web interface must meet WCAG 2.1 Level AA accessibility standards. Compliance is verified through automated tooling (axe-core or equivalent) supplemented by manual keyboard-navigation and colour-contrast checks on all primary installer and homeowner flows.
