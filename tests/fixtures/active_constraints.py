"""Shared test fixture: pre-hydrated ``ActiveConstraintsProvider``.

Story 9.0b adds an ``ActiveConstraintsProvider`` parameter to ``PolicyGuard``
and ``ControlLoop``. Most tests don't exercise hydration / reload — they
just need a hydrated provider whose ``get()`` returns values seeded from a
``Settings`` instance. This helper builds one synchronously by setting the
cached snapshot directly, bypassing the async ``hydrate()`` path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from open_ems.core.constraints import ActiveConstraints
from open_ems.services.active_constraints import ActiveConstraintsProvider
from open_ems.settings import Settings
from open_ems.storage.repositories.config_repo import ConfigRepo

_DEFAULT_ACTIVATED_AT = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def make_active_constraints_provider(
    settings: Settings,
    *,
    activated_at: datetime = _DEFAULT_ACTIVATED_AT,
) -> ActiveConstraintsProvider:
    """Return a hydrated provider seeded from ``settings`` (sync; no DB)."""
    provider = ActiveConstraintsProvider(repo=MagicMock(spec=ConfigRepo), settings=settings)
    provider._current = ActiveConstraints(  # noqa: SLF001
        peak_limit_kw=settings.peak_limit_kw,
        battery_reserve_floor_percent=settings.battery_reserve_floor_percent,
        config_version=0,
        activated_at=activated_at,
    )
    return provider
