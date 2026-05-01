---
title: "PRFAQ Distillate: OPEN-EMS"
type: llm-distillate
source: "prfaq-OPEN-EMS.md"
created: "2026-04-29"
purpose: "Token-efficient context for downstream PRD creation"
---

## Product Identity

- **Product name:** OPEN-EMS
- **Concept type:** Commercial open-core product (open-source core + commercial managed tier)
- **One-line:** Vendor-agnostic, local-first energy orchestration platform for residential energy installers
- **Launch market:** Belgium (Flanders + Wallonia); broader European expansion intended
- **Tagline direction:** "Makes mixed-brand energy systems work as one intelligent system — without vendor lock-in"

---

## Customers

- **Primary customer (buyer/deployer):** Residential energy installer — selects, installs, and deploys energy systems for clients; manages a portfolio of 5–50 installations/year; decision-maker for platform adoption
- **Secondary user (daily operator):** Homeowner — sets energy strategy (cost, self-consumption, EV priority), sees outcomes, does not manage technical configuration
- **Explicitly not the primary:** DIY/hobbyist home energy users; large enterprise utilities (that is gridX's market)
- **Rejected framing:** "Advanced home energy users" as co-primary — they are daily operators, not buyers

---

## Problem

- Residential energy systems typically combine hardware from 3–4 manufacturers (inverter, battery, EV charger, smart meter), each with separate apps and closed ecosystems
- Installers lose significant time building fragile custom integrations; support burden falls on them when firmware updates break compatibility
- Homeowners with €15,000+ in hardware investment often see no measurable bill reduction because devices are not coordinated
- Current workarounds (Home Assistant + EVCC) require deep technical expertise, custom per-installation configuration, and ongoing maintenance — not suitable for professional deployment at scale
- Belgium-specific pressure: Flanders capaciteitstarief (live 2023) bills on highest 15-min peak per month; Wallonia incentive tariff (live Jan 2026) makes dynamic pricing the default for digital meter holders

---

## Solution

- **Architecture:** Local-first runtime on standard hardware (Raspberry Pi, NUC, existing home server); optional dedicated hardware planned but not in v1
- **Device model:** Role-based abstraction — devices are assigned energy roles (inverter, battery, EV charger, grid meter), not treated as generic smart home entities; enables consistent logic across installations
- **Decision engine:** Rule-based core (deterministic, conservative by default) + forecast-driven optimization layer using solar production forecasts, dynamic tariff data, and learned consumption patterns based on historical usage data
- **Control philosophy:** Forecasting improves optimization but never overrides hard limits; safety boundaries (peak ceiling, battery reserve minimum, fallback strategies) are set per-site by installers and cannot be overridden by the optimization layer
- **Offline capability:** All core decisions run locally; no cloud dependency for energy control
- **Conflict resolution:** Deterministic priority model — system constraints > optimization goals > convenience preferences; end-user overrides permitted only within installer-defined boundaries

---

## Competitive Positioning

- **gridX (XENON):** Closest architectural analog — vendor-agnostic, installer-facing, multi-brand. Targets utilities and large OEMs; not accessible to independent installers at SME price points. OPEN-EMS is the SME-accessible version of this model.
- **EVCC:** Open-source, strong at EV/solar charging. Does not handle battery dispatch, heating, or full load management. No installer fleet view. Sponsor token required per instance.
- **Home Assistant:** Broad general-purpose automation. Fully DIY. No standardized deployment model, no commercial support path, not installer-deployable at scale. HA + EVCC is the current workaround; OPEN-EMS is the professional alternative, not a replacement.
- **SolarEdge / Fronius:** Deep within their own ecosystems; proprietary lock-in is the core problem OPEN-EMS solves.
- **Loxone:** Installer-only, closed-source, €2–5K hardware entry cost; not open to end-users.
- **Tibber:** Not yet operating in Belgium; retailer, not an orchestration engine.
- **Positioning statement (use verbatim):** "Not more powerful than a fully custom Home Assistant setup — but significantly more reliable, scalable, and practical for installers deploying across tens of projects."

---

## Business Model

- **Open-core:** Core platform is open-source, self-hostable, no licensing fees
- **Commercial tier targets:** Professional installers managing multiple sites
  - Remote fleet management dashboard
  - Multi-site monitoring and remote configuration updates
  - Professional support with next-business-day SLA (standard); faster response via SLA contract
  - Pre-configured hardware bundles (planned)
  - Advanced optimization modules (planned)
- **Assumed conversion rates:** Self-hosters: 5–10% to paid; professional installers at multi-site scale: 20–40%
- **Viability dependency:** Model fails if professional installer segment is not captured; self-hoster conversion is expected to be low and does not drive viability
- **Pricing:** TBD at launch; designed to scale per installation / per managed site; accessible to independent professionals, not just enterprise players
- **RISK:** Pricing must be defined and ready at commercial launch — the current press release CTA implies immediate purchasability; if pricing is not ready, CTA must route to waitlist/early access instead

---

## Go-to-Market

- **Phase 1 — Pilots (months 1–3 post-v1):** 5–10 installers from existing personal/professional network in Belgian EV, PV, and installation sector; treated as controlled validation deployments with direct support; not paying customers
- **Phase 2 — Reference cases (months 3–6):** Convert pilots to documented reference cases with measurable outcomes (peak reduction %, self-consumption rate, installer time saved per deployment); primary sales asset
- **Phase 3 — Referral expansion (months 4–6):** 10–15 additional installers via referrals and targeted outreach through installer associations, trade events, regional networks
- **GAP flagged:** No plan beyond first ~20 installers; PRD should define what drives scale from 20 → 100 (installer association partnerships, certification program, partner channel, etc.)
- **Strategic window:** First 12–18 months are critical before competitor stickiness develops; speed of installer adoption is the top strategic priority

---

## Competitive Moat

- **Primary moat (durable):** Installer workflow integration — network-effect argument; once platform is embedded in an installer's standard deployment workflow, switching costs are real (configuration templates, client documentation, troubleshooting knowledge all invested)
- **Secondary moat:** Accumulated real-world deployment data — edge cases, device quirks, firmware behavior patterns; cannot be purchased
- **Tertiary moat:** Community-contributed integrations — pace of device support outpaces any single competitor team; also creates indirect leverage against manufacturer API restrictions
- **NOT a moat:** Domain model architecture, decision engine logic, device integration library — all replicable by well-funded competitor
- **Vulnerability window:** Pre-stickiness (first 12–18 months); must win installer relationships before a competitor does

---

## v1 Scope

**In scope:**
- Local runtime on standard hardware (Raspberry Pi, NUC, home server)
- Core device abstraction layer with initial supported brand set
- Rule-based decision engine + basic forecasting
- Installer configuration interface: device discovery, role assignment, strategy selection
- Basic local monitoring

**Out of scope for v1:**
- Full fleet management platform (basic monitoring only — press release describes full fleet management as a commercial tier feature; PRD must define v1 vs v2 boundary clearly)
- Advanced forecasting models
- End-user consumer app
- Extended device library beyond launch set
- Home Assistant integration layer

**Timeline:** 6–9 months, team of 3 (1–2 backend engineers + 1 integration-focused developer)
**Primary risk:** Integration validation (device control reliability across firmware versions), not feature development

---

## Initial Supported Devices (Launch)

- Inverters: Fronius Gen24 series, Huawei SUN2000 series, Growatt (selected hybrid models)
- Batteries: BYD HVS and HVM systems
- EV chargers: Wallbox Pulsar series, go-e Charger series
- Smart meters: DSMR-compatible meters
- Protocol coverage: Modbus TCP and OCPP (broad additional hardware)
- Unsupported devices: protocol-compatible interface, community integration request, or monitoring mode

---

## Regulatory and Liability

- Legal framing: supervisory control layer, not safety-critical controller; device-level safety mechanisms remain at device; installer responsible for system configuration
- **Trigger for formal legal/insurance review:** Before transitioning from pilot to commercial scaling (post 5–10 pilot deployments)
- Required before commercial scaling: ToS defining installer responsibility, liability coverage for software-caused damage
- CE compliance: required only if dedicated hardware product introduced
- This is a deferred cost, not a deferred risk; cost is manageable; risk is entering commercial scale without addressing it

---

## Key UX Requirements Surfaced (PRD inputs)

- Priority/conflict model requires configuration UI where installers set per-site constraints and homeowners work within them — non-trivial UX design problem
- Fleet/remote management dashboard is the primary commercial differentiator — must be production-quality at commercial launch for professional credibility
- End-user experience is underdeveloped in PRFAQ — PRD must define the homeowner daily interface, strategy selection UX, and what the experience is during the forecasting learning period
- SLA support infrastructure must be operationally ready before commercial scaling

---

## Pre-PRD Alignment Decisions (Confirmed 2026-04-29)

### Homeowner UX

- **Model:** Strategy-driven, not device-driven. Homeowner selects behavior; installer defines safe operating boundaries. System executes and optimizes within those bounds autonomously.
- **Design principle:** Clarity over control. Interface shows what the system is doing, why it made decisions, and what impact it had on cost and consumption. No device management, no technical parameters.
- **Confirmed user actions:**
  - Select energy strategy: "Minimize Cost" / "Maximize Self-Consumption" / "Prioritize EV Charging"
  - Temporary override: "Charge EV now"
  - View current system state: battery level, PV production, grid usage
  - View weekly summary: peaks avoided, estimated savings (€)
  - Toggle optional behavior: e.g., allow grid charging at low tariff (within installer-defined limits)
- **Interface:** Web-based, mobile-responsive; no native mobile app in v1

### v1 Scope (Confirmed)

**Included:**
- Local runtime (Raspberry Pi / NUC / Linux host)
- Core device abstraction layer (inverter, battery, EV charger, meter as roles)
- Initial device support: Fronius Gen24, Huawei SUN2000, Growatt hybrid, BYD HVS/HVM, Wallbox Pulsar, go-e Charger, DSMR meters + Modbus TCP / OCPP
- Basic rule-based decision engine (peak limiting, energy balancing, battery control, EV scheduling)
- Simple forecast integration (PV production + consumption trends)
- Installer configuration flow (device discovery, role assignment, constraint setup)
- Basic homeowner UI (strategy selection + current status + weekly summary)
- Local monitoring dashboard (single installation view)

**Excluded from v1:**
- Multi-site / fleet management
- Cloud-based remote management
- Advanced AI/ML forecasting models
- Large-scale device compatibility expansion
- Full mobile app (iOS/Android)
- Deep Home Assistant replacement features
- Complex user configuration or automation builder
- Enterprise / commercial energy features

**Rationale:** v1 proves that a stable, vendor-agnostic, local energy control system works reliably in real installations. Goal is trust and repeatability, not feature completeness. Every included feature directly supports installer deployment and system stability.

### Go-To-Market (First 6 Months, Confirmed)

**Steps:**
1. Identify 5–10 trusted installers via personal network (EV/PV/installation sector, Belgium)
2. Direct approach with pilot program: early access + hands-on support
3. Deploy 1–2 real installations per installer (supported devices only)
4. Provide direct onboarding (remote + on-site where needed)
5. Monitor closely, fix issues in real time
6. Document results: installation time, system stability, energy outcomes
7. Convert successful pilots to reference cases
8. Expand to 10–20 installers via referrals and targeted outreach (LinkedIn, industry contacts, local installer networks)

**Pilot structure:** 1–2 installations per installer; direct support during install; structured feedback loop post-deployment; controlled to supported device set only

**Success criteria before scaling:**
- 20–50 stable active installations
- 5–10 installers using the platform repeatedly (not one-off)
- Measurable reduction in installation complexity vs previous method
- Reduced installer support calls post-deployment
- Qualitative signal: "I would use this again" from majority of pilot group

---

## Open Questions for PRD

1. What does the homeowner-facing interface actually look like? How does it differ from manufacturer apps? What is the experience during the system's learning period?
2. What exactly is "fleet management" in v1 (basic monitoring) vs v2 (full remote management)? The press release describes v2 features; v1 delivers v1. This gap must be closed before launch.
3. What is the pricing structure for the commercial tier? Must be ready at launch if the press release CTA implies purchasability.
4. What is the go-to-market strategy beyond the first 20 installers?
5. What early-warning mechanisms exist for detecting manufacturer API changes before they break production integrations?

---

## Verdict Summary

- **Forged in steel:** Market gap, primary customer definition, competitive positioning (honest HA framing), architecture philosophy (local-first, conservative), Belgium market context, liability framing
- **Needs more heat:** End-user UX, forecasting learning period UX, v1 vs v2 fleet management boundary
- **Cracks:** Pricing deferred but CTA is live at launch; go-to-market stops at 20 installers; no manufacturer threat early-warning system
- **Call:** Move to PRD. Three items above are the first questions PRD should answer, not blockers for starting.
