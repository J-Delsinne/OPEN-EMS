"""Raw protocol adapter contracts for OPEN-EMS."""

from open_ems.adapters.protocol import (
    ConnectionStatus,
    ProtocolAdapter,
    ProtocolCommandResult,
    ProtocolDegradedState,
    ProtocolStatus,
    RawDSMRState,
    RawModbusState,
    RawOCPPState,
    RawPayload,
    RawProtocolCommand,
    RawProtocolState,
)

__all__ = [
    "ConnectionStatus",
    "ProtocolAdapter",
    "ProtocolCommandResult",
    "ProtocolDegradedState",
    "ProtocolStatus",
    "RawDSMRState",
    "RawModbusState",
    "RawOCPPState",
    "RawPayload",
    "RawProtocolCommand",
    "RawProtocolState",
]
