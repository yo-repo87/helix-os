#!/bin/bash
# test-proxy-api.sh: exercises the Helix proxy router (webui/backend/routers/proxy.py, §54.6)
# against a REAL NPM backend, in a throwaway privileged debian:bookworm container. It reuses the
# same build+boot steps as test-npm.sh (OpenResty + NPM), then imports the router and calls its
# functions directly (no uvicorn needed). Checks: status, create host (routes), list, delete, the
# self-reference refusal (phase 5), dns-providers, and the NPM-off clean error.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
H="$REPO/build/hooks/03-helix-install.sh"
{ echo 'set -e'
  awk '/^# ── Web server: OpenResty/{p=1} /^# ── Tailscale/{p=0} p' "$H"
  awk '/^# ── Self-signed TLS cert/{p=1} /^# ── systemd service/{p=0} p' "$H"
} > "$WORK/hook-section.sh"
awk '/^cat > \/etc\/systemd\/system\/helix-npm.service/{p=1;next} /^NPMSVC/{p=0} p' "$H" > "$WORK/helix-npm.service"

cat > "$WORK/drive.py" <<'PY'
import os, sys, json
sys.path.insert(0, "/repo/webui/backend")
import routers.proxy as proxy
fails = 0
def ok(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ")+name+("" if cond or not extra else f"  {extra}")); fails += (not cond)
def raises(fn, code=None):
    from fastapi import HTTPException
    try: fn(); return None
    except HTTPException as e: return e
# status
st = proxy.status()
ok("status available", st.get("available") is True, st)
# create a host that forwards to the stand-in app on 9000
r = proxy.create_host(proxy.HostReq(domain="router.test", forward_host="127.0.0.1", forward_port=9000))
ok("create host ok", r.get("ok") and r["host"]["domain_names"]==["router.test"], r)
hid = r["host"]["id"]
# upstream convenience form
r2 = proxy.create_host(proxy.HostReq(domain="up.test", upstream="127.0.0.1:9000"))
ok("create via upstream host:port", r2.get("ok"), r2)
# list contains both
names = {d for h in proxy.list_hosts() for d in h["domain_names"]}
ok("list shows created hosts", {"router.test","up.test"} <= names, names)
# self-reference: forwarding to Helix's own backend port is refused
e = raises(lambda: proxy.create_host(proxy.HostReq(domain="evil.test", forward_host="127.0.0.1", forward_port=8082)))
ok("refuse upstream = Helix backend", e is not None and e.status_code==400, e and e.detail)
# self-reference: a domain that resolves to this box is refused
e = raises(lambda: proxy.create_host(proxy.HostReq(domain="localhost", forward_host="127.0.0.1", forward_port=9000)))
ok("refuse domain resolving to self", e is not None and e.status_code==400, e and e.detail)
# dns providers include cloudflare + powerdns
ids = {d["id"] for d in proxy.dns_providers()}
ok("dns-providers lists cloudflare+powerdns", {"cloudflare","powerdns"} <= ids, sorted(ids)[:6])
# delete
proxy.delete_host(hid); proxy.delete_host(r2["host"]["id"])
ok("deleted", "router.test" not in {d for h in proxy.list_hosts() for d in h["domain_names"]}, "")
print(f"ROUTER {fails} failed")
sys.exit(1 if fails else 0)
PY

cat > "$WORK/offline.py" <<'PY'
import os, sys
sys.path.insert(0, "/repo/webui/backend")
import routers.proxy as proxy
from fastapi import HTTPException
fails = 0
def ok(n,c,e=""):
    global fails; print(("PASS " if c else "FAIL ")+n+("" if c or not e else f"  {e}")); fails+=(not c)
st = proxy.status()
ok("NPM-off status available False", st.get("available") is False, st)
ok("NPM-off has a clear reason", "writable" in (st.get("reason","").lower()) or "not running" in st.get("reason","").lower(), st.get("reason"))
try:
    proxy.create_host(proxy.HostReq(domain="x.test", forward_host="1.2.3.4", forward_port=80)); ok("create raises when off", False)
except HTTPException as e:
    ok("create raises 503 when off", e.status_code==503, e.detail)
print(f"OFFLINE {fails} failed"); sys.exit(1 if fails else 0)
PY

cat > "$WORK/run.sh" <<'EOF'
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends ca-certificates curl openssl iproute2 procps python3 python3-venv python3-pip e2fsprogs >/dev/null
curl -fsSL https://openresty.org/package/pubkey.gpg -o /usr/share/keyrings/openresty.asc
echo "deb [signed-by=/usr/share/keyrings/openresty.asc] https://openresty.org/package/debian bookworm openresty" > /etc/apt/sources.list.d/openresty.list
apt-get update -qq >/dev/null; apt-get install -y -qq --no-install-recommends openresty >/dev/null 2>&1
groupadd helix
mkdir -p /build && cp -r /repo/proxy /build/proxy
echo "== building NPM (hook sections)"
bash /t/hook-section.sh > /t/hook.log 2>&1 || { tail -20 /t/hook.log; echo "hook failed"; exit 1; }
# HELIXDATA + NPM up
truncate -s 256M /hd.img; mkfs.ext4 -q -L HELIXDATA /hd.img; L=$(losetup -f --show /hd.img)
mkdir -p /mnt/data; mount "$L" /mnt/data
/usr/local/sbin/helix-npm-storage; /usr/local/sbin/helix-npm-setup
mkdir -p /srv/app; echo the-app > /srv/app/index.html
(cd /srv/app && python3 -m http.server 9000 --bind 127.0.0.1 >/dev/null 2>&1 &)
mkdir -p /srv/ui; echo helix-ui > /srv/ui/index.html
(cd /srv/ui && python3 -m http.server 8082 --bind 127.0.0.1 >/dev/null 2>&1 &)
/usr/local/sbin/helix-gen-cert
nginx -g 'daemon on; master_process on;'
set -a; . /usr/local/lib/helix/npm.env; . /run/npm/env; set +a
eval "$(sed -n 's/^Environment=//p' /t/helix-npm.service | sed 's/^\([A-Z_]*\)=\(.*\)$/export \1="\2"/')"
cd /opt/npm/backend && node --abort_on_uncaught_exception --max_old_space_size=250 index.js > /t/npm.log 2>&1 &
cd /
for i in $(seq 60); do curl -fs http://127.0.0.1:81/api/ >/dev/null 2>&1 && break; sleep 2; done
# minimal python deps for the router (module-level: fastapi, pydantic; config.py stdlib only)
python3 -m venv /venv; /venv/bin/pip -q install fastapi pydantic >/dev/null 2>&1
export HELIX_CONFIG=/tmp/hcfg STATE_DIR=/tmp/hstate NPM_ADMIN_URL=http://127.0.0.1:81 \
       NPM_ADMIN_FILE=/data/helix-admin.json NPM_STATE_FILE=/run/helix/npm-storage NPM_RUN_DIR=/run/npm \
       NPM_DNS_PLUGINS=/opt/npm/dns-plugins.json NPM_PREINSTALLED=/opt/npm/certbot-preinstalled.txt
mkdir -p /tmp/hcfg /tmp/hstate
echo "== driving the Helix proxy router against real NPM"
/venv/bin/python /t/drive.py; rc1=$?
# route check through nginx (recreate a host to curl it)
TOKEN=$(/venv/bin/python - <<'P'
import json,urllib.request
a=json.load(open("/data/helix-admin.json"))
print(json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:81/api/tokens",data=json.dumps({"identity":a["email"],"secret":a["password"]}).encode(),headers={"Content-Type":"application/json"},method="POST")))["token"])
P
)
curl -s -X POST http://127.0.0.1:81/api/nginx/proxy-hosts -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"domain_names":["route.test"],"forward_scheme":"http","forward_host":"127.0.0.1","forward_port":9000,"access_list_id":0,"certificate_id":0,"meta":{},"advanced_config":"","locations":[],"block_exploits":false,"caching_enabled":false,"allow_websocket_upgrade":true,"http2_support":false,"hsts_enabled":false,"hsts_subdomains":false,"ssl_forced":false}' >/dev/null
sleep 2
[ "$(curl -s -H 'Host: route.test' http://127.0.0.1/)" = the-app ] && echo "PASS host routes through nginx" || { echo "FAIL host routes through nginx"; rc1=1; }
echo "== NPM-off behaviour"
printf 'mode=ram\nreason=read-only boot media\n' > /run/helix/npm-storage
rm -f /run/npm/enabled /data/helix-admin.json
/venv/bin/python /t/offline.py; rc2=$?
nginx -s quit 2>/dev/null; umount /data /etc/letsencrypt /mnt/data 2>/dev/null; losetup -d "$L" 2>/dev/null
[ $rc1 -eq 0 ] && [ $rc2 -eq 0 ] && echo "== ALL PASS" || { echo "== FAILURES"; tail -15 /t/npm.log; exit 1; }
EOF
docker run --rm --privileged -v "$REPO:/repo:ro" -v "$WORK:/t" debian:bookworm bash /t/run.sh
