#!/bin/bash
# test-certbot-plugins.sh: helix-npm-certbot-plugins keeps an un-pre-installed DNS plugin across a
# "reboot" (CLAUDE.md §54.4). Needs Docker; builds a tiny certbot venv + the shipped plugin map in a
# throwaway debian:bookworm container (no HELIXDATA, no NPM — just the venv and renewal confs).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm -i -v "$REPO/proxy:/proxy:ro" debian:bookworm bash -s <<'IN'
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates >/dev/null
pass=0; fail=0
ok(){ if eval "$2"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1  ${3:-}"; fi; }

# A minimal certbot venv with ONLY cloudflare pre-installed (stand-in for the image's 16).
python3 -m venv /opt/certbot
/opt/certbot/bin/pip -q install --upgrade pip >/dev/null
/opt/certbot/bin/pip -q install "certbot==5.10.0" "certbot-dns-cloudflare==5.10.0" >/dev/null 2>&1
# Build-time (compiler present): build the wheelhouse for powerdns, as install-npm.sh does.
apt-get install -y -qq --no-install-recommends gcc python3-dev >/dev/null
mkdir -p /opt/npm/certbot-wheelhouse
/opt/certbot/bin/pip wheel --wheel-dir /opt/npm/certbot-wheelhouse "certbot==5.10.0" "certbot-dns-powerdns~=0.2.1" >/dev/null 2>&1   && echo "  (wheelhouse built)" || echo "  (wheelhouse build FAILED)"
rm -f /opt/npm/certbot-wheelhouse/certbot-*.whl
# Remove the compiler so the runtime path proves it needs no build tools.
apt-get purge -y -qq gcc python3-dev >/dev/null 2>&1; apt-get autoremove -y -qq >/dev/null 2>&1
mkdir -p /opt/npm
# The test ships the plugin map + preinstalled list the way install-npm.sh would.
python3 - <<'PY'
import json, urllib.request
# minimal map with the two providers the test needs
m = {
 "cloudflare": {"full_plugin_name":"dns-cloudflare","package_name":"certbot-dns-cloudflare","version":"=={{certbot-version}}","dependencies":"acme=={{certbot-version}}"},
 "powerdns":   {"full_plugin_name":"dns-powerdns","package_name":"certbot-dns-powerdns","version":"~=0.2.1","dependencies":""},
}
json.dump(m, open("/opt/npm/dns-plugins.json","w"))
open("/opt/npm/certbot-preinstalled.txt","w").write("cloudflare\n")
PY

mkdir -p /etc/letsencrypt/renewal /data/certbot-wheels
touch /run; mkdir -p /run/npm; touch /run/npm/enabled
# One cert uses cloudflare (pre-installed), one uses powerdns (not).
printf 'authenticator = dns-cloudflare\n' > /etc/letsencrypt/renewal/npm-1.conf
printf 'authenticator = dns-powerdns\n'   > /etc/letsencrypt/renewal/npm-2.conf

echo "== first run: installs powerdns OFFLINE from the shipped wheelhouse (no compiler)"
: > /etc/resolv.conf   # prove it needs no network and no build tools
python3 /proxy/helix-npm-certbot-plugins
ok "powerdns importable after first run" "/opt/certbot/bin/python -c 'import certbot_dns_powerdns'"
ok "installed without a compiler (gcc removed)" "! command -v gcc >/dev/null"

echo "== simulate a reboot: the RAM venv loses powerdns, /data cache persists"
/opt/certbot/bin/pip -q uninstall -y certbot-dns-powerdns >/dev/null 2>&1
ok "powerdns gone after 'reboot'" "! /opt/certbot/bin/python -c 'import certbot_dns_powerdns' 2>/dev/null"

echo "== second run OFFLINE: restores again from the wheelhouse"
python3 /proxy/helix-npm-certbot-plugins
ok "powerdns restored offline from the wheelhouse" "/opt/certbot/bin/python -c 'import certbot_dns_powerdns'"

echo "== a cert with no DNS plugin, NPM off -> no-op"
rm /etc/letsencrypt/renewal/npm-2.conf; rm /run/npm/enabled
out=$(python3 /proxy/helix-npm-certbot-plugins)
ok "NPM off -> nothing to do" "echo \"\$out\" | grep -q 'NPM is off'"

echo "== $pass passed, $fail failed"
[ "$fail" = 0 ]
IN
