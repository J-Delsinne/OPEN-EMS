# OPEN-EMS

Local energy management system for residential solar/battery/EV installations.

Runs on Raspberry Pi 4 (or equivalent Linux host). No cloud dependency for core operation.

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Install

```bash
uv sync
```

### Lint & type-check

```bash
uv run ruff check .
uv run mypy src/
```

### Test

```bash
uv run pytest
```

### Pre-commit hooks (local dev)

```bash
uv run pre-commit install
```

Runs ruff, mypy, and a story review-findings check before each commit.

## Deployment

See `docs/installation.md` for Docker Compose and systemd deployment paths.
