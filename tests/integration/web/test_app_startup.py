"""Integration tests for the FastAPI lifespan startup gates.

Story 9.0c (AC5): capability-registry drift detected by
``validate_capability_registry_alignment()`` aborts boot with
``startup_failed reason="capability_registry_drift"`` and ``SystemExit(1)``.

Same enforcement class as the existing migration / constraints-hydrate
failures — close_database() must still run on this failure path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from open_ems.adapters.capabilities import CapabilityRegistryDriftError
from open_ems.storage.database import close_database as real_close_database
from open_ems.web.app import create_app, lifespan


async def test_lifespan_aborts_with_system_exit_on_capability_registry_drift() -> None:
    """If ``validate_capability_registry_alignment()`` raises, lifespan must SystemExit(1).

    P1 (9.0c review): also asserts ``close_database()`` is invoked on this
    failure path. The outer ``try:`` in ``web/app.py`` opens AFTER
    ``init_database()`` so the ``finally`` cleans up — a future refactor that
    moves the validator BEFORE ``init_database()`` would silently regress the
    close path; this assertion locks the invariant.
    """
    app = create_app()

    mock_logger = MagicMock()
    with (
        patch(
            "open_ems.web.app.validate_capability_registry_alignment",
            side_effect=CapabilityRegistryDriftError(["bogus_future_model_v1"]),
        ),
        patch("open_ems.web.app.logger", mock_logger),
        # P1: spy on close_database (wraps=real fn) so the real cleanup still runs;
        # otherwise the leaked DB connection breaks subsequent tests.
        patch(
            "open_ems.web.app.close_database",
            wraps=real_close_database,
        ) as mock_close_database,
    ):
        with pytest.raises(SystemExit) as exc_info:
            async with lifespan(app):
                pass

    assert exc_info.value.code == 1
    # AC5: log carries the explicit reason + the list of missing models.
    error_calls = [
        c for c in mock_logger.error.call_args_list if c.args and c.args[0] == "startup_failed"
    ]
    assert error_calls, "expected a startup_failed error log on registry drift"
    last_call = error_calls[-1]
    assert last_call.kwargs.get("reason") == "capability_registry_drift"
    assert last_call.kwargs.get("missing_models") == ["bogus_future_model_v1"]
    assert last_call.kwargs.get("component") == "startup"
    # P1: close_database MUST run on the drift failure path (lifespan finally block).
    mock_close_database.assert_awaited_once()


async def test_lifespan_aborts_before_constraints_hydrate_on_drift() -> None:
    """Drift detection runs BEFORE ``ActiveConstraintsProvider`` is constructed."""
    app = create_app()

    with (
        patch(
            "open_ems.web.app.validate_capability_registry_alignment",
            side_effect=CapabilityRegistryDriftError(["another_missing_model"]),
        ),
        patch("open_ems.web.app.ActiveConstraintsProvider") as mock_provider_cls,
    ):
        with pytest.raises(SystemExit):
            async with lifespan(app):
                pass

    # Lifespan exited before constructing the provider — ordering invariant.
    mock_provider_cls.assert_not_called()


async def test_lifespan_runs_validator_on_happy_path() -> None:
    """Sanity: when the validator returns None, lifespan reaches the application body."""
    app = create_app()

    with patch(
        "open_ems.web.app.validate_capability_registry_alignment",
        return_value=None,
    ) as mock_validator:
        async with lifespan(app):
            pass

    mock_validator.assert_called_once()
