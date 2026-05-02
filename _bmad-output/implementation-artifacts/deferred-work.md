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

## Deferred from: code review of 1-6-configure-github-actions-ci-pipeline-with-migration-validation (2026-05-01)

- **Stale SQLite DB from prior pytest run may exist when alembic step runs** [`.github/workflows/ci.yml`:38] — `alembic upgrade head` on an already-upgraded DB is a no-op; low risk; revisit if tests ever create and migrate the DB explicitly
- **Dockerfile uses floating `ghcr.io/astral-sh/uv:latest` tag** [`Dockerfile`] — uv version drift between quality job and Docker build; pin to a specific uv version when hardening release reproducibility
- **GHCR image name uppercase latent risk** [`.github/workflows/ci.yml`:70] — `github.repository` may contain uppercase; GHCR requires lowercase; not an issue for this all-lowercase repo; add `.toLowerCase()` filter if repo is ever renamed with uppercase
- **GITHUB_TOKEN `packages:write` may be blocked at org level** [`.github/workflows/ci.yml`:47-48] — org-level Actions policy can override the declared permission; verify org settings before first tagged release
- **GHA cache 10 GB eviction on large multi-arch builds** [`.github/workflows/ci.yml`:84-85] — `cache-to: type=gha,mode=max` evicts silently when the per-repo cache limit is reached; monitor build times after a few releases
- **No `environment` gate on release job** [`.github/workflows/ci.yml`:41] — any `v*` tag triggers Docker release without human checkpoint; add a GitHub environment with required reviewers if stricter release governance is needed
- **`mypy` not checking `tests/`** [`.github/workflows/ci.yml`:33] — type errors in test helpers/conftest.py are not caught; extend mypy scope to `tests/` in a future quality hardening pass
- **No pytest coverage threshold** [`.github/workflows/ci.yml`:35] — `pytest` exits 0 with zero tests collected; add `--cov` and `--cov-fail-under` when coverage reporting is set up

## Deferred from: code review of 1-7-establish-central-configuration-system-with-pydantic-settings (2026-05-02)

- **`secret_key` uses `str` instead of `SecretStr`** [`src/open_ems/settings.py`] — value will appear in repr/model_dump/logs; address when `SECRET_KEY` is first consumed in auth (Epic 2)
- **`_settings` singleton stays `None` after `ValidationError`** [`src/open_ems/settings.py`] — future non-lifespan callers would retry and re-raise; pre-existing singleton pattern; benign while SystemExit terminates the process before retry
- **Structlog lazy binding may produce non-JSON format for startup error log** [`src/open_ems/web/app.py`] — `configure_logging` called inside the except block may fire after structlog already cached its pre-config processor chain; pre-existing ordering hazard noted in Story 1-2 review

## Deferred from: code review of 1-5-add-systemd-native-deployment-with-watchdog-integration (2026-05-01)

- **Silent exception swallowing in `sd_notify` masks notification failures** [`src/open_ems/services/readiness.py:33`] — Pre-existing design decision from Story 1.3; `sd_notify` deliberately swallows all errors because the notification socket is optional (not present outside systemd). Revisit if observability requirements demand notification failure visibility.
- **`curl | sh` supply-chain risk in `install.sh`** [`scripts/install.sh:23`] — Pre-existing design choice for installer convenience (mirrors the official uv install method). Revisit if security posture hardens; mitigation would be vendoring uv binary or verifying a checksum.
- **`NotifyAccess=main` incompatible with multi-worker uvicorn** [`systemd/open-ems.service:14`] — Forward-looking: single-process model assumed throughout Epic 1. If uvicorn is ever configured with multiple workers, `NotifyAccess=exec` or `all` would be required; address at that point.
- **f-string event name `f"clock_{clock_status}"` defeats log aggregation** [`src/open_ems/web/app.py:55`] — Pre-existing from Story 1.3; dynamic event keys prevent grouping in structured log systems. Fix in Story 1.7 (central configuration / logging hardening).
- **`socket.AF_UNIX` not available on Windows; `# type: ignore` hides portability gap** [`src/open_ems/services/readiness.py:31`] — Pre-existing from Story 1.3; intentional — deployment target is Linux only. No action needed unless Windows support is ever added.
- **`WATCHDOG_USEC` read once at startup; dynamic interval extension via systemd not supported** [`src/open_ems/services/watchdog.py:17`] — systemd can dynamically extend the watchdog timeout at runtime; the current implementation ignores `sd_notify("EXTEND_TIMEOUT_USEC=...")`. Epic 8 scope (Story 8.4: watchdog hardening and stall-recovery).

## Deferred from: code review of 2-2-implement-login-form-hardened-session-creation-and-brute-force-protection (2026-05-02)

