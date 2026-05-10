"""``ActiveConstraintsProvider`` — single in-memory owner of active site constraints.

Story 9.0b: PolicyGuard and ControlLoop both read ``peak_limit_kw`` and
``battery_reserve_floor_percent`` from this provider on the hot path. The
provider is constructed and hydrated exactly once during lifespan startup
(``app.py``); subsequent activations from the installer wizard call
``reload()`` to refresh the snapshot without a process restart.

Contract summary:

* ``hydrate()`` runs once at startup. Reads the active row from
  ``ConfigRepo``; if the table is empty (fresh deployment, no installer
  activation yet) it seeds the snapshot from ``Settings`` with
  ``config_version=0``. ``hydrate()`` is idempotent — a second call is a
  no-op + a structured warning.
* ``reload()`` is the post-hydrate update path. Re-reads the active row.
  If the table is unexpectedly empty post-hydrate, the existing snapshot is
  preserved (and a warning is logged) — re-seeding from Settings here
  would mask a DB regression.
* ``get()`` is synchronous, returns the cached immutable snapshot, and
  raises ``RuntimeError`` if called before ``hydrate()``. PolicyGuard's
  hot path MUST never block on DB I/O.
* Snapshot replacement is atomic: ``self._current = new`` is a single
  CPython attribute assignment; readers either see the old reference or
  the new one, never a torn read.
* ``hydrate`` and ``reload`` are serialized by ``self._lock`` so two
  concurrent calls cannot interleave their DB read + assignment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from open_ems.core.constraints import ActiveConstraints
from open_ems.settings import Settings
from open_ems.storage.repositories.config_repo import ConfigRepo

ACTIVE_CONSTRAINTS_PROVIDER_VERSION: str = "1.0"

logger = structlog.get_logger(__name__)


class _RaisingRepo:
    """Sentinel repo used by ``ActiveConstraintsProvider.from_snapshot()``.

    Any access to a public attribute or method raises ``AssertionError`` —
    guards against a future PolicyGuard / ControlLoop hot-path regression that
    reads ``ConfigRepo`` directly instead of going through
    ``ActiveConstraintsProvider``.

    Story 9.0c review (P2): the original implementation returned an async
    closure for every ``__getattr__`` call — but a sync attribute access like
    ``if repo.is_connected: ...`` or ``repo.connection.commit()`` would
    silently evaluate the closure as truthy (or AttributeError-trip on
    ``.commit``) without firing the assertion. The fix is to raise at attribute
    access time. Dunder names (``__class__``, etc.) are allowed to fall through
    to default lookup so Python internals don't trip the sentinel.
    """

    def __getattr__(self, name: str) -> Any:
        # Dunder attributes are Python's internal protocol — fall through.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"PolicyGuard hot path must not touch ConfigRepo (attempted: .{name})")


class ActiveConstraintsProvider:
    def __init__(self, *, repo: ConfigRepo, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings
        self._current: ActiveConstraints | None = None
        self._lock = asyncio.Lock()

    @property
    def is_hydrated(self) -> bool:
        return self._current is not None

    async def hydrate(self) -> None:
        """Read the active row from ``ConfigRepo``; seed from Settings if empty.

        Idempotent: a second call logs ``constraints_provider_already_hydrated``
        and returns without touching state.
        """
        async with self._lock:
            if self._current is not None:
                logger.warning(
                    "constraints_provider_already_hydrated",
                    component="config",
                    config_version=self._current.config_version,
                )
                return
            row = await self._repo.get_active()
            if row is not None:
                self._current = row
                return
            self._current = ActiveConstraints(
                peak_limit_kw=self._settings.peak_limit_kw,
                battery_reserve_floor_percent=self._settings.battery_reserve_floor_percent,
                config_version=0,
                activated_at=datetime.now(UTC),
            )
            logger.info(
                "constraints_hydrate_seeded_from_settings",
                component="config",
                peak_limit_kw=self._current.peak_limit_kw,
                battery_reserve_floor_percent=self._current.battery_reserve_floor_percent,
            )

    async def reload(self) -> None:
        """Re-read the active row and atomically replace the cached snapshot.

        Called by the installer activation endpoint after
        ``ConfigRepo.activate()`` commits.

        Two regression cases preserve the existing snapshot rather than
        mask the regression:

        * DB empty post-hydrate (table truncated / restored to fresh state):
          log ``constraints_reload_found_empty`` and keep the cached snapshot.
        * DB row has a ``config_version`` LOWER than the cached snapshot
          (post-restore-from-backup, manual ``DELETE`` of latest row): log
          ``constraints_reload_version_regressed`` and keep the cached
          snapshot. Downstream consumers tagging events with
          ``config_version`` continue to see a monotonic sequence.
        """
        async with self._lock:
            row = await self._repo.get_active()
            if row is None:
                logger.warning(
                    "constraints_reload_found_empty",
                    component="config",
                    cached_config_version=(
                        self._current.config_version if self._current is not None else None
                    ),
                )
                return
            if self._current is not None and row.config_version < self._current.config_version:
                logger.warning(
                    "constraints_reload_version_regressed",
                    component="config",
                    cached_config_version=self._current.config_version,
                    db_config_version=row.config_version,
                )
                return
            self._current = row

    def get(self) -> ActiveConstraints:
        """Return the cached snapshot. Raises if called before ``hydrate()``."""
        snapshot = self._current
        if snapshot is None:
            raise RuntimeError("ActiveConstraintsProvider.get() called before hydrate()")
        return snapshot

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ActiveConstraints,
        *,
        settings: Settings | None = None,
    ) -> ActiveConstraintsProvider:
        """Test-only constructor. Production code MUST call ``await hydrate()``.

        Returns a provider already hydrated with the given ``snapshot``. The
        underlying ``repo`` is a ``_RaisingRepo`` sentinel — any awaited method
        on it raises ``AssertionError``, so this constructor doubles as a guard
        against a future PolicyGuard hot-path regression that touches the DB.

        Story 9.0c (AC8): new tests use this constructor instead of the
        ``# noqa: SLF001`` private-field write pattern from prior fixtures.
        """
        s = settings if settings is not None else Settings(_env_file=None)  # type: ignore[call-arg]
        provider = cls(repo=cast(ConfigRepo, _RaisingRepo()), settings=s)
        provider._current = snapshot
        return provider
