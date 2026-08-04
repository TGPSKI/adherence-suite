#!/usr/bin/env python3
"""Materialize each arm as a real folder, for looking at.

    python3 -m adherence.trees --mirror fixtures/cli-cli.git \\
        --base <commit> --arms-dir fixtures/cli-cli.arms \\
        --out fixtures/cli-cli.trees

At run time every trial already gets its own checkout, built the same way
and thrown away afterwards. That is correct for measurement and useless for
inspection: you cannot diff two arms, or read what the model was actually
handed, from a temp directory that no longer exists.

This writes one folder per arm, by the same two steps the runner uses —
`git clone --local --shared` at the base commit, then the arm overlay — so
what you read here is what a trial gets, not an approximation of it.

Clones share the mirror's object store, so four arms of a 164 MB repository
cost a few MB each rather than four full copies.

These trees are for reading. Runs never use them: the runner materializes
its own, because an agent mutates the tree it works in and a shared one
would leak the first trial's edits into the second.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from adherence.runner import apply_arm


def build(mirror: Path, base: str, arms_dir: Path, out: Path,
          arms: list[str]) -> list[tuple[str, int, int]]:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for arm in arms:
        dest = out / arm
        if dest.exists():
            shutil.rmtree(dest)
        subprocess.run(["git", "clone", "--local", "--shared", "--quiet",
                        "--no-checkout", str(mirror), str(dest)], check=True)
        subprocess.run(["git", "checkout", "--detach", "--quiet", base],
                       cwd=dest, check=True)
        note = apply_arm(dest, arms_dir, arm)
        # What the harness will see as "unchanged" once it commits the
        # baseline. Surfacing it here is the cheap way to notice that an
        # overlay dirtied something it should not have.
        surface = sorted(p.relative_to(dest).as_posix()
                         for p in dest.rglob("*")
                         if p.is_file() and ".git/" not in p.as_posix()
                         and (p.name in ("AGENTS.md", "CLAUDE.md")
                              or ".subagents/" in p.as_posix()))
        rows.append((arm, len(surface),
                     sum((dest / s).stat().st_size for s in surface)))
        print(f"  {arm}: {note}")
        for s in surface:
            print(f"       {s}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--arms-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--arms", default="a0,a1,a2,a3")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    rows = build(Path(args.mirror).resolve(), args.base,
                 Path(args.arms_dir).resolve(), Path(args.out), arms)

    print(f"\n{'arm':<6}{'files':>7}{'bytes':>10}   instruction surface")
    for arm, n, b in rows:
        print(f"{arm:<6}{n:>7}{b:>10,}")
    print(f"\nfolders under {args.out}/ — read them, diff them, do not run "
          f"from them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
