---
title: "PRFAQ: OPEN-EMS"
status: "complete"
created: "2026-04-29"
updated: "2026-04-29"
stage: 5
inputs: []
---

# OPEN-EMS Makes Mixed-Brand Energy Systems Work as One Intelligent System — Without Vendor Lock-In

## Residential energy installers can now configure, deploy, and remotely manage complete multi-brand energy systems using a single open platform — while their clients automatically optimize for lower bills, peak avoidance, and smarter EV charging.

**Ghent, Belgium — 2026** — OPEN-EMS today announces an open-core energy orchestration platform built for residential energy installers and the homes they power. Installers can now deploy solar inverters, home batteries, EV chargers, smart meters, and dynamic energy tariffs from different manufacturers as a single, intelligent system — without writing custom code, without betting on a single hardware brand, and without constantly fielding support calls when devices lose compatibility after updates.

The growing complexity of residential energy systems has outpaced the tools available to the people who install them. A typical home today has hardware from three or four different manufacturers — each with its own app, its own cloud, and its own idea of what "integration" means. Installers spend hours building fragile workarounds to make these systems function together. When something breaks, it's always their phone ringing. And the homeowner sitting on a €15,000 investment in solar and batteries still doesn't know why their electricity bill hasn't dropped the way they were promised.

With the rapid adoption of EVs, home batteries, and dynamic energy tariffs, the need for coordinated energy systems has never been greater — yet the tools available to installers have not kept pace.

OPEN-EMS is built to solve this. Installers configure each installation once using a structured, role-based device model — the platform understands that a Fronius inverter, a BYD battery, and a Wallbox EV charger are not just "smart devices" but energy roles in a system with defined responsibilities. From that point, OPEN-EMS takes over: a local-first decision engine continuously balances energy flows, limits peaks, schedules battery charge cycles, and times EV charging based on solar production forecasts, dynamic tariff data, and continuously learned consumption patterns based on historical usage data. The homeowner sets a strategy — minimize cost, maximize self-consumption, or prioritize EV charging — and the system executes it, making increasingly accurate decisions as it learns the home's unique behavior over time.

> "The energy transition is happening at the home level, but the tools stopped at the device level. OPEN-EMS is built to close that gap — giving installers a reliable, professional platform and giving homeowners the intelligent system they thought they were buying."
> — Jordan, Founder, OPEN-EMS

### How It Works

An installer receives a new project: a home with a Fronius Gen24 inverter, a BYD HVM battery, a Wallbox Pulsar EV charger, and a digital meter on a Flanders capacity tariff. Today, making these work together intelligently takes hours of configuration across four separate apps, custom automations in Home Assistant, and ongoing maintenance when any device updates.

With OPEN-EMS, the installer opens the configuration interface, assigns each device its role in the energy system, sets the homeowner's optimization strategy, and deploys. The platform runs locally on standard hardware such as a Raspberry Pi, NUC, or existing home server — with optional dedicated hardware planned. No cloud dependency for core decisions — ensuring predictable behavior and full control for both installer and homeowner. The installer can monitor the system remotely, push configuration updates, and manage their full portfolio of installations from a single dashboard.

The homeowner sees a clean interface showing their energy flows, current strategy, and what the system optimized over the past week. When Flanders bills them on their highest 15-minute consumption peak of the month, OPEN-EMS has already been watching that number — and acting on it.

> "Before this, every installation was a bit of a custom project. Different apps, different systems, and things breaking after updates. With OPEN-EMS, I finally have one system I can rely on across all my installs."
> — Independent Energy Installer, Belgium

### Getting Started

OPEN-EMS is available as an open-core platform. The core system is open-source — installers and developers can self-host, contribute integrations, and build on the platform without licensing fees. A commercial tier provides remote fleet management, professional support SLAs, pre-configured hardware bundles, and advanced optimization modules for installers managing multiple deployments.

Visit [open-ems.io] to access the platform, documentation, and installer onboarding.

---

## Customer FAQ

### Q: What devices and brands are actually supported at launch?

OPEN-EMS launches with support for the most commonly installed residential systems in Belgium and the wider European market:

