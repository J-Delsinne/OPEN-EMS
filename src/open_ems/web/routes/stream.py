from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from open_ems.core import StateStore, SystemSnapshot
from open_ems.web.dependencies import (
    StreamUser,
    get_state_store,
    require_stream_user,
    stream_session_is_valid,
)
from open_ems.web.state_serialization import (
    serialize_homeowner_snapshot,
    serialize_installer_snapshot,
)

router = APIRouter()

DEFAULT_POLL_INTERVAL_SECONDS = 0.25
KEEPALIVE_SECONDS = 15.0
SESSION_VALIDATION_INTERVAL_SECONDS = 15.0

Role = Literal["installer", "homeowner"]
SessionValidator = Callable[[str, str, str, Role], Awaitable[bool]]


def get_stream_poll_interval_seconds() -> float:
    return DEFAULT_POLL_INTERVAL_SECONDS


def _parse_last_event_id(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value.strip())
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def _format_state_event(snapshot: SystemSnapshot, role: Role) -> str:
    payload = (
        serialize_installer_snapshot(snapshot)
        if role == "installer"
        else serialize_homeowner_snapshot(snapshot)
    )
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: state_update\nid: {snapshot.sequence_id}\ndata: {data}\n\n"


async def _session_is_valid_for_stream(
    session_token_hash: str,
    session_id: str,
    user_id: str,
    role: Role,
) -> bool:
    return await stream_session_is_valid(
        session_token_hash=session_token_hash,
        session_id=session_id,
        user_id=user_id,
        expected_role=role,
    )


async def _state_event_generator(
    *,
    request: Any,
    store: StateStore,
    role: Role,
    session_token_hash: str,
    session_id: str,
    user_id: str,
    session_expires_at: datetime,
    last_event_id: int | None,
    poll_interval_seconds: float,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
    session_validation_interval_seconds: float = SESSION_VALIDATION_INTERVAL_SECONDS,
    session_validator: SessionValidator = _session_is_valid_for_stream,
    now: Callable[[], float] = time.monotonic,
) -> AsyncGenerator[str, None]:
    last_sent_sequence_id = last_event_id if last_event_id is not None else -1
    last_frame_at = now()
    next_session_validation_at = last_frame_at + session_validation_interval_seconds

    while True:
        if await request.is_disconnected():
            return

        current_time = now()
        current_datetime = datetime.now(UTC)
        if current_datetime >= session_expires_at or current_time >= next_session_validation_at:
            valid = await session_validator(session_token_hash, session_id, user_id, role)
            if not valid:
                return
            next_session_validation_at = current_time + session_validation_interval_seconds

        snapshot = store.get_snapshot()
        if last_sent_sequence_id > snapshot.sequence_id:
            last_sent_sequence_id = -1
        if snapshot.sequence_id > last_sent_sequence_id:
            valid = await session_validator(session_token_hash, session_id, user_id, role)
            if not valid:
                return
            last_sent_sequence_id = snapshot.sequence_id
            last_frame_at = current_time
            next_session_validation_at = current_time + session_validation_interval_seconds
            yield _format_state_event(snapshot, role)
            continue

        if current_time - last_frame_at >= keepalive_seconds:
            last_frame_at = current_time
            yield ": keepalive\n\n"
            continue

        await asyncio.sleep(poll_interval_seconds)


@router.get("/api/stream/state")
async def stream_state(
    request: Request,
    user: StreamUser = Depends(require_stream_user),  # noqa: B008
    store: StateStore = Depends(get_state_store),  # noqa: B008
    poll_interval_seconds: float = Depends(get_stream_poll_interval_seconds),  # noqa: B008
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    last_event_id = _parse_last_event_id(last_event_id_header)
    return StreamingResponse(
        _state_event_generator(
            request=request,
            store=store,
            role=user.role,  # type: ignore[arg-type]
            session_token_hash=user.session_token_hash,
            session_id=user.session_id,
            user_id=user.user_id,
            session_expires_at=user.expires_at,
            last_event_id=last_event_id,
            poll_interval_seconds=poll_interval_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
