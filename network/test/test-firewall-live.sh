#!/bin/bash
# Live firewall test with real packets, isolated in a privileged container's own network namespace.
#   network/test/test-firewall-live.sh        (needs Docker; run on the build host, e.g. 192.168.50.142)
# Inside: a "server" (the container's netns) with a LAN client, an internet client and a fake Docker bridge
# (br-test) whose "container" is published by DNAT like Docker does. Applies the real helix-firewall and checks
# who gets through in learn / enforce / off, that the confirm-or-revert safety net refuses to keep a change it
# cannot undo, and that other tables (Docker's) are never touched.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${1:-}" != "--inside" ]; then
    exec docker run --rm --privileged -v "$REPO":/src:ro debian:bookworm bash /src/network/test/test-firewall-live.sh --inside
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends nftables iproute2 python3 curl procps >/dev/null 2>&1

PASS=0; FAIL=0
ok()  { echo "PASS $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $1"; FAIL=$((FAIL + 1)); }
reach() { ip netns exec "$1" curl -s -o /dev/null -m 2 "http://$2/" && echo yes || echo no; }
expect() { local got; got=$(reach "$2" "$3"); [ "$got" = "$4" ] && ok "$1 ($2 -> $3: $got)" || bad "$1 ($2 -> $3: got $got, want $4)"; }

# ── network ──
sysctl -qw net.ipv4.ip_forward=1
for ns in lanc wanc ctr; do ip netns add $ns; done
ip link add lan0 type veth peer name eth0 netns lanc
ip link add wan0 type veth peer name eth0 netns wanc
ip link add br-test type bridge; ip link add ctrv type veth peer name eth0 netns ctr; ip link set ctrv master br-test
ip addr add 192.168.77.1/24 dev lan0; ip addr add 203.0.113.1/24 dev wan0; ip addr add 172.30.0.1/24 dev br-test
for l in lan0 wan0 br-test ctrv; do ip link set $l up; done
ip -n lanc addr add 192.168.77.2/24 dev eth0; ip -n wanc addr add 203.0.113.2/24 dev eth0; ip -n ctr addr add 172.30.0.2/24 dev eth0
for ns in lanc wanc ctr; do ip -n $ns link set eth0 up; ip -n $ns link set lo up; done
ip -n lanc route add default via 192.168.77.1; ip -n wanc route add default via 203.0.113.1; ip -n ctr route add default via 172.30.0.1
# "Docker": its own tables, a DNAT for the published port 9090 -> container:80, and a filter chain we must not touch.
nft -f - <<'EOF'
table ip nat { chain prerouting { type nat hook prerouting priority dstnat; tcp dport 9090 dnat to 172.30.0.2:80; } }
table ip filter { chain DOCKER { } }
EOF

# ── servers ──
for p in 8080 443 6379; do (cd /tmp && python3 -m http.server $p >/dev/null 2>&1 &); done
ip netns exec ctr sh -c 'cd /tmp && python3 -m http.server 80 >/dev/null 2>&1 &'
ip netns exec wanc sh -c 'cd /tmp && python3 -m http.server 8000 >/dev/null 2>&1 &'
sleep 1.5

# ── helper ──
export HELIX_CONFIG=/tmp/hc STATE_DIR=/tmp/st HELIX_BACKEND=/src/webui/backend
mkdir -p $HELIX_CONFIG $STATE_DIR
FW="python3 /src/network/helix-firewall"
pending() { echo "$1" > $STATE_DIR/firewall.pending.json; }

expect "no firewall: internet reaches a service" wanc 192.168.77.1:8080 yes

$FW boot >/dev/null
[ "$(python3 -c "import json;print(json.load(open('$HELIX_CONFIG/firewall.json'))['mode'])")" = learn ] \
    && ok "first boot without a config starts in learn mode" || bad "first boot mode"
expect "learn: nothing is blocked" wanc 203.0.113.1:6379 yes
expect "learn: published container port open" wanc 203.0.113.1:9090 yes

pending '{"mode":"enforce","rules":[{"name":"Web","ports":"8080","source":"lan"},{"name":"App","ports":"9090","source":"lan"}]}'
$FW apply >/dev/null && ok "enforce applied" || bad "enforce apply"
expect "enforce: LAN reaches the allowed port"        lanc 192.168.77.1:8080 yes
expect "enforce: internet blocked on the LAN-only port" wanc 203.0.113.1:8080 no
expect "enforce: web UI always open from LAN"          lanc 192.168.77.1:443 yes
expect "enforce: web UI not open to the internet"      wanc 203.0.113.1:443 no
expect "enforce: port without a rule blocked for LAN"  lanc 192.168.77.1:6379 no
expect "enforce: LAN reaches the published container"  lanc 192.168.77.1:9090 yes
expect "enforce: internet blocked from the container"  wanc 203.0.113.1:9090 no
expect "enforce: container still reaches the outside"  ctr 203.0.113.2:8000 yes

pending '{"mode":"enforce","rules":[{"name":"Web","ports":"8080","source":"any"},{"name":"App","ports":"9090","source":"custom","cidrs":["203.0.113.2"]}]}'
$FW apply >/dev/null
expect "rule 'anyone': internet reaches it"            wanc 203.0.113.1:8080 yes
expect "rule 'specific address': that address gets in" wanc 203.0.113.1:9090 yes
expect "rule 'specific address': LAN does not"         lanc 192.168.77.1:9090 no

pending '{"mode":"enforce","rules":[]}'
if $FW apply --revert-after 5 >/tmp/out 2>&1; then bad "apply with undo kept a change it could not undo"
else grep -q "could not schedule" /tmp/out && ok "no undo timer (no systemd) -> change refused" || bad "unexpected: $(cat /tmp/out)"; fi
expect "refused change rolled back to the saved rules" wanc 203.0.113.1:8080 yes

pending '{"mode":"enforce","rules":[{"name":"x","ports":"1","source":"custom","cidrs":["not-an-ip"]}]}'
$FW apply >/dev/null 2>&1 && bad "invalid config accepted" || ok "invalid config refused, nothing changed"
expect "after a refused config the old rules still work" wanc 203.0.113.1:8080 yes

nft list table ip filter >/dev/null 2>&1 && nft list table ip nat | grep -q "dnat to" && ok "Docker's tables untouched" || bad "Docker's tables changed"

$FW off >/dev/null
nft list table inet helix >/dev/null 2>&1 && bad "table still there after off" || ok "off removes the table"
expect "off: everything reachable again" wanc 203.0.113.1:6379 yes
nft list table ip filter >/dev/null 2>&1 && ok "Docker's tables untouched by off" || bad "off touched Docker's tables"

$FW boot >/dev/null
nft list table inet helix >/dev/null 2>&1 && bad "boot re-enabled a firewall that was turned off" || ok "boot keeps it off after 'off'"

echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
