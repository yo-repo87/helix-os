#!/bin/bash
# install-npm.sh: build Nginx Proxy Manager into the image (CLAUDE.md §54, phase 3).
# Runs as root inside the build chroot (called by build/hooks/03-helix-install.sh), with network.
# Every failure is fatal: an image must not offer a proxy manager that cannot start.
#
#   install-npm.sh [SRC_DIR]     SRC_DIR holds npm.env, nginx.conf, ... (default: this script's folder)
#
# Result:
#   /opt/npm/node            Node.js (runtime only: npm/corepack/headers removed)
#   /opt/npm/backend         NPM backend, production node_modules, patched (see below)
#   /opt/npm/frontend        NPM admin UI, built here with vite
#   /opt/npm/production.conf.template   admin-port server block (helix-npm-setup fills it at boot)
#   /opt/certbot             certbot + DNS plugins in a venv (NPM calls `certbot` from PATH)
#   /etc/nginx/conf.d/include/*, /etc/nginx/mime.types, /etc/letsencrypt.ini   NPM's nginx pieces
#
# Build cache: when /build-cache/npm exists (build.sh bind-mounts $HELIX_NPM_CACHE there), the result is saved as
# npm-<arch>-<key>.tar and reused while the key matches. The key covers npm.env (all version pins), this script,
# wheelhouse-specs.py, the architecture and the libc6 / libstdc++6 / python3 versions. A cached copy is only used
# after the same runtime checks a fresh build must pass; if they fail it is deleted and the build runs. Under qemu
# (arm64 on x86) this turns roughly an hour of webpack/sqlite3/wheel building into seconds.
set -euo pipefail
SRC=${1:-$(cd "$(dirname "$0")" && pwd)}
# shellcheck source=npm.env
. "$SRC/npm.env"
log() { echo "  [npm] $*"; }

PREFIX=/opt/npm
WORK=$(mktemp -d /var/tmp/npm-build.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

case "$(dpkg --print-architecture)" in
    amd64) NODE_ARCH=x64 ;;
    arm64) NODE_ARCH=arm64 ;;
    *) echo "install-npm: unsupported architecture $(dpkg --print-architecture)" >&2; exit 1 ;;
esac

# ── Build cache ──────────────────────────────────────────────────────────────
CACHE_DIR="${HELIX_NPM_CACHE_DIR:-/build-cache/npm}"
ARCH_DEB="$(dpkg --print-architecture)"
CACHE_KEY="$( { cat "$SRC/npm.env" "${BASH_SOURCE[0]}" "$SRC/wheelhouse-specs.py"; echo "$ARCH_DEB";
               dpkg-query -W -f='${Package}=${Version}\n' libc6 libstdc++6 python3 2>/dev/null; } | sha256sum | cut -c1-16)"
CACHE_TAR="$CACHE_DIR/npm-$ARCH_DEB-$CACHE_KEY.tar"
CACHE_PATHS=(opt/npm opt/certbot etc/nginx/conf.d/include etc/nginx/mime.types etc/letsencrypt.ini)

ensure_libatomic() {   # Node needs it at runtime; mark it manual so purging build tools never removes it
    if ! dpkg-query -W -f='${Status}' libatomic1 2>/dev/null | grep -q "install ok installed"; then
        apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libatomic1 >/dev/null
    fi
    apt-mark manual libatomic1 >/dev/null
}
runtime_ok() {         # the checks every build must pass (also run on a cached copy)
    "$PREFIX/node/bin/node" -e 'require("node:crypto")' >/dev/null 2>&1 \
    && ( cd "$PREFIX/backend" && "$PREFIX/node/bin/node" -e 'new (require("better-sqlite3"))(":memory:"); require("sqlite3"); require("bcrypt")' ) >/dev/null 2>&1 \
    && /opt/certbot/bin/certbot --version >/dev/null 2>&1 \
    && [ -s "$PREFIX/frontend/index.html" ]
}
save_cache() {
    [ -d "$CACHE_DIR" ] || return 0
    if tar -cf "$CACHE_TAR.tmp" -C / "${CACHE_PATHS[@]}" && mv "$CACHE_TAR.tmp" "$CACHE_TAR"; then
        log "saved to the build cache: $(basename "$CACHE_TAR")"
        ls -t "$CACHE_DIR"/npm-"$ARCH_DEB"-*.tar 2>/dev/null | tail -n +3 | xargs -r rm -f   # keep the newest two
    else
        rm -f "$CACHE_TAR.tmp"; log "could not save to the build cache (continuing)"
    fi
}
# HELIX_NPM_SEED=1: save an existing, working /opt/npm (from a finished build) to the cache; build nothing.
if [ -n "${HELIX_NPM_SEED:-}" ]; then
    runtime_ok || { echo "install-npm: nothing working to seed the cache with" >&2; exit 1; }
    save_cache; exit 0
