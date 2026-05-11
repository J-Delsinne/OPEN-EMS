"""``ManualEntryService`` — installer-supplied device entry path (Story 9.1 AC5/AC6).

Owns the server-authoritative form validation, the OCPP rejection (AC5
clause 3), the post-persist probe dispatch, and the validation lifecycle bit
(AC6's one-way ``validated=0 → validated=1`` transition is enforced by
``DeviceRepo.mark_validated`` — there is no ``mark_unvalidated``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from pydantic import BaseModel, ConfigDict, StringConstraints

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.discovery import DeviceProbeError, DiscoveryService
from open_ems.core.devices import CapabilityStatus
from open_ems.storage.repositories.device_repo import (
    CapabilityStatusLiteral,
    DeviceRegistryEntry,
    DeviceRepo,
    ManualDeviceEntryInput,
)

logger = structlog.get_logger(__name__)

# AC5 contract: rejection message is an exact-match string consumed by tests.
OCPP_MANUAL_ENTRY_REJECTION_REASON: str = "manual_entry_unsupported_for_protocol: ocpp_1_6"


def _capability_to_literal(status: CapabilityStatus) -> CapabilityStatusLiteral:
    value: str = status.value
    if value not in ("full", "reduced", "unsupported"):
        raise ValueError(f"unexpected capability status: {value!r}")
    return value  # type: ignore[return-value]


# Protocol-specific address shapes. All checks are server-authoritative; the
# form template performs client-side hints only.
_HOST_PORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9._-]+):(?P<port>[1-9][0-9]{0,4})$")
# DSMR serial paths must live under ``/dev/``. The broader ``^/...`` shape would
# accept arbitrary filesystem paths that ``pyserial`` cannot open as a tty (e.g.
# ``/etc/passwd``); restricting at validation time gives the installer a clean
# 400 instead of a confusing ``open()`` system error at probe time.
_SERIAL_PATH_RE = re.compile(r"^/dev/[A-Za-z0-9._-]+$")


class ManualEntryFormError(ValueError):
    """Raised by ``ManualEntryService.validate_form`` with a stable ``reason`` code.

    The route handler translates the ``reason`` into either a 400 + inline field
    fragment (validation failure) or a 409 + banner (duplicate device_id).
    """

    def __init__(self, *, field: str | None, reason: str) -> None:
        super().__init__(reason)
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class ManualEntryProbeOutcome:
    """Result of one post-persist probe dispatch (AC5 step 3-5).

    ``status="validated"`` means ``mark_validated`` was called and the row is
    now ``validated=1``. ``status="failed"`` means the probe surfaced an
    exception; the row stays ``validated=0`` and the UI renders the failure
    reason.
    """

    status: Literal["validated", "failed"]
    failure_reason: str | None = None


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ManualEntryFormRequest(BaseModel):
    """Untyped form payload as received from the route handler.

    Field values are pre-stripped by FastAPI's ``Form`` parsing; we re-validate
    types and lengths here so the service does not trust the route.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    protocol: str
    address: str
    model: str | None = None
    firmware_version: str | None = None


