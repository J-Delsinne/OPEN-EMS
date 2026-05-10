"""P7 — Concurrent-writer stress test on the shared aiosqlite connection.

Verifies Layer 2's hazard hypothesis: that ``ConfigRepo.activate()``'s
``commit()`` → ``BEGIN IMMEDIATE`` pair, run on the process-wide shared
aiosqlite connection alongside other writers (``EventLogRepo.append`` from
PolicyGuard rejections and watchdog audits, etc.), can race with implicit
transactions opened by those other writers — manifesting as
``OperationalError: cannot start a transaction within a transaction``.

If this test PASSES (no OperationalError; activation outcomes are coherent;
config_version is monotonic; audit-log payloads match the surviving
active_constraints rows): aiosqlite + the production patterns are robust
enough under realistic load. The Layer 2 hazard is then a theoretical
concern, not an active one.

If this test FAILS: the hazard is real, and a database-module-level write
lock (or equivalent) needs to be added before this story can ship cleanly.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config

from open_ems.core.constraints import ActiveConstraintsInput
from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.config_repo import ConfigRepo
from open_ems.storage.repositories.event_log_repo import EventLogRepo


def _project_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").exists():
            return parent
    raise RuntimeError("alembic.ini not found")


@pytest_asyncio.fixture
async def migrated_db(tmp_path: pathlib.Path) -> AsyncGenerator[None, None]:
    db_path = str(tmp_path / "concurrent_writers.db")
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
    await init_database(db_path)
    yield
    await close_database()


@pytest.mark.asyncio
async def test_concurrent_activate_and_event_log_append_do_not_corrupt_state(
    migrated_db: None,
) -> None:
    """Hammer ``ConfigRepo.activate`` and ``EventLogRepo.append`` in parallel on
    the same shared connection. Confirm:

    1. No ``OperationalError`` ("cannot start a transaction within a transaction").
    2. Every successful ``activate`` returns a config_version that appears
       in ``active_constraints`` AND has a matching ``config_audit_log`` row.
    3. The cross-table monotonic version contract (P8) holds — versions are
       strictly increasing across the surviving activations.
    """
    config_repo = ConfigRepo()
    event_log_repo = EventLogRepo()

    n_activations = 30
    n_event_appends = 30

    async def do_activations() -> list[int]:
        versions: list[int] = []
        for i in range(n_activations):
            peak = 25.0 + i * 0.1
            v = await config_repo.activate(
                ActiveConstraintsInput(
                    peak_limit_kw=peak,
                    battery_reserve_floor_percent=20.0,
                ),
                actor="system",
            )
            versions.append(v)
        return versions

    async def do_event_appends() -> int:
        count = 0
        for i in range(n_event_appends):
            await event_log_repo.append(
                schema_version=1,
                timestamp=datetime.now(UTC),
                actor="system",
                event_type="SYSTEM",
                summary=f"stress writer iteration {i}",
                detail={"i": i},
                device_id=None,
                config_version=None,
            )
            count += 1
            # Interleave: yield to give the other task a chance to run.
            await asyncio.sleep(0)
        return count

    activate_versions, event_count = await asyncio.gather(do_activations(), do_event_appends())

    # 1. All activations succeeded — no OperationalError was raised.
    assert len(activate_versions) == n_activations
    assert event_count == n_event_appends

    # 2. Versions are strictly monotonic across activations.
    assert activate_versions == sorted(set(activate_versions))
    assert len(activate_versions) == len(set(activate_versions)), (
        "Duplicate config_versions assigned across concurrent activations"
    )

    # 3. DB state matches: active_constraints rows match the returned versions,
    #    and every assigned version has a corresponding audit-log row.
    conn = get_connection()
    async with conn.execute(
        "SELECT config_version FROM active_constraints ORDER BY config_version"
    ) as cursor:
        active_versions_in_db = [int(r[0]) for r in await cursor.fetchall()]
    assert active_versions_in_db == sorted(activate_versions), (
        "active_constraints versions diverge from activate() return values"
    )

    async with conn.execute(
        "SELECT DISTINCT config_version FROM config_audit_log"
        " WHERE config_version IS NOT NULL"
        " ORDER BY config_version"
    ) as cursor:
        audit_versions_in_db = [int(r[0]) for r in await cursor.fetchall()]
    # Audit log carries activated_at × one-row-per-changed-field; we just need
    # every activated version to be present somewhere in the audit log.
    for v in activate_versions:
        assert v in audit_versions_in_db, (
            f"config_version {v} returned by activate() has no audit-log row"
        )


@pytest.mark.asyncio
async def test_concurrent_activations_do_not_produce_duplicate_versions(
    migrated_db: None,
) -> None:
    """Stronger contract: two ``activate()`` calls launched in parallel never
    yield the same ``config_version``. Tests P1's "previous-inside-transaction"
    invariant AND P8's cross-table monotonic version assignment under the most
    adversarial timing the asyncio scheduler is likely to produce in CI.
    """
    config_repo = ConfigRepo()

    async def one_activation(peak: float, floor: float, actor: str) -> int:
        return await config_repo.activate(
            ActiveConstraintsInput(
                peak_limit_kw=peak,
                battery_reserve_floor_percent=floor,
            ),
            actor=actor,  # type: ignore[arg-type]
        )

    # Launch 10 pairs of parallel activations with strictly-increasing,
    # always-distinct inputs so the no-changes guard is never tripped
    # (consecutive iterations must not echo the previous iteration's values).
    results: list[int] = []
    for i in range(10):
        a, b = await asyncio.gather(
            one_activation(25.0 + i * 2, 20.0 + i * 0.1, "system"),
            one_activation(25.0 + i * 2 + 1, 20.0 + i * 0.1 + 0.05, "installer"),
        )
        results.extend([a, b])

    assert len(results) == len(set(results)), (
        f"Duplicate config_versions across concurrent activations: {results}"
    )
    assert results == sorted(set(results)), (
        f"Concurrent activations produced non-monotonic versions: {results}"
    )
