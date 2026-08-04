#!/usr/bin/env bash
# adapters/opencode.sh <sandbox> <model> <prompt-file> <out-dir>
#
# Runs `opencode run` in the sandbox, exports the session, converts the
# export JSON to the normalized transcript. Session-id resolution and the
# export shape vary across opencode versions — the conversion script is
# defensive, and the two marked spots below are where to adjust if your
# installed version differs (consult help/opencode-help.md).
#
# Unattended runs need permissions resolved: either a benchmark-only
# config allowing bash within the sandbox, or --auto inside a throwaway
# container/user. NEVER --auto against your primary account config.
set -uo pipefail
SANDBOX="$1"; MODEL="$2"; PROMPT_FILE="$3"; OUT="$4"; TARGET_AGENT="${5:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

cd "$SANDBOX"

# Belt-and-suspenders: project-level config in the sandbox. Primary
# isolation is bench/isolate.sh's XDG_CONFIG_HOME swap; this covers the
# case where the runner is invoked without the wrapper. Skipped if a
# scenario fixture ships its own opencode.json.
#
# ADH_BENCH_CONFIG is set by isolate.sh and may carry a rewritten
# baseURL pointing at the recording proxy. Prefer it: the committed
# opencode-bench.json names the real endpoint, so copying that one here
# would override the proxy for exactly the calls being measured.
BENCH_CONFIG="${ADH_BENCH_CONFIG:-$HERE/bench/opencode-bench.json}"
if [ ! -f opencode.json ] && [ -f "$BENCH_CONFIG" ]; then
  cp "$BENCH_CONFIG" opencode.json
fi

# ADJUST(1): add --auto here only in a sandboxed environment.
AGENT_ARGS=()
if [ -n "$TARGET_AGENT" ]; then
  AGENT_ARGS=(--agent "$TARGET_AGENT")
fi

# --format json: stdout becomes an NDJSON event stream carrying one
# step_finish per inference call, with per-call tokens and cache fields
# (design §3.1). Verified on opencode 1.18.10. The human-readable format
# carries none of that.
RUN_EXIT=0
opencode run --format json -m "$MODEL" "${AGENT_ARGS[@]}" "$(cat "$PROMPT_FILE")" \
  > "$OUT/stdout.txt" 2> "$OUT/stderr.txt" || RUN_EXIT=$?

if [ "$RUN_EXIT" -ne 0 ]; then
  echo "opencode-adapter: opencode run exited $RUN_EXIT" >&2
  echo "--- stdout ---" >&2; tail -n 40 "$OUT/stdout.txt" >&2 || true
  echo "--- stderr ---" >&2; tail -n 40 "$OUT/stderr.txt" >&2 || true
  # Fail the adapter loudly. runner.py records this as a clean
  # "adapter" check failure with the captured stderr as evidence,
  # instead of a silent empty-transcript grading pass.
  exit "$RUN_EXIT"
fi

# Session id comes from the stream's own events, not from
# `opencode session list | head -1`. That listing is global and ordered by
# recency, so under any parallel execution (H11) it hands back another
# trial's session -- silently grading run A against run B's transcript.
SID="$(head -1 "$OUT/stdout.txt" 2>/dev/null \
        | grep -oE '"sessionID":"ses_[A-Za-z0-9]+"' \
        | grep -oE 'ses_[A-Za-z0-9]+' | head -1 || true)"
if [ -z "$SID" ]; then
  SID="$(opencode session list 2>/dev/null | grep -oE 'ses_[A-Za-z0-9]+' | head -1 || true)"
fi
if [ -n "$SID" ]; then
  opencode export "$SID" > "$OUT/export.json" 2>/dev/null || true

  # Subagents run in their own sessions, invisible to both the root
  # stream and the root export. Export each so parent+child token totals
  # exist at all -- see adapters/opencode_children.py for the measured
  # size of the gap. Best-effort: no children, no directory, no harm.
  mkdir -p "$OUT/children"
  PYTHONPATH="$HERE/src" python3 -m adherence.adapters.children "$SID" 2>/dev/null \
  | while IFS="$(printf '\t')" read -r CSID CAGENT; do
      [ -n "$CSID" ] || continue
      opencode export "$CSID" > "$OUT/children/${CAGENT:-sub}.$CSID.json" \
        2>/dev/null || true
    done
fi

# The export is now secondary: it supplies only the per-message `agent`
# field, which the stream does not carry. If the stream is empty (an
# opencode version without --format json), fall back to the export walker.
if [ -s "$OUT/stdout.txt" ]; then
  PYTHONPATH="$HERE/src" python3 -m adherence.adapters.opencode_stream \
    "$OUT/stdout.txt" "$OUT/export.json" "$OUT/stdout.txt" "$OUT" \
    "$OUT/children"
else
  PYTHONPATH="$HERE/src" python3 -m adherence.adapters.opencode_export \
    "$OUT/export.json" "$OUT/stdout.txt" "$OUT"
fi
