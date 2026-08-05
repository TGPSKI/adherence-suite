#!/usr/bin/env python3
"""Stop a run and reclaim everything it left behind. Deletes no results.

    python3 -m adherence.stoprun            # plan only
    python3 -m adherence.stoprun --yes      # do it

Restarting a run cleanly is a five-step ritual with three ways to get it
wrong, and every one of them has already happened here:

  * `pkill -f adherence.runner` kills the runner and ORPHANS its opencode
    grandchildren, which keep holding the GPU. Two of those left running
    turned a --jobs 3 probe into a five-way contention for three slots,
    visible only as unexplained slowness.
  * the paired proxy log gets forgotten, so the next run's calibration
    reads against the last run's calls.
  * the temp sandboxes stay behind, and the live view keeps rendering a
    torn-down run as though it were still going.

So the awkward part is one command.

**What this deliberately does NOT do is delete results.** Bundling "kill
the processes" with "remove the data" into a single verb is how a habit
formed for the safe purpose ends up performing the destructive one, and a
`--force` flag exists to be used. Clearing a results file stays a manual
`rm`: explicit, visible, and already a thing you know how to undo by not
typing it. This command is safe to run whenever, which is the point --
a reclaim step nobody hesitates over is a reclaim step that actually gets
run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from adherence import REPO_ROOT
from adherence.live import sweep
from adherence.runner import strays

RUNNER_MATCH = "adherence.runner"


def runner_groups() -> list[tuple[int, int, str]]:
    """(pid, pgid, argv) for every live runner."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,pgid,args"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, pgid, args = parts
        if RUNNER_MATCH not in args or " -eo " in args:
            continue
        if "python" not in args.split()[0] and "/python" not in args:
            continue
        try:
            found.append((int(pid), int(pgid), args[:110]))
        except ValueError:
            continue
    return found


def purpose_counts(path: Path) -> dict:
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "all_pass" in r:
                counts[r.get("purpose", "(unlabelled)")] = counts.get(
                    r.get("purpose", "(unlabelled)"), 0) + 1
    except OSError:
        pass
    return counts


def stop(groups, grace: float = 15.0) -> list[str]:
    """SIGTERM the whole group, then SIGKILL what ignored it."""
    notes = []
    if os.name != "posix":
        import contextlib
        for pid, _pgid, _a in groups:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
        return ["no process groups on this platform; killed the runner only"]
    import signal
    pgids = sorted({g[1] for g in groups})
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pgid in pgids:
            try:
                os.killpg(pgid, sig)
                alive.append(pgid)
            except ProcessLookupError:
                continue
            except OSError as e:
                notes.append(f"pgid {pgid}: {e}")
        if not alive:
            break
        deadline = time.time() + (grace if sig == signal.SIGTERM else 5.0)
        while time.time() < deadline:
            still = []
            for pgid in alive:
                try:
                    os.killpg(pgid, 0)
                    still.append(pgid)
                except ProcessLookupError:
                    pass
            if not still:
                return notes
            time.sleep(0.3)
        pgids = alive
        notes.append(f"escalating to SIGKILL for {pgids}")
    return notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true",
                    help="actually do it; without this only the plan prints")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="leave sandboxes and out-dirs in place")
    ap.add_argument("--out", default="",
                    help="results file to REPORT on (never deleted)")
    args = ap.parse_args()

    out = Path(args.out) if args.out else None
    if out is not None and not out.is_absolute():
        out = REPO_ROOT / out

    groups = runner_groups()
    left = strays()

    print("plan" if not args.yes else "stopping")
    print(f"  stop        {len(groups)} runner(s)"
          + (f", process group(s) {sorted({g[1] for g in groups})}"
             if groups else ""))
    for _pid, _pgid, a in groups[:4]:
        print(f"                {a}")
    print(f"  reclaim     {len(left)} unowned harness process(es)")
    if not args.keep_tmp:
        print("  sweep       sandboxes and out-dirs from finished/killed runs")
    print("  delete      nothing. results are never touched by this command")

    if out is not None:
        counts = purpose_counts(out)
        if counts:
            print(f"\n{out} holds {sum(counts.values())} row(s): {counts}")
            print("  clear it yourself when you mean to:")
            print(f"    rm {out}"
                  + (f" {str(out)[:-len('.jsonl')]}.proxy.jsonl"
                     if out.name.endswith(".jsonl") else ""))
            if counts.get("experiment"):
                print("  NOTE: some of those rows are marked "
                      "purpose=experiment — registered data, not "
                      "regenerable.")

    if not args.yes:
        print("\nnothing done. re-run with --yes")
        return 0

    if groups:
        for note in stop(groups):
            print(f"  ! {note}")
    # Give the group a moment to die before deciding what is a stray.
    time.sleep(1.0)
    remaining = strays()
    if remaining:
        print(f"  ! {len(remaining)} process(es) still running and unowned:")
        for pid, d in remaining[:6]:
            print(f"      {pid}  {d}")
        print("    kill them before starting a new run, or its timings will "
              "reflect contention it did not cause")

    if not args.keep_tmp:
        try:
            n, freed = sweep()
            print(f"  swept {n} director{'y' if n == 1 else 'ies'}, "
                  f"{freed / 1e6:.0f} MB")
        except RuntimeError as e:
            print(f"  ! sweep skipped: {e}")

    print("\nstopped and reclaimed. results untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