fi
if [ -d "$CACHE_DIR" ] && [ -f "$CACHE_TAR" ]; then
    log "using the cached build $(basename "$CACHE_TAR")"
    rm -rf "$PREFIX" /opt/certbot
    if tar -xf "$CACHE_TAR" -C / && ensure_libatomic && runtime_ok; then
        log "done (cached): $(du -sh "$PREFIX" /opt/certbot | awk '{printf "%s %s  ", $2, $1}')"
        exit 0
    fi
    log "the cached build does not run in this image: deleting it and building"
    rm -f "$CACHE_TAR"; rm -rf "$PREFIX" /opt/certbot
fi

# Build-only tools for native modules (better-sqlite3, sqlite3, bcrypt) when no prebuilt binary
# matches this Node; removed again at the end if this script added them.
BUILD_DEPS=""
for p in make g++ python3; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || BUILD_DEPS="$BUILD_DEPS $p"
done
if [ -n "$BUILD_DEPS" ]; then
    log "temporary build tools:$BUILD_DEPS"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $BUILD_DEPS >/dev/null
fi

# ── Node.js (official build, checked against nodejs.org's SHASUMS256) ────────
# .tar.gz, not .tar.xz: the image has no xz.
NODE_TGZ="node-v$NODE_VERSION-linux-$NODE_ARCH.tar.gz"
log "Node.js $NODE_VERSION ($NODE_ARCH)"
curl -fsSL --retry 3 "https://nodejs.org/dist/v$NODE_VERSION/$NODE_TGZ" -o "$WORK/$NODE_TGZ"
curl -fsSL --retry 3 "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" -o "$WORK/SHASUMS256.txt"
( cd "$WORK" && grep " $NODE_TGZ\$" SHASUMS256.txt | sha256sum -c --quiet - ) \
    || { echo "install-npm: Node.js checksum mismatch" >&2; exit 1; }
rm -rf "$PREFIX"; mkdir -p "$PREFIX/node"
tar -xzf "$WORK/$NODE_TGZ" -C "$PREFIX/node" --strip-components=1
export PATH="$PREFIX/node/bin:$WORK/yarn/bin:$PATH"
# Node needs libatomic1 at runtime. g++ pulls it in too, so mark it manual or purging the
# build tools at the end would take it away.
if ! dpkg-query -W -f='${Status}' libatomic1 2>/dev/null | grep -q "install ok installed"; then
    apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libatomic1 >/dev/null
fi
apt-mark manual libatomic1 >/dev/null
node -v >/dev/null
# yarn classic (NPM's lockfiles are yarn v1), only for the build.
npm install -g --silent --prefix "$WORK/yarn" "yarn@$YARN_VERSION" >/dev/null
export YARN_CACHE_FOLDER="$WORK/yarn-cache" npm_config_cache="$WORK/npm-cache" HOME="$WORK/home"
mkdir -p "$HOME"

# ── NPM source (pinned tarball + sha256) ─────────────────────────────────────
log "Nginx Proxy Manager $NPM_VERSION"
curl -fsSL --retry 3 "https://github.com/NginxProxyManager/nginx-proxy-manager/archive/refs/tags/v$NPM_VERSION.tar.gz" -o "$WORK/npm.tgz"
echo "$NPM_SHA256  $WORK/npm.tgz" | sha256sum -c --quiet - \
    || { echo "install-npm: NPM source checksum mismatch" >&2; exit 1; }
mkdir -p "$WORK/src"; tar -xzf "$WORK/npm.tgz" -C "$WORK/src" --strip-components=1
S="$WORK/src"

