from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_ems.services import rate_limiter as rl


@pytest.fixture(autouse=True)
def reset() -> None:
    rl._reset_all()


def test_not_limited_initially() -> None:
    assert not rl.is_rate_limited("admin")


def test_not_limited_below_threshold() -> None:
    for _ in range(rl._MAX_FAILURES - 1):
        rl.record_failure("admin")
    assert not rl.is_rate_limited("admin")


def test_limited_after_threshold_by_username() -> None:
    for _ in range(rl._MAX_FAILURES):
        rl.record_failure("admin")
    assert rl.is_rate_limited("admin")


def test_different_username_not_limited() -> None:
    for _ in range(rl._MAX_FAILURES):
        rl.record_failure("admin")
    assert not rl.is_rate_limited("other_user")


def test_reset_clears_limit() -> None:
    for _ in range(rl._MAX_FAILURES):
        rl.record_failure("admin")
    assert rl.is_rate_limited("admin")
    rl.reset_for_key("admin")
    assert not rl.is_rate_limited("admin")


def test_window_pruning_old_failures_not_counted() -> None:
    stale_time = datetime.now(UTC) - timedelta(seconds=rl._WINDOW_SECONDS + 1)
    rl._failures["user:admin"] = [stale_time] * rl._MAX_FAILURES
    assert not rl.is_rate_limited("admin")


def test_stale_entries_cleaned_from_dict() -> None:
    stale_time = datetime.now(UTC) - timedelta(seconds=rl._WINDOW_SECONDS + 1)
    rl._failures["user:admin"] = [stale_time] * rl._MAX_FAILURES
    rl.is_rate_limited("admin")
    assert "user:admin" not in rl._failures


def test_reset_all_clears_everything() -> None:
    for _ in range(rl._MAX_FAILURES):
        rl.record_failure("admin")
    rl._reset_all()
    assert not rl.is_rate_limited("admin")