- **Inverters:** Fronius Gen24 series, Huawei SUN2000 series, Growatt (selected hybrid models)
- **Batteries:** BYD HVS and HVM systems
- **EV chargers:** Wallbox Pulsar series, go-e Charger series
- **Smart meters:** DSMR-compatible meters

Support also extends to any device accessible via Modbus TCP or OCPP, covering a broad range of additional hardware.

If your brand isn't on the initial list, you have three practical options: use a protocol-compatible interface (Modbus TCP or OCPP), request or contribute an integration through the open-source community, or run the system in monitoring mode while waiting for full integration. New integrations are added continuously based on installer demand — but never at the cost of stability for what's already supported.

---

### Q: How much does it cost — and how does pricing change as my business grows?

The OPEN-EMS core is fully open-source and free to self-host. There are no licensing fees for local installations.

Pricing for the commercial tier — which includes remote fleet management, multi-site monitoring, and professional support — will be announced at launch. The model is designed to scale with the size of an installer's portfolio, so independent professionals aren't priced out in favour of large enterprise players.

If you want early pricing information before the public announcement, contact the team directly.

---

### Q: What happens to my clients' installations if OPEN-EMS shuts down or stops being maintained?

All core functionality runs locally. If OPEN-EMS ceased operations tomorrow, every deployed installation would continue to function — no cloud dependency, no kill switch.

Because the core platform is open-source, installers can maintain and update their own deployments independently. The community can continue development. Integrations remain fully accessible and modifiable. No proprietary lock-in means no forced migration.

---

### Q: If something breaks at 9pm, who do I call?

That depends on your support tier.

Community users have access to documentation and community support channels — suitable for technically confident installers comfortable with self-service troubleshooting.

Professional tier users have access to dedicated support with a next-business-day response time for standard issues. For time-sensitive or critical situations, SLA contracts are available with faster response times based on the selected level.

The goal is to give professional installers predictable support — not leave them dependent on a forum post at 9pm.

---

### Q: How is this actually different from Home Assistant combined with EVCC?

Home Assistant and EVCC are powerful tools — but they're general-purpose, and every installation requires custom configuration from scratch. There is no standardized deployment model, no built-in energy decision logic, and no remote fleet management designed for professional use.

OPEN-EMS provides three things that combination doesn't:

1. **A structured energy domain model** — devices are assigned roles (inverter, battery, EV charger, grid meter), not treated as generic smart home entities. This makes logic consistent and repeatable across every installation.
2. **A built-in decision engine** — energy optimization happens automatically based on the homeowner's chosen strategy. No custom automations. No manual rules to maintain.
3. **A repeatable professional deployment model** — installers configure once, deploy consistently, and manage the full portfolio remotely.

To be clear: a fully custom Home Assistant setup built by an expert can be highly capable. OPEN-EMS is not trying to compete with that. It is designed to be more reliable, more maintainable, and more practical for installers who deploy across tens of projects — not just one.

---

### Q: What happens when the forecasting is wrong and my client gets an unexpected peak charge?

The core decision engine is rule-based and conservative by default. Forecasting improves optimization — it does not override safety boundaries.

Every installation is configured with hard limits: a peak consumption ceiling, a minimum battery reserve, and fallback strategies that activate when forecast confidence is low. These are set by the installer and cannot be overridden by the optimization layer.

In practice: if the system is uncertain, it defaults to the safe configuration rather than making an aggressive bet. The peak limit is always respected. The battery never drops below its reserve. The forecasting layer operates within those boundaries, not above them.

Installers configure the safety margins per site. Homeowners can adjust their optimization strategy within the limits their installer has defined.

---

### Q: How long does a first installation actually take?

A first installation — including device discovery, role assignment, and strategy configuration — typically takes a few hours, depending on the number of devices and the complexity of the site.

As installers become familiar with the platform, the process becomes significantly faster. Because every installation follows the same structured model, experienced users can deploy repeat configurations in a fraction of the time.

Onboarding documentation and a guided configuration flow are included to minimize the learning curve on the first project.

---

### Q: What happens when two devices have conflicting needs at the same moment?

