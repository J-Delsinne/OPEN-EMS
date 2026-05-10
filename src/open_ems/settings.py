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
    peak_limit_kw: float = Field(default=25.0, gt=0.0)
    battery_reserve_floor_percent: float = Field(default=20.0, ge=0.0, le=100.0)
    command_max_retries: int = Field(default=2, ge=0, le=5)
    command_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=5.0)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
