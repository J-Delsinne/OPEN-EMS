# Deferred Work Log

## Deferred from: code review of 7-4-implement-battery-control-and-ev-scheduling-logic (2026-05-05)

- **`resolve_conflicts` priority weight sort direction undocumented** [`src/open_ems/engine/rules/energy_balancing.py:resolve_conflicts`] — ascending sort means weight=1 beats weight=100; field name is misleading; add a docstring or rename when story 7.3 is revisited.
- **`tiebreaker_key` lexicographic sort produces alphabetically-dependent resolution order** [`src/open_ems/engine/rules/energy_balancing.py:resolve_conflicts`] — renaming a strategy or action type changes resolution order silently; consider a numeric tiebreaker.
- **Simultaneous charge+discharge candidates possible in `maximize_self_consumption`** [`src/open_ems/engine/rules/energy_balancing.py:_evaluate_maximize_self_consumption`] — when PV surplus and grid import co-occur, both candidates are emitted; conflict resolution silently picks charge; no diagnostic flag.
- **Action type strings are untyped literals with no central registry** [`src/open_ems/engine/rules/`] — a typo silently falls through to a hold intent with reason_code "unavailable"; consider a shared `ActionType` enum or registry.
- **DST fold attribute stripped by `_is_inside_window` during clock-back transitions** [`src/open_ems/engine/rules/ev_scheduling.py:_is_inside_window`] — repeated hour during fall-back DST makes both UTC instants compare identically against window boundaries; decide on DST policy when EV charging windows are operationally validated.
- **No SOC upper-bound check before approving battery charge intent** [`src/open_ems/engine/rules/battery_control.py:evaluate_battery_control`] — a 100% SOC battery receives a charge intent every cycle; BMS is expected to reject it; add an optional ceiling field to `BatteryControlContext` if BMS tolerance becomes a concern.

## Deferred from: code review of 7-2-implement-peak-limiting-logic-consuming-peakcontext (2026-05-04)

- **`current_monthly_recorded_peak_kw`, `current_interval_start`, `current_interval_elapsed_seconds` unused in rule logic** [`src/open_ems/engine/rules/peak_limiting.py`] — intentional per spec; fields validated and available for Stories 7.3–7.5 and Epic 8 runtime.
- **`current_interval_elapsed_seconds=900` (fully elapsed) treated identically to partial window** [`src/open_ems/engine/models.py:IntervalElapsedSeconds`] — no branch differentiates fully elapsed vs. mid-window; Story 7.5 / Epic 8 may need boundary differentiation.
- **`_require_utc` private helper used exactly once** [`src/open_ems/engine/models.py`] — indirection without reuse; inline or promote to shared utility when a second UTC datetime field is validated.
- **`LoadReductionAction` StrEnum members carry redundant explicit string values** [`src/open_ems/engine/rules/peak_limiting.py:14-15`] — cosmetic; `StrEnum` defaults to lowercase name; remove explicit values or document if the serialized string is an external contract.

## Deferred from: code review of 7-1-implement-systemoperatingmode-derivation-and-degradation-matrix (2026-05-04)

- **`DegradedDeviceState.role` not validated against positional slot** [`src/open_ems/engine/models.py`] — spec uses `state.role` as source of truth; `from_snapshot` produces correct objects; add cross-validation when/if defensive hardening of direct construction is required.

## Deferred from: code review of 6-3-implement-installer-note-storage-and-config-audit-log (2026-05-04)

- **Non-atomic `config_version` increment** [`src/open_ems/storage/repositories/config_audit_repo.py:55`] — `SELECT COALESCE(MAX(config_version), 0) + 1` + `INSERT` without `BEGIN IMMEDIATE`; same read-then-write pattern as `EventLogRepo`; SQLite serialized write-lock mitigates in single-process deployment; fix with explicit transaction wrapping when/if multi-process access is ever required.
- **`ConfigAuditRepo.get_connection()` fallback path untested** [`src/open_ems/storage/repositories/config_audit_repo.py:35`] — the `None`-conn constructor fallback has no test coverage; consistent with the pre-existing `EventLogRepo` constructor pattern; cover when/if the injection contract is ever formalized.
- **Migration `0006` downgrade path not tested** [`migrations/versions/0006_add_config_audit_log_table.py`] — `downgrade()` drops indexes then the table but has no integration test; consistent with `0005` approach; cover if a downgrade test suite is ever added to CI.

## Deferred from: code review of 4-4-implement-device-capability-profile-system-and-capability-gate (2026-05-03)