OPEN-EMS uses a deterministic priority model. When conflicts arise — for example, an urgent EV charge request while a peak limit is close to being breached — the system applies a clear decision hierarchy.

That hierarchy is defined by the installer at setup time: system constraints (peak limits, battery protection thresholds) take precedence over optimization goals, which take precedence over convenience preferences. End users can apply temporary overrides — for example, flagging an urgent EV charge — but only within the safe boundaries the installer has set. They cannot override a peak limit or discharge the battery below its reserve.

The result is predictable: the system never silently breaks a rule. If a request can't be fulfilled within the configured constraints, the system communicates why and falls back to the safest available action.

---

## Internal FAQ

### I1. What is the single hardest technical problem — and do you know how to solve it?

The hardest problem is not building the system — it is making control reliable across devices that were never designed to be controlled by anything other than their own ecosystem.

Reading data from inverters, batteries, and chargers is tractable. Writing control commands — charging a battery to a specific state, capping an inverter's output, delaying an EV charge session — is where the risk lives. Protocols are inconsistently implemented. Behavior is often undocumented. Safety constraints vary by firmware version. A command that works on a Fronius Gen24 running v1.14 may behave differently on v1.16.

The solution is architectural: strict device abstraction with declared capability contracts, per-device validation layers that assume nothing about uniform behavior, conservative control logic with hard safety limits, and a deliberate policy of gradual device onboarding with real-world validation before a brand is listed as supported.

This is not a problem solved once at design time. It is an ongoing engineering discipline. The cost of getting it wrong is system instability in a client's home — which is why the architecture is built to be conservative before it is built to be optimal.

---

### I2. How do you acquire the first 20 installers — specifically?

Not through marketing. Through direct, controlled outreach to people who already have reason to trust the concept.

**Phase 1 — Pilots (months 1–3 post-v1):** Identify 5–10 installers from existing personal and professional networks in the Belgian EV, PV, and installation sector. Offer hands-on onboarding and direct support for their first deployments. These are not paying customers yet — they are validation partners.

**Phase 2 — Reference cases (months 3–6):** Convert successful pilots into documented reference cases: specific hardware combinations, measurable outcomes (peak reduction, self-consumption rate, installer time saved). These become the primary sales asset.

**Phase 3 — Referral expansion (months 4–6):** Use reference installers to open introductions to 10–15 additional installers through targeted outreach and sector-specific channels (installer associations, trade events, regional networks).

The first goal is proof, not scale: real installations, real savings, real reliability. Without that, nothing else works.

---

### I3. What is the competitive moat — and how durable is it?

The honest answer is that the domain model and decision engine are not a moat. A well-funded competitor can reverse-engineer the architecture. Device integrations can be replicated. Open-source code can be forked.

The durable moat is **installer workflow integration** — and it is a network-effect argument, not a technology argument.

Once an installer has deployed OPEN-EMS on five sites, configured their standard device set, and built their support workflow around the platform, the switching cost is real. Their configuration templates, their client documentation, their troubleshooting knowledge — all of it is invested in the platform. This is how enterprise software retains customers, and it applies equally here.

Supporting assets that deepen that moat over time:
- **Accumulated real-world deployment data** — edge cases, device quirks, and optimization patterns that come only from field experience at scale. This cannot be purchased.
- **Community-contributed integrations** — once installers and developers are contributing integrations, the pace of device support outpaces what any single competitor team can match.

The window of vulnerability is the first 12–18 months before installer stickiness develops. Speed of adoption in the pilot and early commercial phase is the strategic priority.

---

### I4. What does v1 actually include — and when does it ship?

**Timeline:** 6–9 months with a focused team of 1–2 backend engineers and 1 integration-focused developer.

**v1 scope:**
- Local runtime on standard hardware (Raspberry Pi, NUC, existing home server)
- Core device abstraction layer with the initial supported brand set
- Rule-based decision engine with basic forecasting
- Installer configuration interface (device assignment, role setup, strategy selection)
- Basic local monitoring

**Explicitly out of scope for v1:**
- Fleet management platform
- Advanced forecasting models
- End-user consumer app
- Extended device library beyond the launch set
- Home Assistant integration layer

