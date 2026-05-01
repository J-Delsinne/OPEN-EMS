# OPEN-EMS Installation Guide

Two deployment paths are supported. Both are production-ready for Raspberry Pi 4 and x86-64 Linux.

## Prerequisites

| Requirement | Docker path | Native systemd path |
|---|---|---|
| OS | Raspberry Pi OS (Bookworm 64-bit) or Ubuntu 22.04+ | Same |
| Docker + Compose | Required | Not needed |
| Python 3.12+ | Not needed (inside container) | Required |
| uv | Not needed | Required (installed by script) |
| curl | For health check | For uv install + health check |
| rsync | Not needed | Required |

---

## Path A — Docker Compose

### 1. Clone and configure

```bash
git clone https://github.com/jdelsinne/open-ems.git
cd open-ems
cp .env.example .env
# Edit .env — set LOG_LEVEL, DB_PATH, etc.
```

### 2. Generate TLS certificate

```bash
bash scripts/generate-tls.sh
```

This creates `certs/cert.pem` and `certs/key.pem` with CN and SAN entries for the host's
hostname and LAN IP. No internet access required.

### 3. Start the service

```bash
docker compose up -d
```

### 4. Verify

```bash
docker compose ps          # should show "healthy"
docker compose logs -f     # structured JSON log output
curl -k https://localhost:8443/health/ready   # should return {"status":"ready"}
```

To stop: `docker compose down`

---

## Path B — Native systemd

### 1. Clone the project

```bash
git clone https://github.com/jdelsinne/open-ems.git
cd open-ems
```

### 2. Run the installer

```bash
sudo bash scripts/install.sh
```

The script performs the following steps (idempotent — safe to re-run):

1. Checks for `uv`; installs it if missing
2. Creates the `open-ems` system user
3. Copies the project to `/opt/open-ems`
4. Installs Python dependencies into `/opt/open-ems/.venv`
5. Scaffolds `/etc/open-ems/open-ems.env` from `.env.example` (first run only)
6. Generates a TLS certificate at `/opt/open-ems/certs/` (first run only)
7. Installs `/etc/systemd/system/open-ems.service`
8. Enables and starts the service

### 3. Edit the environment file

```bash
sudo nano /etc/open-ems/open-ems.env
```

Key variables to review:

| Variable | Default | Notes |
|---|---|---|
| `DB_PATH` | `open_ems.db` | Set to an absolute path, e.g. `/opt/open-ems/data/open_ems.db` |
| `LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |
| `PORT` | `8443` | HTTPS port |
| `TLS_CERT_PATH` | `certs/cert.pem` | Relative to `WorkingDirectory=/opt/open-ems` |
| `TLS_KEY_PATH` | `certs/key.pem` | Same |

After editing, restart the service:

```bash
sudo systemctl restart open-ems
```

### 4. Verify

```bash
systemctl status open-ems               # should show "active (running)"
journalctl -u open-ems -n 50            # last 50 log lines (structured JSON)
journalctl -u open-ems -f               # follow live
curl -k https://localhost:8443/health/ready   # should return {"status":"ready"}
```

### Updating the installation

To deploy a new version:

```bash
git pull
sudo bash scripts/install.sh   # re-runs safely; restarts service
```

---

## Watchdog behaviour (native systemd only)

The systemd unit file configures `WatchdogSec=30s`. When running under systemd, the
application sends `WATCHDOG=1` heartbeats every 15 seconds. If the process stalls and
misses two consecutive heartbeats, systemd kills and automatically restarts it.

This is visible in the journal:

```bash
journalctl -u open-ems -f
# normal:  {"event":"watchdog_started","interval_seconds":15.0,...}
# stall:   {"event":"watchdog_missed","expected_interval":15.0,...}
```

---

## Browser trust warning

OPEN-EMS uses a self-signed certificate. Browsers will show a trust warning on first
access. Accept it once to proceed:

- **Chrome / Edge:** Click "Advanced" → "Proceed to \<hostname\> (unsafe)"
- **Firefox:** Click "Advanced…" → "Accept the Risk and Continue"

The certificate is valid for 365 days and covers the host's hostname, `localhost`,
its LAN IP, and `127.0.0.1`. Reinstall the certificate any time by re-running
`scripts/generate-tls.sh` (Docker) or `sudo bash scripts/install.sh` (native).

---

## Uninstall (native systemd)

```bash
sudo systemctl disable --now open-ems
sudo rm /etc/systemd/system/open-ems.service
sudo systemctl daemon-reload
sudo userdel open-ems
sudo rm -rf /opt/open-ems /etc/open-ems
```
