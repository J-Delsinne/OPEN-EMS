"""DSMR P1 adapter — continuous telegram reading via serial or TCP transport."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, Self

import structlog
from dsmr_parser import telegram_specifications
from dsmr_parser.clients.telegram_buffer import TelegramBuffer
from dsmr_parser.parsers import TelegramParser
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from open_ems.adapters.protocol import (
    ProtocolCommandResult,
    ProtocolDegradedState,
    RawDSMRState,
    RawProtocolCommand,
)

logger = structlog.get_logger(__name__)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_DSMR_SPECS: dict[str, object] = {
    "2.2": telegram_specifications.V2_2,
    "4": telegram_specifications.V4,
    "4+": telegram_specifications.V5,
    "5": telegram_specifications.V5,
    "5B": telegram_specifications.BELGIUM_FLUVIUS,
}


class DSMRAdapterConfig(BaseModel):
    """Configuration for one DSMR P1 adapter instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: NonEmptyStr
    serial_port: str | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None
    dsmr_version: Literal["2.2", "4", "4+", "5", "5B"] = "5"
    stale_after_s: float = Field(default=60.0, gt=0)
    reconnect_delay_s: float = Field(default=5.0, ge=0)

    @field_validator("stale_after_s", "reconnect_delay_s", mode="before")
    @classmethod
    def _float_fields_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("value must be a numeric timeout or delay")
        return value

    @model_validator(mode="after")
    def _validate_transport(self) -> Self:
        has_serial = self.serial_port is not None
        has_tcp_host = self.tcp_host is not None
        has_tcp_port = self.tcp_port is not None
        has_tcp = has_tcp_host and has_tcp_port

        if has_tcp_host != has_tcp_port:
            raise ValueError("tcp_host and tcp_port must both be set for TCP transport")
        if has_serial and has_tcp:
            raise ValueError(
                "only one transport may be configured: serial_port or (tcp_host + tcp_port)"
            )
        if not has_serial and not has_tcp:
            raise ValueError(
                "exactly one transport must be configured: serial_port or (tcp_host + tcp_port)"
            )
        if self.tcp_port is not None and not (1 <= self.tcp_port <= 65535):
            raise ValueError("tcp_port must be between 1 and 65535")
        return self


class _DSMRTransport(Protocol):
    """Minimal read/close interface over a byte stream source."""

    async def read_line(self) -> bytes:
        """Read one line; raises OSError on disconnect or EOF."""

    async def close(self) -> None:
        """Close the underlying transport."""


class _TelegramSource(Protocol):
    """Factory that opens a new transport connection."""

    async def open(self) -> _DSMRTransport:
        """Open and return a fresh transport; raises OSError on failure."""


class _SerialTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read_line(self) -> bytes:
        line = await self._reader.readline()
        if not line:
            raise OSError("serial EOF")
        return line

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


class _TcpTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read_line(self) -> bytes:
        line = await self._reader.readline()
        if not line:
            raise OSError("TCP EOF")
        return line

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


class _SerialSource:
    def __init__(self, config: DSMRAdapterConfig) -> None:
        self._config = config

    async def open(self) -> _SerialTransport:
        import serial_asyncio_fast  # transitive dep via dsmr-parser; not in pyproject.toml directly

        reader, writer = await serial_asyncio_fast.open_serial_connection(
            url=self._config.serial_port,
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
        )
        return _SerialTransport(reader, writer)


class _TcpSource:
    def __init__(self, config: DSMRAdapterConfig) -> None:
        self._config = config

    async def open(self) -> _TcpTransport:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._config.tcp_host, self._config.tcp_port),
            timeout=30.0,
        )
        return _TcpTransport(reader, writer)