**The primary timeline risk is not feature development — it is integration validation.** Each supported device brand requires real-world testing across firmware versions and installation configurations. Slippage in v1 is most likely to come from device control reliability, not from the core engine.

The goal of v1 is a system that installers can deploy on a real client site, walk away from, and not hear about again. That bar — not feature completeness — is what defines v1 done.

---

### I5. What is the regulatory and liability exposure — and when does it need to be resolved?

OPEN-EMS operates as a supervisory control layer. It does not replace the built-in safety mechanisms of any device. Battery protection, inverter limits, and charger safety functions remain at the device level. OPEN-EMS operates within the bounds those devices allow — it cannot command a battery into an unsafe state if the battery's own firmware prevents it.

This is the correct legal framing: OPEN-EMS is a configuration and optimization layer, not a safety-critical controller. The installer is responsible for the overall system configuration, as they are for any installation.

**The trigger for formal legal and insurance review is clearly defined:** before transitioning from pilot installations (the initial 5–10 controlled deployments) to commercial scaling with paying customers.

Pilot installations are treated as controlled deployments with direct support involvement. Commercial scaling changes the liability profile. Before that transition, the following must be addressed: terms of service that clearly define installer responsibility, liability coverage for software-caused damage, and — if a dedicated hardware product is introduced — CE compliance for that hardware.

This is a deferred cost, not a deferred risk. The cost is known and manageable. The risk is entering commercial scale without having addressed it.

---

### I6. Why will professional installers pay — and is the conversion math viable?

The business model does not depend on converting self-hosters. It depends on converting professional installers managing multiple sites.

**Assumed conversion rates:**
- Individual / technically advanced self-hosters: 5–10% to paid (low, and expected)
- Professional installers managing multiple installations: 20–40% once they reach operational scale

The commercial tier is not a convenience upgrade for self-hosters — it is an operational tool for professionals. Remote fleet management, centralized monitoring, and SLA-backed support have compounding value as the number of managed sites grows. An installer managing 20 sites gains meaningful time savings from every feature in the commercial tier. A hobbyist managing one home does not.

The viability of the model depends entirely on capturing the professional installer segment. If the platform never achieves meaningful adoption with professional users and remains primarily a hobbyist tool, the business model fails regardless of how many free users exist. This is the right segment to optimize for — and the pilot strategy is explicitly designed to validate it.

---

### I7. What happens when a device manufacturer actively works against OPEN-EMS?

This is a real risk. Manufacturers have strong financial incentives to keep installers within their ecosystems. Firmware updates that break third-party integrations — whether intentional or not — are a recurring problem across the industry. A major brand launching a competing platform is a plausible scenario within 3–5 years if OPEN-EMS reaches meaningful scale.

**Mitigation strategy:**

**Protocol diversity over proprietary APIs.** Integrations are built on open and widely implemented protocols (Modbus TCP, OCPP) wherever possible. A manufacturer can restrict or change a proprietary API; they cannot deprecate Modbus.

**No single-vendor dependency by design.** The device abstraction layer is built to be replaceable. If a brand breaks an integration, the affected integration is isolated — it does not affect the rest of the platform or other supported devices.

**Community-contributed integrations as indirect leverage.** When an integration is maintained by the installer and developer community, breaking it generates visible pushback from that manufacturer's own customer base. This is not a guarantee, but it is a friction cost that pure proprietary approaches don't face.

**The honest edge case:** if a dominant manufacturer — Fronius, Huawei, or a future market leader — builds a compelling competing platform and bundles it with hardware at no additional cost, that is a genuine competitive threat that protocol diversity alone cannot fully address. The answer to that scenario is speed of installer adoption before that happens, and a community-built integration ecosystem that no single manufacturer can shut down.

---

## The Verdict

### Concept Readiness: Strong — with three items to resolve before PRD

OPEN-EMS survived the gauntlet in better shape than most concepts. The problem is real, the customer is precisely defined, the competitive gap is documented, and the architecture philosophy is sound. The thinking sharpened under pressure rather than collapsing. That matters.

Here is the honest assessment.

---

### Forged in Steel

