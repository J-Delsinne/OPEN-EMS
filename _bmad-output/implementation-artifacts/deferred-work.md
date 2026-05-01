# Deferred Work Log

## Deferred from: code review of 1-1-initialize-project-repository-with-uv-and-dependency-lockfile (2026-05-01)

- **Unbounded `>=` version constraints on all deps** — lockfile mitigates for locked installs; revisit if upgrading without lockfile or moving to library distribution
- **`pytest-asyncio` future major-version risk** — lockfile protects until `uv lock --upgrade`; pin upper bound when upgrading
- **`.python-version` (3.14) gitignored — deployment Python uncontrolled** — document required Python version in deployment guide (Story 1.4/1.5); `requires-python = ">=3.12"` enforces minimum at install
- **`cryptography>=47.0.0` Rust build requirement on 32-bit Pi OS** — deployment guide (Story 1.4) should document armv7l prerequisites; 64-bit Pi OS (aarch64) has pre-built wheels
- **`tailer==0.4.1` (2015, sdist-only) via dsmr-parser** — Python 3.14 compatibility unverified; test dsmr-parser import on 3.14 dev machine when implementing DSMR adapter (Story 3.4)
- **`dlms-cosem==21.3.2` (2021) via dsmr-parser** — same as above; assess at Story 3.4
- **`pymodbus` uncapped + fully mypy-ignored** — lockfile protects until `uv lock --upgrade`; add upper-bound pin and partial stubs when implementing Modbus adapter (Story 3.2)
- **`hatchling` not pinned as explicit dep** — uv manages via build-system lockfile; low risk in practice
- **`__version__` duplicated in `__init__.py` and `pyproject.toml`** — acceptable for v0.1.0; migrate to `importlib.metadata.version("open-ems")` before first tagged release

## Deferred from: code review of 1-2-bootstrap-fastapi-application-with-sqlite-alembic-migrations-and-structured-logging (2026-05-01)

- **Pre-lifespan uvicorn startup logs go through stdlib before `configure_logging` is called** [`src/open_ems/web/app.py`] — Inherent FastAPI lifespan ordering constraint: uvicorn logs its startup banner before the lifespan context manager runs, so the first few lines of output are plain-text stdlib format rather than JSON. The fix (call `configure_logging` before `create_app()`) is an architectural decision. Revisit in Story 1.7 (central configuration) or when adding the main.py entrypoint hardening.

## Deferred from: code review of 1-3-implement-health-endpoints-and-startup-sequence-with-ntp-check (2026-05-01)

- **`_ready` not reset in lifespan teardown** [`src/open_ems/services/readiness.py`] — Module global persists across ASGI warm-reload cycles; not actionable without StateStore integration; revisit in Epic 5 when StateStore lifecycle is defined.
- **`get_settings()` singleton race** [`src/open_ems/settings.py:24`] — Check-then-set TOCTOU under threads; GIL protects in CPython; pre-existing from Story 1.2.
- **`SystemExit(1)` in lifespan version-dependent** [`src/open_ems/web/app.py:46`] — Starlette may or may not propagate cleanly; currently tested; pre-existing from Story 1.2.
- **`db_path` relative default problematic under systemd/Docker** [`src/open_ems/settings.py:15`] — Story 1.7 (central configuration) is the correct place to address deployment paths.
- **`_PROJECT_ROOT` parents[3] wrong under non-standard install** [`src/open_ems/web/app.py:20`] — Pre-existing from Story 1.2; address when Docker/systemd deployment stories (1.4/1.5) are implemented.
- **IPv4-only NTP** [`src/open_ems/services/time_sync.py:35`] — No IPv6 fallback; low risk for home LAN deployment; revisit if IPv6 support is ever needed.
- **`configure_logging` multiple-call inconsistency** [`src/open_ems/logging_config.py`] — structlog caches processor chain on first use; pre-existing from Story 1.2; benign in practice.
- **`alembic.ini` missing gives misleading `migration_failed` log** [`src/open_ems/web/app.py:28`] — Pre-existing from Story 1.2; add a pre-flight existence check in a later hardening story.
- **`from None` discards migration exception context** [`src/open_ems/web/app.py:46`] — Pre-existing from Story 1.2; exception is logged before raise so diagnostic info is preserved.
- **`/health/live` unreachable during uvicorn lifespan startup** [`src/open_ems/web/app.py`] — Uvicorn does not serve requests until lifespan `yield`; both `/health/live` and `/health/ready` are inaccessible during migrations and NTP check. The liveness/readiness distinction is architecturally correct but the pre-lifespan availability of `/health/live` (AC1) cannot be satisfied without a separate thread or port. Defer to Story 1.4 (Docker) and 1.5 (systemd) where deployment topology and healthcheck timing are defined. Accepted by Jordan on 2026-05-01.