class DSMRAdapter:
    """Raw DSMR P1 adapter implementing the ProtocolAdapter contract.

    Reads telegrams continuously from a serial port or TCP stream, parses them
    with dsmr-parser, and exposes the latest reading via get_raw_state().
    get_raw_state() returns ProtocolDegradedState when no telegram has arrived
    yet or when the last telegram is older than stale_after_s seconds.
    """

    def __init__(
        self,
        config: DSMRAdapterConfig,
        *,
        transport_factory: _TelegramSource | None = None,
    ) -> None:
        spec = _DSMR_SPECS[config.dsmr_version]
        self.config = config
        self._parser = TelegramParser(spec, apply_checksum_validation=True)
        self._buffer = TelegramBuffer()
        self._lock = asyncio.Lock()
        self._last_telegram: dict[str, Any] | None = None
        self._received_at: datetime | None = None
        self._connected: bool = False
        self._stale_logged: bool = False
        self._task: asyncio.Task[None] | None = None
        self._transport: _DSMRTransport | None = None
        if transport_factory is not None:
            self._transport_factory: _TelegramSource = transport_factory
        elif config.serial_port is not None:
            self._transport_factory = _SerialSource(config)
        else:
            self._transport_factory = _TcpSource(config)

    async def start(self) -> None:
        """Start the background read loop; idempotent if already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Cancel the background read loop and reset internal state."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._received_at = None
            self._last_telegram = None
            self._connected = False

    async def get_raw_state(self) -> RawDSMRState | ProtocolDegradedState:
        """Return the latest parsed telegram or a typed degraded state."""
        async with self._lock:
            if self._received_at is None:
                return self._degraded("dsmr_unavailable")
            age_s = (datetime.now(UTC) - self._received_at).total_seconds()
            if age_s > self.config.stale_after_s:
                if not self._stale_logged:
                    self._stale_logged = True
                    logger.warning(
                        "adapter_stale",
                        component="dsmr",
                        device_id=self.config.device_id,
                        seconds_since_last_telegram=age_s,
                    )
                return self._degraded("dsmr_stale")
            self._stale_logged = False
            return RawDSMRState(
                device_id=self.config.device_id,
                telegram_fields=self._last_telegram,  # type: ignore[arg-type]
                received_at=self._received_at,
            )

    async def send_raw_command(self, command: RawProtocolCommand) -> ProtocolCommandResult:
        """DSMR P1 is read-only; all commands return an error."""
        return ProtocolCommandResult(
            correlation_id=command.correlation_id,
            device_id=command.device_id,
            protocol_status="error",
            raw_response={"error": "dsmr_read_only"},
        )

    def _degraded(self, reason: str) -> ProtocolDegradedState:
        return ProtocolDegradedState(
            device_id=self.config.device_id,
            reason=reason,
            occurred_at=datetime.now(UTC),
        )

    def _telegram_to_dict(self, telegram: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            for obis_ref, cosem_obj in telegram:
                try:
                    result[str(obis_ref)] = {
                        "value": cosem_obj.value,
                        "unit": cosem_obj.unit,
                    }
                except (AttributeError, TypeError, ValueError):
                    # dsmr-parser CosemObject properties may be absent or malformed for some
                    # OBIS refs; store nulls so the rest of the telegram is still usable.
                    logger.debug(
                        "adapter_obis_field_error",
                        component="dsmr",
                        device_id=self.config.device_id,
                        obis_ref=str(obis_ref),
                    )
                    result[str(obis_ref)] = {"value": None, "unit": None}
        except Exception as exc:  # noqa: BLE001 — third-party telegram iteration semantics are undocumented; log and return partial dict
            logger.warning(
                "adapter_telegram_iteration_error",
                component="dsmr",
                device_id=self.config.device_id,
                reason=str(exc),
            )
        return result

    async def _read_loop(self) -> None:
        """Background loop: connect, read lines, parse telegrams; reconnect on failure."""
        while True:
            transport: _DSMRTransport | None = None
            self._buffer = TelegramBuffer()  # clear stale fragments from previous session
            try:
                transport = await self._transport_factory.open()
                self._transport = transport
                self._connected = True

                while True:
                    line = await transport.read_line()  # raises OSError on disconnect/EOF
                    try:
                        decoded = line.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    self._buffer.append(decoded)
                    for telegram_str in self._buffer.get_all():
                        try:
                            telegram = self._parser.parse(telegram_str)
                            fields = self._telegram_to_dict(telegram)
                            async with self._lock:
                                self._last_telegram = fields
                                self._received_at = datetime.now(UTC)
                                self._connected = True
                        except Exception as exc:  # noqa: BLE001 — dsmr_parser raises ParseError, InvalidChecksumError, and undocumented internal errors; all logged at boundary
                            logger.warning(
                                "adapter_parse_error",
                                component="dsmr",
                                device_id=self.config.device_id,
                                reason=str(exc),
                            )
            except OSError as exc:
                logger.warning(
                    "adapter_connection_failed",
                    component="dsmr",
                    device_id=self.config.device_id,
                    reason=str(exc),
                )
                async with self._lock:
                    self._connected = False
                    self._received_at = None
                    self._last_telegram = None
            finally:
                if transport is not None:
                    try:
                        await transport.close()
                    except OSError as exc:
                        logger.debug(
                            "adapter_transport_close_error",
                            component="dsmr",
                            device_id=self.config.device_id,
                            reason=str(exc),
                        )
                    if self._transport is transport:
                        self._transport = None

            # CancelledError from stop() propagates here; sleep gives it a chance
            await asyncio.sleep(self.config.reconnect_delay_s)
