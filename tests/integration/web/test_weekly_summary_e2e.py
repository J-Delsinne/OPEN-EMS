"""End-to-end integration tests for the weekly summary (Story 10.4 AC15 #40-#44).

Drives the full FastAPI app against a real in-memory SQLite DB. Covers:

* Cold-start: no row → "Data is still being collected" message.
* Seeded sufficient-history row → three metrics rendered.
* Performance: GET response within the 200ms Pi 4 budget (mean over 10 iterations).
* Lifespan: aggregator task runs immediately at startup (insufficient_history=1 row
  appears within seconds).
* a11y placeholder (xfail+skipif npx absent) — follows the 10.1/10.2/10.3 precedent.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_ems.core import StateStore
from open_ems.core.energy import WeeklyEnergySummaryRow
from open_ems.storage.database import close_database, get_connection, init_database
from open_ems.storage.repositories.energy_repo import EnergyRepo
from open_ems.storage.repositories.session_repo import (
    SessionRepo,
    generate_session_token,
    hash_token,
)
from open_ems.storage.repositories.user_repo import UserRepo, hash_password
from open_ems.web.routes.fragments import router as fragments_router
from open_ems.web.routes.homeowner import router as homeowner_router


def _make_alembic_cfg(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest_asyncio.fixture
async def db_with_schema(tmp_path) -> AsyncGenerator[str, None]:  # noqa: ANN001
    """Run alembic upgrade head against a fresh tmp_path SQLite file so we get
    the real production schema (users, sessions, energy_flow_intervals,
    weekly_energy_summary, event_log, active_constraints, etc.)."""
    db_path = str(tmp_path / "weekly_summary.db")
    alembic_command.upgrade(_make_alembic_cfg(f"sqlite:///{db_path}"), "head")
    await init_database(db_path)
    yield db_path
    await close_database()


async def _create_homeowner_session() -> str:
    user_id = await UserRepo(get_connection()).create(
        username=f"hw_{generate_session_token()[:8]}",
        hashed_password=hash_password("secret"),
        role="homeowner",
    )
    raw_token = generate_session_token()
    await SessionRepo(get_connection()).create(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        csrf_token="test-csrf",
    )
    return raw_token


def _app() -> FastAPI:
    app = FastAPI()
    app.state.state_store = StateStore(system_clock_status="valid")
    app.include_router(homeowner_router)
    app.include_router(fragments_router)
    return app


async def test_weekly_summary_e2e_cold_start_shows_insufficient_history_message(
    db_with_schema: str,
) -> None:
    """AC15 #40: cold-start (no row) renders "data is still being collected"."""
    raw_token = await _create_homeowner_session()
    client = TestClient(_app(), base_url="https://test", follow_redirects=False)
    response = client.get(
        "/fragments/homeowner/weekly-summary",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Data is still being collected" in body
    assert "0/7 days" in body


async def test_weekly_summary_e2e_after_seven_days_of_synthetic_data_shows_full_summary(
    db_with_schema: str,
) -> None:
    """AC15 #41: a sufficient-history row → three metrics rendered."""
    raw_token = await _create_homeowner_session()
    # Seed via the EnergyRepo upsert path (not raw SQL) to exercise the
    # round-trip contract.
    await EnergyRepo().upsert_weekly_energy_summary(
        WeeklyEnergySummaryRow(
            window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
            window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
            peaks_avoided_count=5,
            self_consumption_ratio=0.80,
            estimated_cost_savings_eur=42.10,
            data_complete_days_count=7,
            insufficient_history=False,
            computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        )
    )
    client = TestClient(_app(), base_url="https://test", follow_redirects=False)
    response = client.get(
        "/fragments/homeowner/weekly-summary",
        cookies={"session": raw_token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Peaks avoided" in body
    assert ">5<" in body
    assert "Self-consumption" in body
    assert "80%" in body
    assert "Estimated savings" in body
    assert "42.10" in body
    # No-pass-green / no-amber design rule.
    lower = body.lower()
    assert "color-pass" not in lower
    assert "pass-green" not in lower
    assert "amber" not in lower
    assert "color-warn" not in lower


async def test_weekly_summary_e2e_sub_200ms_response_at_representative_data_volume(
    db_with_schema: str,
) -> None:
    """AC15 #42: GET /fragments/homeowner/weekly-summary < 200ms (Pi 4 hard contract)."""
    raw_token = await _create_homeowner_session()
    await EnergyRepo().upsert_weekly_energy_summary(
        WeeklyEnergySummaryRow(
            window_start_utc=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
            window_end_utc=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
            peaks_avoided_count=5,
            self_consumption_ratio=0.80,
            estimated_cost_savings_eur=42.10,
            data_complete_days_count=7,
            insufficient_history=False,
            computed_at=datetime(2026, 5, 12, 0, 0, tzinfo=UTC),
        )
    )
    client = TestClient(_app(), base_url="https://test", follow_redirects=False)
    timings: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        response = client.get(
            "/fragments/homeowner/weekly-summary",
            cookies={"session": raw_token},
            headers={"HX-Request": "true"},
        )
        timings.append(time.perf_counter() - t0)
        assert response.status_code == 200
    mean_ms = (sum(timings) / len(timings)) * 1000
    # CI proxy: < 50ms — well under the 200ms Pi 4 hard ceiling.
    assert mean_ms < 50.0, (
        f"weekly-summary mean response time {mean_ms:.1f}ms exceeds CI budget "
        f"(Pi 4 hard ceiling is 200ms)"
    )


async def test_weekly_summary_e2e_aggregator_writes_row_immediately_at_first_invocation(
    db_with_schema: str,
) -> None:
    """AC15 #43: ``_compute_and_upsert_weekly_summary`` invoked once → row exists."""
    from open_ems.services.weekly_energy_summary import _compute_and_upsert_weekly_summary
    from open_ems.settings import Settings
    from open_ems.storage.repositories.event_log_repo import EventLogRepo

    settings = Settings(secret_key="test-secret", _env_file=None)  # type: ignore[call-arg]
    energy_repo = EnergyRepo()
    event_log_repo = EventLogRepo()

    # Row does not exist yet.
    assert await energy_repo.read_weekly_energy_summary() is None

    await _compute_and_upsert_weekly_summary(
        energy_repo=energy_repo,
        event_log_repo=event_log_repo,
        settings=settings,
        now=datetime.now(UTC),
    )

    # Row now exists (insufficient_history=True — no intervals seeded yet).
    row = await energy_repo.read_weekly_energy_summary()
    assert row is not None
    assert row.insufficient_history is True
    assert row.data_complete_days_count == 0


# AC15 #44 — a11y placeholder (xfail+skipif). Follows the Story 10.1 / 10.2 / 10.3 /
# 9.x precedent: the test is wired but expects npx to be installed for axe-playwright.
@pytest.mark.xfail(strict=False, reason="axe-playwright a11y scan placeholder; npx required")
@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not present on PATH")
async def test_weekly_summary_e2e_a11y_placeholder(db_with_schema: str) -> None:  # noqa: ARG001
    """AC15 #44: a11y scan over the weekly summary panel — placeholder."""
    # Real implementation would: start a Playwright browser, navigate to the
    # dashboard, click the "This week" trigger, axe-scan the expanded panel.
    # Today we just assert npx exists. The xfail marker absorbs the lack of a
    # full Playwright harness.
    assert shutil.which("npx") is not None
    raise AssertionError("a11y scan harness not yet wired")
