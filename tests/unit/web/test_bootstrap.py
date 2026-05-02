from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from open_ems.storage.repositories.user_repo import UserRepo, verify_password
from open_ems.web.app import _bootstrap_admin_if_needed


async def test_bootstrap_exits_when_no_password(user_repo: UserRepo) -> None:
    with pytest.raises(SystemExit) as exc_info:
        await _bootstrap_admin_if_needed(user_repo, None)
    assert exc_info.value.code == 1


async def test_bootstrap_failure_log_fields(user_repo: UserRepo) -> None:
    mock_logger = MagicMock()
    with patch("open_ems.web.app.logger", mock_logger):
        with pytest.raises(SystemExit):
            await _bootstrap_admin_if_needed(user_repo, None)
    mock_logger.error.assert_called_once_with(
        "startup_failed",
        reason="INITIAL_ADMIN_PASSWORD required when no users exist",
        component="startup",
    )


async def test_bootstrap_creates_admin_user(user_repo: UserRepo) -> None:
    password = SecretStr("bootstrap-secret")
    await _bootstrap_admin_if_needed(user_repo, password)
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert row["username"] == "admin"
    assert row["role"] == "installer"
    assert row["must_change_password"] == 1
    assert row["hashed_password"].startswith("$2b$")


async def test_bootstrap_stores_valid_hash(user_repo: UserRepo) -> None:
    raw = "my-admin-pass"
    await _bootstrap_admin_if_needed(user_repo, SecretStr(raw))
    row = await user_repo.get_by_username("admin")
    assert row is not None
    assert verify_password(raw, row["hashed_password"])


async def test_bootstrap_password_not_logged(user_repo: UserRepo) -> None:
    secret = "super-secret-bootstrap-password"
    mock_logger = MagicMock()
    with patch("open_ems.web.app.logger", mock_logger):
        await _bootstrap_admin_if_needed(user_repo, SecretStr(secret))
    assert secret not in str(mock_logger.mock_calls)


async def test_bootstrap_skips_when_users_exist(user_repo: UserRepo) -> None:
    await _bootstrap_admin_if_needed(user_repo, SecretStr("first"))
    row_after_first = await user_repo.get_by_username("admin")
    assert row_after_first is not None
    original_hash = row_after_first["hashed_password"]

    await _bootstrap_admin_if_needed(user_repo, None)
    assert await user_repo.count() == 1
    row_after_second = await user_repo.get_by_username("admin")
    assert row_after_second is not None
    assert row_after_second["hashed_password"] == original_hash
