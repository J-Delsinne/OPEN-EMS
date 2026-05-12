"""Story 11.1 — installer-dashboard anomaly detection + dismiss state machine.

The module is intentionally pure-functional with one stateful surface:
``_DISMISSED_ANOMALIES`` — a module-level ``dict[session_id, AnomalySignature]``
that records each session's most-recently-dismissed signature for the
elevation comparison.

Process restart clears the dict (correct semantic: a new process is a fresh
operational view; the installer's prior dismissals do not carry forward).

Structural invariant: ``detect_anomaly_from_snapshot`` and
``evaluate_dismiss_state`` are pure functions over their inputs — neither
touches the database, the filesystem, or any external resource. The function
signatures encode this: ``(SystemSnapshot) -> AnomalySignature | None`` and
``(current, dismissed) -> Literal[...]``. The Story 11.1 AC7 epic line 2291
constraint ("anomaly detection reads exclusively from the current StateStore
snapshot — no additional database queries are triggered for anomaly
detection") is enforced by these signatures plus a structural-discipline
test that patches the DB repos and asserts they are not called during
detection.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import structlog

from open_ems.core.devices import DeviceRole
from open_ems.core.state import (
    REQUIRED_DEVICE_ROLES,
    ComponentState,
    SystemOperatingMode,
    SystemSnapshot,
)

logger = structlog.get_logger(__name__)

Severity = Literal["WARN", "FAIL"]

# AC7 / Path A — PEAK_APPROACHING is OMITTED. The strict "StateStore-only"
# rule from epic line 2291 forbids reading active_constraints from the
# detection path, and the peak tracker (AC5) already surfaces the
# "(approaching)" / "(exceeded)" labels in its own fragment.
AnomalyType = Literal[
    "DEVICE_ERROR",
    "DEVICE_UNAVAILABLE",
    "SYSTEM_DEGRADED",
    "SYSTEM_FAILED",
]

# Severity per anomaly type. The signature's severity is FAIL iff ANY active
# type maps to FAIL; otherwise WARN. The order in this mapping is purely
# documentation — the lookup is by key.
_SEVERITY_MAPPING: Mapping[AnomalyType, Severity] = {
    "SYSTEM_FAILED": "FAIL",
    "DEVICE_UNAVAILABLE": "FAIL",
    "SYSTEM_DEGRADED": "WARN",
    "DEVICE_ERROR": "WARN",
}

# Severity total order — used by evaluate_dismiss_state for the strict
# "elevation iff strictly higher" rule.
_SEVERITY_ORDER: Mapping[Severity, int] = {"WARN": 0, "FAIL": 1}

# Time-window suffix for the "View in event log" pre-filter link. v1 uses a
# fixed 24h window; Story 11.2 may extend.
_LINK_WINDOW = "24h"

# P8: _ANOMALY_SUMMARIES single-source-of-truth table for the per-part wording
# the installer sees in the notice. Each key represents an aggregator part
# rendered by ``_build_anomaly_summary``. Keeping the strings here (not inlined
# at the call site) honors the spec line 220 contract.
_ANOMALY_SUMMARIES: Mapping[str, str] = {
    "cold_start": "System is starting up",
    "system_failed": "System is in safe mode",
    "system_degraded": "System is operating with reduced functionality",
    "device_unavailable_singular": "1 required device unavailable",
    "device_unavailable_plural": "{count} required devices unavailable",
    "device_error_singular": "1 device degraded",
    "device_error_plural": "{count} devices degraded",
    "fallback": "System anomaly detected",
}

# P19 (D2 resolution): bound the per-session dismiss dict. Each fresh login
# adds a new session_id; without a bound, a long-running process with daily
# logouts leaks entries indefinitely. 100 entries is generous for OPEN-EMS's
# expected deployment (single installer, 1-2 active sessions) and small enough
# to be invisible in memory.
_MAX_DISMISS_ENTRIES = 100


@dataclass(frozen=True)
class AnomalySignature:
    """Set of anomaly facts active at a single snapshot evaluation.

    Used both as the rendered notice content AND as the dict-key for the
    dismiss/elevation state machine. Frozen for hashability and for the
    invariant "a recorded dismissed signature cannot be mutated".

    Fields:
        severity: FAIL or WARN (derived deterministically from anomaly_types).
        anomaly_types: frozenset of active AnomalyType members.
        summary: plain-language aggregated copy shown to the installer.
        link_query: query-string suffix for the "View in event log" link.
    """

    severity: Severity
    anomaly_types: frozenset[AnomalyType]
    summary: str
    link_query: str


def detect_anomaly_from_snapshot(snapshot: SystemSnapshot) -> AnomalySignature | None:
    """Return the active anomaly signature for this snapshot, or None.

    Reads ONLY ``snapshot.operating_mode`` + ``snapshot.component_states``.
    The function signature ``(SystemSnapshot) -> AnomalySignature | None``
    structurally enforces the AC7 / epic line 2291 "StateStore-only" rule —
    no I/O is possible without changing the signature.

    Cold-start path: if ``snapshot.sequence_id == 0`` AND the snapshot would
    otherwise trigger an anomaly, the summary is overridden to the calm
    "System is starting up" copy (per R5 — a default snapshot operating in
    degraded mode pre-first-tick must not alarm an installer watching the
    dashboard come up).
    """
    active_types: set[AnomalyType] = set()

    # System-level anomalies derived directly from operating_mode.
    if snapshot.operating_mode is SystemOperatingMode.fail_safe:
        active_types.add("SYSTEM_FAILED")
    elif snapshot.operating_mode in (
        SystemOperatingMode.degraded,
        SystemOperatingMode.conservative,
    ):
        active_types.add("SYSTEM_DEGRADED")

    # Device-level anomalies derived from per-role component_states.
    # Count required-role unavailability as a FAIL (DEVICE_UNAVAILABLE);
    # optional-role unavailability is NOT an anomaly on its own (an EV
    # charger absent at a site without one is the normal state).
    # Any ERROR state on any role is a WARN (DEVICE_ERROR).
    # P18 (D1 resolution): ``ComponentState.stale`` on a required role counts
    # as DEVICE_UNAVAILABLE so the anomaly contract aligns with the device-row
    # display contract (state_serialization._INSTALLER_COMPONENT_STATE_DISPLAY
    # maps stale → "UNAVAILABLE"); otherwise the dashboard self-contradicts.
    device_error_count = 0
    device_unavailable_required_count = 0
    for role in DeviceRole:
        component_state = snapshot.component_states.get(role)
        if component_state is ComponentState.error:
            device_error_count += 1
        elif (
            component_state in (ComponentState.unavailable, ComponentState.stale)
            and role in REQUIRED_DEVICE_ROLES
        ):
            device_unavailable_required_count += 1

    if device_error_count > 0:
        active_types.add("DEVICE_ERROR")
    if device_unavailable_required_count > 0:
        active_types.add("DEVICE_UNAVAILABLE")

    if not active_types:
        return None

    # Severity: FAIL if any active type maps to FAIL; else WARN.
    severity: Severity = (
        "FAIL" if any(_SEVERITY_MAPPING[t] == "FAIL" for t in active_types) else "WARN"
    )

    summary = _build_anomaly_summary(
        snapshot=snapshot,
        severity=severity,
        active_types=active_types,
        device_error_count=device_error_count,
        device_unavailable_required_count=device_unavailable_required_count,
    )

    link_query = _build_anomaly_link_query(active_types)

    return AnomalySignature(
        severity=severity,
        anomaly_types=frozenset(active_types),
        summary=summary,
        link_query=link_query,
    )


def _build_anomaly_summary(
    *,
    snapshot: SystemSnapshot,
    severity: Severity,
    active_types: set[AnomalyType],
    device_error_count: int,
    device_unavailable_required_count: int,
) -> str:
    """Aggregate plain-language summary for the active anomaly types.

    Cold-start special case (per R5): if sequence_id==0 AND the severity is
    NOT FAIL, the summary is overridden to the calm "System is starting up"
    copy. The default snapshot's operating_mode=degraded would otherwise
    produce "System is operating with reduced functionality" on a dashboard
    the installer just opened during boot — alarming and incorrect.

    P3 review fix: at sequence_id==0 the override is gated on
    ``severity != "FAIL"``. If a FAIL-level anomaly is somehow active at
    sequence_id==0 (e.g., a fail_safe boot or a required device missing on
    the cold-start snapshot), the badge says FAIL and the summary must agree
    — surfacing the real fact instead of soothing copy.

    Wording is sourced from ``_ANOMALY_SUMMARIES`` (P8 review fix) so the
    "single source of truth" contract from spec line 220 is honored
    structurally; no string literal is inlined at the call site.
    """
    if snapshot.sequence_id == 0 and severity != "FAIL":
        return _ANOMALY_SUMMARIES["cold_start"]

    parts: list[str] = []
    if "SYSTEM_FAILED" in active_types:
        parts.append(_ANOMALY_SUMMARIES["system_failed"])
    elif "SYSTEM_DEGRADED" in active_types:
        parts.append(_ANOMALY_SUMMARIES["system_degraded"])
    if "DEVICE_UNAVAILABLE" in active_types:
        if device_unavailable_required_count == 1:
            parts.append(_ANOMALY_SUMMARIES["device_unavailable_singular"])
        else:
            parts.append(
                _ANOMALY_SUMMARIES["device_unavailable_plural"].format(
                    count=device_unavailable_required_count
                )
            )
    if "DEVICE_ERROR" in active_types:
        if device_error_count == 1:
            parts.append(_ANOMALY_SUMMARIES["device_error_singular"])
        else:
            parts.append(_ANOMALY_SUMMARIES["device_error_plural"].format(count=device_error_count))
    return " · ".join(parts) if parts else _ANOMALY_SUMMARIES["fallback"]


def _build_anomaly_link_query(active_types: set[AnomalyType]) -> str:
    """Build the "View in event log" query-string suffix (AC9 contract).

    Per Q4 resolution (2026-05-12): comma-separated single-key format —
    ``?type=SYSTEM,DEVICE&window=24h``. Story 11.2's URL parser honors
    exactly this format.
    """
    type_filters: set[str] = set()
    if "SYSTEM_FAILED" in active_types or "SYSTEM_DEGRADED" in active_types:
        type_filters.add("SYSTEM")
    if "DEVICE_ERROR" in active_types or "DEVICE_UNAVAILABLE" in active_types:
        type_filters.add("DEVICE")
    # Sorted for deterministic output (test stability).
    type_value = ",".join(sorted(type_filters)) if type_filters else "SYSTEM"
    return f"?type={type_value}&window={_LINK_WINDOW}"


DismissEvaluation = Literal["render", "suppress", "no_anomaly"]


def evaluate_dismiss_state(
    current: AnomalySignature | None,
    dismissed: AnomalySignature | None,
) -> DismissEvaluation:
    """Return whether the notice should render given current + dismissed.

    Per AC8 + R2 elevation table:
    - "no_anomaly" — current is None (nothing to show).
    - "render" — current is non-None AND (dismissed is None OR current
      strictly elevates over dismissed: higher severity OR a new anomaly
      type not in the dismissed set).
    - "suppress" — current is non-None AND the dismissed signature covers
      it (same-or-higher severity AND types-subset).

    De-escalation explicitly does NOT re-display: a FAIL→WARN transition
    with covered types stays suppressed (the installer already acknowledged
    the more severe state; less-severe later facts about the same surface
    do not warrant re-prompting).
    """
    if current is None:
        return "no_anomaly"
    if dismissed is None:
        return "render"

    current_severity_rank = _SEVERITY_ORDER[current.severity]
    dismissed_severity_rank = _SEVERITY_ORDER[dismissed.severity]

    # Elevation rule #1: strictly higher severity → render.
    if current_severity_rank > dismissed_severity_rank:
        return "render"

    # Elevation rule #2: new anomaly type → render.
    if not current.anomaly_types.issubset(dismissed.anomaly_types):
        return "render"

    # Otherwise the dismissed signature covers the current one.
    return "suppress"


# ── Per-session dismiss state ─────────────────────────────────────────────


# Module-level LRU-bounded dict (Q3 resolution; P19 review fix).
# OrderedDict + ``move_to_end`` after every write gives O(1) LRU semantics.
# Process restart re-initializes the module and clears all dismissals — the
# correct v1 semantic. ``_MAX_DISMISS_ENTRIES`` caps the in-process footprint
# regardless of session churn (fresh login on every browser visit would
# otherwise grow the dict monotonically).
_DISMISSED_ANOMALIES: OrderedDict[str, AnomalySignature] = OrderedDict()


def dismiss_signature(session_id: str, signature: AnomalySignature) -> None:
    """Record the dismissed signature for the given session.

    Overwrites any prior dismissed signature for the session — when an
    installer dismisses an already-elevated notice, the elevation becomes
    the new baseline for future comparisons. LRU-evicts the oldest entry
    when the dict reaches ``_MAX_DISMISS_ENTRIES`` (P19 review fix).
    """
    if session_id in _DISMISSED_ANOMALIES:
        _DISMISSED_ANOMALIES.move_to_end(session_id)
    elif len(_DISMISSED_ANOMALIES) >= _MAX_DISMISS_ENTRIES:
        evicted_session_id, _ = _DISMISSED_ANOMALIES.popitem(last=False)
        logger.info(
            "anomaly_dismiss_lru_eviction",
            evicted_session_id=evicted_session_id,
            new_session_id=session_id,
            component="installer_dashboard",
        )
    _DISMISSED_ANOMALIES[session_id] = signature
    _DISMISSED_ANOMALIES.move_to_end(session_id)
    logger.info(
        "anomaly_dismissed",
        session_id=session_id,
        severity=signature.severity,
        anomaly_types=sorted(signature.anomaly_types),
        component="installer_dashboard",
    )


def get_dismissed_signature(session_id: str) -> AnomalySignature | None:
    """Return the dismissed signature for the session, or None.

    Touches LRU recency on hit so an actively-polling installer's entry
    survives churn from new login sessions.
    """
    if session_id not in _DISMISSED_ANOMALIES:
        return None
    _DISMISSED_ANOMALIES.move_to_end(session_id)
    return _DISMISSED_ANOMALIES[session_id]


def clear_dismissed_signature(session_id: str) -> None:
    """Best-effort clear (called on session logout if a hook is available)."""
    _DISMISSED_ANOMALIES.pop(session_id, None)


def _reset_dismiss_state_for_tests() -> None:
    """Test-only helper. Clear the module-level dismiss dict between tests."""
    _DISMISSED_ANOMALIES.clear()
