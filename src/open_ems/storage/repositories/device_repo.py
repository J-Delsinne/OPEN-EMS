"""``DeviceRepo`` — single source of truth for the installer-acknowledged device set.

Story 9.1: the ``device_registry`` table records every device the installer has
intentionally added (manual entry) or that has self-registered (OCPP). Live
scans never write here directly; only the manual-entry path and the
``mark_validated`` / ``update_last_seen`` / ``acknowledge_unvalidated`` flows
mutate rows. The bit ``validated`` transitions ``0 → 1`` exactly once (AC6);
no ``mark_unvalidated`` is exposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

import aiosqlite
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from open_ems.storage.database import get_connection, get_write_lock

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

Protocol = Literal["modbus_tcp", "ocpp_1_6", "dsmr_p1"]
Source = Literal["manual_entry", "ocpp_self_registration"]
CapabilityStatusLiteral = Literal["full", "reduced", "unsupported"]


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _parse_utc(raw: str, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return _require_utc(parsed, field_name)


def _parse_optional_utc(raw: str | None, field_name: str) -> datetime | None:
    if raw is None:
        return None
    return _parse_utc(raw, field_name)


class DeviceRegistryEntry(BaseModel):
    """Immutable view of one ``device_registry`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    protocol: Protocol
    address: NonEmptyStr
    model: str | None = None
    firmware_version: str | None = None
    source: Source
    validated: bool
    last_capability_status: CapabilityStatusLiteral | None = None
    last_limitation_reason: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime | None = None
    installer_acknowledged_unvalidated_at: datetime | None = None

    @field_validator("first_seen_at")
    @classmethod
    def _first_seen_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "first_seen_at")

    @field_validator("last_seen_at")
    @classmethod
    def _last_seen_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "last_seen_at")

    @field_validator("installer_acknowledged_unvalidated_at")
    @classmethod
    def _ack_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "installer_acknowledged_unvalidated_at")

    @field_validator("model", "firmware_version", "last_limitation_reason")
    @classmethod
    def _optional_strings_non_empty_when_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value == "":
            raise ValueError("optional string field must be omitted instead of empty")
        return value

    @model_validator(mode="after")
    def _validated_requires_capability_status(self) -> DeviceRegistryEntry:
        # R3 #1: a validated row MUST have a resolved capability classification.
        # ``mark_validated`` enforces this on the write side; this guard enforces
        # it on every read (including round-trip from the DB) so a manually
        # tampered row cannot present ``validated=1`` with no classification.
        if self.validated and self.last_capability_status is None:
            raise ValueError(
                "validated=True requires last_capability_status to be set"
                f" (device_id={self.device_id!r})"
            )
        return self


class ManualDeviceEntryInput(BaseModel):
    """Validated input for ``DeviceRepo.upsert_manual``.

    Used by the route handler after server-side form validation has resolved
    the protocol-specific address shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    protocol: Protocol
    address: NonEmptyStr
    model: str | None = Field(default=None, min_length=1)
    firmware_version: str | None = Field(default=None, min_length=1)


class DeviceRepo:
    def __init__(self, conn: aiosqlite.Connection | None = None) -> None:
        self._conn = conn if conn is not None else get_connection()

    async def list_all(self) -> list[DeviceRegistryEntry]:
        async with self._conn.execute(
            "SELECT device_id, protocol, address, model, firmware_version, source,"
            " validated, last_capability_status, last_limitation_reason,"
            " first_seen_at, last_seen_at, installer_acknowledged_unvalidated_at"
            " FROM device_registry"
            " ORDER BY first_seen_at ASC, device_id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_entry(row) for row in rows]

    async def get_by_device_id(self, device_id: str) -> DeviceRegistryEntry | None:
        async with self._conn.execute(
            "SELECT device_id, protocol, address, model, firmware_version, source,"
            " validated, last_capability_status, last_limitation_reason,"
            " first_seen_at, last_seen_at, installer_acknowledged_unvalidated_at"
            " FROM device_registry WHERE device_id = ?",
            (device_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_entry(row)

    async def upsert_manual(
        self,
        entry: ManualDeviceEntryInput,
        *,
        first_seen_at: datetime,
    ) -> DeviceRegistryEntry:
        """Insert a manual-entry row with ``validated=0``.

        Raises ``ValueError`` if ``device_id`` already exists. The
        ``first_seen_at`` is taken from the caller so tests can pin time.
        """
        _require_utc(first_seen_at, "first_seen_at")
        async with get_write_lock():
            await self._conn.commit()
            try:
                await self._conn.execute(
                    "INSERT INTO device_registry"
                    " (device_id, protocol, address, model, firmware_version, source,"
                    "  validated, first_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, 'manual_entry', 0, ?)",
                    (
                        entry.device_id,
                        entry.protocol,
                        entry.address,
                        entry.model,
                        entry.firmware_version,
                        first_seen_at.isoformat(),
                    ),
                )
                await self._conn.commit()
            except aiosqlite.IntegrityError as exc:
                raise ValueError(f"device_id already registered: {entry.device_id!r}") from exc
        result = await self.get_by_device_id(entry.device_id)
        if result is None:
            raise RuntimeError("upsert_manual: row missing after insert")
        return result

    async def mark_validated(
        self,
        device_id: str,
        *,
        last_capability_status: CapabilityStatusLiteral,
        last_limitation_reason: str | None,
        last_seen_at: datetime,
    ) -> None:
        """Flip ``validated`` to 1 and update capability/seen fields atomically.

        One-way: ``DeviceRepo`` exposes no ``mark_unvalidated`` (AC6). Raises
        ``ValueError`` if no row matches.
        """
        _require_utc(last_seen_at, "last_seen_at")
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                # Clearing ``installer_acknowledged_unvalidated_at`` here keeps the
                # audit trail honest: once a row genuinely validates, the prior
                # "installer waved this through unvalidated" record no longer
                # describes the row's current state.
                "UPDATE device_registry SET"
                " validated = 1,"
                " last_capability_status = ?,"
                " last_limitation_reason = ?,"
                " last_seen_at = ?,"
                " installer_acknowledged_unvalidated_at = NULL"
                " WHERE device_id = ?",
                (
                    last_capability_status,
                    last_limitation_reason,
                    last_seen_at.isoformat(),
                    device_id,
                ),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(f"No device_registry row for device_id={device_id!r}")
            await self._conn.commit()

    async def update_last_seen(
        self,
        device_id: str,
        *,
        last_capability_status: CapabilityStatusLiteral,
        last_limitation_reason: str | None,
        last_seen_at: datetime,
    ) -> None:
        """Refresh ``last_*`` fields without touching ``validated``.

        Used by subsequent successful scans against already-validated rows.
        """
        _require_utc(last_seen_at, "last_seen_at")
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                "UPDATE device_registry SET"
                " last_capability_status = ?,"
                " last_limitation_reason = ?,"
                " last_seen_at = ?"
                " WHERE device_id = ?",
                (
                    last_capability_status,
                    last_limitation_reason,
                    last_seen_at.isoformat(),
                    device_id,
                ),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(f"No device_registry row for device_id={device_id!r}")
            await self._conn.commit()

    async def acknowledge_unvalidated(
        self,
        device_id: str,
        *,
        acknowledged_at: datetime,
    ) -> None:
        """Set ``installer_acknowledged_unvalidated_at`` for an unvalidated row.

        Raises ``ValueError`` if the device_id is not present or if the row is
        already ``validated=1`` (acknowledgement is only meaningful for the
        unvalidated state).
        """
        _require_utc(acknowledged_at, "acknowledged_at")
        async with get_write_lock():
            await self._conn.commit()
            async with self._conn.execute(
                "UPDATE device_registry SET"
                " installer_acknowledged_unvalidated_at = ?"
                " WHERE device_id = ? AND validated = 0",
                (acknowledged_at.isoformat(), device_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    await self._conn.commit()
                    raise ValueError(
                        f"No unvalidated device_registry row for device_id={device_id!r}"
                    )
            await self._conn.commit()


def _row_to_entry(row: aiosqlite.Row | tuple[object, ...]) -> DeviceRegistryEntry:
    return DeviceRegistryEntry(
        device_id=str(row[0]),
        protocol=str(row[1]),  # type: ignore[arg-type]
        address=str(row[2]),
        model=str(row[3]) if row[3] is not None else None,
        firmware_version=str(row[4]) if row[4] is not None else None,
        source=str(row[5]),  # type: ignore[arg-type]
        validated=bool(row[6]),
        last_capability_status=(
            str(row[7]) if row[7] is not None else None  # type: ignore[arg-type]
        ),
        last_limitation_reason=str(row[8]) if row[8] is not None else None,
        first_seen_at=_parse_utc(str(row[9]), "first_seen_at"),
        last_seen_at=_parse_optional_utc(
            str(row[10]) if row[10] is not None else None,
            "last_seen_at",
        ),
        installer_acknowledged_unvalidated_at=_parse_optional_utc(
            str(row[11]) if row[11] is not None else None,
            "installer_acknowledged_unvalidated_at",
        ),
    )