- **Template profiles with `device_id="__placeholder__"` in public `__all__`** — direct dict import bypasses `get_profile` and returns profiles with a fake device identity; consider removing from `__all__` or documenting the hazard explicitly.
- **`_SUPPORTED_MODELS` and capability registry have no cross-validation** — adding a model to an adapter's `_SUPPORTED_MODELS` without adding it to the capability registry silently degrades to a REDUCED profile; a startup assertion could guard this.
- **`model: str` on `DeviceCapabilityProfile` allows empty/whitespace** — pre-existing field; replacing with `NonEmptyStr` would be more defensive, consistent with `device_id`.
- **`model_copy` in `get_profile()` could propagate accidental `limitation_reason` from a misconfigured template** — theoretical risk; all current templates are clean; add a no-`limitation_reason` invariant test if templates grow complex.



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

## Deferred from: code review of 3-2-implement-modbus-tcp-adapter-with-connection-management-and-timeout-enforcement (2026-05-02)

- **`asyncio.CancelledError` propagates from adapter** [`src/open_ems/adapters/modbus/tcp.py`] — Not in `_RECOVERABLE_EXCEPTIONS`; `CancelledError` is a `BaseException` and correctly propagates for cooperative task cancellation; not a communication failure — correct Python async behavior

## Deferred from: code review of 2-5-implement-session-expiry-multi-device-concurrency-policy-and-logout (2026-05-02)

- **Unrecognised role silently gets homeowner session timeout** [`src/open_ems/web/dependencies.py:85-88`] — `else` branch applies homeowner timeout to any non-installer role value; only installer/homeowner exist today; pre-existing design concern not caused by this diff
- **`SessionRepo()` instantiated inside background task with implicit global connection** [`src/open_ems/web/app.py:89`] — pre-existing pattern across all request handlers; low risk with single-process asyncio model; revisit if connection lifecycle is ever refactored
- **`touch()` accepts a naive `datetime` for `new_expires_at` without validation** [`src/open_ems/storage/repositories/session_repo.py:touch`] — all callers pass tz-aware datetimes today; a future caller passing a naive datetime would store a tz-naive string, breaking lexicographic comparisons; add a tzinfo assertion when defensive hardening is desired

## Deferred from: code review of 3-3-implement-ocpp-16-central-system-adapter (2026-05-02)

- **`OCPPCentralSystem.register()` silently overwrites existing adapter** [`src/open_ems/adapters/ocpp/central_system.py:274-278`] — No guard or warning if `register()` is called twice for the same `charge_point_id`; the first adapter (and its state/in-flight connections) is silently discarded. Pre-existing design choice; no story requirement to guard it; revisit if multi-registration scenarios arise in Epic 4 or production debugging.

## Deferred from: code review of 3-4-implement-dsmr-p1-adapter-with-raw-telegram-parsing-and-staleness-timestamping (2026-05-02)

- **Serial baudrate hardcoded at 115200 for all DSMR versions** [`src/open_ems/adapters/dsmr/p1.py:_SerialSource.open()`] — DSMR v2.2 and v4 specify 9600 baud per the DSMR standard, but the story spec says "use V5 settings as base"; this follows the spec. Address if v2.2/v4 serial support must be accurate in a future story.
- **`serial_asyncio_fast` `ImportError` propagates as non-`OSError` and kills `_read_loop` permanently** [`src/open_ems/adapters/dsmr/p1.py:_SerialSource.open()`] — `serial_asyncio_fast` is a transitive dep of `dsmr-parser` so normally always installed; failure would only occur in malformed venv scenarios; address if explicit ImportError handling is needed.
- **`_connected` flag written both inside and outside `_lock`** [`src/open_ems/adapters/dsmr/p1.py`] — Flag is not currently exposed externally nor read in `get_raw_state()`; inconsistent locking discipline is a latent issue if the flag is ever surfaced via a status method.

## Deferred from: code review of story 4-1-define-domain-device-state-model-deviceadapter-protocol-and-energy-sign-conventions (2026-05-03)

