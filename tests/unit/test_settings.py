from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from open_ems.core import ComponentState, DeviceRole, GridMeterState, InverterState, StateStore
from open_ems.settings import Settings


def test_port_default() -> None:
    s = Settings(_env_file=None)
    assert s.port == 8443


def test_tls_cert_path_default_none() -> None:
    s = Settings(_env_file=None)
    assert s.tls_cert_path is None


def test_tls_key_path_default_none() -> None:
    s = Settings(_env_file=None)
    assert s.tls_key_path is None


def test_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9443")
    s = Settings(_env_file=None)
    assert s.port == 9443


def test_tls_cert_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLS_CERT_PATH", "/tmp/test.pem")
    s = Settings(_env_file=None)
    assert s.tls_cert_path == "/tmp/test.pem"


def test_tls_key_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLS_KEY_PATH", "/tmp/test.key")
    s = Settings(_env_file=None)
    assert s.tls_key_path == "/tmp/test.key"


def test_secret_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secret_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "my-production-secret")
    s = Settings(_env_file=None)
    assert s.secret_key.get_secret_value() == "my-production-secret"


def test_installer_session_timeout_default() -> None:
    s = Settings(_env_file=None)
    assert s.installer_session_timeout_hours == 4


def test_homeowner_session_timeout_default() -> None:
    s = Settings(_env_file=None)
    assert s.homeowner_session_timeout_days == 30


def test_installer_session_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALLER_SESSION_TIMEOUT_HOURS", "8")
    s = Settings(_env_file=None)
    assert s.installer_session_timeout_hours == 8


def test_homeowner_session_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOWNER_SESSION_TIMEOUT_DAYS", "7")
    s = Settings(_env_file=None)
    assert s.homeowner_session_timeout_days == 7


def test_stale_threshold_default() -> None:
    s = Settings(_env_file=None)
    assert s.stale_threshold_seconds == 30


def test_stale_threshold_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STALE_THRESHOLD_SECONDS", "45")
    s = Settings(_env_file=None)
    assert s.stale_threshold_seconds == 45


def test_invalid_stale_threshold_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STALE_THRESHOLD_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


async def test_stale_threshold_from_settings_applied_to_state_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STALE_THRESHOLD_SECONDS", "1")
    settings = Settings(_env_file=None)
    store = StateStore(
        system_clock_status="valid",
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )
    old = datetime.now(UTC) - timedelta(seconds=2)
    snapshot = await store.publish(
        {
            DeviceRole.inverter: InverterState(
                device_id="inv-001",
                pv_power_kw=1.0,
                ac_power_kw=1.0,
                operating_mode="normal",
                read_at=old,
            ),
            DeviceRole.grid_meter: GridMeterState(
                device_id="grid-001",
                grid_power_kw=0.5,
                energy_delivered_kwh=10.0,
                energy_returned_kwh=0.0,
                received_at=old,
            ),
        }
    )
    assert snapshot.component_states[DeviceRole.inverter] == ComponentState.stale
    assert snapshot.component_states[DeviceRole.grid_meter] == ComponentState.stale