## Deferred from: code review of 1-4-add-docker-compose-deployment-with-offline-tls-certificate-generation (2026-05-01)

- **`get_settings()` singleton not thread-safe** [`src/open_ems/settings.py:28-31`] — Check-then-set TOCTOU; GIL protects in CPython uvicorn/asyncio; pre-existing (also noted in 1-3 review)
- **`app = create_app()` executes at module-level import** [`src/open_ems/main.py:8`] — Intentional ASGI pattern; side-effect on import; pre-existing
- **Bind address `host="0.0.0.0"` not configurable via Settings** [`src/open_ems/main.py:14`] — Not in spec; address in Story 1.7 if multi-homed host support is needed
- **`./certs` directory existence not enforced before `docker compose up -d`** [`docker-compose.yml`] — Mitigated by documented installer workflow; add enforcement in a future hardening story if needed
- **`validity_days` not exposed as a configurable Settings field** [`src/open_ems/tools/cert_gen.py`] — Out of scope for Story 1.4; address in Story 1.7 or a dedicated cert management story
- **`generate_tls.py` outputs to stdout with no error handling** [`scripts/generate_tls.py`] — Acceptable for one-shot CLI; improve in installer hardening pass
- **cert_path write is not atomic** [`src/open_ems/tools/cert_gen.py:63-64`] — Low risk for one-time cert generation; improve if cert rotation is ever automated
- **`socket.gethostname()` empty string not guarded** [`scripts/generate_tls.py:10`] — System misconfiguration edge case; cryptography library would raise with adequate context
- **`generate-tls.sh` `dirname "$0"` unreliable if script is symlinked** [`scripts/generate-tls.sh:3`] — Known bash limitation; add marker-file check if symlink-based invocation becomes a deployment pattern
- **`test_cert_valid_period` asserts `>= 364` rather than `>= 365`** [`tests/unit/tools/test_cert_gen.py`] — One-day slack unexplained; functionally correct but weak assertion; clarify intent

## Deferred from: code review of 1-5-add-systemd-native-deployment-with-watchdog-integration (2026-05-01)

- **Silent exception swallowing in `sd_notify` masks notification failures** [`src/open_ems/services/readiness.py:33`] — Pre-existing design decision from Story 1.3; `sd_notify` deliberately swallows all errors because the notification socket is optional (not present outside systemd). Revisit if observability requirements demand notification failure visibility.
- **`curl | sh` supply-chain risk in `install.sh`** [`scripts/install.sh:23`] — Pre-existing design choice for installer convenience (mirrors the official uv install method). Revisit if security posture hardens; mitigation would be vendoring uv binary or verifying a checksum.
- **`NotifyAccess=main` incompatible with multi-worker uvicorn** [`systemd/open-ems.service:14`] — Forward-looking: single-process model assumed throughout Epic 1. If uvicorn is ever configured with multiple workers, `NotifyAccess=exec` or `all` would be required; address at that point.
- **f-string event name `f"clock_{clock_status}"` defeats log aggregation** [`src/open_ems/web/app.py:55`] — Pre-existing from Story 1.3; dynamic event keys prevent grouping in structured log systems. Fix in Story 1.7 (central configuration / logging hardening).
- **`socket.AF_UNIX` not available on Windows; `# type: ignore` hides portability gap** [`src/open_ems/services/readiness.py:31`] — Pre-existing from Story 1.3; intentional — deployment target is Linux only. No action needed unless Windows support is ever added.
- **`WATCHDOG_USEC` read once at startup; dynamic interval extension via systemd not supported** [`src/open_ems/services/watchdog.py:17`] — systemd can dynamically extend the watchdog timeout at runtime; the current implementation ignores `sd_notify("EXTEND_TIMEOUT_USEC=...")`. Epic 8 scope (Story 8.4: watchdog hardening and stall-recovery).
