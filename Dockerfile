FROM python:3.12-slim

WORKDIR /app

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

# Install dependencies first (cached layer — rebuilt only when lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install project source (hatchling requires README.md from pyproject.toml readme field)
COPY README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev

# Copy runtime assets
COPY migrations/ migrations/
COPY alembic.ini .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8443

CMD ["python", "-m", "open_ems.main"]
