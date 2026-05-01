#!/usr/bin/env bash
# install.sh — Fresh-host installer for OPEN-EMS (native systemd deployment)
# Run as root from the project root: sudo bash scripts/install.sh
set -euo pipefail

INSTALL_DIR="/opt/open-ems"
SERVICE_USER="open-ems"
ENV_DIR="/etc/open-ems"
UNIT_DST="/etc/systemd/system/open-ems.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── 1. Verify running as root ─────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo bash scripts/install.sh)" >&2
    exit 1
fi

echo "==> Installing OPEN-EMS from $PROJECT_ROOT"

# ── 2. Verify uv is available ─────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "==> Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
# Resolve absolute path now so later steps don't depend on PATH propagation
UV_BIN="$(command -v uv)"
echo "==> uv: $($UV_BIN --version)"

# ── 3. Create service user (idempotent) ───────────────────────────────────
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "==> Created system user: $SERVICE_USER"
else
    echo "==> System user already exists: $SERVICE_USER"
fi

# ── 4. Deploy project files ───────────────────────────────────────────────
echo "==> Deploying project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='.env' \
    --exclude='certs/' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='.git/' \
    --exclude='.mypy_cache/' \
    --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='_bmad/' \
    --exclude='_bmad-output/' \
    "$PROJECT_ROOT/" "$INSTALL_DIR/"

# ── 5. Install Python dependencies ────────────────────────────────────────
echo "==> Installing Python dependencies..."
cd "$INSTALL_DIR"
"$UV_BIN" sync --frozen --no-dev

# ── 6. Create env config scaffold (skip if already exists) ────────────────
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_DIR/open-ems.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$ENV_DIR/open-ems.env"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │  IMPORTANT: Edit $ENV_DIR/open-ems.env before starting.  │"
    echo "  │  Set DB_PATH, LOG_LEVEL, and any other required values.     │"
    echo "  └─────────────────────────────────────────────────────────────┘"
    echo ""
else
    echo "==> Env config already exists: $ENV_DIR/open-ems.env (not overwritten)"
fi

# ── 7. Generate TLS certificate (skip if already exists) ──────────────────
if [[ ! -f "$INSTALL_DIR/certs/cert.pem" ]]; then
    echo "==> Generating TLS certificate..."
    cd "$INSTALL_DIR"
    "$UV_BIN" run python scripts/generate_tls.py
else
    echo "==> TLS certificate already exists (not regenerated)"
fi

# ── 8. Install systemd unit ───────────────────────────────────────────────
echo "==> Installing systemd unit: $UNIT_DST"
# Track whether the unit file actually changed so we can skip unnecessary restarts
_unit_changed=false
if [[ ! -f "$UNIT_DST" ]] || ! cmp -s "$INSTALL_DIR/systemd/open-ems.service" "$UNIT_DST"; then
    _unit_changed=true
fi
cp "$INSTALL_DIR/systemd/open-ems.service" "$UNIT_DST"
systemctl daemon-reload

# ── 9. Set ownership ──────────────────────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$ENV_DIR"

# ── 10. Enable and (re)start only when needed ─────────────────────────────
echo "==> Enabling open-ems..."
systemctl enable open-ems

if ! systemctl is-active --quiet open-ems; then
    echo "==> Starting open-ems (service not running)..."
    systemctl start open-ems
elif [[ "$_unit_changed" == true ]]; then
    echo "==> Restarting open-ems (unit file changed)..."
    systemctl restart open-ems
else
    echo "==> Service already running with unchanged unit file; skipping restart"
fi

echo ""
echo "✅  OPEN-EMS installed."
echo "    Check status : systemctl status open-ems"
echo "    View logs    : journalctl -u open-ems -f"
echo "    Health check : curl -k https://localhost:8443/health/ready"
