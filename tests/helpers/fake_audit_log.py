from __future__ import annotations

import json
from html import escape

from open_ems.services.audit_log import (
    MAX_INSTALLER_NOTE_LENGTH,
    VALID_ACTORS,
    VALID_EVENT_TYPES,
    serialize_audit_detail,
)


class FakeAuditLog:
    """In-memory test double for ObservabilityService.

    Applies the same validation as the real service so call-site errors are caught
    without requiring a database connection.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def audit(
        self,
        *,
        actor: str,
        event_type: str,
        summary: str,
        detail: dict | None = None,
        device_id: str | None = None,
        config_version: str | None = None,
    ) -> None:
        if not summary or not summary.strip():
            raise ValueError("summary must be non-empty")
        if actor not in VALID_ACTORS:
            raise ValueError(f"Invalid actor {actor!r}")
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Invalid event_type {event_type!r}")
        detail_snapshot = (
            None if detail is None else json.loads(serialize_audit_detail(detail) or "{}")
        )
        self.events.append(
            {
                "actor": actor,
                "event_type": event_type,
                "summary": summary,
                "detail": detail_snapshot,
                "device_id": device_id,
                "config_version": config_version,
            }
        )

    async def installer_note(self, note: str) -> None:
        if len(note) > MAX_INSTALLER_NOTE_LENGTH:
            raise ValueError(f"installer note must be <= {MAX_INSTALLER_NOTE_LENGTH} characters")
        stripped = note.strip()
        if not stripped:
            raise ValueError("installer note must be non-empty")
        await self.audit(
            actor="installer",
            event_type="INSTALLER",
            summary=escape(stripped, quote=True),
        )

    def assert_has_event(self, *, event_type: str, actor: str) -> None:
        matching = [e for e in self.events if e["event_type"] == event_type and e["actor"] == actor]
        assert matching, (
            f"No event with event_type={event_type!r} actor={actor!r} found. "
            f"Captured: {self.events}"
        )

    def assert_event_count(self, count: int) -> None:
        assert len(self.events) == count, (
            f"Expected {count} events, got {len(self.events)}: {self.events}"
        )

    def clear(self) -> None:
        self.events.clear()
