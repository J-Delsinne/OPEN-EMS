"""Story 11.2 AC9 — Event-log filter parsing.

Single source of truth for the parsed filter state used by the page route,
the HTMX list fragment, and the repository read methods. The parser silently
drops invalid values (URL-tampering-resilience): an unknown type, malformed
date, or unknown window must never raise — the surface is exposed via URL
query params that the installer may type, share, or copy from older link
emitters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog

from open_ems.services.audit_log import VALID_EVENT_TYPES

logger = structlog.get_logger(__name__)

EVENT_LOG_PAGE_SIZE: Final[int] = 50

_VALID_WINDOWS: Final[dict[str, timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@dataclass(frozen=True)
class EventLogFilter:
    """Parsed event-log filter state.

    Single source of truth for ``list_filtered`` / ``count_filtered`` WHERE-clause
    construction. Empty ``event_types`` means "no type filter"; ``keyword=""``
    means "no keyword filter"; ``since=None`` / ``until=None`` mean unbounded.

    ``window`` records the provenance of ``since``/``until``: when non-None,
    the date range was derived from the relative ``window`` URL param (and the
    form's date inputs should render empty so resubmission preserves the
    "now-relative" semantics rather than freezing to the literal date).
    """

    event_types: frozenset[str]
    device_id: str | None
    since: datetime | None
    until: datetime | None
    keyword: str
    window: str | None = None

    def is_filtered(self) -> bool:
        """True iff any filter field is non-default."""
        return bool(self.event_types or self.device_id or self.since or self.until or self.keyword)


def _parse_event_log_filters(
    *,
    type: str | None,
    device_id: str | None,
    window: str | None,
    from_date: str | None,
    to_date: str | None,
    q: str | None,
) -> EventLogFilter:
    """Parse URL query params into an ``EventLogFilter``.

    Invalid values are silently dropped (URL-tampering-resilience). When both
    ``window`` and explicit ``from_date`` / ``to_date`` are present, the
    explicit values win.
    """
    parsed_types = _parse_types(type)
    since, until, resolved_window = _parse_date_range(window, from_date, to_date)
    keyword = q.strip() if q is not None else ""
    return EventLogFilter(
        event_types=parsed_types,
        device_id=device_id if device_id else None,
        since=since,
        until=until,
        keyword=keyword,
        window=resolved_window,
    )


def _parse_types(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    valid = {p for p in parts if p in VALID_EVENT_TYPES}
    dropped = [p for p in parts if p not in VALID_EVENT_TYPES]
    if dropped:
        logger.debug(
            "installer_event_log_filter_invalid_value",
            param="type",
            dropped=dropped,
            component="event_log_filters",
        )
    return frozenset(valid)


def _parse_date_range(
    window: str | None,
    from_date: str | None,
    to_date: str | None,
) -> tuple[datetime | None, datetime | None, str | None]:
    """Returns ``(since, until, resolved_window)``.

    ``resolved_window`` is the validated window string when the date range was
    derived from ``?window=...`` and no explicit dates won; otherwise None.
    The page builder uses this to leave the date inputs empty for
    window-derived ranges (preserving the now-relative semantics).
    """
    # Explicit from/to override window. If only one of from/to is provided,
    # that one is honored alongside a None counterpart (open-ended range)
    # and the window is dropped — see AC5 partial-explicit clarification.
    explicit_since = _parse_date(from_date, end_of_day=False)
    explicit_until = _parse_date(to_date, end_of_day=True)
    if (
        explicit_since is not None
        and explicit_until is not None
        and explicit_since >= explicit_until
    ):
        logger.debug(
            "installer_event_log_filter_invalid_value",
            param="date_range",
            reason="from >= to",
            from_value=from_date,
            to_value=to_date,
            component="event_log_filters",
        )
        return None, None, None
    if explicit_since is not None or explicit_until is not None:
        return explicit_since, explicit_until, None

    if window is None:
        return None, None, None
    delta = _VALID_WINDOWS.get(window)
    if delta is None:
        logger.debug(
            "installer_event_log_filter_invalid_value",
            param="window",
            value=window,
            component="event_log_filters",
        )
        return None, None, None
    now = datetime.now(UTC)
    return now - delta, now, window


def _parse_date(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse YYYY-MM-DD into a UTC midnight datetime.

    ``end_of_day=True`` returns the NEXT-day midnight (so the date range can be
    used as an exclusive upper bound: ``timestamp < until`` semantically means
    "anything on or before <to_date>").
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        logger.debug(
            "installer_event_log_filter_invalid_value",
            param="date",
            value=value,
            component="event_log_filters",
        )
        return None
    if end_of_day:
        d = d + timedelta(days=1)
    return d