class ManualEntryService:
    def __init__(
        self,
        *,
        device_repo: DeviceRepo,
        discovery_service: DiscoveryService,
    ) -> None:
        self._device_repo = device_repo
        self._discovery_service = discovery_service

    def validate_form(self, form: ManualEntryFormRequest) -> ManualDeviceEntryInput:
        """Server-authoritative form validation (AC5 step 1).

        Raises ``ManualEntryFormError`` with a stable ``reason`` code on the
        first failure. The route handler maps each ``reason`` to the inline
        fragment for the offending field.
        """
        device_id = form.device_id.strip()
        if not device_id:
            raise ManualEntryFormError(field="device_id", reason="device_id_required")
        if len(device_id) > 64:
            raise ManualEntryFormError(field="device_id", reason="device_id_too_long")

        if form.protocol not in ("modbus_tcp", "ocpp_1_6", "dsmr_p1"):
            raise ManualEntryFormError(
                field="protocol",
                reason=f"protocol_invalid: {form.protocol!r}",
            )

        # AC5 clause 3: OCPP manual entry is rejected at the route handler
        # (server-side) before any DeviceRepo call.
        if form.protocol == "ocpp_1_6":
            raise ManualEntryFormError(
                field="protocol",
                reason=OCPP_MANUAL_ENTRY_REJECTION_REASON,
            )

        address = form.address.strip()
        if not address:
            raise ManualEntryFormError(field="address", reason="address_required")
        _validate_address_shape(form.protocol, address)

        model = (form.model or "").strip() or None
        firmware = (form.firmware_version or "").strip() or None

        return ManualDeviceEntryInput(
            device_id=device_id,
            protocol=form.protocol,  # type: ignore[arg-type]
            address=address,
            model=model,
            firmware_version=firmware,
        )

    async def persist_unvalidated(
        self,
        entry: ManualDeviceEntryInput,
        *,
        now: datetime,
    ) -> DeviceRegistryEntry:
        """Persist as ``validated=0`` (AC5 step 2). Returns the new row."""
        return await self._device_repo.upsert_manual(entry, first_seen_at=now)

    async def probe_and_validate(
        self,
        entry: DeviceRegistryEntry,
    ) -> ManualEntryProbeOutcome:
        """AC5 steps 3-5: probe the persisted row; flip ``validated`` on success.

        Returns the outcome so the caller can compose the UI fragment. The
        method never raises ``DeviceProbeError`` to the caller — probe failures
        leave the row ``validated=0`` and surface as ``status="failed"``.
        """
        if entry.protocol == "ocpp_1_6":
            # The route handler rejects OCPP at validation; reaching this
            # branch would be a contract violation. Refuse loudly.
            raise RuntimeError(
                "ManualEntryService.probe_and_validate refuses ocpp_1_6 manual entries"
            )

        try:
            if entry.protocol == "modbus_tcp":
                host, port = _split_host_port(entry.address)
                await self._discovery_service.probe_modbus_endpoint(
                    device_id=entry.device_id,
                    host=host,
                    port=port,
                    model=entry.model,
                )
            else:  # dsmr_p1
                serial_port, tcp_host, tcp_port = _split_dsmr_address(entry.address)
                await self._discovery_service.probe_dsmr_endpoint(
                    device_id=entry.device_id,
                    serial_port=serial_port,
                    tcp_host=tcp_host,
                    tcp_port=tcp_port,
                )
        except DeviceProbeError as exc:
            logger.warning(
                "manual_entry_probe_failed",
                component="manual_entry",
                device_id=entry.device_id,
                protocol=entry.protocol,
                reason=str(exc),
            )
            return ManualEntryProbeOutcome(status="failed", failure_reason=str(exc))

        # R1.6 / R6: route the validation lifecycle through the capability
        # registry rather than trusting the probe's view directly. ``get_profile``
        # is the single source of truth (Story 9.0c), so its classification AND
        # ``limitation_reason`` are what we persist — this keeps the row column
        # consistent with everything else that resolves capabilities.
        profile = get_profile(entry.device_id, entry.model or "")
        await self._device_repo.mark_validated(
            entry.device_id,
            last_capability_status=_capability_to_literal(profile.capability_status),
            last_limitation_reason=profile.limitation_reason,
            last_seen_at=datetime.now(UTC),
        )
        logger.info(
            "manual_entry_validated",
            component="manual_entry",
            device_id=entry.device_id,
            protocol=entry.protocol,
            capability_status=profile.capability_status.value,
        )
        return ManualEntryProbeOutcome(status="validated")


def _validate_address_shape(protocol: str, address: str) -> None:
    if protocol == "modbus_tcp":
        if not _HOST_PORT_RE.match(address):
            raise ManualEntryFormError(
                field="address",
                reason="modbus_address_must_be_host_port",
            )
        return
    if protocol == "dsmr_p1":
        if address.startswith("/"):
            if not _SERIAL_PATH_RE.match(address):
                raise ManualEntryFormError(
                    field="address",
                    reason="dsmr_serial_path_invalid",
                )
            return
        if not _HOST_PORT_RE.match(address):
            raise ManualEntryFormError(
                field="address",
                reason="dsmr_address_must_be_host_port_or_serial_path",
            )
        return
    # Defense-in-depth: ``validate_form`` already whitelists the protocol, so
    # this branch is unreachable through the normal flow. A future direct caller
    # that bypasses ``validate_form`` would otherwise silently succeed.
    raise ManualEntryFormError(
        field="protocol",
        reason=f"protocol_invalid: {protocol!r}",
    )


def _split_host_port(address: str) -> tuple[str, int]:
    match = _HOST_PORT_RE.match(address)
    if match is None:
        raise ValueError(f"invalid host:port: {address!r}")
    return match.group("host"), int(match.group("port"))


def _split_dsmr_address(
    address: str,
) -> tuple[str | None, str | None, int | None]:
    if address.startswith("/"):
        return address, None, None
    host, port = _split_host_port(address)
    return None, host, port