# Patches. Each one checks that upstream still has the text it replaces, so a new NPM release
# that changed it fails the build here instead of shipping unpatched.
patch_text() {   # file old new
    python3 - "$1" "$2" "$3" <<'PY'
import sys
path, old, new = sys.argv[1:4]
s = open(path).read()
if s.count(old) != 1:
    sys.exit(f"install-npm: patch target not found exactly once in {path}: {old!r}")
open(path, "w").write(s.replace(old, new))
PY
}
# 1) The API binds loopback on NPM_BACKEND_PORT (upstream: 3000 on every interface).
patch_text "$S/backend/index.js" 'app.listen(3000, () => {' \
    'app.listen(Number(process.env.NPM_BACKEND_PORT || 3000), process.env.NPM_BACKEND_HOST || "127.0.0.1", () => {'
# 2) "Default Site" must not take the default server from the Helix UI (two default servers on
#    :80 fail `nginx -t`). It stays a named server that only matches default-host.localhost.
patch_text "$S/backend/templates/default.conf" '  listen 80 default;' '  listen 80;'
patch_text "$S/backend/templates/default.conf" '  listen [::]:80 default;' '  listen [::]:80;'
# 3) The admin server block proxies to the patched port and answers the LAN only.
patch_text "$S/docker/rootfs/etc/nginx/conf.d/production.conf.template" \
    'proxy_pass            http://127.0.0.1:3000/;' 'proxy_pass            http://127.0.0.1:{{NPM_BACKEND_PORT}}/;'
patch_text "$S/docker/rootfs/etc/nginx/conf.d/production.conf.template" \
    '	access_log /dev/null;' '	access_log /dev/null;
	include /etc/nginx/helix-lan-only.conf;'

# ── Frontend ─────────────────────────────────────────────────────────────────
log "building the admin UI"
# Same steps as upstream's scripts/ci/frontend-build (locale-compile generates src/locale/lang,
# which the build imports), minus lint and tests.
( cd "$S/frontend" && export CI=true NODE_OPTIONS=--openssl-legacy-provider \
    && yarn install --frozen-lockfile --non-interactive --network-timeout 600000 && yarn locale-compile && yarn build ) > "$WORK/frontend.log" 2>&1 \
    || { tail -40 "$WORK/frontend.log" >&2; echo "install-npm: frontend build failed" >&2; exit 1; }
[ -s "$S/frontend/dist/index.html" ] || { echo "install-npm: frontend build produced no index.html" >&2; exit 1; }
cp -r "$S/frontend/dist" "$PREFIX/frontend"

# ── Backend ──────────────────────────────────────────────────────────────────
log "installing the backend"
cp -r "$S/backend" "$PREFIX/backend"
rm -rf "$PREFIX/backend/test"
# sqlite3's prebuilt binary needs glibc 2.38 (bookworm has 2.36): compile that one module here,
# against this Node's own headers (no header download).
( cd "$PREFIX/backend" && export npm_config_build_from_source=sqlite3 npm_config_nodedir="$PREFIX/node" \
    && yarn install --production --frozen-lockfile --non-interactive --network-timeout 600000 ) > "$WORK/backend.log" 2>&1 \
    || { tail -40 "$WORK/backend.log" >&2; echo "install-npm: backend dependency install failed" >&2; exit 1; }
