from __future__ import annotations

import pytest

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
