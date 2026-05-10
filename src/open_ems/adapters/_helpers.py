"""Shared adapter helpers — used by every controllable adapter's ``send_command``."""

from __future__ import annotations

from open_ems.core.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
)

__all__ = ["failed_result"]


def failed_result(cmd: DeviceCommand, reason: str) -> CommandResult:
    """Build a ``CommandResult(status=failed, applied=False, reason=...)`` for ``cmd``."""
    return CommandResult(
        correlation_id=cmd.correlation_id,
        device_id=cmd.device_id,
        status=CommandStatus.failed,
        applied=False,
        reason=reason,
    )
