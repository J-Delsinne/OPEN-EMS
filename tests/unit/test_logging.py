import json

import pytest
import structlog


def _reset_and_configure(log_level: str = "INFO") -> None:
    """Reset structlog defaults then apply our configuration.

    Call this at the start of each logging test to ensure a clean slate.
    """
    structlog.reset_defaults()
    from open_ems.logging_config import configure_logging

    configure_logging(log_level)


def test_json_output_format(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_and_configure("INFO")
    log = structlog.get_logger("test.component")
    log.info("test_event")
    captured = capsys.readouterr()
    line = captured.out.strip()
    assert line, "Expected log output but got nothing"
    data = json.loads(line)
    assert isinstance(data, dict)


def test_required_fields_present(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_and_configure("INFO")
    log = structlog.get_logger("test.component")
    log.info("test_event")
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    for field in ("timestamp", "level", "event", "component"):
        assert field in data, f"Required field '{field}' missing from log output"
    assert data["event"] == "test_event"
    assert data["level"] == "info"
    assert data["component"] == "test.component"
    # timestamp must be an ISO-8601 string
    ts = data["timestamp"]
    assert isinstance(ts, str) and "T" in ts


def test_exc_info_serializable(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_and_configure("INFO")
    log = structlog.get_logger("test.exc")
    try:
        raise ValueError("test error")
    except ValueError:
        log.error("error_event", exc_info=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "error_event"
    assert "exception" in data
    assert "ValueError" in data["exception"]


def test_log_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_and_configure("INFO")
    log = structlog.get_logger("test.filter")
    log.debug("should_not_appear")
    log.info("should_appear")
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.strip().splitlines() if ln]
    assert len(lines) == 1, f"Expected 1 log line at INFO level, got: {lines}"
    data = json.loads(lines[0])
    assert data["event"] == "should_appear"
