"""Firewall engine (services/firewall.py). Offline: python webui/tests/test_firewall.py"""
import os, sys, tempfile

tmp = tempfile.mkdtemp()
os.environ["HELIX_CONFIG"] = os.path.join(tmp, "config"); os.environ["STATE_DIR"] = os.path.join(tmp, "state")
os.makedirs(os.environ["HELIX_CONFIG"]); os.makedirs(os.environ["STATE_DIR"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import firewall as fw  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name)

def raises(fn, *a):
    try:
        fn(*a)
        return False
    except ValueError:
        return True

# ── ports ──
check("ports parsed and merged", fw.parse_ports("443, 80,8000-8010,80") == [(80, 80), (443, 443), (8000, 8010)])
for bad in ["", "0", "70000", "10-5", "80;rm", "a", "1-2-3"]:
    check(f"bad ports {bad!r} rejected", raises(fw.parse_ports, bad))
check("65 ranges rejected", raises(fw.parse_ports, ",".join(str(p) for p in range(1, 66))))

# ── rules / config ──
r = fw.clean_rule({"name": "Plex", "ports": "32400", "proto": "tcp", "source": "lan"})
check("rule cleaned", r["ports"] == "32400" and r["enabled"] and r["source"] == "lan")
check("rule name with a quote/brace rejected", raises(fw.clean_rule, {"name": 'x"} drop', "ports": "1"}))
check("custom source needs addresses", raises(fw.clean_rule, {"name": "a", "ports": "1", "source": "custom"}))
check("custom source bad address rejected", raises(fw.clean_rule, {"name": "a", "ports": "1", "source": "custom", "cidrs": ["1.2.3"]}))
c = fw.clean_rule({"name": "a", "ports": "1", "source": "custom", "cidrs": ["203.0.113.7", "2001:db8::/32"]})
check("custom addresses normalised", c["cidrs"] == ["203.0.113.7/32", "2001:db8::/32"])
check("bad mode rejected", raises(fw.clean_config, {"mode": "panic"}))
check("empty LAN rejected", raises(fw.clean_config, {"mode": "enforce", "lan_networks": []}))
check("default LAN used when missing", fw.clean_config({"mode": "learn"})["lan_networks"])
ids = [x["id"] for x in fw.clean_config({"rules": [{"id": "a", "name": "x", "ports": "1"}, {"id": "a", "name": "y", "ports": "2"}]})["rules"]]
check("duplicate rule ids made unique", len(set(ids)) == 2)

# ── ruleset ──
check("mode off -> no ruleset", fw.generate({"mode": "off"}) == "")
cfg = {"mode": "enforce", "rules": [
    {"name": "Plex", "ports": "32400", "proto": "tcp", "source": "lan"},
    {"name": "WireGuard", "ports": "51820", "proto": "udp", "source": "any"},
    {"name": "Office", "ports": "8000-8010", "proto": "both", "source": "custom", "cidrs": ["203.0.113.0/24"]},
    {"name": "Off rule", "ports": "9999", "enabled": False}]}
s = fw.generate(cfg)
check("atomic replace of our own table only", s.startswith("table inet helix {}\ndelete table inet helix\n") and "flush ruleset" not in s)
check("input + forward hooks before Docker's (priority filter - 10)", s.count("priority filter - 10") == 2)
check("management always allowed from LAN", 'ip saddr @lan4 tcp dport { 22, 80, 443, 8443 } accept' in s)
check("LAN rule for both families", "ip saddr @lan4 meta l4proto tcp th dport { 32400 } accept" in s
      and "ip6 saddr @lan6 meta l4proto tcp th dport { 32400 } accept" in s)
check("'any' rule has no source match", "    meta l4proto udp th dport { 51820 } accept" in s)
check("custom rule with tcp+udp", "ip saddr { 203.0.113.0/24 } meta l4proto { tcp, udp } th dport { 8000-8010 } accept" in s)
check("disabled rule left out", "9999" not in s)
check("container ports matched on the original (pre-DNAT) port", "ct original proto-dst { 32400 }" in s)
check("only DNATed traffic into Docker bridges is filtered", "ct status dnat jump published" in s and 'oifname "br-*" jump to_containers' in s)
check("VM bridge br0 is not a Docker bridge", not any(p in ("br0", "br*") for p in fw.DOCKER_IFACES))
check("enforce ends in drop with a log", s.count("    drop\n") == 2 and 'log prefix "helix-fw-drop: "' in s)
ls = fw.generate({**cfg, "mode": "learn"})
check("learn never drops (only invalid)", "    drop\n" not in ls and 'log prefix "helix-fw-learn: "' in ls)
check("no log when logging off", "log prefix" not in fw.generate({**cfg, "log_blocked": False}))

# ── listeners, audit, learn ──
SS = """tcp   LISTEN 0 4096 0.0.0.0:32400 0.0.0.0:* users:(("docker-proxy",pid=11,fd=4))
tcp   LISTEN 0 128  0.0.0.0:22    0.0.0.0:* users:(("sshd",pid=1,fd=3))
tcp   LISTEN 0 128  127.0.0.1:8082 0.0.0.0:* users:(("uvicorn",pid=2,fd=3))
tcp   LISTEN 0 128  [::]:6379 [::]:* users:(("redis-server",pid=3,fd=3))
udp   UNCONN 0 0    0.0.0.0:51820 0.0.0.0:*
udp   UNCONN 0 0    127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=4,fd=3))
tcp   LISTEN 0 128  0.0.0.0:6379 0.0.0.0:* users:(("redis-server",pid=3,fd=4))"""
li = fw.parse_ss(SS)
check("ss parsed (dedup v4/v6, loopback flagged)", [(x["proto"], x["port"], x["loopback"]) for x in li]
      == [("tcp", 22, False), ("udp", 53, True), ("tcp", 6379, False), ("tcp", 8082, True), ("tcp", 32400, False), ("udp", 51820, False)])
ctrs = fw.parse_docker_ports("plex\t0.0.0.0:32400->32400/tcp, :::32400->32400/tcp\nweb\t0.0.0.0:8000-8002->80-82/tcp\n")
check("docker ports parsed incl. ranges", ctrs[("tcp", 32400)] == "plex" and ctrs[("tcp", 8001)] == "web")

rows = {(x["proto"], x["port"]): x for x in fw.audit({"mode": "learn"}, li, ctrs)}
check("learn: everything reachable by anyone", rows[("tcp", 32400)]["reach"] == "anyone")
check("loopback-only is this machine", rows[("tcp", 8082)]["reach"] == "this machine")
check("open database is warned about", "Redis" in rows[("tcp", 6379)]["warn"])
check("container name shown", rows[("tcp", 32400)]["container"] == "plex")

sug = fw.learn_rules({"mode": "learn"}, li, ctrs)
names = {x["name"]: x for x in sug}
check("learn: one LAN rule per service, SSH/loopback skipped", set(names) == {"plex", "redis-server", "Port 51820"}
      and all(x["source"] == "lan" and x["learned"] for x in sug))
ecfg = {"mode": "enforce", "rules": [names["plex"]]}
rows = {(x["proto"], x["port"]): x for x in fw.audit(ecfg, li, ctrs)}
check("enforce: ruled port is LAN only", rows[("tcp", 32400)]["reach"] == "LAN")
check("enforce: unruled port is blocked", rows[("tcp", 6379)]["reach"] == "nobody" and not rows[("tcp", 6379)]["covered"])
check("enforce: SSH always LAN and VPN", rows[("tcp", 22)]["reach"] == "LAN and VPN")
check("learn skips already covered ports", all(x["name"] != "plex" for x in fw.learn_rules(ecfg, li, ctrs)))

# ── log ──
L = ["1795200000.123 Tower kernel: helix-fw-drop: IN=eno1 OUT= MAC=x SRC=203.0.113.9 DST=192.168.50.142 LEN=60 PROTO=TCP SPT=5555 DPT=6379",
     "1795200001.5 Tower kernel: helix-fw-learn: IN=eno1 SRC=192.168.50.20 DST=192.168.50.142 PROTO=UDP SPT=1 DPT=161",
     "1795200002 Tower kernel: unrelated"]
ev = fw.parse_log(L)
check("log lines parsed", len(ev) == 2 and ev[0]["kind"] == "blocked" and ev[0]["dport"] == 6379 and ev[0]["src"] == "203.0.113.9"
      and ev[1]["kind"] == "would block" and ev[1]["proto"] == "udp" and ev[0]["ts"] == 1795200000.123)

# ── lockout check in the router ──
from routers import firewall as fr  # noqa: E402
e = fw.clean_config({"mode": "enforce"})
check("LAN client keeps access", fr._client_keeps_access(e, "192.168.50.20"))
check("loopback (NPM / tunnel) keeps access", fr._client_keeps_access(e, "127.0.0.1"))
check("public client would be locked out", not fr._client_keeps_access(e, "203.0.113.9"))
e2 = fw.clean_config({"mode": "enforce", "rules": [{"name": "me", "ports": "443", "source": "custom", "cidrs": ["203.0.113.9"]}]})
check("public client allowed by a custom rule keeps access", fr._client_keeps_access(e2, "203.0.113.9"))
check("learn mode never locks out", fr._client_keeps_access(fw.clean_config({"mode": "learn"}), "203.0.113.9"))

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
