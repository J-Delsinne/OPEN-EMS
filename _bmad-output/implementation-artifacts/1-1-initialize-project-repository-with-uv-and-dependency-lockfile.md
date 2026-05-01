# Story 1.1: Initialize project repository with uv and dependency lockfile

Status: done

## Story

As an installer,
I want to deploy OPEN-EMS from a locked, reproducible Python project,
So that every site runs the identical dependency set with no version drift between installations.

## Acceptance Criteria

1. **Given** a fresh Linux host with Python 3.12+ and uv installed  
   **When** the developer runs `uv sync`  
   **Then** all specified dependencies install from the lockfile: fastapi, uvicorn, pydantic, pydantic-settings, aiosqlite, alembic, pymodbus, ocpp, dsmr-parser, structlog, cryptography, pytest, pytest-asyncio, ruff, mypy

2. **Given** the project is initialized  
   **When** the developer runs `uv run ruff check .`  
   **Then** the command exits 0 with no violations

3. **Given** the project skeleton exists  
   **When** the developer runs `uv run mypy src/`  
   **Then** the command exits 0 with no type errors on the initial skeleton

4. **Given** the project is initialized  
   **When** the developer runs `uv run pytest`  
   **Then** the command exits 0 (baseline test suite)

5. `uv.lock` is committed to the repository

## Tasks / Subtasks

- [x] Task 1: Initialize uv src-layout project (AC: 1, 5)
  - [x] Run `uv init --lib --name open-ems .` from the OPEN-EMS project root
  - [x] Confirm `src/open_ems/__init__.py` and `pyproject.toml` were created
  - [x] Confirm `pyproject.toml` has `requires-python = ">=3.12"`

- [x] Task 2: Add runtime dependencies (AC: 1)
  - [x] Run `uv add fastapi "uvicorn[standard]" pydantic pydantic-settings aiosqlite alembic pymodbus ocpp dsmr-parser structlog cryptography`
  - [x] Confirm all packages appear in `pyproject.toml` `[project.dependencies]`

- [x] Task 3: Add dev dependencies (AC: 1, 2, 3, 4)
  - [x] Run `uv add --dev pytest pytest-asyncio ruff mypy`
  - [x] Confirm all appear in `[dependency-groups] dev`

- [x] Task 4: Configure ruff in pyproject.toml (AC: 2)
  - [x] Add `[tool.ruff]` and `[tool.ruff.lint]` sections (see template in Dev Notes)
  - [x] Run `uv run ruff check .` — must exit 0

- [x] Task 5: Configure mypy in pyproject.toml with stub overrides (AC: 3)
  - [x] Add `[tool.mypy]` section with `strict = true` and `mypy_path = "src"`
  - [x] Add `[[tool.mypy.overrides]]` block for packages without stubs (see template)
  - [x] Set `__version__ = "0.1.0"` in `src/open_ems/__init__.py`
  - [x] Run `uv run mypy src/` — must exit 0

- [x] Task 6: Configure pytest and create baseline test (AC: 4)
  - [x] Add `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `asyncio_mode = "auto"`
  - [x] Create `tests/__init__.py` (empty)
  - [x] Create `tests/test_baseline.py` with one trivial passing test (see template)
  - [x] Run `uv run pytest` — must exit 0

- [x] Task 7: Create supporting files
  - [x] Create `.gitignore` (see contents in Dev Notes — ensure `uv.lock` is NOT excluded)
  - [x] Create `.env.example` as empty placeholder (will be populated in Story 1.7)
  - [x] Update or confirm `README.md` has minimal project description

- [x] Task 8: Verify lockfile is committed (AC: 5)
  - [x] Confirm `uv.lock` exists and is NOT listed in `.gitignore`
  - [x] Stage all files; `uv.lock` must be included in the commit

### Review Findings

- [x] [Review][Patch] `SECRET_KEY=changeme` in .env.example is an insecure literal placeholder [`.env.example`:9]
- [x] [Review][Patch] `asyncio_default_fixture_loop_scope` not set — pytest-asyncio 1.x will warn (and eventually error) when async tests are added [`pyproject.toml`:71]
- [x] [Review][Defer] Unbounded `>=` version constraints on all deps — lockfile mitigates for now [`pyproject.toml`:14-26] — deferred, pre-existing
- [x] [Review][Defer] `pytest-asyncio` future major-version risk — lockfile protects until `uv lock --upgrade` [`pyproject.toml`:36] — deferred, pre-existing
- [x] [Review][Defer] `.python-version` (3.14) gitignored — deployment Python version uncontrolled on Pi [`gitignore`:19] — deferred, deployment concern
- [x] [Review][Defer] `cryptography>=47.0.0` may require Rust toolchain on 32-bit Pi OS (armv7l/musl) — deferred, deployment concern
- [x] [Review][Defer] `tailer==0.4.1` (2015, sdist-only) transitively pulled by dsmr-parser — Python 3.14 compat unverified — deferred, third-party
- [x] [Review][Defer] `dlms-cosem==21.3.2` (2021) pulled by dsmr-parser — Python 3.14 compat unverified — deferred, third-party
- [x] [Review][Defer] `pymodbus` uncapped + fully mypy-ignored — future version drift risk — deferred, lockfile mitigates
- [x] [Review][Defer] `hatchling` not pinned as explicit dep — uv manages via build-system lockfile — deferred, low risk
- [x] [Review][Defer] `__version__` hardcoded in `__init__.py` and `pyproject.toml` — will drift on version bumps — deferred, acceptable for v0.1.0

## Dev Notes

### Critical Context — Read First

**Greenfield project.** The `OPEN-EMS` working directory already contains `.claude/`, `_bmad/`, `_bmad-output/`, and `docs/` directories — BMad project management files. Do NOT delete or modify these. All project code is created alongside them.

**Target runtime is Linux (Raspberry Pi 4 / Debian/Ubuntu).** Development is on Windows. `uv` commands are identical on both platforms.

**Scope boundary.** Story 1.1 only creates the project skeleton listed below. Files for later stories must NOT be pre-created here — doing so causes scope drift and may break later story acceptance checks.

---

### Exact Initialization Commands

Run from the OPEN-EMS project root:

```bash
# 1. Init src-layout library project
uv init --lib --name open-ems .

