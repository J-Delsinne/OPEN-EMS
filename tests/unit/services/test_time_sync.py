import time
from unittest.mock import patch

import open_ems.services.time_sync as ts_module
from open_ems.services.time_sync import check_clock, get_clock_status


def _reset_clock_status() -> None:
    ts_module._clock_status = "unknown"


def test_unknown_when_no_ntp_host() -> None:
    _reset_clock_status()
    result = check_clock(None, 2.0)
    assert result == "unknown"


def test_unknown_when_ntp_unreachable() -> None:
    _reset_clock_status()
    with patch("open_ems.services.time_sync._query_ntp", side_effect=OSError("timeout")):
        result = check_clock("ntp.example.com", 2.0)
    assert result == "unknown"


def test_valid_when_no_drift() -> None:
    _reset_clock_status()
    with patch("open_ems.services.time_sync._query_ntp", return_value=time.time()):
        result = check_clock("ntp.example.com", 2.0)
    assert result == "valid"


def test_suspect_when_high_drift() -> None:
    _reset_clock_status()
    with patch("open_ems.services.time_sync._query_ntp", return_value=time.time() + 60):
        result = check_clock("ntp.example.com", 2.0)
    assert result == "suspect"


def test_clock_status_stored() -> None:
    _reset_clock_status()
    with patch("open_ems.services.time_sync._query_ntp", return_value=time.time()):
        check_clock("ntp.example.com", 2.0)
    assert get_clock_status() == "valid"
