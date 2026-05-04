# Story 6.3: Implement installer note storage and config audit log

Status: done

<!-- Completion note: Ultimate context engine analysis completed - comprehensive developer guide created. -->

## Story

As a developer,
I want installer note entries writable to the event log and a config audit log table that records all safety-relevant configuration changes with structured before/after values,
So that installers can annotate the audit trail with context and every configuration change affecting system behavior is durably traceable with full unit and structure preserved.

## Acceptance Criteria

**AC1 - Installer note validation and sanitization**
Given an installer submits a freetext note
When the note is processed
Then empty or whitespace-only notes are rejected with `ValueError`
And notes exceeding 2,000 characters are rejected with `ValueError` and are not truncated
And the accepted note body is stripped and sanitized for safe HTML rendering before storage
And the note is written through `ObservabilityService.audit()` as an `event_log` entry with `actor=installer`, `event_type=INSTALLER`, and sanitized text as `summary`
And installer notes remain subject to the existing append-only event log guarantee.

**AC2 - Config audit schema**
Given the database is migrated to head
When migration `0006` runs
Then it creates `config_audit_log` with: `id`, `actor`, `timestamp`, `field`, `previous_value`, `new_value`, `config_version`
And `timestamp` is stored as timezone-aware UTC ISO 8601 text
And `previous_value` and `new_value` are stored as JSON text, not Python repr strings
And the migration includes useful indexes for `timestamp`, `config_version`, and `field`
And downgrade drops the indexes before dropping the table.

**AC3 - Config audit activation write**
Given a safety-relevant configuration change is activated
When the activation is committed through the config audit repository/service
Then one row is written to `config_audit_log` for each changed safety-relevant field
And all rows in the same activation share one monotonically increasing `config_version`
And each later activation receives the next version
And the row stores `actor`, UTC `timestamp`, `field`, JSON `previous_value`, JSON `new_value`, and `config_version`
And invalid actors are rejected before any rows are written.

**AC4 - Safety-relevant field coverage**
Given a changed field is one of the safety-relevant fields
When audit rows are created
Then the field is accepted and recorded.
The required safety-relevant fields are:
- `peak_consumption_limit`
- `battery_reserve_floor`
- `ev_charging_window_preference`
- `energy_strategy_default`
- `device_role_assignment`
- `capability_override`
- `capability_acceptance`

Given a changed field is outside this set
When audit rows are created
Then the call raises `ValueError` before writing any rows.

**AC5 - Structured JSON integrity**
Given previous or new values contain structured data such as `{"value": 3.5, "unit": "kW"}`
When a config audit row is written
Then the stored JSON round-trips to the same structure
And non-JSON-serializable or non-finite values raise `ValueError` before any row is written.

**AC6 - Tests**
- Unit tests verify installer notes produce correctly typed `event_log` rows with sanitized content.
- Unit tests verify over-length notes are rejected.
- Unit tests verify empty or whitespace-only notes are rejected.
- Unit tests verify a config change produces `config_audit_log` rows with correct JSON values, actor attribution, UTC timestamp, field, and shared config version.
- Unit tests verify `config_version` increments once per activation, not once per field.
- Unit tests verify every defined safety-relevant field can produce an audit row.
- Unit tests verify an unknown field, invalid actor, and invalid JSON values reject before any partial write.
- Integration migration tests continue to pass with Alembic upgrade to head and verify the `config_audit_log` schema exists.

## Tasks / Subtasks

