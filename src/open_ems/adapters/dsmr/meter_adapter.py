"""Domain-level grid meter adapter: translates RawDSMRState → GridMeterState."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError

from open_ems.adapters.capabilities import get_profile
from open_ems.adapters.dsmr.p1 import DSMRAdapter
from open_ems.adapters.protocol import ProtocolDegradedState, RawDSMRState
from open_ems.core.devices import (
    DegradedDeviceState,
    DeviceCapabilityProfile,
    DeviceRole,
    GridMeterState,
)

logger = structlog.get_logger(__name__)

_DSMR_STALE_SECONDS: float = 60.0

# PLACEHOLDER — verify against actual P1 spec for other DSMR versions
_OBIS_USAGE = "1-0:1.7.0"  # current_electricity_usage (kW, always >= 0)
_OBIS_DELIVERY = "1-0:2.7.0"  # current_electricity_delivery (kW, always >= 0)
_OBIS_USED_T1 = "1-0:1.8.1"  # electricity_used_tariff_1 (kWh)
_OBIS_USED_T2 = "1-0:1.8.2"  # electricity_used_tariff_2 (kWh)
_OBIS_DELIV_T1 = "1-0:2.8.1"  # electricity_delivered_tariff_1 (kWh)
_OBIS_DELIV_T2 = "1-0:2.8.2"  # electricity_delivered_tariff_2 (kWh)


class MissingDSMRFieldError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"missing_dsmr_field:{key}")


class GridMeterAdapter:
    """Domain-level grid meter adapter: translates RawDSMRState → GridMeterState."""

    def __init__(
        self,
        device_id: str,
        protocol_adapter: DSMRAdapter,
    ) -> None:
        self.device_id = device_id
        self._protocol_adapter = protocol_adapter

    async def connect(self) -> None:
        await self._protocol_adapter.start()

    async def disconnect(self) -> None:
        await self._protocol_adapter.stop()

    async def get_state(self) -> GridMeterState | DegradedDeviceState:
        raw = await self._protocol_adapter.get_raw_state()
        if isinstance(raw, ProtocolDegradedState):
            return self._to_degraded(raw.reason, raw.occurred_at)

        if not isinstance(raw, RawDSMRState):
            return self._to_degraded("unexpected_raw_state_type", datetime.now(UTC))

        age_s = (datetime.now(UTC) - raw.received_at).total_seconds()
        if age_s > _DSMR_STALE_SECONDS:
            return self._to_degraded("dsmr_stale", datetime.now(UTC))

        try:
            return _map_telegram(raw, self.device_id)
        except (MissingDSMRFieldError, ValidationError) as exc:
            reason = (
                str(exc) if isinstance(exc, MissingDSMRFieldError) else f"validation_error:{exc}"
            )
            return self._to_degraded(reason, datetime.now(UTC))

    async def get_capabilities(self) -> DeviceCapabilityProfile:
        profile = get_profile(
            device_id=self.device_id,
            model="dsmr_p1",
            firmware_version=None,
        )
        if profile.limitation_reason is not None:
            logger.warning(
                "capability_profile_unknown",
                component="adapters",
                device_id=self.device_id,
                model="dsmr_p1",
                firmware_version=None,
            )
        return profile

    def _to_degraded(self, reason: str, occurred_at: datetime) -> DegradedDeviceState:
        logger.warning(
            "device_degraded",
            component="adapters",
            device_id=self.device_id,
            role=DeviceRole.grid_meter.value,
            reason=reason,
        )
        return DegradedDeviceState(
            device_id=self.device_id,
            role=DeviceRole.grid_meter,
            reason=reason,
            occurred_at=occurred_at,
        )


def _get_field(fields: dict[str, Any], key: str) -> float:
    entry = fields.get(key)
    if entry is None or entry.get("value") is None:
        raise MissingDSMRFieldError(key)
    try:
        return float(entry["value"])
    except (TypeError, ValueError) as exc:
        raise MissingDSMRFieldError(key) from exc


def _map_telegram(raw: RawDSMRState, device_id: str) -> GridMeterState:
    fields = raw.telegram_fields
    usage_kw = _get_field(fields, _OBIS_USAGE)
    delivery_kw = _get_field(fields, _OBIS_DELIVERY)
    grid_power_kw = usage_kw - delivery_kw

    energy_delivered_kwh = _get_field(fields, _OBIS_USED_T1) + _get_field(fields, _OBIS_USED_T2)
    energy_returned_kwh = _get_field(fields, _OBIS_DELIV_T1) + _get_field(fields, _OBIS_DELIV_T2)

    return GridMeterState(
        device_id=device_id,
        grid_power_kw=grid_power_kw,
        energy_delivered_kwh=energy_delivered_kwh,
        energy_returned_kwh=energy_returned_kwh,
        received_at=raw.received_at,
    )
