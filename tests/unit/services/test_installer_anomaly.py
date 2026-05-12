"""Story 11.1 AC7 + AC8 — installer-anomaly detection + dismiss state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from open_ems.core.devices import DeviceRole
from open_ems.core.state import (
    ComponentState,
    EnergyStrategy,
    GlobalState,
    SystemOperatingMode,
    SystemSnapshot,
)
from open_ems.services.installer_anomaly import (
    AnomalySignature,
    _reset_dismiss_state_for_tests,
    detect_anomaly_from_snapshot,
    dismiss_signature,
    evaluate_dismiss_state,
    get_dismissed_signature,
)

_NOW_UTC = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)


def _snapshot(
    *,
    sequence_id: int = 1,
    operating_mode: SystemOperatingMode = SystemOperatingMode.normal,
    inverter_state: ComponentState = ComponentState.active,
    battery_state: ComponentState = ComponentState.unavailable,
    ev_charger_state: ComponentState = ComponentState.unavailable,
    grid_meter_state: ComponentState = ComponentState.active,
) -> SystemSnapshot:
    """Construct a minimal SystemSnapshot for anomaly detection tests.

    Only ``operating_mode`` + ``component_states`` matter for the anomaly
    detector; the device slot fields are left as None (or as DeviceSlot
    None) — detect_anomaly_from_snapshot never reads them.
    """
    return SystemSnapshot(
        sequence_id=sequence_id,
        captured_at=_NOW_UTC,
        global_state=GlobalState.normal,
        operating_mode=operating_mode,
        active_strategy=EnergyStrategy.maximize_self_consumption,
        inverter=None,
        battery=None,
        ev_charger=None,
        grid_meter=None,
        component_states={
            DeviceRole.inverter: inverter_state,
            DeviceRole.battery: battery_state,
            DeviceRole.ev_charger: ev_charger_state,
            DeviceRole.grid_meter: grid_meter_state,
        },
        data_age_seconds={
            DeviceRole.inverter: 0,
            DeviceRole.battery: None,
            DeviceRole.ev_charger: None,
            DeviceRole.grid_meter: 0,
        },
        system_clock_status="valid",
    )


@pytest.fixture(autouse=True)
def _clear_dismiss_state() -> None:
    """Module-level _DISMISSED_ANOMALIES is process-wide; reset between tests."""
    _reset_dismiss_state_for_tests()


# ── AC7 detection ────────────────────────────────────────────────────────────


def test_detect_anomaly_normal_snapshot_returns_none() -> None:
    """AC7: a fully-normal snapshot has no active anomaly."""
    snapshot = _snapshot()
    assert detect_anomaly_from_snapshot(snapshot) is None


def test_detect_anomaly_operating_mode_degraded_returns_warn_system_degraded() -> None:
    """AC7: operating_mode=degraded → WARN + SYSTEM_DEGRADED."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.degraded)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "WARN"
    assert sig.anomaly_types == frozenset({"SYSTEM_DEGRADED"})


def test_detect_anomaly_operating_mode_conservative_returns_warn_system_degraded() -> None:
    """AC7: conservative is treated as a WARN-level SYSTEM_DEGRADED (same display category)."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.conservative)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "WARN"
    assert sig.anomaly_types == frozenset({"SYSTEM_DEGRADED"})


def test_detect_anomaly_operating_mode_fail_safe_returns_fail_system_failed() -> None:
    """AC7: fail_safe → FAIL + SYSTEM_FAILED."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.fail_safe)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "FAIL"
    assert sig.anomaly_types == frozenset({"SYSTEM_FAILED"})


def test_detect_anomaly_required_role_unavailable_returns_fail_device_unavailable() -> None:
    """AC7: a required role (grid_meter, inverter) unavailable → FAIL."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.normal,
        inverter_state=ComponentState.unavailable,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "FAIL"
    assert "DEVICE_UNAVAILABLE" in sig.anomaly_types


def test_detect_anomaly_optional_role_unavailable_does_not_promote_to_fail() -> None:
    """AC7: an optional role (battery, ev_charger) unavailable is the NORMAL state at
    sites without that device — must NOT promote the anomaly to FAIL."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.normal,
        ev_charger_state=ComponentState.unavailable,
        battery_state=ComponentState.unavailable,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is None


def test_detect_anomaly_optional_role_error_returns_warn_device_error() -> None:
    """AC7: any role in ComponentState.error → WARN + DEVICE_ERROR."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.normal,
        battery_state=ComponentState.error,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "WARN"
    assert sig.anomaly_types == frozenset({"DEVICE_ERROR"})


def test_detect_anomaly_multiple_active_aggregates_summary_and_takes_highest_severity() -> None:
    """AC7: when multiple types are active, severity = highest, types aggregated."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.fail_safe,
        inverter_state=ComponentState.error,
        battery_state=ComponentState.error,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.severity == "FAIL"
    assert "SYSTEM_FAILED" in sig.anomaly_types
    assert "DEVICE_ERROR" in sig.anomaly_types
    # Summary aggregates: "System is in safe mode · 2 devices degraded"
    assert "safe mode" in sig.summary
    assert "2 devices degraded" in sig.summary