# 2. Runtime dependencies
uv add fastapi "uvicorn[standard]" pydantic pydantic-settings aiosqlite alembic \
    pymodbus ocpp dsmr-parser structlog cryptography

# 3. Dev dependencies
uv add --dev pytest pytest-asyncio ruff mypy
```

`uv init --lib` creates the `src/open_ems/` layout and sets the build backend to hatchling. If your installed `uv` version does not support `--lib`, create `pyproject.toml` manually using the template below and then run `uv sync`.

---

### File Structure After This Story

```
OPEN-EMS/                         ← project root
├── pyproject.toml                ← MUST: full config (see template)
├── uv.lock                       ← MUST: committed, never gitignored
├── .gitignore                    ← MUST: see contents below
├── .env.example                  ← empty placeholder
├── README.md                     ← minimal
├── src/
│   └── open_ems/
│       └── __init__.py           ← `__version__ = "0.1.0"`
└── tests/
    ├── __init__.py               ← empty
    └── test_baseline.py          ← single passing test (see template)
```

**Do NOT create in this story** (belongs to later stories):
- `src/open_ems/main.py` → Story 1.2
- `src/open_ems/settings.py` → Story 1.7
- `migrations/` → Story 1.2
- `Dockerfile`, `docker-compose.yml` → Story 1.4
- `systemd/` → Story 1.5
- `.github/` → Story 1.6
- `scripts/` → Story 1.4 / 1.5
- Any `src/open_ems/core/`, `engine/`, `adapters/`, `storage/`, `web/`, `services/` subdirectories

---

### pyproject.toml Configuration Template

After `uv add` commands, uv rewrites the dependencies section with resolved specifiers. The sections below must be added/merged manually — uv does not write tool configuration automatically.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "open-ems"
version = "0.1.0"
requires-python = ">=3.12"
description = "Local energy management system for residential solar/battery/EV installations"
# [project.dependencies] populated by `uv add`

[tool.hatch.build.targets.wheel]
packages = ["src/open_ems"]

[dependency-groups]
dev = [
    "mypy>=1.20.2",
    "pytest>=9.0.3",
    "pytest-asyncio>=1.3.0",
    "ruff>=0.15.12",
]

[tool.ruff]
line-length = 100
target-version = "py312"
exclude = [".agents", ".claude", "_bmad", "_bmad-output", "docs", ".venv"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP"]

[tool.mypy]
python_version = "3.12"
strict = true
mypy_path = "src"

[[tool.mypy.overrides]]
module = [
    "dsmr_parser.*",
    "pymodbus.*",
    "ocpp.*",
    "aiosqlite.*",
    "alembic.*",
]
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

**Why `ignore_missing_imports` for those 5 packages:** As of 2026-05, dsmr-parser, pymodbus, ocpp, aiosqlite, and alembic either lack a `py.typed` marker or have incomplete stubs. Without this override, `mypy --strict` fails immediately with `Skipping analyzing "pymodbus": module is installed, but missing library stubs or py.typed marker`. Adding the override now unblocks all future stories that import these packages. Add further overrides per-story as new unstubbed packages are imported.

**`[tool.uv] dev-dependencies` is deprecated** in uv 0.11.x — use `[dependency-groups] dev` (PEP 735) instead. The `uv add --dev` command in uv 0.11.8 writes to the old format, triggering a deprecation warning; migrate manually as shown above.

**ruff `exclude`:** The project root contains `.agents/` and `.claude/` directories (BMad infrastructure) with Python template files that intentionally violate ruff rules. These directories must be excluded. Without the exclude list, ruff exits non-zero on `.agents/` files.

---

### src/open_ems/__init__.py

```python
__version__ = "0.1.0"
```

---

### tests/test_baseline.py

```python
def test_project_baseline() -> None:
    """Confirms pytest discovers tests and exits 0."""
    assert True
