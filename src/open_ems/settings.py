from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: str = "open_ems.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    ntp_host: str | None = None
    ntp_drift_threshold_seconds: float = Field(default=2.0, gt=0.0)
    port: int = 8443
    secret_key: SecretStr
    initial_admin_password: SecretStr | None = None
    alembic_ini_path: str | None = None
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    installer_session_timeout_hours: int = Field(default=4, gt=0)
    homeowner_session_timeout_days: int = Field(default=30, gt=0)
    stale_threshold_seconds: int = Field(default=30, gt=0)
    trusted_proxy_ips: list[str] = []
    control_loop_interval_seconds: float = Field(default=10.0, gt=0.0)
    # Cold-start seed only. Runtime reads MUST go through ActiveConstraintsProvider.
    # After the first installer activation (Story 9.3), the DB row is authoritative
    # and the value here is unread.
    peak_limit_kw: float = Field(default=25.0, gt=0.0)
    # Cold-start seed only. Runtime reads MUST go through ActiveConstraintsProvider.
    # After the first installer activation (Story 9.3), the DB row is authoritative
    # and the value here is unread.
    battery_reserve_floor_percent: float = Field(default=20.0, ge=0.0, le=100.0)
    command_max_retries: int = Field(default=2, ge=0, le=5)
    command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)
    watchdog_missed_cycle_threshold: int = Field(default=2, ge=1, le=10)
    watchdog_cycle_deadline_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    # Story 9.4 — Deployment validation bounded timeouts. Per-check upper bound
    # for the whole check including any per-device fan-out; per-device probe
    # bound sized below the check bound so a single slow device cannot starve
    # sibling probes. Addresses Story 9.1's deferred-finding for bounded probe
    # fan-out in the validation context.
    deployment_validation_check_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    deployment_validation_device_probe_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    # Story 10.2 — EV homeowner override windows.
    # Total active-override window. The override expires after this regardless
    # of dispatch state; default 2 hours.
    ev_override_window_seconds: int = Field(default=7200, gt=0)
    # UI-side confirmation budget (rendered to the dashboard as a data-* hint /
    # JS constant). Not enforced server-side — the Alpine.js client uses it to
    # surface Fallback if EVChargerState.session_active=true is not observed
    # before the timeout.
    ev_override_confirmation_timeout_seconds: int = Field(default=30, ge=5, le=60)
    # Default rate sent with SetEVChargingRateCommand on homeowner-triggered
    # overrides. PolicyGuard P4 will reject if the EV charger profile lacks
    # set_charge_rate capability — that is the correct path (Fallback with
    # plain-language reason). Strictly positive: a 0 kW override would dispatch
    # successfully but never start a session, stranding the UI in Optimistic.
    ev_override_default_rate_kw: float = Field(default=11.0, gt=0.0)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
