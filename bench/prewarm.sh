#!/usr/bin/env bash
# bench/prewarm.sh <mirror.git> <base-commit> [test-subset-cmd]
#
# H10 / design §16.2 B5. Two phases against one fixture:
#
#   warm   — materialize the fixture at base_commit and fetch its
#            dependencies ONCE, with network, into a fixture-local cache.
#   verify — materialize it again and run the test subset with the
#            network actually removed (a new network namespace, not a
#            deny-list) and the cache mounted read-only.
#
# Why a namespace and not the config's deny globs: `bench/opencode-bench.json`
# denies curl/wget/nc *as bash commands*. A `go test` or `pytest` that opens a
# socket itself is not a bash command and sails straight through. A fixture
# that "builds offline" because nothing tried to check is not a result, and
# §13 is explicit that the hermetic build must VETO a fixture rather than
# negotiate the sandbox open.
#
# The deny posture in opencode-bench.json is not touched by any of this.
set -uo pipefail
MIRROR="${1:?usage: prewarm.sh <mirror.git> <base-commit> [test-cmd]}"
BASE="${2:?need a base commit -- the baseline must not be 'whatever HEAD is'}"
TEST_CMD="${3:-}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"
MIRROR="$(cd "$(dirname "$MIRROR")" && pwd)/$(basename "$MIRROR")"
NAME="$(basename "$MIRROR" .git)"
CACHE="$HERE/fixtures/$NAME.cache"
mkdir -p "$CACHE"

materialize() {
  local dest="$1"
  git clone --local --shared --quiet --no-checkout "$MIRROR" "$dest"
  git -C "$dest" checkout --detach --quiet "$BASE"
}

detect() {
  local d="$1"
  [ -f "$d/go.mod" ]           && { echo go; return; }
  [ -f "$d/Cargo.toml" ]       && { echo rust; return; }
  [ -f "$d/package.json" ]     && { echo node; return; }
  [ -f "$d/pyproject.toml" ]   && { echo python; return; }
  echo unknown
}

echo "== phase 1: warm =="
WARM="$(mktemp -d)"
T0=$(date +%s.%N)
materialize "$WARM"
T1=$(date +%s.%N)
echo "materialize: $(echo "$T1 - $T0" | bc)s   (H8 target: < 2s)"

KIND="$(detect "$WARM")"
echo "ecosystem: $KIND"
case "$KIND" in
  go)     export GOMODCACHE="$CACHE/gomod" GOFLAGS=-mod=mod
          mkdir -p "$GOMODCACHE"
          ( cd "$WARM" && go mod download all ) ;;
  rust)   export CARGO_HOME="$CACHE/cargo"
          mkdir -p "$CARGO_HOME"
          ( cd "$WARM" && cargo fetch ) ;;
  node)   export npm_config_cache="$CACHE/npm"
          mkdir -p "$npm_config_cache"
          ( cd "$WARM" && npm ci --ignore-scripts ) ;;
  python) export PIP_CACHE_DIR="$CACHE/pip"
          mkdir -p "$PIP_CACHE_DIR"
          ( cd "$WARM" && python3 -m pip download -d "$PIP_CACHE_DIR" . ) ;;
  *)      echo "prewarm: unknown ecosystem -- warm the cache by hand" >&2 ;;
esac
WARM_RC=$?
echo "warm exit=$WARM_RC   cache: $(du -sh "$CACHE" 2>/dev/null | cut -f1)"
command rm -rf "$WARM"

[ -n "$TEST_CMD" ] || {
  echo
  echo "no test subset given -- warm phase only. Re-run with a <5 min"
  echo "deterministic subset to complete the H10 gate (design §8.1)."
  exit 0
}

echo
echo "== phase 2: verify offline =="
VER="$(mktemp -d)"
T0=$(date +%s.%N)
materialize "$VER"
T1=$(date +%s.%N)
MAT="$(echo "$T1 - $T0" | bc)"
echo "materialize: ${MAT}s"

# Read-only cache: if the test run needs to WRITE to the dependency cache
# it is still fetching, and it will fail here rather than in the middle of
# a 2,100-run grid.
chmod -R a-w "$CACHE" 2>/dev/null

case "$KIND" in
  go)     ENVV=(GOMODCACHE="$CACHE/gomod" GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local) ;;
  rust)   ENVV=(CARGO_HOME="$CACHE/cargo" CARGO_NET_OFFLINE=true) ;;
  node)   ENVV=(npm_config_cache="$CACHE/npm" npm_config_offline=true) ;;
  python) ENVV=(PIP_CACHE_DIR="$CACHE/pip" PIP_NO_INDEX=1) ;;
  *)      ENVV=() ;;
esac

# Loopback is brought UP inside the namespace, and nothing else is. Real
# test suites stand up local HTTP servers -- cli/cli's own factory tests
# use httptest -- and a namespace with lo DOWN fails those with connection
# errors that look exactly like a fixture needing the internet. That would
# veto good fixtures for a defect in this script. There is still no route
# off the host: loopback-only is the posture, not "some network".
echo "running with NO external network (loopback only): $TEST_CMD"
T0=$(date +%s.%N)
unshare -rn -- bash -c "ip link set lo up 2>/dev/null; \
  cd '$VER' && env $(printf '%q ' "${ENVV[@]}") $TEST_CMD"
RC=$?
T1=$(date +%s.%N)
DUR="$(echo "$T1 - $T0" | bc)"

chmod -R u+w "$CACHE" 2>/dev/null
command rm -rf "$VER"

echo
echo "test subset exit=$RC  duration=${DUR}s"
if [ "$RC" -eq 0 ]; then
  echo "H10: PASS -- builds and tests with the network removed and the"
  echo "dependency cache read-only. Materialization ${MAT}s (H8 target < 2s)."
  echo "Check the duration against the <5 min subset criterion (§8.1)."
else
  echo "H10: FAIL -- this fixture does not build offline. Per §13 that VETOES"
  echo "the fixture; do not relax the sandbox to accommodate it."
fi
exit "$RC"
