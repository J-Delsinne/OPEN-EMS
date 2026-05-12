"""Homeowner action endpoints (Story 10.2 + 10.3).

Hosts the imperative ``/actions/*`` namespace described in architecture.md
§"API endpoints" (line 269). Inhabitants:

* ``POST /actions/ev-override`` (Story 10.2) — fire-and-forget command dispatch
  through PolicyGuard. Returns ~50ms; dispatch settles asynchronously.
* ``POST /actions/set-strategy`` (Story 10.3) — synchronous StateStore mutation
  + audit + headline-fragment render. No PolicyGuard touchpoint (strategy is
  not a device command). Returns the rendered headline as the HTMX swap target.
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from open_ems.core import (
    CommandOrigin,
    CommandStatus,
    DeviceRole,
    EnergyStrategy,
    EVChargerState,
    EVOverrideState,
    SetEVChargingRateCommand,
    StateStore,
)
from open_ems.engine.policy_guard import PolicyGuard
from open_ems.services.audit_log import ObservabilityService
from open_ems.settings import Settings
from open_ems.web.dependencies import (
    HomeownerUser,
    get_policy_guard,
    get_settings_dep,
    get_state_store,
    require_homeowner,
)
from open_ems.web.state_serialization import build_homeowner_headline_context

router = APIRouter()
logger = structlog.get_logger(__name__)

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_STRATEGY_FAILURE_MESSAGE = "Strategy update failed. Your previous setting is still active."

# Module-level set holding references to outstanding dispatch tasks. Per the
# asyncio docs (``asyncio.create_task``), a task may be garbage-collected
# mid-execution if the only reference goes out of scope. The route's local
# ``task`` variable goes out of scope on return, so we keep a strong reference
# here and have each task remove itself on completion.
_OUTSTANDING_DISPATCH_TASKS: set[asyncio.Task[None]] = set()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@router.post("/actions/ev-override")
async def post_ev_override(
    request: Request,
    user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    state_store: StateStore = Depends(get_state_store),  # noqa: B008
    policy_guard: PolicyGuard = Depends(get_policy_guard),  # noqa: B008
    settings: Settings = Depends(get_settings_dep),  # noqa: B008
) -> JSONResponse:
    """Homeowner-initiated EV charging override (Story 10.2 AC5–AC7).

    Idempotent within the active-override window. Always returns within ~50ms;
    the underlying device dispatch runs in a fire-and-forget background task.
    """
    snapshot = state_store.get_snapshot()
    observability = _resolve_observability(request)

    # AC6 step 1 — no EV charger means there is no command to issue.
    if not isinstance(snapshot.ev_charger, EVChargerState):
        await _safe_audit(
            observability,
            actor="homeowner",
            event_type="DEVICE",
            summary="EV override rejected: no EV charger available",
            detail={
                "event": "homeowner_ev_override_rejected_no_charger",
                "user_id": user.user_id,
            },
        )
        return JSONResponse(
            status_code=400,
            content={
                "status": "failed",
                "message": "EV charger is not available right now.",
                "action_id": None,
                "timestamp": _utcnow().isoformat(),
                "details": {},
            },
        )

    now = _utcnow()
    existing = snapshot.active_ev_override

    # AC6 step 2 — idempotent replay within the active window.
    if existing is not None and existing.dispatch_status == "pending" and existing.expires_at > now:
        await _safe_audit(
            observability,
            actor="homeowner",
            event_type="DEVICE",
            summary="EV override idempotent replay",
            detail={
                "event": "homeowner_ev_override_idempotent_replay",
                "existing_correlation_id": str(existing.correlation_id),
                "user_id": user.user_id,
            },
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "EV charging is already starting.",
                "action_id": str(existing.correlation_id),
                "timestamp": now.isoformat(),
                "details": {"idempotent_replay": True},
            },
        )

    # AC6 step 3 — install a fresh override.
    new_override = EVOverrideState(
        correlation_id=uuid.uuid4(),
        requested_at=now,
        expires_at=now + timedelta(seconds=settings.ev_override_window_seconds),
        dispatch_status="pending",
    )
    await state_store.set_ev_override(new_override)

    # AC6 step 4 — build the command. correlation_id round-trip is a contract
    # invariant (see PolicyGuard P5 in engine/policy_guard.py).
    command = SetEVChargingRateCommand(
        device_id=snapshot.ev_charger.device_id,
        device_role=DeviceRole.ev_charger,
        origin=CommandOrigin.homeowner,
        correlation_id=new_override.correlation_id,
        rate_kw=settings.ev_override_default_rate_kw,
    )

    await _safe_audit(
        observability,
        actor="homeowner",
        event_type="DEVICE",
        summary="Homeowner requested EV charge override",
        detail={
            "event": "homeowner_ev_override_requested",
            "correlation_id": str(new_override.correlation_id),
            "requested_at_iso": new_override.requested_at.isoformat(),
            "expires_at_iso": new_override.expires_at.isoformat(),
            "user_id": user.user_id,
            "dispatch_status": "pending",
        },
        device_id=snapshot.ev_charger.device_id,
    )

    # AC6 step 6 — fire-and-forget dispatch.
    task = asyncio.create_task(
        _dispatch_and_settle_ev_override(
            policy_guard=policy_guard,
            state_store=state_store,
            command=command,
        ),
        name=f"ev_override_dispatch_{new_override.correlation_id}",
    )
    _OUTSTANDING_DISPATCH_TASKS.add(task)
    task.add_done_callback(_OUTSTANDING_DISPATCH_TASKS.discard)
    task.add_done_callback(_log_task_completion)

    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "message": "EV charging has been requested.",
            "action_id": str(new_override.correlation_id),
            "timestamp": now.isoformat(),
            "details": {},
        },
    )


async def _dispatch_and_settle_ev_override(
    *,
    policy_guard: PolicyGuard,
    state_store: StateStore,
    command: SetEVChargingRateCommand,
) -> None:
    """Background task that drives the command through PolicyGuard (Story 10.2 AC7).

    PolicyGuard enforces its own ``COMMAND_DISPATCH_TIMEOUT_SECONDS = 10.0``
    outer timeout, so no second timeout layer is added here.

    On success: leaves ``dispatch_status="pending"`` — Confirmed is observed
    only when ``EVChargerState.session_active=True`` lands in a future
    snapshot via ``StateStore.publish``'s session-flip detection (AC4).

    On failure / timeout / rejection: rewrites the override's
    ``dispatch_status`` (with concurrency check against the live snapshot's
    correlation_id so we never clobber a newer override).
    """
    task_logger = logger.bind(
        component="actions",
        correlation_id=str(command.correlation_id),
        device_id=command.device_id,
    )
    try:
        result = await policy_guard.authorize_and_dispatch(command)
    except asyncio.CancelledError:
        task_logger.info("ev_override_dispatch_cancelled")
        raise

    # AC7 step 2 — pre-check the snapshot for fast-path orphan detection. The
    # authoritative check happens atomically inside ``compare_and_set_ev_override``
    # below; this read avoids a needless lock acquire on the common "still
    # ours" path and lets us log the orphan reason without holding the lock.
    current = state_store.get_snapshot().active_ev_override
    if current is None or current.correlation_id != command.correlation_id:
        task_logger.info(
            "ev_override_dispatch_result_orphan",
            new_correlation_id=str(current.correlation_id) if current else None,
        )
        return

    if result.status is CommandStatus.success and result.applied:
        # AC7 step 3 — stay pending until StateStore.publish observes session_active=True.
        task_logger.info("ev_override_dispatched_success")
        return

    # AC7 step 4 — terminal failure path. correlation_broken folds into "failed"
    # so the UX surfaces a Fallback with the indeterminate-applied semantics.
    mapped_status: str
    if result.status is CommandStatus.failed:
        mapped_status = "failed"
    elif result.status is CommandStatus.timeout:
        mapped_status = "timeout"
    elif result.status is CommandStatus.rejected:
        mapped_status = "rejected"
    else:  # correlation_broken
        mapped_status = "failed"

    updated = current.model_copy(
        update={
            "dispatch_status": mapped_status,
            "failure_reason": result.reason,
        }
    )
    # Compare-and-swap under the writer lock so a concurrent ``publish()`` that
    # cleared (expiry / natural-completion) or replaced (newer override) the
    # active value between the lock-free read above and this write cannot be
    # clobbered. If the swap fails, the orphan condition surfaced between read
    # and write — log and exit.
    swapped = await state_store.compare_and_set_ev_override(command.correlation_id, updated)
    if not swapped:
        latest = state_store.get_snapshot().active_ev_override
        task_logger.info(
            "ev_override_dispatch_result_orphan_late",
            new_correlation_id=str(latest.correlation_id) if latest else None,
        )
        return
    task_logger.info(
        "ev_override_dispatch_settled_terminal",
        dispatch_status=mapped_status,
        failure_reason=result.reason,
    )


def _log_task_completion(task: asyncio.Task[None]) -> None:
    """Surface unhandled exceptions from the fire-and-forget dispatch task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "ev_override_dispatch_task_unhandled_exception",
            component="actions",
            task_name=task.get_name(),
            error=repr(exc),
        )


