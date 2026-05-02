from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

_WINDOW_SECONDS = 300  # 5-minute sliding window
_MAX_FAILURES = 5  # threshold before rate limiting

# {key: [failure_timestamps]}
# key is "user:<username>"
_failures: dict[str, list[datetime]] = defaultdict(list)


def _prune(key: str, now: datetime) -> None:
    if key not in _failures:
        return
    cutoff = now - timedelta(seconds=_WINDOW_SECONDS)
    pruned = [t for t in _failures[key] if t > cutoff]
    if pruned:
        _failures[key] = pruned
    else:
        del _failures[key]


def record_failure(username: str) -> None:
    """Record a failed login attempt for the given username."""
    now = datetime.now(UTC)
    key = f"user:{username}"
    _prune(key, now)
    _failures[key].append(now)


def is_rate_limited(username: str) -> bool:
    """Return True if the username has exceeded the failure threshold."""
    now = datetime.now(UTC)
    key = f"user:{username}"
    _prune(key, now)
    return len(_failures.get(key, [])) >= _MAX_FAILURES


def reset_for_key(username: str) -> None:
    """Clear rate limit counter on successful login."""
    _failures.pop(f"user:{username}", None)


def _reset_all() -> None:
    """Clear all rate limit state. For testing only."""
    _failures.clear()
