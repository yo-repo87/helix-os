#!/bin/bash
# checkmk/setup.sh — One-time Checkmk Community Edition setup.
# Run as root: sudo bash setup.sh
# After running, Checkmk UI is at http://<host>:5000/cmk/

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRET_FILE="/etc/helix/checkmk_secret"
CMK_PORT="${CMK_PORT:-5050}"
CMK_IMAGE="checkmk/check-mk-community:2.5.0-latest"
CMK_API_PATH="cmk/check_mk/api/1.0"

echo "==> Starting Checkmk container..."
if docker ps --format '{{.Names}}' | grep -q '^checkmk$'; then
    echo "    Already running."
else
    docker rm -f checkmk 2>/dev/null || true
    docker run -d \
        --name checkmk \
        --restart unless-stopped \
        -p "${CMK_PORT}:5000" \
        -p 6557:6557 \
        -v checkmk-data:/omd/sites \
        "$CMK_IMAGE"
    echo "    Container started."
fi

echo ""
echo "==> Waiting for Checkmk to initialize (up to 90s)..."
CMK_URL="http://localhost:${CMK_PORT}/${CMK_API_PATH}"
for i in $(seq 1 90); do
    if curl -sf "${CMK_URL}/version" \
            -u "cmkadmin:$(docker logs checkmk 2>&1 | grep -oP '(?<=cmkadmin: )\S+' | tail -1)" \
            -o /dev/null 2>/dev/null; then
        echo "    Ready after ${i}s."
        break
    fi
    sleep 1
done

echo ""
echo "==> Getting admin password..."
ADMIN_PASS=$(docker logs checkmk 2>&1 | grep -oP '(?<=cmkadmin: )\S+' | tail -1)
if [ -z "$ADMIN_PASS" ]; then
    ADMIN_PASS="z19nFV27A7XQ"
    echo "    Using default password: $ADMIN_PASS"
fi
echo "    Admin pass: $ADMIN_PASS"

echo ""
echo "==> Installing checkmk-agent on host..."
if ! which check_mk_agent cmk-agent-ctl >/dev/null 2>&1; then
    # Copy agent deb from the running container (most reliable method)
    docker cp checkmk:/omd/versions/2.5.0p18.community/share/check_mk/agents/check-mk-agent_2.5.0p18-1_all.deb \
        /tmp/cmk-agent.deb 2>/dev/null || {
        echo "    Could not copy agent from container"
    }
    [ -f /tmp/cmk-agent.deb ] && dpkg -i /tmp/cmk-agent.deb && echo "    Agent installed." || true
else
    echo "    Agent already installed."
fi

echo ""
echo "==> Installing mk_docker.py plugin..."
PLUGIN_DIR="/usr/lib/check_mk_agent/plugins"
mkdir -p "$PLUGIN_DIR"
cp "$SCRIPT_DIR/mk_docker.py" "$PLUGIN_DIR/"
chmod +x "$PLUGIN_DIR/mk_docker.py"
echo "    Installed: $PLUGIN_DIR/mk_docker.py"

echo ""
echo "==> Creating automation user in Checkmk..."
SECRET="helixauto123"
curl -sf -X POST "${CMK_URL}/domain-types/user_config/collections/all" \
    -u "cmkadmin:${ADMIN_PASS}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d "{\"username\":\"automation\",\"fullname\":\"Helix Automation\",\"auth_option\":{\"auth_type\":\"automation\",\"secret\":\"${SECRET}\"},\"roles\":[\"admin\"]}" \
    -o /dev/null && echo "    Automation user created." || echo "    User may already exist."

mkdir -p "$(dirname $SECRET_FILE)"
echo "automation" > "${SECRET_FILE%_secret}_user" 2>/dev/null || true
echo "$SECRET" > "$SECRET_FILE"
chmod 600 "$SECRET_FILE" 2>/dev/null || true
echo "    Credentials saved."

echo ""
echo "==> Registering host machine in Checkmk..."
curl -sf -X POST "${CMK_URL}/domain-types/host_config/collections/all" \
    -u "cmkadmin:${ADMIN_PASS}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d "{\"host_name\":\"$(hostname)\",\"folder\":\"/\"}" \
    -o /dev/null && echo "    Host registered." || echo "    Host may already exist."

echo ""
echo "==> Running service discovery and activating..."
docker exec checkmk bash -c "su - cmk -c 'cmk -I $(hostname) 2>/dev/null; cmk -O 2>/dev/null'" || true
echo "    Discovery and activation done."

echo ""
echo "==> Installing docker event bridge as systemd service..."
cp "$SCRIPT_DIR/cmk-docker-bridge.py" /usr/local/sbin/
chmod +x /usr/local/sbin/cmk-docker-bridge.py
cp "$SCRIPT_DIR/cmk-docker-bridge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cmk-docker-bridge
echo "    Service: cmk-docker-bridge ($(systemctl is-active cmk-docker-bridge))"

echo ""
echo "════════════════════════════════════════════════"
echo " Checkmk setup complete!"
echo "  Web UI:  http://$(hostname -I | awk '{print $1}'):${CMK_PORT}/cmk/"
echo "  Login:   automation / <secret in $SECRET_FILE>"
echo "  Livestatus TCP: port 6557"
echo "════════════════════════════════════════════════"
