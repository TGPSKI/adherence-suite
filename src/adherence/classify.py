#!/usr/bin/env python3
"""Decide which grader can judge each existing task, by compiling.

    python3 -m adherence.classify --tasks scenarios-pr/cli-cli \\
        --mirror fixtures/cli-cli.git

`mkpr` makes this decision for new tasks. Task sets extracted before the
distinction existed carry no `grader` field, and this backfills it in
place.

The decision cannot be made by reading diffs. A static scan for
identifiers the PR's code adds and its tests reference finds the obvious
cases and **misses the rest**: measured against the validation grid, it
flagged 13393 and 13967 correctly but missed 13624 (`cannot use
verifier.noVerifierSet()` -- a signature change on an existing type) and
13675 (`field.onSearchDone.Store undefined` -- a field added to an
existing struct). Neither adds a top-level declaration, so neither shows
up in a declaration scan, and both are just as unpassable.

The compiler is the only authority on whether a test compiles. So this
does what the runtime does: check the PR's tests out onto the parent tree
and build them. A build error naming something undefined means the test
cannot compile against ANY implementation that chose different names, and
the task goes to the CLI grader. Anything else -- tests that build and
fail on assertions -- stays with the unit grader.

Costs one build per task and no GPU.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from adherence.mkpr import ECOSYSTEMS, names_the_test_requires, packages_for, warm_at


def classify_one(mirror: Path, task: dict, env: dict, eco: dict) -> tuple[str, list[str], str]:
    """(grader, invented_symbols, note) for one task."""
    tests = task.get("test_files") or []
    if not tests:
        return "unit", [], "no test files"
    pkgs = packages_for(tests, eco)
    with tempfile.TemporaryDirectory(prefix="adh-classify-") as d:
        try:
            subprocess.run(["git", "clone", "--local", "--shared", "--quiet",
                            "--no-checkout", str(mirror), d],
                           check=True, capture_output=True, timeout=600)
            subprocess.run(["git", "checkout", "--detach", "--quiet",
                            task["base_commit"]], cwd=d, check=True,
                           capture_output=True, timeout=600)
            warm_at(d, env, eco)
            subprocess.run(["git", "checkout", task["merge_commit"], "--"]
                           + tests, cwd=d, check=True, capture_output=True,
                           timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            return "unit", [], f"could not materialize: {str(e)[-120:]}"

        # `go vet` builds the test binary without running it, which is the
        # cheapest way to ask the compiler the only question that matters.
        cmd = "go vet " + " ".join(pkgs) if pkgs else "go build ./..."
        try:
            r = subprocess.run(cmd, shell=True, cwd=d, env=env,
                               capture_output=True, text=True, timeout=900)
        except subprocess.SubprocessError:
            return "unit", [], "vet timed out"
        if r.returncode == 0:
            return "unit", [], "tests compile at the parent"
        invented = names_the_test_requires(r.stdout + r.stderr)
        if invented:
            return "cli", invented, "tests name what the fix introduces"
        return "unit", [], "compile failure, but not a missing identifier"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--mirror", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    gt = Path(args.tasks) / "ground-truth.jsonl"
    if not gt.is_file():
        sys.exit(f"no ground-truth.jsonl in {args.tasks}")
    tasks = [json.loads(x) for x in gt.read_text().splitlines() if x.strip()]
    if args.only:
        want = {int(x) for x in args.only.split(",")}
        tasks = [t for t in tasks if t["pr"] in want]

    mirror = Path(args.mirror).resolve()
    eco = ECOSYSTEMS["go"]
    import os
    env = dict(os.environ)
    env["GOFLAGS"] = "-mod=mod"

    counts = {"unit": 0, "cli": 0}
    for t in tasks:
        grader, syms, note = classify_one(mirror, t, env, eco)
        t["grader"] = grader
        t["invented_symbols"] = syms
        counts[grader] += 1
        detail = f" [{', '.join(syms[:3])}]" if syms else ""
        print(f"  #{t['pr']:<7}{grader:<5}{note}{detail}", file=sys.stderr,
              flush=True)

    all_tasks = [json.loads(x) for x in gt.read_text().splitlines() if x.strip()]
    by_pr = {t["pr"]: t for t in tasks}
    with open(gt, "w") as f:
        for t in all_tasks:
            f.write(json.dumps(by_pr.get(t["pr"], t)) + "\n")
    print(f"\n{counts['unit']} unit-graded, {counts['cli']} cli-graded "
          f"-> {gt}", file=sys.stderr)
    print("re-run `make mkscenarios` so the scenarios carry the choice",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
