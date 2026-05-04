from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog

from open_ems.storage.repositories.event_log_repo import EventLogRepo

logger = structlog.get_logger(__name__)

EVENT_LOG_SCHEMA_VERSION: int = 1
VALID_ACTORS: frozenset[str] = frozenset({"system", "installer", "homeowner"})
VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {"DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"}
)


def serialize_audit_detail(detail: dict[str, object] | None) -> str | None:
    if detail is None:
        return None
    try:
        return json.dumps(detail, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("detail must be JSON-serializable") from exc


class ObservabilityService:
    def __init__(self, repo: EventLogRepo | None = None) -> None:
        self._repo = repo if repo is not None else EventLogRepo()

    async def audit(
        self,
        *,
        actor: str,
        event_type: str,
        summary: str,
        detail: dict[str, object] | None = None,
        device_id: str | None = None,
        config_version: str | None = None,
    ) -> None:
        if not summary or not summary.strip():
            raise ValueError("summary must be non-empty")
        if actor not in VALID_ACTORS:
            raise ValueError(f"Invalid actor {actor!r}")
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Invalid event_type {event_type!r}")
        serialize_audit_detail(detail)

        logger.info(
            "audit_event",
            audit=True,
            actor=actor,
            event_type=event_type,
            summary=summary,
            device_id=device_id,
            config_version=config_version,
        )

        timestamp = datetime.now(UTC)
        await self._repo.append(
            schema_version=EVENT_LOG_SCHEMA_VERSION,
            timestamp=timestamp,
            actor=actor,
            event_type=event_type,
            summary=summary,
            detail=detail,
            device_id=device_id,
            config_version=config_version,
        )
