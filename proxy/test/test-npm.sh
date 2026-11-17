#!/bin/bash
# test-npm.sh: end-to-end test of the native Nginx Proxy Manager setup (CLAUDE.md §54).
# Needs Docker on the host; everything happens in a throwaway privileged debian:bookworm container
# (privileged for the loop device + bind mounts). Nothing on the host is touched.
#
#   proxy/test/test-npm.sh [--arch amd64|arm64]     (arm64 needs qemu binfmt; slow)
#
# Steps inside the container: OpenResty from its repo; the web-server, Helix-site and cert sections
# of build/hooks/03-helix-install.sh exactly as written (this builds NPM); an ext4 loop image as
# HELIXDATA; helix-npm-storage + helix-npm-setup; nginx and NPM's backend started the way their
# units start them; then NPM's API: sign in with the generated admin, create a proxy host, and
# check nginx routes it while the server's own address still reaches the Helix UI.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PLATFORM=linux/amd64
[ "${1:-}" = --arch ] && PLATFORM=linux/${2:?}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
H="$REPO/build/hooks/03-helix-install.sh"
{ echo 'set -e'
  awk '/^# ── Web server: OpenResty/{p=1} /^# ── Tailscale/{p=0} p' "$H"
  awk '/^# ── Self-signed TLS cert/{p=1} /^# ── systemd service/{p=0} p' "$H"
} > "$WORK/hook-section.sh"
# Unit lines, read from the hook, so the test starts NPM exactly like helix-npm.service.
awk '/^cat > \/etc\/systemd\/system\/helix-npm.service/{p=1;next} /^NPMSVC/{p=0} p' "$H" > "$WORK/helix-npm.service"

cat > "$WORK/run.sh" <<'EOF'
set -u
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends ca-certificates curl openssl iproute2 procps python3 python3-venv e2fsprogs >/dev/null
curl -fsSL https://openresty.org/package/pubkey.gpg -o /usr/share/keyrings/openresty.asc
case "$(dpkg --print-architecture)" in arm64) R=https://openresty.org/package/arm64/debian ;; *) R=https://openresty.org/package/debian ;; esac
echo "deb [signed-by=/usr/share/keyrings/openresty.asc] $R bookworm openresty" > /etc/apt/sources.list.d/openresty.list
apt-get update -qq >/dev/null; apt-get install -y -qq --no-install-recommends openresty >/dev/null 2>&1
groupadd helix
mkdir -p /build && cp -r /repo/proxy /build/proxy
echo "== hook sections (builds NPM)"
start=$(date +%s)
if ! bash /t/hook-section.sh > /t/hook.log 2>&1; then tail -30 /t/hook.log; echo "== hook failed"; exit 1; fi
grep "\[npm\]\|web server" /t/hook.log; echo "   took $(( $(date +%s) - start ))s"
ok "build-time nginx -t passed twice" "[ \$(grep -c 'test is successful' /t/hook.log) -ge 2 ]"
ok "build tools removed again" "! command -v g++ >/dev/null"
ok "certbot plugins: cloudflare" "/opt/certbot/bin/certbot plugins --text 2>/dev/null | grep -q dns-cloudflare"

# HELIXDATA
truncate -s 256M /hd.img; mkfs.ext4 -q -L HELIXDATA /hd.img; L=$(losetup -f --show /hd.img)
mkdir -p /mnt/data; mount "$L" /mnt/data
/usr/local/sbin/helix-npm-storage; /usr/local/sbin/helix-npm-setup
ok "storage persistent" "grep -q mode=persistent /run/helix/npm-storage"
ok "NPM enabled" "[ -e /run/npm/enabled ] && [ -s /etc/nginx/conf.d/production.conf ]"
ok "admin account generated, root 600" "[ \"\$(stat -c %a /data/helix-admin.json)\" = 600 ]"

