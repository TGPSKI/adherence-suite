#!/usr/bin/env bash
# bench/isolate.sh — run a command under the benchmark-only opencode config.
#
#   bench/isolate.sh make all MODEL=local/qwen36-35b-a3b-nvfp4 TRIALS=5
#
# Swaps XDG_CONFIG_HOME to a bench-local directory holding only
# opencode-bench.json, so opencode resolves its *global* config from there
# and your daily ~/.config/opencode is never loaded: no merge, no leaked
# ask-rules, no plugins. Confirm the swap took:
#
#   bench/isolate.sh opencode debug paths
#
# Set ADH_PROXY_LOG to route every inference call through the recording
# proxy — it starts on entry and stops on exit:
#
#   ADH_PROXY_LOG=runs/proxy.jsonl make all TRIALS=5
#
# This is LAYER TWO, not the isolation boundary. Config cannot stop a
# shell one-liner the deny globs don't anticipate. For the tier that
# actually contains, use a throwaway user or a container with no home
# mount (keep --network=host only if the endpoint is local):
#
#   podman run --rm --network=host -v "$PWD:/work" -w /work <img> bench/isolate.sh ...
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BENCH_XDG="${ADH_BENCH_XDG:-$HERE/.xdg}"
PORT="${ADH_PROXY_PORT:-8010}"

# The proxy records the tokens and round trips this suite exists to
# measure, and design §3.2 makes it AUTHORITATIVE over the adapter -- so
# it runs by default. It used to be opt-in behind ADH_PROXY_LOG, which
# meant the ordinary path produced numbers nothing had verified and the
# H4 agreement gate could not be checked at all for the run you were
# actually doing.
#
# The log is paired to the results file rather than shared, so a proxy log
# always belongs to exactly one run: runs/probe.jsonl -> runs/probe.proxy.jsonl.
# ADH_NO_PROXY=1 opts out (no endpoint, or deliberately measuring the
# adapter alone).
if [ -z "${ADH_PROXY_LOG:-}" ] && [ "${ADH_NO_PROXY:-0}" != "1" ]; then
  _out=""
  _next=0
  for _a in "$@"; do
    if [ "$_next" = "1" ]; then _out="$_a"; _next=0; fi
    if [ "$_a" = "--out" ]; then _next=1; fi
  done
  if [ -n "$_out" ]; then
    ADH_PROXY_LOG="${_out%.jsonl}.proxy.jsonl"
  else
    ADH_PROXY_LOG="$ROOT/runs/proxy.jsonl"
  fi
  export ADH_PROXY_LOG
fi

mkdir -p "$BENCH_XDG/opencode"
cp "$HERE/opencode-bench.json" "$BENCH_XDG/opencode/opencode.json"

# Register the custom agent pack as global agents under this config.
# Verify with: bench/isolate.sh opencode agent list
if [ -d "$HERE/agent" ]; then
  mkdir -p "$BENCH_XDG/opencode/agent"
  cp "$HERE"/agent/*.md "$BENCH_XDG/opencode/agent/" 2>/dev/null || true
fi

# Recording proxy. opencode-bench.json pins the provider's baseURL, so
# routing through the proxy is a one-line rewrite of the *copy* — the
# committed config keeps naming the real endpoint, because what is under
# measurement should not depend on an edit someone forgot to revert.
# Reentrant: `make all` wraps itself in this script, so a caller who also
# wraps (or exports ADH_PROXY_LOG for a whole session) nests two isolates.
# The inner one must not fight the outer for the port. ADH_PROXY set means
# an outer instance already started the proxy and rewrote the config.
if [ -n "${ADH_PROXY_LOG:-}" ] && [ -z "${ADH_PROXY:-}" ]; then
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/__proxy/health" >/dev/null 2>&1; then
    echo "isolate: something already listening on $PORT — refusing to start" >&2
    echo "isolate: stop it, or set ADH_PROXY_PORT to a free port" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$ADH_PROXY_LOG")"
  PYTHONPATH="$ROOT/src" python3 -m adherence.proxy --port "$PORT" \
    --upstream "${ADH_PROXY_UPSTREAM:-http://127.0.0.1:8000}" \
    --log "$ADH_PROXY_LOG" &
  PROXY_PID=$!
  trap 'kill "$PROXY_PID" 2>/dev/null || true; wait "$PROXY_PID" 2>/dev/null || true' EXIT

  for _ in $(seq 1 50); do
    curl -fsS -m 1 "http://127.0.0.1:$PORT/__proxy/health" >/dev/null 2>&1 && break
    sleep 0.1
  done
  curl -fsS -m 2 "http://127.0.0.1:$PORT/__proxy/health" >/dev/null 2>&1 || {
    echo "isolate: proxy failed to come up on $PORT" >&2; exit 1; }

  export ADH_PROXY="http://127.0.0.1:$PORT/v1"
  python3 - "$BENCH_XDG/opencode/opencode.json" "$ADH_PROXY" <<'PY'
import json, sys
path, url = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
for name, prov in cfg.get("provider", {}).items():
    prov.setdefault("options", {})["baseURL"] = url
json.dump(cfg, open(path, "w"), indent=2)
PY
  echo "isolate: recording every inference call to $ADH_PROXY_LOG" >&2
fi

# adapters/opencode.sh copies THIS config into each sandbox as project
# config. Without the export it would copy the committed one, which still
# names the real endpoint and wins the merge — producing an empty proxy
# log that looks exactly like a model that made no calls.
export ADH_BENCH_CONFIG="$BENCH_XDG/opencode/opencode.json"

# Everything run under isolate is this project, so isolate owns the
# package path. Callers cannot prefix `PYTHONPATH=src` on the command:
# this script runs "$@" directly, where a VAR=value prefix is argv[0],
# not an assignment.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

export XDG_CONFIG_HOME="$BENCH_XDG"
export XDG_DATA_HOME="$BENCH_XDG/data"
export XDG_STATE_HOME="$BENCH_XDG/state"
export XDG_CACHE_HOME="$BENCH_XDG/cache"
mkdir -p "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"

"$@"