- **`update_last_active` raises `ValueError` on missing session — no caller yet** [`src/open_ems/storage/repositories/session_repo.py:78-83`] — Story 2.5 will call this on every authenticated request; at that point the caller must catch or the unhandled exception produces a 500; assess whether to raise a domain-specific exception or return a sentinel
- **`get_by_token_hash` does not filter by `expires_at`** [`src/open_ems/storage/repositories/session_repo.py:46-53`] — Expired token rows are returnable; session validation in Story 2.5 must explicitly check `expires_at` after fetching the row
- **No expired session cleanup from `sessions` table** [`migrations/versions/0003_add_sessions_table.py`] — Rows accumulate indefinitely; Story 2.5 will add the bulk prune `DELETE FROM sessions WHERE expires_at < ?` using the `ix_sessions_expires_at` index already in place
- **No rollback in `session_repo.create()` if `commit()` fails after `execute()`** [`src/open_ems/storage/repositories/session_repo.py:38-43`] — Partial write inconsistency on commit failure; pre-existing limitation of aiosqlite singleton pattern; revisit if connection reliability becomes a concern
- **Single shared aiosqlite connection — no pooling** — All concurrent auth requests serialise on one connection; pre-existing architecture decision; revisit at scale or if latency under concurrent login becomes measurable

## Deferred from: code review of 2-1-define-user-model-upgradeable-credential-storage-and-safe-admin-bootstrap (2026-05-02)

- **`UserRepo()` hard-coded in lifespan with no dependency injection** [`src/open_ems/web/app.py:124`] — Works correctly today; ties lifespan integration testing to the live `get_connection()` singleton; revisit when writing lifespan-level integration tests.
- **`INITIAL_ADMIN_PASSWORD` persists in process memory via pydantic singleton** [`src/open_ems/settings.py:27`] — `SecretStr` prevents repr/log leakage; clearing the field post-bootstrap is non-standard pydantic; acceptable risk for embedded single-process deployment.
- **`server_default="1"` string DDL for INTEGER column** [`migrations/versions/0002_add_users_table.py:31`] — Semantically correct in SQLite; diverges from stricter dialects; SQLite-only project, no action needed unless DB backend changes.
- **AC5: No raw-SQL boundary enforcement via linting or CI grep** — Repo pattern is stated in the spec but unenforceable without tooling; enforce via `ruff` custom rule or grep-in-CI in a future quality hardening pass.
- **CI migration validation step has no `SECRET_KEY` env var** [`.github/workflows/ci.yml:44`] — Pre-existing from 1.6; if `alembic/env.py` calls `get_settings()`, the migration CI step will crash on secrets validation rather than migration errors; investigate when hardening CI environment variables.

## Deferred from: code review of 2-4-implement-csrf-protection-on-state-changing-routes (2026-05-02)

- **`request.body()` in middleware may conflict with downstream body readers in some Starlette versions** [`src/open_ems/web/csrf.py:60`] — Body is cached in `request._body` which Starlette's `Request.body()` uses on subsequent reads; low risk for current use case; revisit if switching from BaseHTTPMiddleware to pure ASGI middleware
- **Expired sessions trigger CSRF 403 instead of session-expired error** [`src/open_ems/web/csrf.py:47`] — `get_by_token_hash` has no `expires_at` filter; expired but un-purged session rows satisfy `session_row is not None`, causing CSRF enforcement to fire and return 403 before the route handler can return a meaningful expired-session response; deferred to Story 2-5 (session expiry and cleanup)

- **Expired sessions authenticate indefinitely** [`src/open_ems/web/dependencies.py:42-53`] — `_resolve_session` never reads `expires_at`; explicitly deferred to Story 2.5 per Dev Notes and Story 2.2 review findings
- **No exception handling in `_resolve_session` for DB failures** [`src/open_ems/web/dependencies.py:42-53`] — `get_connection()` raises `RuntimeError` if DB not initialized; unreachable in normal app flow but unhandled at the dependency level; revisit if exception taxonomy is ever hardened
- **`_is_htmx_or_api` treats all non-HTMX as browser** [`src/open_ems/web/dependencies.py:32-35`] — Minor deviation from AC3's strict positive-browser-detection wording (`Accept: text/html` AND no `HX-Request`); Dev Notes explicitly specify the inverse-logic implementation; API clients not sending `Accept: application/json` get a redirect instead of 401; benign in practice
- **`InstallerUser`/`HomeownerUser` type aliases provide no compile-time role enforcement** [`src/open_ems/web/dependencies.py:28-29`] — Simple name aliases (`= AuthenticatedUser`); `NewType` would make mismatched dependencies detectable by mypy; spec-mandated design; revisit as a quality improvement before Epic 10/11

## Deferred from: code review of 2-5-implement-session-expiry-multi-device-concurrency-policy-and-logout (2026-05-02)

- **Unrecognised role silently gets homeowner session timeout** [`src/open_ems/web/dependencies.py:85-88`] — `else` branch applies homeowner timeout to any non-installer role value; only installer/homeowner exist today; pre-existing design concern not caused by this diff
- **`SessionRepo()` instantiated inside background task with implicit global connection** [`src/open_ems/web/app.py:89`] — pre-existing pattern across all request handlers; low risk with single-process asyncio model; revisit if connection lifecycle is ever refactored
- **`touch()` accepts a naive `datetime` for `new_expires_at` without validation** [`src/open_ems/storage/repositories/session_repo.py:touch`] — all callers pass tz-aware datetimes today; a future caller passing a naive datetime would store a tz-naive string, breaking lexicographic comparisons; add a tzinfo assertion when defensive hardening is desired
