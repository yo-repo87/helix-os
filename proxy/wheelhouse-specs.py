#!/usr/bin/env python3
# Print pip specs (package + deps, certbot-version substituted) for one DNS plugin. Used by
# install-npm.sh to build the wheelhouse. Args: <dns-plugins.json>. Env: WH_KEY, CERTBOT_VERSION.
import json, os, sys
v = os.environ["CERTBOT_VERSION"]; key = os.environ["WH_KEY"]
p = json.load(open(sys.argv[1])).get(key)
if not p:
    sys.exit(1)
out = [p["package_name"] + p["version"].replace("{{certbot-version}}", v)]
out += [d.replace("{{certbot-version}}", v) for d in (p.get("dependencies") or "").split() if d]
print(" ".join(out))
