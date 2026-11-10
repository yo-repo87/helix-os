#!/bin/bash
# Exercises helix-mover against a throwaway tree. No root, no /mnt.
set -u
cd "$(dirname "$0")/.."
MOVER=sbin/helix-mover
fail=0
ok() { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=1; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
CACHE=$ROOT/cache
DEST=$ROOT/array
mkdir -p "$CACHE/movies/nested" "$CACHE/system/docker" "$CACHE/.hidden" "$DEST/movies"

echo "keep-me" > "$CACHE/readme.txt"
echo "movie" > "$CACHE/movies/nested/a.mkv"
echo "open-data" > "$CACHE/movies/open.bin"
echo "docker-layer" > "$CACHE/system/docker/layer"
echo "secret" > "$CACHE/.hidden/secret"
echo "already" > "$CACHE/movies/exists.txt"
echo "original" > "$DEST/movies/exists.txt"
mkdir -p "$CACHE/movies/empty/dir"

# Hold one file open for the whole run.
python3 -c 'import os,time; os.open("'"$CACHE"'/movies/open.bin", os.O_RDONLY); time.sleep(60)' &
HOLDER=$!
sleep 0.3

bash "$MOVER" --cache "$CACHE" --dest "$DEST" >"$ROOT/out" 2>"$ROOT/err"
rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc ($(cat "$ROOT/err"))"

[ ! -e "$CACHE/movies/nested/a.mkv" ] && [ "$(cat "$DEST/movies/nested/a.mkv")" = "movie" ] \
  && ok "ordinary file moved" || bad "ordinary file was not moved"
[ ! -d "$CACHE/movies/nested" ] && ok "empty source directory removed" || bad "empty directory left behind"
[ -d "$CACHE/movies" ] && ok "share directory kept" || bad "share directory removed"
[ -f "$CACHE/readme.txt" ] && ok "top-level file kept" || bad "top-level file moved"
[ -f "$CACHE/system/docker/layer" ] && [ ! -e "$DEST/system/docker/layer" ] \
  && ok "docker data-root kept" || bad "docker data-root was moved"
[ -f "$CACHE/.hidden/secret" ] && ok "hidden directory kept" || bad "hidden directory moved"
[ -f "$CACHE/movies/open.bin" ] && [ ! -e "$DEST/movies/open.bin" ] \
  && ok "open file kept" || bad "open file was moved"
[ "$(cat "$DEST/movies/exists.txt")" = "original" ] && [ "$(cat "$CACHE/movies/exists.txt")" = "already" ] \
  && ok "existing array file not overwritten" || bad "existing file was overwritten or source deleted"
grep -q "in use" "$ROOT/out" && ok "in-use is logged" || bad "in-use was not logged"
grep -q "RESULT moved=1 in_use=1 exists=1" "$ROOT/out" \
  && ok "result counts" || bad "result counts: $(grep RESULT "$ROOT/out")"

# A directory the mover cannot write into must not lose the source.
mkdir -p "$CACHE/locked" "$DEST/locked"
echo "precious" > "$CACHE/locked/file"
chmod 555 "$DEST/locked"
bash "$MOVER" --cache "$CACHE" --dest "$DEST" >"$ROOT/out2" 2>"$ROOT/err2"
chmod 755 "$DEST/locked"
[ -f "$CACHE/locked/file" ] && ok "failed copy keeps the source" || bad "source deleted after a failed copy"
grep -q "copy failed" "$ROOT/out2" && ok "failed copy is logged" || bad "failed copy was not logged"

# Dry run moves nothing.
echo "dry" > "$CACHE/movies/dry.txt"
bash "$MOVER" --dry-run --cache "$CACHE" --dest "$DEST" >"$ROOT/dry" 2>"$ROOT/dryerr"
[ -f "$CACHE/movies/dry.txt" ] && [ ! -e "$DEST/movies/dry.txt" ] \
  && ok "dry-run does not copy" || bad "dry-run copied"
grep -q "would move" "$ROOT/dry" && ok "dry-run says what it would do" || bad "dry-run was silent"

# Overlap is refused.
bash "$MOVER" --cache "$CACHE" --dest "$CACHE/movies" >"$ROOT/over" 2>"$ROOT/overerr"
[ "$?" -eq 2 ] && grep -q "refuse" "$ROOT/overerr" && ok "overlap refused" || bad "overlap was not refused"

kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true

if [ "$fail" -eq 0 ]; then
  echo "ALL PASSED"
else
  echo "SOME FAILED"
  echo "----- first run -----"
  cat "$ROOT/out" "$ROOT/err"
  exit 1
fi