**The market gap is real and verified.** Research confirms no tool currently combines Belgian tariff intelligence, multi-brand orchestration, installer fleet management, and affordable SME pricing. gridX fills this space for enterprises. Nobody fills it for independent installers. This is not an assumption — it is a documented structural gap.

**The primary customer is precisely defined.** Installer as deployer and decision-maker; homeowner as daily operator. The two-sided model is coherent, the press release speaks to the right hero, and the go-to-market strategy is correctly oriented around installer adoption.

**The competitive positioning is honest and therefore credible.** "Not more powerful than a custom Home Assistant setup, but more reliable, scalable, and practical for professionals" is the kind of statement that builds trust with exactly the audience you need. Resist any temptation to soften this later.

**The architecture philosophy is right.** Local-first, deterministic core, conservative by default, forecasting as an enhancement layer rather than a control layer — this is the correct approach for a system that installers stake their reputation on. It is also the correct legal posture.

**Belgium is the right launch market.** The capaciteitstarief and Wallonia incentive tariff create direct, calculable financial incentives for HEMS. Customers have a reason to care about peak shaving that is printed on their electricity bill every month. This is a better launch context than most European markets.

**The liability answer is clear and defensible.** Supervisory control layer, not safety-critical controller. Installer responsible for system configuration. Device-level safety mechanisms remain at the device. This framing is correct and needs to be documented formally before commercial scaling — but the framing itself is solid.

---

### Needs More Heat

**The end-user experience is a ghost.** The press release mentions "a clean interface showing their energy flows" and strategy selection. That is all. There is no clarity on what that interface actually looks like, how it differs from the six apps the homeowner already has, or what the experience is during the first weeks when the system is still learning. The homeowner's daily experience is the other half of the value proposition — it needs to be designed, not implied.

**The forecasting learning period is unaddressed.** "Continuously learned consumption patterns based on historical usage data" raises an obvious question: how long until the system is actually optimizing well? A week? A month? A season? What does the homeowner experience during that period? If the answer is "it works conservatively from day one and improves over time," say that explicitly — it is reassuring, not a weakness.

**The commercial tier handoff is unclear.** The press release describes remote fleet management as a current commercial tier feature. v1 delivers basic monitoring. There is a gap between what is described at launch and what ships at launch. The PRD needs to define exactly what "fleet management" means in v1 vs v2, so the press release and the product are describing the same thing.

---

### Cracks in the Foundation

**Pricing is deferred but the CTA is live.** The Getting Started section says "visit [open-ems.io] to access the platform." The commercial tier pricing is "announced at launch." These two things happen at the same time — which means the first professional installer who reads the press release and wants to buy has no price to evaluate. Either pricing needs to be ready at launch, or the CTA needs to route to a waitlist / early access flow rather than implying the product is immediately purchasable.

**The go-to-market stops at 20 installers.** The pilot strategy is sound and specific. The referral expansion is plausible. But there is no plan for what happens after the first cohort. Referrals get you to 20. What gets you to 100? Installer associations? A certification program? A partner channel? This is not a v1 problem — but it is a gap the PRD should at minimum flag as a near-term strategic question.

**The manufacturer threat has no early warning system.** The mitigation strategy (protocol diversity, replaceable integrations, community leverage) is the right long-term posture. But there is no mechanism for detecting early signs of a manufacturer moving against OPEN-EMS before an integration breaks in production. Monitoring firmware changelogs, maintaining relationships with manufacturer technical contacts, and having a documented response playbook for integration breaks would close this gap.

---

### The Call

**Move to PRD.** The concept is ready. The three items above — end-user UX clarity, v1 vs v2 fleet management definition, and pricing readiness at launch — are not blockers for starting the PRD. They are the first questions the PRD process should answer. Everything else is built on solid enough ground to start designing.

---

<!-- coaching-notes-stage-4 -->
**Feasibility risks identified:**
- Control reliability (write commands across brands) is the primary technical risk, not feature development
- Integration validation timeline is the main v1 slippage risk
- Manufacturer API restriction is a real and acknowledged threat

**Resource / timeline:**
- v1: 6–9 months, team of 3 (1–2 backend + 1 integration dev)
- Pilot phase: 5–10 installations, months 1–3 post-v1
- Commercial scaling: after pilot validation, months 4–6