def _resolve_observability(request: Request) -> ObservabilityService | None:
    obs = getattr(request.app.state, "observability", None)
    return obs if isinstance(obs, ObservabilityService) else None


async def _safe_audit(
    observability: ObservabilityService | None,
    *,
    actor: str,
    event_type: str,
    summary: str,
    detail: dict[str, object] | None = None,
    device_id: str | None = None,
) -> None:
    """Audit emission with best-effort error handling.

    A failed audit MUST NOT block the homeowner override flow. The structlog
    logger receives a structured event regardless of repo write success.
    """
    if observability is None:
        logger.warning(
            "ev_override_audit_skipped_no_observability",
            component="actions",
            summary=summary,
        )
        return
    try:
        await observability.audit(
            actor=actor,
            event_type=event_type,
            summary=summary,
            detail=detail,
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001 — audit must never propagate; CancelledError escapes
        logger.warning(
            "ev_override_audit_failed",
            component="actions",
            summary=summary,
            error=repr(exc),
        )


# ---------------------------------------------------------------------------
# Story 10.3 — POST /actions/set-strategy
# ---------------------------------------------------------------------------


@router.post("/actions/set-strategy")
async def post_set_strategy(
    request: Request,
    user: HomeownerUser = Depends(require_homeowner),  # noqa: B008
    state_store: StateStore = Depends(get_state_store),  # noqa: B008
    settings: Settings = Depends(get_settings_dep),  # noqa: B008
) -> HTMLResponse:
    """Homeowner-initiated energy-strategy change (Story 10.3 AC2).

    Synchronous: validate → mutate StateStore → emit audit (on actual change)
    → re-read snapshot → render headline fragment. Returns within ~50ms; the
    response IS the HTMX swap target so the headline updates in one
    round-trip without a separate fragment fetch.

    Invalid strategy values yield a 200 + failure-fragment response so HTMX
    swaps the calm-notice text into ``#status-headline`` without leaving the
    headline frozen on a 4xx.
    """
    form = await request.form()
    raw_strategy = form.get("strategy")
    observability = _resolve_observability(request)

    # AC2 step 2 + AC4 — invalid value path. ``EnergyStrategy`` is a StrEnum;
    # ``EnergyStrategy(value)`` raises ``ValueError`` only (KeyError is
    # produced by subscript lookup ``EnergyStrategy[name]`` which we do not
    # use here). Strip surrounding whitespace defensively — clipboard /
    # extension / proxy paths sometimes inject a trailing space which
    # otherwise routes silently to the failure fragment with zero signal.
    try:
        if not isinstance(raw_strategy, str):
            raise ValueError("strategy is required")
        new_strategy = EnergyStrategy(raw_strategy.strip())
    except ValueError:
        logger.warning(
            "set_strategy_invalid_value",
            component="actions",
            raw_strategy=raw_strategy if isinstance(raw_strategy, str) else None,
            user_id=user.user_id,
        )
        return _render_strategy_failure(request, state_store, settings, user)

    # AC2 step 3 — capture previous value BEFORE mutation so the audit row
    # records the correct from→to transition.
    previous_strategy = state_store.get_snapshot().active_strategy
    try:
        mutated = await state_store.set_active_strategy(new_strategy)
    except Exception:  # noqa: BLE001 — mutator must not 500 the homeowner flow
        logger.exception(
            "set_active_strategy_mutator_failed",
            component="actions",
            previous_strategy=previous_strategy.value,
            requested_strategy=raw_strategy,
        )
        return _render_strategy_failure(request, state_store, settings, user)

    # AC2 step 4 + AC6 — audit emission only when actual mutation occurred.
    if mutated:
        await _safe_audit(
            observability,
            actor="homeowner",
            event_type="DEVICE",
            summary="Homeowner changed energy strategy",
            detail={
                "event": "homeowner_strategy_changed",
                "previous_strategy": previous_strategy.value,
                "new_strategy": new_strategy.value,
                "user_id": user.user_id,
            },
        )

    # AC2 step 5 — render the post-mutation headline fragment as the HTMX
    # swap target. Both the mutated and idempotent-no-op paths reach this.
    try:
        snapshot = state_store.get_snapshot()
        context = build_homeowner_headline_context(snapshot)
        context["csrf_token"] = user.csrf_token
        context["strategy_update_confirmation_timeout_seconds"] = (
            settings.strategy_update_confirmation_timeout_seconds
        )
        context["failure_message"] = _STRATEGY_FAILURE_MESSAGE
        return _templates.TemplateResponse(
            request,
            "fragments/homeowner/status-headline.html",
            context,
        )
    except Exception:  # noqa: BLE001 — render failure must not 500 the flow
        logger.exception("set_strategy_headline_render_failed", component="actions")
        return _render_strategy_failure(request, state_store, settings, user)


def _render_strategy_failure(
    request: Request,
    state_store: StateStore,
    settings: Settings,
    user: HomeownerUser,
) -> HTMLResponse:
    """AC4: render the calm-notice failure fragment without mutating state."""
    snapshot = state_store.get_snapshot()
    context = build_homeowner_headline_context(snapshot)
    context["csrf_token"] = user.csrf_token
    context["strategy_update_confirmation_timeout_seconds"] = (
        settings.strategy_update_confirmation_timeout_seconds
    )
    context["failure_message"] = _STRATEGY_FAILURE_MESSAGE
    return _templates.TemplateResponse(
        request,
        "fragments/homeowner/strategy-failure.html",
        context,
    )
