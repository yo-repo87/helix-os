#!/bin/bash
# Install Helix OS web UI dependencies and set up the systemd service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"
VENV="$SCRIPT_DIR/.venv"

echo "==> Creating Python virtual environment..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$BACKEND/requirements.txt"
echo "    venv: $VENV"

echo "==> Writing systemd service..."
cat > /etc/systemd/system/helix-webui.service <<EOF
[Unit]
Description=Helix OS Web UI (FastAPI)
After=network.target helix-array.service
Wants=helix-array.service

[Service]
Type=simple
WorkingDirectory=$BACKEND
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port 8082
WorkingDirectory=$BACKEND
Restart=always
RestartSec=5
Environment=HELIX_CONFIG=/boot/config
Environment=ARRAY_SBIN=/usr/local/sbin
Environment=STATE_DIR=/var/local/helix
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable helix-webui
systemctl restart helix-webui

echo ""
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8000"
