"""Story 11.2 AC9 — EventLogFilter parser tests.

Covers ``_parse_event_log_filters`` and the ``EventLogFilter`` dataclass.
The parser's contract: silently drop invalid values for URL-tampering-resilience;
never raise on user-supplied input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from open_ems.web.event_log_filters import (
    EVENT_LOG_PAGE_SIZE,
    EventLogFilter,
    _parse_event_log_filters,
)


def test_parse_no_params_returns_empty_filter() -> None:
    result = _parse_event_log_filters(
        type=None, device_id=None, window=None, from_date=None, to_date=None, q=None
    )
    assert result == EventLogFilter(
        event_types=frozenset(),
        device_id=None,
        since=None,
        until=None,
        keyword="",
    )
    assert result.is_filtered() is False


def test_parse_type_single_returns_one_member_frozenset() -> None:
    result = _parse_event_log_filters(
        type="DECISION", device_id=None, window=None, from_date=None, to_date=None, q=None
    )
    assert result.event_types == frozenset({"DECISION"})


def test_parse_type_comma_separated_returns_full_frozenset() -> None:
    result = _parse_event_log_filters(
        type="DECISION,DEVICE,SYSTEM",
        device_id=None,
        window=None,
        from_date=None,
        to_date=None,
        q=None,
    )
    assert result.event_types == frozenset({"DECISION", "DEVICE", "SYSTEM"})


def test_parse_type_with_invalid_member_drops_invalid_silently() -> None:
    result = _parse_event_log_filters(
        type="GARBAGE,DECISION",
        device_id=None,
        window=None,
        from_date=None,
        to_date=None,
        q=None,
    )
    assert result.event_types == frozenset({"DECISION"})


def test_parse_type_all_invalid_returns_empty_set_no_filter() -> None:
    result = _parse_event_log_filters(
        type="NOPE,GARBAGE",
        device_id=None,
        window=None,
        from_date=None,
        to_date=None,
        q=None,
    )
    assert result.event_types == frozenset()


def test_parse_window_24h_sets_since_to_now_minus_24_hours() -> None:
    before = datetime.now(UTC)
    result = _parse_event_log_filters(
        type=None, device_id=None, window="24h", from_date=None, to_date=None, q=None
    )
    after = datetime.now(UTC)
    assert result.since is not None
    assert result.until is not None
    # since ≈ now - 24h, within the wall-clock spread of the test
    expected_since_low = before - timedelta(hours=24)
    expected_since_high = after - timedelta(hours=24)
    assert expected_since_low <= result.since <= expected_since_high
    # until ≈ now
    assert before <= result.until <= after


def test_parse_window_unknown_value_is_dropped_silently() -> None:
    result = _parse_event_log_filters(
        type=None, device_id=None, window="99h", from_date=None, to_date=None, q=None
    )
    assert result.since is None
    assert result.until is None


def test_parse_explicit_from_to_overrides_window() -> None:
    result = _parse_event_log_filters(
        type=None,
        device_id=None,
        window="24h",
        from_date="2026-05-01",
        to_date="2026-05-02",
        q=None,
    )
    assert result.since == datetime(2026, 5, 1, tzinfo=UTC)
    # ``to`` is exclusive end-of-day → next-day midnight
    assert result.until == datetime(2026, 5, 3, tzinfo=UTC)


def test_parse_from_invalid_date_string_is_dropped() -> None:
    result = _parse_event_log_filters(
        type=None,
        device_id=None,
        window=None,
        from_date="not-a-date",
        to_date=None,
        q=None,
    )
    assert result.since is None


def test_parse_keyword_strips_outer_whitespace_preserves_inner() -> None:
    result = _parse_event_log_filters(
        type=None,
        device_id=None,
        window=None,
        from_date=None,
        to_date=None,
        q="  peak  limit  ",
    )
    assert result.keyword == "peak  limit"


def test_parse_device_id_preserved_verbatim() -> None:
    result = _parse_event_log_filters(
        type=None,
        device_id="batt-1",
        window=None,
        from_date=None,
        to_date=None,
        q=None,
    )
    assert result.device_id == "batt-1"


def test_event_log_filter_is_filtered_predicate_is_true_when_any_field_set() -> None:
    base = EventLogFilter(
        event_types=frozenset(), device_id=None, since=None, until=None, keyword=""
    )
    assert base.is_filtered() is False
    assert (
        base.__class__(
            event_types=frozenset({"DECISION"}),
            device_id=None,
            since=None,
            until=None,
            keyword="",
        ).is_filtered()
        is True
    )
    assert (
        base.__class__(
            event_types=frozenset(),
            device_id="batt-1",
            since=None,
            until=None,
            keyword="",
        ).is_filtered()
        is True
    )
    assert (
        base.__class__(
            event_types=frozenset(),
            device_id=None,
            since=datetime(2026, 5, 1, tzinfo=UTC),
            until=None,
            keyword="",
        ).is_filtered()
        is True
    )
    assert (
        base.__class__(
            event_types=frozenset(),
            device_id=None,
            since=None,
            until=datetime(2026, 5, 1, tzinfo=UTC),
            keyword="",
        ).is_filtered()
        is True
    )
    assert (
        base.__class__(
            event_types=frozenset(),
            device_id=None,
            since=None,
            until=None,
            keyword="peak",
        ).is_filtered()
        is True
    )


def test_event_log_page_size_is_50() -> None:
    """AC3 + Q2 resolution: initial render serves 50 entries."""
    assert EVENT_LOG_PAGE_SIZE == 50


@pytest.mark.parametrize(
    "window,expected_delta",
    [
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
    ],
)
def test_parse_window_supported_values(window: str, expected_delta: timedelta) -> None:
    """All three v1 windows from _VALID_WINDOWS are honored."""
    before = datetime.now(UTC)
    result = _parse_event_log_filters(
        type=None, device_id=None, window=window, from_date=None, to_date=None, q=None
    )
    after = datetime.now(UTC)
    assert result.since is not None
    assert (before - expected_delta) <= result.since <= (after - expected_delta)
