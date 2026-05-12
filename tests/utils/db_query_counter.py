"""Story 11.1 AC10 / Q5 — aiosqlite execute interception for query counting.

Used to assert structural-discipline invariants like "the dashboard shell
route triggers zero DB queries" without resorting to side_effect=AssertionError
patches that only catch known-bad call paths.

Usage:

    async with count_queries() as counter:
        response = await client.get("/installer/dashboard")
    assert counter.count == 0
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

import aiosqlite


@dataclass
class QueryCounter:
    """Counter for aiosqlite execute() calls made during the context."""

    count: int = 0
    queries: list[str] = field(default_factory=list)


@asynccontextmanager
async def count_queries() -> AsyncIterator[QueryCounter]:
    """Intercept aiosqlite.Connection.execute calls within the context.

    Captures the SQL statement and increments the counter. The intercept
    delegates back to the original execute so the queries still run — this
    is a counter, not a side-effect injector.
    """
    counter = QueryCounter()
    original_execute = aiosqlite.Connection.execute

    def wrapped_execute(self: aiosqlite.Connection, sql: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counter.count += 1
        counter.queries.append(sql.strip().split()[0].upper() if sql.strip() else "")
        return original_execute(self, sql, *args, **kwargs)

    with patch.object(aiosqlite.Connection, "execute", wrapped_execute):
        yield counter