# The "Helix UI" stand-in on 8082 and an app on 9000.
mkdir -p /srv/ui /srv/app; echo helix-ui > /srv/ui/index.html; echo the-app > /srv/app/index.html
(cd /srv/ui && python3 -m http.server 8082 --bind 127.0.0.1 >/dev/null 2>&1 &)
(cd /srv/app && python3 -m http.server 9000 --bind 127.0.0.1 >/dev/null 2>&1 &)
/usr/local/sbin/helix-gen-cert
nginx -t -q -g 'daemon on; master_process on;' && nginx -g 'daemon on; master_process on;'
# NPM backend as helix-npm.service runs it.
set -a; . /usr/local/lib/helix/npm.env; . /run/npm/env; set +a
eval "$(sed -n 's/^Environment=//p' /t/helix-npm.service | sed 's/^\([A-Z_]*\)=\(.*\)$/export \1="\2"/')"
cd /opt/npm/backend && node --abort_on_uncaught_exception --max_old_space_size=250 index.js > /t/npm-backend.log 2>&1 &
cd /
for i in $(seq 60); do curl -fs "http://127.0.0.1:$NPM_BACKEND_PORT/" >/dev/null 2>&1 && break; sleep 2; done
ok "API on 127.0.0.1:$NPM_BACKEND_PORT" "curl -fs http://127.0.0.1:$NPM_BACKEND_PORT/ >/dev/null"
IP=$(hostname -I | awk '{print $1}')
ok "API not on the LAN address" "! curl -fs --max-time 2 http://$IP:$NPM_BACKEND_PORT/ >/dev/null"
ok "admin UI on :81" "curl -fs http://127.0.0.1:81/ | grep -qi '<html'"
ok "admin UI API through nginx" "curl -fs http://127.0.0.1:81/api/ | grep -q '\"status\"'"
ok "Helix UI still the default on :80" "[ \"\$(curl -s http://127.0.0.1/)\" = helix-ui ]"
ok "Helix UI still the default on :443" "[ \"\$(curl -sk https://127.0.0.1/)\" = helix-ui ]"

TOKEN=$(python3 - <<'PY'
import json, urllib.request
a = json.load(open("/data/helix-admin.json"))
req = urllib.request.Request("http://127.0.0.1:81/api/tokens", method="POST",
    data=json.dumps({"identity": a["email"], "secret": a["password"]}).encode(),
    headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req))["token"])
PY
)
ok "signed in with the generated admin" "[ -n \"$TOKEN\" ]"
R=$(curl -s -X POST http://127.0.0.1:81/api/nginx/proxy-hosts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain_names":["app.test"],"forward_scheme":"http","forward_host":"127.0.0.1","forward_port":9000,"access_list_id":0,"certificate_id":0,"meta":{},"advanced_config":"","locations":[],"block_exploits":false,"caching_enabled":false,"allow_websocket_upgrade":true,"http2_support":false,"hsts_enabled":false,"hsts_subdomains":false,"ssl_forced":false}')
echo "   create: ${R:0:160}"
sleep 2
ok "proxy host config on HELIXDATA" "ls /mnt/data/npm/data/nginx/proxy_host/*.conf >/dev/null 2>&1"
ok "app.test routed to the app" "[ \"\$(curl -s -H 'Host: app.test' http://127.0.0.1/)\" = the-app ]"
ok "plain address still the Helix UI" "[ \"\$(curl -s http://127.0.0.1/)\" = helix-ui ]"
ok "per-host logs in RAM, not HELIXDATA" "ls /var/log/npm/proxy-host-* >/dev/null 2>&1 && [ -L /mnt/data/npm/data/logs ]"
# Default Site set to 404 must not take over the default server.
curl -s -X PUT http://127.0.0.1:81/api/settings/default-site -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"value":"404"}' >/dev/null
sleep 2
ok "NPM Default Site cannot replace the Helix UI" "[ \"\$(curl -s http://127.0.0.1/)\" = helix-ui ] && nginx -t -q"
# NPM down: nginx keeps serving.
pkill -f '[i]ndex.js'; sleep 1
ok "NPM stopped: app.test still served" "[ \"\$(curl -s -H 'Host: app.test' http://127.0.0.1/)\" = the-app ]"
du -sh /opt/npm /opt/certbot | sed 's/^/   size /'
nginx -s quit; sleep 1; umount /data /etc/letsencrypt /mnt/data; losetup -d "$L"
echo "== $pass passed, $fail failed"
[ "$fail" = 0 ] || { echo "--- backend log"; tail -20 /t/npm-backend.log; }
EOF
docker run --rm --privileged --platform "$PLATFORM" -v "$REPO:/repo:ro" -v "$WORK:/t" debian:bookworm bash /t/run.sh