def test_detect_anomaly_link_query_for_system_failed_is_type_system_window_24h() -> None:
    """AC9: SYSTEM_FAILED alone → ?type=SYSTEM&window=24h."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.fail_safe)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.link_query == "?type=SYSTEM&window=24h"


def test_detect_anomaly_link_query_for_mixed_types_is_multi_type_filter() -> None:
    """AC9 / Q4: mixed system+device types → comma-separated ?type=DEVICE,SYSTEM&window=24h."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.degraded,
        battery_state=ComponentState.error,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    # Sorted for stable output: DEVICE before SYSTEM alphabetically.
    assert sig.link_query == "?type=DEVICE,SYSTEM&window=24h"


def test_detect_anomaly_cold_start_summary_overrides_to_starting_up() -> None:
    """R5: sequence_id==0 → calm "System is starting up" summary, NOT alarming copy.

    The default StateStore snapshot at construction has operating_mode=degraded
    AND sequence_id=0; without this special case the installer dashboard would
    show "System is operating with reduced functionality" during the ~10s
    lifespan-to-first-tick window.
    """
    snapshot = _snapshot(
        sequence_id=0,
        operating_mode=SystemOperatingMode.degraded,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.summary == "System is starting up"
    # The other fields still derive from operating_mode normally.
    assert sig.severity == "WARN"
    assert "SYSTEM_DEGRADED" in sig.anomaly_types


def test_detect_anomaly_post_cold_start_uses_normal_summary() -> None:
    """R5: sequence_id>=1 reverts to the operational summary table."""
    snapshot = _snapshot(
        sequence_id=1,
        operating_mode=SystemOperatingMode.degraded,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert sig.summary == "System is operating with reduced functionality"


def test_detect_anomaly_summary_counts_multiple_device_errors() -> None:
    """AC7: '2 devices degraded' when two roles are in error state."""
    snapshot = _snapshot(
        operating_mode=SystemOperatingMode.normal,
        battery_state=ComponentState.error,
        ev_charger_state=ComponentState.error,
    )
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert "2 devices degraded" in sig.summary


def test_detect_anomaly_anomaly_types_uses_frozenset_for_hashability() -> None:
    """R3 #1: AnomalySignature must be hashable (used as dict-key in dismiss machine)."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.fail_safe)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    assert isinstance(sig.anomaly_types, frozenset)
    # Hashable check — would raise if anomaly_types were a mutable set.
    _ = hash(sig)


def test_detect_anomaly_signature_is_frozen() -> None:
    """R3 #1: AnomalySignature must be immutable (dataclass(frozen=True))."""
    snapshot = _snapshot(operating_mode=SystemOperatingMode.fail_safe)
    sig = detect_anomaly_from_snapshot(snapshot)
    assert sig is not None
    with pytest.raises((AttributeError, TypeError, ValueError)):
        sig.severity = "WARN"  # type: ignore[misc]


# ── AC8 dismiss-state machine elevation table ────────────────────────────────


def _make_sig(
    severity: str = "WARN",
    types: frozenset[str] | None = None,
) -> AnomalySignature:
    """Test-only helper to build a synthetic AnomalySignature."""
    return AnomalySignature(
        severity=severity,  # type: ignore[arg-type]
        anomaly_types=types if types is not None else frozenset({"DEVICE_ERROR"}),  # type: ignore[arg-type]
        summary="test summary",
        link_query="?type=DEVICE&window=24h",
    )


def test_evaluate_dismiss_state_no_anomaly_returns_no_anomaly() -> None:
    """R2 / AC8: no current → no_anomaly regardless of dismissed state."""
    assert evaluate_dismiss_state(None, None) == "no_anomaly"
    assert evaluate_dismiss_state(None, _make_sig()) == "no_anomaly"


def test_evaluate_dismiss_state_no_dismissed_returns_render() -> None:
    """R2 / AC8: first time anomaly is seen → render."""
    assert evaluate_dismiss_state(_make_sig(), None) == "render"


def test_evaluate_dismiss_state_same_signature_returns_suppress() -> None:
    """R2 / AC8: same severity AND same types → suppress."""
    sig = _make_sig()
    assert evaluate_dismiss_state(sig, sig) == "suppress"


def test_evaluate_dismiss_state_severity_elevation_returns_render() -> None:
    """R2 / AC8: WARN → FAIL re-displays."""
    dismissed = _make_sig(severity="WARN", types=frozenset({"DEVICE_ERROR"}))
    current = _make_sig(severity="FAIL", types=frozenset({"DEVICE_ERROR"}))
    assert evaluate_dismiss_state(current, dismissed) == "render"


def test_evaluate_dismiss_state_new_type_returns_render() -> None:
    """R2 / AC8: a new anomaly type not in the dismissed set → render."""
    dismissed = _make_sig(types=frozenset({"DEVICE_ERROR"}))
    current = _make_sig(
        types=frozenset({"DEVICE_ERROR", "SYSTEM_DEGRADED"})  # type: ignore[arg-type]
    )
    assert evaluate_dismiss_state(current, dismissed) == "render"


def test_evaluate_dismiss_state_de_escalation_returns_suppress() -> None:
    """R2 / AC8: FAIL→WARN with covered types stays suppressed (de-escalation rule)."""
    dismissed = _make_sig(severity="FAIL", types=frozenset({"DEVICE_ERROR"}))
    current = _make_sig(severity="WARN", types=frozenset({"DEVICE_ERROR"}))
    assert evaluate_dismiss_state(current, dismissed) == "suppress"


def test_evaluate_dismiss_state_clear_and_return_same_signature_returns_suppress() -> None:
    """R2 / AC8: a clear-and-recur of the same signature does NOT re-display.

    (The conservative-behavior decision documented in AC8 dev notes.)
    """
    dismissed = _make_sig()
    # Anomaly cleared then returned with identical signature.
    current_same = _make_sig()
    assert evaluate_dismiss_state(current_same, dismissed) == "suppress"


def test_evaluate_dismiss_state_strict_subset_check_not_equality() -> None:
    """R3 #3: the new-type rule uses subset comparison, not equality.

    Removing a type but keeping the rest covered should NOT re-display.
    """
    dismissed = _make_sig(types=frozenset({"DEVICE_ERROR", "SYSTEM_DEGRADED"}))
    # current is a strict subset of dismissed.
    current = _make_sig(types=frozenset({"DEVICE_ERROR"}))
    assert evaluate_dismiss_state(current, dismissed) == "suppress"


# ── Module-level dismiss dict + accessors ─────────────────────────────────────


def test_anomaly_dismiss_dict_is_module_level_not_request_scoped() -> None:
    """AC10 #3: per-session dismiss state survives across function calls within
    the same process. The dict is module-level — no request-scoped, no
    client-cookie-stored alternative."""
    sig = _make_sig(severity="FAIL", types=frozenset({"SYSTEM_FAILED"}))
    dismiss_signature("session-1", sig)
    # Independent retrieval call returns the same signature.
    assert get_dismissed_signature("session-1") == sig


def test_dismiss_signature_overwrites_prior_dismissal() -> None:
    """R2 transition S4→S3: dismissing an elevated notice overwrites the prior entry."""
    initial = _make_sig(severity="WARN", types=frozenset({"DEVICE_ERROR"}))
    elevated = _make_sig(severity="FAIL", types=frozenset({"DEVICE_ERROR", "SYSTEM_FAILED"}))
    dismiss_signature("session-1", initial)
    dismiss_signature("session-1", elevated)
    assert get_dismissed_signature("session-1") == elevated


def test_get_dismissed_signature_returns_none_for_unknown_session() -> None:
    """Default for a never-dismissed session is None."""
    assert get_dismissed_signature("never-seen") is None


# ── Structural-discipline test: anomaly detection consumes only the snapshot ──


def test_anomaly_detection_consumes_only_snapshot() -> None:
    """AC10 test #1 — anomaly detection must NOT trigger DB I/O.

    Patches the three repo classes that the dashboard route uses (EnergyRepo,
    EventLogRepo, DeviceRepo) with side_effect=AssertionError. Calls
    detect_anomaly_from_snapshot. Asserts none are invoked.

    The function signature ``(SystemSnapshot) -> AnomalySignature | None``
    encodes the contract structurally, but this test catches a future
    refactor that smuggles a repo import into the function body.
    """
    with (
        patch(
            "open_ems.storage.repositories.energy_repo.EnergyRepo.get_current_monthly_peak_kw",
            side_effect=AssertionError("anomaly detection must NOT trigger EnergyRepo I/O"),
        ) as mock_peak,
        patch(
            "open_ems.storage.repositories.event_log_repo.EventLogRepo.list_recent",
            side_effect=AssertionError("anomaly detection must NOT trigger EventLogRepo I/O"),
        ) as mock_events,
        patch(
            "open_ems.storage.repositories.device_repo.DeviceRepo.list_all",
            side_effect=AssertionError("anomaly detection must NOT trigger DeviceRepo I/O"),
        ) as mock_devices,
    ):
        sig = detect_anomaly_from_snapshot(_snapshot(operating_mode=SystemOperatingMode.fail_safe))

    assert sig is not None
    mock_peak.assert_not_called()
    mock_events.assert_not_called()
    mock_devices.assert_not_called()


def test_anomaly_type_literal_covers_all_documented_types() -> None:
    """R3 #7: every AnomalyType in the literal MUST have a severity mapping.

    Guards against future enum additions (or removals) that ship without
    updating _SEVERITY_MAPPING.
    """
    from typing import get_args

    from open_ems.services.installer_anomaly import (  # noqa: PLC0415
        _SEVERITY_MAPPING,
    )
    from open_ems.services.installer_anomaly import AnomalyType as AT  # noqa: PLC0415

    declared = set(get_args(AT))
    mapped = set(_SEVERITY_MAPPING.keys())
    drift = declared ^ mapped
    assert declared == mapped, f"AnomalyType vs _SEVERITY_MAPPING drift: {drift}"