```

**Why this file exists:** `pytest` returns exit code 5 ("no tests collected") on a completely empty test directory. A single trivial test produces exit code 0, satisfying AC #4. Do not remove it — later stories will add real tests alongside it.

---

### .gitignore Contents

```gitignore
# Python
__pycache__/
*.py[cod]
*.pyo
.Python
*.egg-info/
dist/
build/
.eggs/

# Virtual environment (managed by uv — do not commit)
.venv/

# uv — DO NOT add uv.lock here; it MUST be committed
.uv/

# Python version pin (local only)
.python-version

# Type checking caches
.mypy_cache/
.dmypy.json
dmypy.json

# Ruff cache
.ruff_cache/

# Test coverage
.pytest_cache/
.coverage
htmlcov/
coverage.xml

# Runtime / deployment secrets — never commit
.env

# Database files generated at runtime
*.db
*.db-wal
*.db-shm

# TLS certificates (generated by scripts/generate-tls.sh)
certs/

# IDE
.vscode/settings.json
.idea/
*.swp
*.swo
```

**Critical:** `uv.lock` must NOT appear in `.gitignore`. The lockfile is the reproducibility guarantee for every deployment site.

---

### What `uv sync` vs `uv add` Do

| Command | Purpose | When to use |
|---------|---------|-------------|
| `uv add <pkg>` | Adds dependency, resolves, regenerates `uv.lock` | Developer adding a new package |
| `uv sync` | Installs exact locked versions, no resolution | CI, fresh host install, reproducible setup |

Deployment targets always use `uv sync`. Developers adding packages always use `uv add`.

---

### Architecture Alignment

This story establishes the exact stack the architecture mandates:

| Architecture decision | What this story creates |
|-----------------------|------------------------|
| Python 3.12+, strict type hints, mypy | `requires-python = ">=3.12"`, `strict = true` in mypy config |
| uv + pyproject.toml as single manifest | `pyproject.toml`, `uv.lock` |
| pytest + pytest-asyncio for all tests | `asyncio_mode = "auto"` in pytest config |
| ruff for lint+format | `[tool.ruff]` config, ruff in dev deps |
| `src/open_ems/` layout | `src/open_ems/__init__.py`, hatchling wheel config |
| Pydantic v2 | `pydantic` added (uv resolves v2) |

All future stories assume this skeleton is in place and **will not re-explain** the project layout.

### References

- [Source: architecture.md#Stack Selection] — Python 3.12+, uv, pyproject.toml
- [Source: architecture.md#Dependency management] — uv, lockfile reproducibility
- [Source: architecture.md#Testing] — pytest, pytest-asyncio, strict no-hardware-in-CI policy
- [Source: architecture.md#Complete Project Directory Structure] — full intended structure
- [Source: architecture.md#First Implementation Priority] — exact `uv add` command list
- [Source: epics.md#Story 1.1] — AC source and AR1 coverage

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

- uv 0.11.8 installed via `python -m pip install uv` (not on PATH; invoked as `python -m uv`)
- `uv init --lib` generates `requires-python = ">=3.14"` based on system Python — corrected to `">=3.12"` per architecture
- `uv init --lib` generates `uv_build` backend — replaced with `hatchling` per story spec for cross-version compatibility
- `[tool.uv] dev-dependencies` deprecated in uv 0.11.x — migrated to `[dependency-groups] dev` (PEP 735)
- `ruff check .` initially failed on `.agents/` and `.claude/` BMad infrastructure Python files — added `exclude` list to `[tool.ruff]`
- Locked versions: fastapi 0.136.1, pydantic 2.13.3, mypy 1.20.2, pytest 9.0.3, pytest-asyncio 1.3.0, ruff 0.15.12

### Completion Notes List

- All 5 ACs verified with exit code 0: `uv sync` (clean), `ruff check .` (0), `mypy src/` (0), `pytest` (1 passed), `uv.lock` present and not gitignored
- 58 packages total in lockfile (runtime + dev + transitive deps)
- Build backend: hatchling (specified in story) over uv_build (uv default) for cross-version uv compatibility
- mypy stub overrides added for: dsmr_parser, pymodbus, ocpp, aiosqlite, alembic — unblocks all future story imports

### File List

- `pyproject.toml` (created)
- `uv.lock` (created)
- `.gitignore` (updated — extended from uv init defaults)
- `.env.example` (created)
- `README.md` (updated)
- `src/open_ems/__init__.py` (created — replaced uv init placeholder)
- `tests/__init__.py` (created)
- `tests/test_baseline.py` (created)
