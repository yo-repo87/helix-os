#!/bin/bash
# Hook 03: Install Helix OS web backend into the rootfs.
# Runs inside the chroot after package install.
set -euo pipefail

echo "==> [hook 03] Installing Helix OS backend"

HIVE_DIR="/opt/helix"
VENV_DIR="$HIVE_DIR/venv"
BACKEND_SRC="/build/webui/backend"
FRONTEND_SRC="/build/webui/frontend"

# ── Skip if the web UI source isn't present in the build dir ─────────────────
if [ ! -d "$BACKEND_SRC" ]; then
    echo "  SKIP: $BACKEND_SRC not found in chroot (copy webui/ into build/ before building)"
    exit 0
fi

# ── Install Python 3 venv if missing ─────────────────────────────────────────
python3 -m ensurepip --upgrade 2>/dev/null || true
apt-get install -y --no-install-recommends python3-venv python3-dev gcc 2>/dev/null || true

# ── Create venv + install deps ───────────────────────────────────────────────
mkdir -p "$HIVE_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet || echo "  WARN: pip self-upgrade failed (continuing)"
# Non-fatal: a flaky package download must not abort the whole image build.
# Prefer prebuilt wheels so no compiler/Rust toolchain is needed in the chroot.
if ! "$VENV_DIR/bin/pip" install -r "$BACKEND_SRC/requirements.txt" --quiet; then
    echo "  WARN: backend dependency install failed — image will build, but the"
    echo "        helix-backend service may not start until deps are installed."
fi

# ── Copy app files ────────────────────────────────────────────────────────────
cp -r "$BACKEND_SRC"  "$HIVE_DIR/backend"
[ -d "$FRONTEND_SRC" ] && cp -r "$FRONTEND_SRC" "$HIVE_DIR/frontend"

chown -R helix:helix "$HIVE_DIR"

# ── Nginx config: reverse proxy to FastAPI backend ───────────────────────────
cat > /etc/nginx/sites-available/helix <<'NGINXEOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    # SSE / streaming: disable buffering
    proxy_buffering off;
    proxy_cache off;

    location / {
        proxy_pass         http://127.0.0.1:8082;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINXEOF

# Remove default site, enable ours
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/helix /etc/nginx/sites-enabled/helix

# ── systemd service ──────────────────────────────────────────────────────────
cat > /etc/systemd/system/helix-backend.service <<'SVCEOF'
[Unit]
Description=Helix OS Web Backend (FastAPI)
After=network.target
Wants=network.target

[Service]
Type=simple
User=helix
Group=helix
WorkingDirectory=/opt/helix/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/helix/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8082 --workers 1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl enable nginx
systemctl enable helix-backend

# ── State directory ───────────────────────────────────────────────────────────
mkdir -p /var/lib/helix
chown helix:helix /var/lib/helix

echo "==> [hook 03] Helix OS backend installed at $HIVE_DIR"
