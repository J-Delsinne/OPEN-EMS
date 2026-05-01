from collections.abc import Generator

import pytest

from open_ems.services.readiness import is_ready, mark_ready, reset


@pytest.fixture(autouse=True)
def clean_readiness() -> Generator[None, None, None]:
    reset()
    yield
    reset()


def test_initial_not_ready() -> None:
    assert not is_ready()


def test_mark_ready() -> None:
    mark_ready()
    assert is_ready()


def test_reset() -> None:
    mark_ready()
    reset()
    assert not is_ready()


def test_sd_notify_skipped_when_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    mark_ready()  # must not raise
    assert is_ready()