**Unknowns with resolution triggers:**
- Liability/legal: must be resolved before commercial scaling (post-pilot trigger clearly defined)
- Pricing: TBD at launch, not blocking v1 development
- CE compliance: required only if dedicated hardware introduced

**Strategic positioning decisions:**
- Moat reframed from technology to installer workflow integration (network effects)
- Business model viability depends entirely on professional installer conversion (20-40%), not self-hoster conversion (5-10%)
- Speed of early installer adoption is the critical strategic window (12-18 months before stickiness develops)

**Technical constraints / dependencies surfaced:**
- Device control layer is the architectural load-bearing element — conservative by design
- Protocol diversity (Modbus TCP, OCPP) is the primary manufacturer-independence strategy
- Community-contributed integrations are both a scaling mechanism and indirect competitive leverage
<!-- end coaching-notes-stage-4 -->

<!-- coaching-notes-stage-3 -->
**Gaps revealed by customer questions:**
- Device list specificity: initial list resolved to 7 named brands/series + Modbus TCP / OCPP coverage
- Pricing: commercial tier TBD at launch — honest, not aspirational; core remains free
- SLA: next-business-day standard; faster tiers via SLA contract

**Trade-off decisions made:**
- Pricing TBD = accepted gap; not a launch blocker (open-source core is free; commercial pricing can follow)
- "Not more powerful than expert HA" framing = accepted honest positioning, not a weakness
- Forecast failure = not a blocker; hard limits always respected; conservative fallback is the safety net

**Competitive intelligence surfaced:**
- HA + EVCC is the real comparison most installers make; Q5 answer directly addresses it
- EVCC does not handle battery dispatch, heating, or load management — OPEN-EMS covers the full orchestration scope
- gridX is the enterprise equivalent; OPEN-EMS is the SME-accessible version of that model

**Scope / requirements signals:**
- Priority model (Q8) implies a configuration UI with per-site constraint settings and user override boundaries — a non-trivial UX requirement
- Fleet dashboard with remote monitoring is the key commercial differentiator — must be production-quality at launch for professional credibility
- SLA offering requires a support infrastructure decision before launch
<!-- end coaching-notes-stage-3 -->

<!-- coaching-notes-stage-1 -->
**Concept type:** Commercial open-core product (open-source core + commercial managed tier)

**Primary customer:** Residential energy installer (decision-maker, deployer, fleet manager)
**Secondary user:** Homeowner/end user (daily operator, strategy setter)

**Initial assumptions challenged:**
- User led with solution framing ("vendor-agnostic platform") → redirected to installer pain (integration time, support calls, hardware lock-in)
- "Advanced home energy users" initially listed as co-primary → clarified as daily operators, not buyers

**Key competitive context (web research, 2026):**
- gridX (XENON): closest analog — vendor-agnostic, installer-facing — but targets utilities/OEMs, not SME installers (5–50 installs/year). This is the structural gap.
- EVCC: open-source, EV/solar focus only, no battery dispatch, no fleet management, sponsor token per instance
- Home Assistant: broad but fully DIY, no commercial support path, not installer-deployable at scale
- Tibber: not yet operating in Belgium
- No tool currently combines: Belgian tariff intelligence + multi-brand orchestration + installer fleet dashboard + affordable SME pricing

**Belgium market signals:**
- Flanders capaciteitstarief (live 2023): 15-min peak billing, clear HEMS ROI
- Wallonia incentive tariff (live Jan 2026): dynamic pricing now default for digital meter holders
- Market: ~€1.59B HEMS in Europe 2025, 15.7% CAGR to 2031

**Decision engine clarified:**
- Rule-based core (deterministic, predictable) + forecast-driven optimization layer
- Local-first by default — offline-capable for core decisions
- Uses solar forecasts + dynamic tariff data + learned consumption patterns
- Forward-looking: battery scheduling, EV charge timing, peak anticipation

**Positioning not used:**
- "Home Assistant replacement" → rejected; Jordan positioned as complementary layer
- Pure DIY/community framing → rejected; commercial viability requires the managed tier
<!-- end coaching-notes-stage-1 -->
