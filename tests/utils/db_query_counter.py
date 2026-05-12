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
    """Counter for aiosqlite execute() calls made during the context.

    ``queries`` holds the FULL normalized SQL text (whitespace-collapsed,
    uppercased) for each intercepted execute call. Substring assertions
    against table names work against this list as expected.
    """

    count: int = 0
    queries: list[str] = field(default_factory=list)


def _normalize_sql(sql: str) -> str:
    """Collapse runs of whitespace and uppercase for stable substring matching."""
    return " ".join(sql.split()).upper()


@asynccontextmanager
async def count_queries() -> AsyncIterator[QueryCounter]:
    """Intercept aiosqlite.Connection.execute calls within the context.

    Captures the full normalized SQL string and increments the counter. The
    intercept delegates back to the original execute so queries still run —
    this is a counter, not a side-effect injector. Storing the full SQL is
    load-bearing: assertions like ``"PEAK_INTERVALS" not in " ".join(queries)``
    rely on the table-name substring being captured (P1 review fix).
    """
    counter = QueryCounter()
    original_execute = aiosqlite.Connection.execute

    def wrapped_execute(self: aiosqlite.Connection, sql: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counter.count += 1
        counter.queries.append(_normalize_sql(sql) if sql else "")
        return original_execute(self, sql, *args, **kwargs)

    with patch.object(aiosqlite.Connection, "execute", wrapped_execute):
        yield counter
