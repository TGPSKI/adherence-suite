#!/usr/bin/env bash
# bench/ci-local.sh [job] — run CI's own steps locally, from the workflow file.
#
#   make ci-local            # the `validate` job
#   make ci-local JOB=lint
#
# Why this exists: twice, a CI step was pushed broken because it was checked
# by hand-copying it into a terminal instead of running the thing that was
# committed. Hand-copying loses the shell options (`bash -e`, and the
# `set -euo pipefail` inside each block), which is exactly what broke the
# first one — a pipeline whose left side is *supposed* to exit non-zero.
#
# This extracts each `run:` block from .github/workflows/ci.yml and executes
# it under the same shell GitHub uses (`bash -e`), so what runs here is what
# runs there. Steps using `uses:` (checkout, setup-python) are skipped —
# there is nothing local to reproduce.
#
# `${{ ... }}` expressions cannot be evaluated locally. A step containing one
# is skipped and reported, rather than silently mangled.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
JOB="${1:-${JOB:-validate}}"
cd "$HERE"

mapfile -t STEPS < <(python3 - "$JOB" <<'PY'
import json, re, sys

job = sys.argv[1]
text = open(".github/workflows/ci.yml").read()

# Deliberately not a YAML parser: this repo is stdlib-only, and the shapes
# used in the workflow are narrow and stable.
lines = text.splitlines()
in_job = False
job_indent = None
steps, name, buf, env, collecting = [], None, [], {}, None

def flush():
    if name and buf:
        steps.append({"name": name, "run": "\n".join(buf), "env": dict(env)})

for i, ln in enumerate(lines):
    m = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", ln)
    if m:
        if in_job:
            break
        in_job = m.group(1) == job
        continue
    if not in_job:
        continue

    m = re.match(r"^      - name: (.+?)\s*$", ln)
    if m:
        flush()
        name, buf, env, collecting = m.group(1), [], {}, None
        continue
    if re.match(r"^        uses:", ln):
        name = None
        continue
    # Block form MUST be tested first: `run: |` also matches `run: (.+)`,
    # which would turn every multi-line step into the one-liner "|".
    if re.match(r"^        run: \|\s*$", ln):
        buf, collecting = [], "run"
        continue
    m = re.match(r"^        run: (.+)$", ln)
    if m:
        buf = [m.group(1)]
        collecting = None
        continue
    if re.match(r"^        env:\s*$", ln):
        collecting = "env"
        continue
    if collecting == "run":
        if ln.startswith("          "):
            buf.append(ln[10:])
        elif ln.strip():
            collecting = None
    elif collecting == "env":
        m = re.match(r"^          ([A-Za-z_][A-Za-z0-9_]*): (.+)$", ln)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"')
        elif ln.strip():
            collecting = None
flush()
print(json.dumps(steps))
PY
)

python3 - "$JOB" "${STEPS[0]:-[]}" <<'PY' > /tmp/adh-ci-steps.sh
import json, shlex, sys
job, blob = sys.argv[1], sys.argv[2]
steps = json.loads(blob)
if not steps:
    print(f'echo "ci-local: no runnable steps found for job {job}"; exit 1')
    sys.exit(0)
print('rc=0')
for s in steps:
    if "${{" in s["run"]:
        print(f'echo "-- SKIP {s["name"]} (contains a GitHub expression)"')
        continue
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in s["env"].items())
    print(f'echo "-- {s["name"]}"')
    print(f'{envs} bash -e -c {shlex.quote(s["run"])} || {{ '
          f'echo "   FAILED: {s["name"]}"; rc=1; }}')
print('exit $rc')
PY

# `python` is the interpreter name on a GitHub runner; make it resolvable here.
if ! command -v python >/dev/null 2>&1; then
  SHIM="$(mktemp -d)"
  ln -s "$(command -v python3)" "$SHIM/python"
  export PATH="$SHIM:$PATH"
fi

bash /tmp/adh-ci-steps.sh
RC=$?
echo
[ "$RC" -eq 0 ] && echo "ci-local($JOB): all steps passed" \
                || echo "ci-local($JOB): FAILURES above"
exit "$RC"