- [x] Task 0: Pre-story quality gate (AC: all)
  - [x] Run `uv run python -m pytest tests/ --no-cov -q` and record the baseline (currently expected around 578 passing tests).
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m mypy src/`.

- [x] Task 1: Add installer note API on ObservabilityService (AC: AC1, AC6)
  - [x] Modify `src/open_ems/services/audit_log.py`.
  - [x] Add `MAX_INSTALLER_NOTE_LENGTH: int = 2000`.
  - [x] Use the stdlib `html.escape(..., quote=True)` for storage-time HTML escaping; do not add a sanitizer dependency.
  - [x] Add `async def installer_note(self, note: str) -> None`.
  - [x] Validate `note.strip()` is non-empty.
  - [x] Validate the original note length is `<= MAX_INSTALLER_NOTE_LENGTH`; reject instead of truncating.
  - [x] Store `html.escape(note.strip(), quote=True)` as the `summary`.
  - [x] Call `self.audit(actor="installer", event_type="INSTALLER", summary=sanitized)`.
  - [x] Do not create a direct `EventLogRepo.append()` path for notes.

- [x] Task 2: Add config audit migration (AC: AC2)
  - [x] Create `migrations/versions/0006_add_config_audit_log_table.py`.
  - [x] Set `revision = "0006"` and `down_revision = "0005"`.
  - [x] Create table `config_audit_log`:
    - `id INTEGER PRIMARY KEY AUTOINCREMENT`
    - `actor TEXT NOT NULL`
    - `timestamp TEXT NOT NULL`
    - `field TEXT NOT NULL`
    - `previous_value TEXT NOT NULL`
    - `new_value TEXT NOT NULL`
    - `config_version INTEGER NOT NULL`
  - [x] Add indexes `ix_config_audit_log_timestamp`, `ix_config_audit_log_config_version`, and `ix_config_audit_log_field`.
  - [x] Keep the Alembic style consistent with `migrations/versions/0005_add_event_log_table.py`.

- [x] Task 3: Implement config audit repository (AC: AC3-AC5)
  - [x] Create `src/open_ems/storage/repositories/config_audit_repo.py`.
  - [x] Define `SAFETY_RELEVANT_CONFIG_FIELDS: frozenset[str]` containing exactly the AC4 field names.
  - [x] Define repository-local `VALID_CONFIG_AUDIT_ACTORS: frozenset[str] = frozenset({"system", "installer", "homeowner"})`; do not import from `services.audit_log`.
  - [x] Implement a small immutable value object, e.g. `ConfigAuditChange(field: str, previous_value: dict[str, object], new_value: dict[str, object])`.
  - [x] Implement `ConfigAuditRepo` using the same constructor pattern as `EventLogRepo`: optional `aiosqlite.Connection`, otherwise `get_connection()`.
  - [x] Implement `async def append_activation(actor: str, changes: Sequence[ConfigAuditChange]) -> int`.
  - [x] Reject empty change sets with `ValueError`.
  - [x] Reject invalid actors before writing any rows.
  - [x] Reject unknown fields before writing any rows.
  - [x] Serialize `previous_value` and `new_value` with `json.dumps(..., allow_nan=False)` before writing; wrap `TypeError`/`ValueError` as `ValueError("config audit values must be JSON-serializable")`.
  - [x] Compute the next version with `SELECT COALESCE(MAX(config_version), 0) + 1 FROM config_audit_log`.
  - [x] Use one UTC timestamp per activation for all rows.
  - [x] Insert all rows, then commit once. If validation fails, do not commit partial data.
  - [x] Return the new `config_version`.

- [x] Task 4: Add focused tests (AC: AC1-AC6)
  - [x] Extend `tests/unit/services/test_audit_log.py` for `installer_note()`.
  - [x] Create `tests/unit/storage/repositories/test_config_audit_repo.py`.
  - [x] Use in-memory `aiosqlite.connect(":memory:")` fixtures with explicit DDL, mirroring existing repository tests.
  - [x] Assert sanitized installer note summary for a note containing HTML-significant characters such as `<script>alert("x")</script>`.
  - [x] Assert note length 2,001 rejects and no row is written.
  - [x] Assert whitespace-only note rejects and no row is written.
  - [x] Assert structured values round-trip through JSON for unit-bearing values.
  - [x] Assert two changes in one activation share the same version.
  - [x] Assert a second activation increments version by one.
  - [x] Parametrize every `SAFETY_RELEVANT_CONFIG_FIELDS` value.
  - [x] Assert unknown field rejects before writing any rows.
  - [x] Assert invalid actor rejects before writing any rows.
  - [x] Assert invalid JSON values such as `float("nan")` reject before writing any rows.
  - [x] Extend `tests/integration/test_migrations.py` or add a focused migration test that upgrades a temporary SQLite DB to head and asserts `config_audit_log` columns and indexes.

- [x] Task 5: Final validation (AC: all)
  - [x] Run `uv run python -m ruff check .`.
  - [x] Run `uv run python -m ruff format --check .`.
  - [x] Run `uv run python -m mypy src/`.
  - [x] Run `uv run python -m pytest tests/ --no-cov -q`.
  - [x] Leave the story in `review` only after implementation is complete and all checkboxes reflect reality.

### Review Findings

- [x] [Review][Patch] Multi-change partial-write guard test missing — no test exercises a mixed-validity `Sequence[ConfigAuditChange]` where one entry is valid and another is invalid; the AC6 "no partial write" guarantee is only verified for single-change lists [tests/unit/storage/repositories/test_config_audit_repo.py]
- [x] [Review][Patch] FakeAuditLog missing `installer_note()` method — the test double for `ObservabilityService` does not expose the new method; duck-typed consumers of `FakeAuditLog` will fail at runtime when calling `installer_note()` [tests/helpers/fake_audit_log.py]
- [x] [Review][Patch] Error message hardcodes literal `"2000"` — should reference `MAX_INSTALLER_NOTE_LENGTH` so the message stays accurate if the constant changes [src/open_ems/services/audit_log.py:76]
- [x] [Review][Defer] Non-atomic `config_version` increment — `SELECT MAX + INSERT` without `BEGIN IMMEDIATE`; same read-then-write pattern as `EventLogRepo`; SQLite serialized writes mitigate in single-process deployment [src/open_ems/storage/repositories/config_audit_repo.py:55] — deferred, pre-existing
- [x] [Review][Defer] `ConfigAuditRepo.get_connection()` fallback path untested — the `None`-conn constructor fallback has no test coverage; consistent with `EventLogRepo` constructor pattern [src/open_ems/storage/repositories/config_audit_repo.py:35] — deferred, pre-existing
- [x] [Review][Defer] Migration downgrade path not tested — `0006` downgrade has no test; consistent with `0005` approach [migrations/versions/0006_add_config_audit_log_table.py] — deferred, pre-existing

## Dev Notes

### Scope and Non-Scope

This story is the storage/service foundation for FR21 and AR19. It does not build the installer event-log UI, config UI, staged validation flow, device registry persistence, or full site config model. Those are later Epic 9 and Epic 11 consumers.

Do not create an installer route just to satisfy note storage. The direct implementation target is:
- `ObservabilityService.installer_note()` for validated, sanitized note writes.
- `config_audit_log` migration and `ConfigAuditRepo` for durable config-change audit rows.

### Existing Code to Preserve

`src/open_ems/services/audit_log.py`
- Current state: `ObservabilityService.audit()` validates non-empty summary, actor enum, event type enum, JSON-serializable detail, emits `structlog` `audit_event`, then appends through `EventLogRepo`.
- Change: add a convenience method for installer notes that delegates to `audit()`.
- Preserve: `audit()` remains the only entry point for audit-worthy `event_log` writes. Do not bypass its validation or structlog output.

`src/open_ems/storage/repositories/event_log_repo.py`
- Current state: `append()` writes UTC ISO timestamps and JSON detail; `update()` and `delete()` raise `RuntimeError`; `prune_expired()` is the only deletion path.
- Change: none expected for config audit except importing its patterns.
- Preserve: installer notes use this through `ObservabilityService.audit()` and inherit append-only semantics.

`migrations/versions/0005_add_event_log_table.py`
- Current state: latest migration creates `event_log` and timestamp/event type indexes.
- Change: add `0006`, do not edit `0005`.
- Preserve: migration chain must remain linear and Alembic upgrade head must still run against in-memory SQLite.

### Implementation Guardrails

- Use `html.escape` from the Python stdlib. The project has no HTML sanitizer dependency and this story only needs safe rendering for a text summary, not rich text.
- Store sanitized installer note text in `event_log.summary`; do not store raw note text in `detail`.
- Reject over-length notes before escaping. Escaping can expand characters, and the AC is about submitted note length, not escaped storage length.
- Do not silently trim over-length notes. `strip()` is only for empty detection and removing surrounding whitespace from accepted notes.
- Keep `actor="installer"` hardcoded in `installer_note()`; caller identity and user-id level attribution can be expanded when the installer UI and user context need it.
- `ConfigAuditRepo` owns raw SQL for `config_audit_log`; do not put raw SQL in services or routes.
- Config audit JSON values must preserve unit context. A scalar `3.5` is not enough for safety settings; use structures such as `{"value": 3.5, "unit": "kW"}` in tests.
- Monotonic `config_version` is per activation. Multiple changed fields in one activation share the same version.
- If validation fails for any change in an activation, write zero rows.

### Suggested Repository Shape

```python
SAFETY_RELEVANT_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "peak_consumption_limit",
        "battery_reserve_floor",
        "ev_charging_window_preference",
        "energy_strategy_default",
        "device_role_assignment",
        "capability_override",
        "capability_acceptance",
    }
)
```

Keep imports one-way: `storage/repositories` may import `open_ems.storage.database`, but must not import `services.audit_log`. This matches the Story 6.2 lesson that repository-level constants belong with the repository when the repository enforces the policy.

### Previous Story Intelligence

Story 6.1 established:
- `VALID_EVENT_TYPES` already includes `INSTALLER`; do not add a new event type.
- `FakeAuditLog` validates the same actor/event type/summary rules as the real service.
- `EventLogRepo.append()` serializes detail with `allow_nan=False` and commits after mutations.

Story 6.2 established:
- `event_log` append-only enforcement is already in place through `EventLogRepo.update()` and `EventLogRepo.delete()`.
- Repository tests use in-memory SQLite DDL and `conn.row_factory = aiosqlite.Row`.
- Timestamp comparisons assume UTC ISO text. Continue storing UTC ISO text for new audit rows.
- Deferred debt exists around direct `EventLogRepo.append()` coverage and JSON serialization diagnostics. Do not expand this story into unrelated cleanup unless needed for new tests.

Recent git history:
- `98cbc7a 6.2` added retention pruning and event log repository tests.
- `03ff345 5.3`, `b17b6f1 5.2`, and `b25305b 5.1` completed StateStore/SSE/HTMX state distribution. No config repository exists yet, so this story should create the config audit repository foundation without pretending a full config activation pipeline already exists.

### Architecture References

- `_bmad-output/planning-artifacts/epics.md` - Epic 6 requires dual logging, append-only guarantees, plain-language summaries, actor attribution, retention, and `config_audit_log`.
- `_bmad-output/planning-artifacts/epics.md` - Story 6.3 defines installer note validation, `event_type=INSTALLER`, `config_audit_log`, structured before/after JSON, and config version increment tests.
- `_bmad-output/planning-artifacts/epics.md` - Epic 6 cross-story constraints require `ObservabilityService.audit()` as the sole event-log entry point.
- `_bmad-output/planning-artifacts/architecture.md` - Decision 5.7 requires configuration versioning and audit for constraint and device configuration changes.
- `_bmad-output/planning-artifacts/architecture.md` - Project structure places `config_audit_repo.py` and `event_log_repo.py` under `src/open_ems/storage/repositories/`.
- `_bmad-output/planning-artifacts/prd.md` - Journey 2 requires installers to add event-log notes documenting reduced capability decisions.

### Latest Technical Information

No external API or new third-party library is required for this story. Use the current project stack from `pyproject.toml`: Python `>=3.12`, `aiosqlite`, `alembic`, `structlog`, `fastapi`, strict `mypy`, `ruff`, and `pytest`. Prefer stdlib `html.escape` for note summary escaping to avoid expanding the runtime dependency surface.

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- Baseline: `uv run python -m pytest tests/ --no-cov -q` - 578 passed, 43 warnings.
- Baseline: `uv run python -m ruff check .` - passed.
- Baseline: `uv run python -m mypy src/` - passed.
- Red phase: focused new tests failed on missing `MAX_INSTALLER_NOTE_LENGTH` and missing `config_audit_repo`.
- Focused validation: `uv run python -m pytest tests/unit/services/test_audit_log.py tests/unit/storage/repositories/test_config_audit_repo.py tests/integration/test_migrations.py --no-cov -q` - 50 passed.
- Focused validation: `uv run python -m ruff check ...` - passed after import ordering fix.
- Focused validation: `uv run python -m mypy src/` - passed.
- Final: `uv run python -m ruff check .` - passed.
- Final: `uv run python -m ruff format --check .` - passed.
- Final: `uv run python -m mypy src/` - passed.
- Final: `uv run python -m pytest tests/ --no-cov -q` - 596 passed, 43 warnings.

### Completion Notes List

- Added `ObservabilityService.installer_note()` with 2,000-character validation, whitespace rejection, stdlib HTML escaping, and delegation through `ObservabilityService.audit()`.
- Added Alembic migration `0006` for `config_audit_log` with timestamp, config version, and field indexes.
- Added `ConfigAuditRepo`, `ConfigAuditChange`, `SAFETY_RELEVANT_CONFIG_FIELDS`, actor validation, JSON value validation, one-version-per-activation semantics, and atomic pre-write validation.
- Added unit coverage for installer notes and config audit writes, plus migration schema coverage for `config_audit_log`.

### File List

- `_bmad-output/implementation-artifacts/6-3-implement-installer-note-storage-and-config-audit-log.md`
- `_bmad-output/implementation-artifacts/sprint-status.yaml`
- `migrations/versions/0006_add_config_audit_log_table.py`
- `src/open_ems/services/audit_log.py`
- `src/open_ems/storage/repositories/config_audit_repo.py`
- `tests/integration/test_migrations.py`
- `tests/unit/services/test_audit_log.py`
- `tests/unit/storage/repositories/test_config_audit_repo.py`

### Change Log

- 2026-05-04: Story created and marked ready for dev.
- 2026-05-04: Implemented installer note storage, config audit schema/repository, and focused validation coverage; story marked ready for review.