- **`BatteryState.capacity_kwh: float` — no `ge=0` constraint** [`src/open_ems/core/devices.py:BatteryState`] — negative capacity is physically impossible; `EnergyKwh` alias exists; spec does not require a guard; address in Story 4.2+ when normalization validates adapter output
- **`EVChargerState.current_power_kw` accepts negative** [`src/open_ems/core/devices.py:EVChargerState`] — V2G sign convention unresolved at this layer; spec does not constrain it; revisit in Story 4.3 when OCPP normalization is defined
- **`EVChargerState` cross-field consistency not validated** [`src/open_ems/core/devices.py:EVChargerState`] — `status="available"` + `session_active=True` accepted; out of scope for Story 4.1 (structural model only); add a model validator in Story 4.3 or 4.4
- **`DeviceAdapter.device_id: str` allows empty string** [`src/open_ems/core/devices.py:DeviceAdapter`] — intentional per spec (`str`, not `NonEmptyStr`); consider documenting the invariant in the protocol docstring in a future story
- **mypy pre-commit hook lacks explicit `--strict` in `args`** [`.pre-commit-config.yaml:mypy hook`] — relies on `pyproject.toml` (`strict = true` confirmed); works correctly in standard usage; add `--strict` to args as belt-and-suspenders if the config is ever moved

## Deferred from: code review of 4-2-implement-modbus-device-normalization-with-register-maps (2025-06-03)

- **Shared singleton register map instances** [inverter_adapter.py, battery_adapter.py] -- _SUPPORTED_MODELS holds module-level singletons; currently safe (maps are stateless), but latent contamination risk if future maps accumulate per-call state. Revisit if register maps add mutable fields.
- **Huawei _DEVICE_STATUS_MAP missing value 3** [huawei_sun2000_v3.py] -- gap between status codes 2 and 4; silently yields 'unknown'. Defer until real Huawei SUN2000 register specification is confirmed for this status code range.
- **ValidationError reason string unbounded** -- large register dumps or long error text can appear in DegradedDeviceState.reason without truncation. Pre-existing NonEmptyStr has no max_length.
- **BYD register gap at address 103** [byd_hvs_v1.py, byd_hvm_v1.py] -- address 103 skipped between _REG_POWER_W=102 and _REG_DIRECTION=104 with no comment; may be a reserved/unused BYD register. Confirm when real BYD HVS/HVM Modbus spec is available.
- **int16()/uint16() accept out-of-range inputs silently** [register_maps/utils.py] -- no 16-bit bounds enforcement; upstream Modbus protocol layer expected to deliver valid values. Add bounds check if a protocol-layer gap is found.

## Deferred from: code review of 4-5-implement-device-discovery-and-connection-management (2026-05-03)

- **AC3 automated reconnection test** — `connect()` behavior covered by earlier adapter unit tests; the startup orchestrator that calls Step 6 (initialize protocol adapters) belongs to Epic 8; no dedicated reconnect-on-restart test added
- **`_RECONNECTING_REASONS` duplicated across 4 adapter modules** — code smell, not a runtime bug; sets legitimately differ per protocol; refactor to a shared utility function if the pattern grows (e.g., `adapters/_reason.py`)
- **`probe_modbus_endpoint`: `ModbusTcpAdapterConfig` `ValidationError` propagates on bad caller args** — caller-error boundary; `DeviceProbeError` is reserved for unreachable devices, not programmer input errors [`discovery.py:76-101`]
- **`probe_dsmr_endpoint`: invalid `dsmr_version` literal raises `DSMRAdapterConfig` `ValidationError`** — caller error; validated at construction [`discovery.py:148-154`]
- **`register_ocpp_discovery`: empty `device_id`/`address` raises Pydantic `ValidationError`** — caller error; `NonEmptyStr` on `DeviceDiscoveryResult` is the correct guard [`discovery.py:204-232`]
- **`_to_degraded` empty reason string raises `ValidationError` in all 4 adapters** — Epic 3 contract violation (`ProtocolDegradedState` should enforce non-empty reason); not Epic 4's responsibility
- **Unit tests couple to live capability registry** — intentional; registry is in-process and stable; only becomes fragile if registry keys change [`test_discovery.py`]
- **`probe_modbus_endpoint`: `adapter.close()` in `finally` may hang if TCP connection is wedged** — pre-existing `ModbusTcpAdapter` behavior; address if close() timeouts become a production issue [`discovery.py:100`]
- **Unit test `test_probe_dsmr_success`: `patch("DSMRAdapter")` does not validate `DSMRAdapterConfig` construction** — inherent patch limitation; config validation is covered by `DSMRAdapter`'s own unit tests [`test_discovery.py`]

## Deferred from: code review of 5-3-implement-htmx-polling-endpoints-and-per-device-stale-and-unavailable-detection (2026-05-04)

