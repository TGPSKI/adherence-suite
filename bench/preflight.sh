#!/usr/bin/env bash
# bench/preflight.sh <model> -- one real turn through the exact path the
# suite uses (isolate.sh + opencode.sh's invocation), before spending
# trials on a broken model arg or config. Prints stdout/stderr in full.
#
# Usage:
#   bench/isolate.sh bench/preflight.sh local/qwen36-35b-a3b-nvfp4
set -uo pipefail
MODEL="${1:?usage: preflight.sh <model>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SB="$(mktemp -d)"
cd "$SB"
# Same rule as adapters/opencode.sh: honour the config isolate.sh
# actually installed, so a preflight run through the recording proxy
# exercises the path the suite will use, not a bypass of it.
cp "${ADH_BENCH_CONFIG:-$HERE/bench/opencode-bench.json}" opencode.json

echo "== opencode debug paths =="
opencode debug paths || true
echo
echo "== opencode debug config (permission.bash truncated) =="
opencode debug config 2>&1 | head -60 || true
echo
echo "== models known to this provider config =="
opencode models 2>&1 | grep -i "local\|qwen" || echo "(no match -- check model name/provider prefix)"
echo
echo "== single real turn: opencode run -m '$MODEL' 'say the word PREFLIGHT-OK and nothing else' =="
RUN_EXIT=0
opencode run -m "$MODEL" "say the word PREFLIGHT-OK and nothing else" \
  > stdout.txt 2> stderr.txt || RUN_EXIT=$?
echo "exit=$RUN_EXIT"
echo "--- stdout ---"; cat stdout.txt
echo "--- stderr ---"; cat stderr.txt
echo
if [ "$RUN_EXIT" -eq 0 ] && grep -qi "PREFLIGHT-OK" stdout.txt; then
  echo "PREFLIGHT: OK -- model reachable through this config, safe to run the suite."
else
  echo "PREFLIGHT: FAILED -- fix the model name / config / vLLM endpoint before running the suite."
  echo "Common causes: model arg missing 'local/' provider prefix; vLLM"
  echo "not listening on 127.0.0.1:8000; XDG_CONFIG_HOME not actually swapped"
  echo "(confirm with 'opencode debug paths' above -- config dir should be"
  echo "under bench/.xdg, not ~/.config/opencode)."
fi
rm -rf "$SB"
