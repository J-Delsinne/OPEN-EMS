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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