# The native modules must load with this Node (catches a missing prebuilt + failed compile).
( cd "$PREFIX/backend" && node -e '
const D = require("better-sqlite3"); new D(":memory:").prepare("select 1").get();
require("bcrypt").hashSync("x", 4);
require("sqlite3");
' ) || { echo "install-npm: a native backend module does not load" >&2; exit 1; }
echo "$NPM_VERSION" > "$PREFIX/VERSION"

# ── nginx pieces NPM's generated configs include ─────────────────────────────
mkdir -p /etc/nginx/conf.d/include
cp "$S/docker/rootfs/etc/nginx/conf.d/include/"*.conf /etc/nginx/conf.d/include/
cp "$S/docker/rootfs/etc/nginx/mime.types" /etc/nginx/mime.types
cp "$S/docker/rootfs/etc/letsencrypt.ini" /etc/letsencrypt.ini
cp "$S/docker/rootfs/etc/nginx/conf.d/production.conf.template" "$PREFIX/production.conf.template"
# The plugin -> certbot-authenticator/package map (CLAUDE.md §54.4): the boot step reads it to
# re-install, from the HELIXDATA wheel cache, any DNS plugin a certificate needs that is not
# baked into the image. Copied from NPM's own source so it never drifts from the frontend.
cp "$S/backend/certbot/dns-plugins.json" "$PREFIX/dns-plugins.json"
# Record which plugins ARE pre-installed in the image venv (so the boot step skips them).
printf '%s\n' $CERTBOT_DNS_PLUGINS > "$PREFIX/certbot-preinstalled.txt"

# ── certbot + DNS plugins (versions from NPM's dns-plugins.json) ─────────────
log "certbot $CERTBOT_VERSION + DNS plugins"
rm -rf /opt/certbot
python3 -m venv /opt/certbot
PIP_ARGS=$(CERTBOT_VERSION="$CERTBOT_VERSION" python3 - "$S/backend/certbot/dns-plugins.json" $CERTBOT_DNS_PLUGINS <<'PY'
import json, os, sys
v = os.environ["CERTBOT_VERSION"]
plugins = json.load(open(sys.argv[1]))
out = [f"certbot=={v}"]
for key in sys.argv[2:]:
    p = plugins.get(key)
    if not p:
        sys.exit(f"install-npm: unknown DNS plugin {key!r}")
    out.append(p["package_name"] + p["version"].replace("{{certbot-version}}", v))
    out += [d.replace("{{certbot-version}}", v) for d in p["dependencies"].split() if d]
print(" ".join(out))
PY
)
/opt/certbot/bin/pip install --no-cache-dir --quiet --upgrade pip >/dev/null
# shellcheck disable=SC2086
/opt/certbot/bin/pip install --no-cache-dir --quiet $PIP_ARGS \
    || { echo "install-npm: certbot / plugin install failed" >&2; exit 1; }
/opt/certbot/bin/certbot --version >/dev/null
find /opt/certbot -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ── Wheelhouse for plugins kept out of the venv (CLAUDE.md §54.4) ─────────────
# Build wheels now, while build-essential is installed, for the DNS plugins not pre-installed
# (powerdns has no PyPI wheel; google is large). helix-npm-certbot-plugins installs from here
# offline at the first boot a cert needs one. Non-fatal: a plugin that will not build is skipped.
WHEELHOUSE="$PREFIX/certbot-wheelhouse"
mkdir -p "$WHEELHOUSE"
for key in $CERTBOT_DNS_WHEELHOUSE; do
    specs=$(WH_KEY="$key" CERTBOT_VERSION="$CERTBOT_VERSION" python3 "$SRC/wheelhouse-specs.py" "$S/backend/certbot/dns-plugins.json") \
        || { log "wheelhouse: unknown plugin $key, skipping"; continue; }
    # shellcheck disable=SC2086
    /opt/certbot/bin/pip wheel --wheel-dir "$WHEELHOUSE" "certbot==$CERTBOT_VERSION" $specs >/dev/null 2>&1 \
        && log "wheelhouse: built $key" || log "wheelhouse: could not build $key (will need network at runtime)"
done
# certbot itself is in every wheel set; drop it so the wheelhouse only carries the extra plugins.
rm -f "$WHEELHOUSE"/certbot-*.whl 2>/dev/null || true

# ── Trim the runtime ─────────────────────────────────────────────────────────
rm -rf "$PREFIX/node/lib/node_modules" "$PREFIX/node/include" "$PREFIX/node/share" \
       "$PREFIX/node/bin/npm" "$PREFIX/node/bin/npx" "$PREFIX/node/bin/corepack" \
       "$PREFIX/node/CHANGELOG.md" "$PREFIX/node/README.md"
if [ -n "$BUILD_DEPS" ]; then
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq $BUILD_DEPS >/dev/null || true
    apt-get autoremove -y -qq >/dev/null || true
    apt-get clean
fi
# The runtime must still work without the build tools.
"$PREFIX/node/bin/node" -e 'require("node:crypto")' \
    || { echo "install-npm: node does not start after removing the build tools" >&2; exit 1; }
( cd "$PREFIX/backend" && "$PREFIX/node/bin/node" -e 'new (require("better-sqlite3"))(":memory:"); require("sqlite3"); require("bcrypt")' ) \
    || { echo "install-npm: native modules do not load after removing the build tools" >&2; exit 1; }
runtime_ok || { echo "install-npm: the finished build does not pass its runtime checks" >&2; exit 1; }
save_cache
log "done: $(du -sh "$PREFIX" /opt/certbot | awk '{printf "%s %s  ", $2, $1}')"
