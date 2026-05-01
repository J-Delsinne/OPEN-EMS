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
