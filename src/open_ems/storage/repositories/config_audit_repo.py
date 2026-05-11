from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from open_ems.storage.database import get_connection

SAFETY_RELEVANT_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "peak_consumption_limit",
        "battery_reserve_floor",
        # Story 9.3: EV charging window endpoints emit one combined audit row
        # under this field. ``ev_charging_window_preference`` is the legacy
        # 6.3-era name kept for completeness in case prior audit rows reference
        # it; new writes (Story 9.3) use ``ev_charging_window``.
        "ev_charging_window",
        "ev_charging_window_preference",
        "energy_strategy_default",
        "device_role_assignment",
        "capability_override",
        "capability_acceptance",
    }
)
VALID_CONFIG_AUDIT_ACTORS: frozenset[str] = frozenset({"system", "installer", "homeowner"})


@dataclass(frozen=True)
class ConfigAuditChange:
    field: str
    previous_value: dict[str, object]
    new_value: dict[str, object]


class ConfigAuditRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def append_activation(
        self,
        *,
        actor: str,
        changes: Sequence[ConfigAuditChange],
        config_version: int | None = None,
        commit: bool = True,
    ) -> int:
        """Append one config_audit_log row per change.

        ``config_version`` and ``commit`` exist so ``ConfigRepo.activate``
        can drive a single atomic transaction that spans both
        ``active_constraints`` and ``config_audit_log`` (Story 9.0b AC2):
        the caller passes the version it has already chosen and skips the
        local commit so the outer transaction commits both inserts together.
        Direct callers (no outer transaction) get the legacy semantics.
        """
        if actor not in VALID_CONFIG_AUDIT_ACTORS:
            raise ValueError(f"Invalid actor {actor!r}")
        if not changes:
            raise ValueError("changes must be non-empty")

        serialized_changes: list[tuple[str, str, str]] = []
        for change in changes:
            if change.field not in SAFETY_RELEVANT_CONFIG_FIELDS:
                raise ValueError(f"Unknown config audit field {change.field!r}")
            serialized_changes.append(
                (
                    change.field,
                    _serialize_config_value(change.previous_value),
                    _serialize_config_value(change.new_value),
                )
            )

        if config_version is None:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(config_version), 0) + 1 FROM config_audit_log"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("SQLite did not return a config version.")
            config_version = int(row[0])
        timestamp = datetime.now(UTC).isoformat()

        await self._conn.executemany(
            "INSERT INTO config_audit_log"
            " (actor, timestamp, field, previous_value, new_value, config_version)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (actor, timestamp, field, previous_value, new_value, config_version)
                for field, previous_value, new_value in serialized_changes
            ],
        )
        if commit:
            await self._conn.commit()
        return config_version


def _serialize_config_value(value: dict[str, object]) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("config audit values must be JSON-serializable") from exc