- **`_build_slots` mutates `_known_device_ids` for roles before a `_validate_state_role` failure** [`src/open_ems/core/state_store.py:_build_slots`] — pre-existing pattern; `_last_successful_states` follows the same convention; if `_validate_state_role` raises mid-loop, roles already processed have committed to both dicts while the snapshot is not updated; revisit if multi-role publish atomicity is ever required.
- **No dedicated concurrency test for HTMX `get_snapshot()` + rapid `publish()`** [`tests/unit/core/test_state_store.py`] — `get_snapshot()` is lock-free by design; the existing SSE concurrency test covers the harder write-path case; HTMX reads cannot observe out-of-order state by construction; add an explicit combined test when HTMX + SSE + publish load testing is required.

## Deferred from: code review of 6-2-enforce-append-only-semantics-and-event-retention-policy (2026-05-04)

- **`append()` has no direct repository-level unit tests** [`src/open_ems/storage/repositories/event_log_repo.py:17`] — story 6-1 debt; `append()` is only exercised indirectly through `ObservabilityService`; repo-level contract violations (row_id=None path, allow_nan enforcement) have no direct regression protection
- **`_CRITICAL_EVENT_TYPES` empty produces `NOT IN ()` SQL** [`src/open_ems/storage/repositories/event_log_repo.py:62`] — non-standard SQL; SQLite accepts it (semantically correct: all rows prunable), but behavior is undocumented and non-portable; low practical risk with a hardcoded frozenset
- **Pruning task starts unconditionally — silently retries if event_log migration not applied** [`src/open_ems/web/app.py:212`] — matches session cleanup task pattern; `init_database()` runs migrations before any tasks start; theoretical only
- **rowcount read before commit in `prune_expired` — logged count may reflect uncommitted deletions** [`src/open_ems/storage/repositories/event_log_repo.py:63-68`] — pre-existing; SQLite commit failures are extremely rare in WAL mode; acceptable risk
- **`cursor.lastrowid` checked after commit in `append()`** [`src/open_ems/storage/repositories/event_log_repo.py:50`] — pre-existing story 6-1; INSERT committed before None guard fires; guard is a post-commit diagnostic, not a pre-commit safety valve
- **`json.dumps(allow_nan=False)` in `append()` unhandled** [`src/open_ems/storage/repositories/event_log_repo.py:30`] — pre-existing story 6-1; `ValueError` on non-finite floats propagates to caller without domain context; audit record silently lost if caller does not handle it
- **`ObservabilityService` validates `detail` then discards; `append()` re-serializes — double-serialization not atomic** [`src/open_ems/services/audit_log.py:48`] — pre-existing story 6-1; both use `allow_nan=False` so currently harmless; a detail dict mutating between the two calls could produce divergent JSON
- **`_CRITICAL_EVENT_TYPES` not validated as subset of `VALID_EVENT_TYPES`** [`src/open_ems/storage/repositories/event_log_repo.py:10`] — `VALID_EVENT_TYPES` defines `{"DECISION", "DEVICE", "SYSTEM", "CONSTRAINT", "INSTALLER"}` while `_CRITICAL_EVENT_TYPES` only contains `{"CONSTRAINT"}`; no test or assertion enforces the subset relationship; a future event type added to `VALID_EVENT_TYPES` without updating the frozenset silently loses pruning protection

## Deferred from: code review of 4-3-implement-ocpp-and-dsmr-device-normalization (2026-05-03)

- **`reversed()` ordering assumption for MeterValues** [`src/open_ems/adapters/ocpp/central_system.py:on_meter_values`] — latest sample taken from `reversed(meter_value)[-1]`; assumes OCPP chronological ordering of the list. OCPP 1.6 spec does not guarantee order. Acceptable for Story 4.3 scope; harden in Story 8 when production charger behaviour is observed.
- **Missing `"value"` key in MeterValues sampled_value silently skipped** [`src/open_ems/adapters/ocpp/central_system.py:on_meter_values`] — measurands without a `"value"` key are skipped via `continue`; this is correct protocol-layer robustness (malformed frames must not crash the adapter). No change needed.
- **`_ChargerState` / `RawOCPPState` field mapping is hand-maintained** [`src/open_ems/adapters/ocpp/central_system.py`] — `get_raw_state()` manually copies every field from `_ChargerState` to `RawOCPPState`; adding a field to one without the other is a latent omission risk. Story 8 hardening candidate: replace with a dataclass-to-model helper or shared field set.
- **Negative power guard fires on every poll once triggered** [`src/open_ems/adapters/ocpp/charger_adapter.py:get_state`] — once `last_meter_values_power_kw < 0`, every subsequent `get_state()` call returns `DegradedDeviceState` until the OCPP state changes. By design at this layer; consumers (Epic 5 StateStore) are responsible for deduplicating repeated degraded states.
