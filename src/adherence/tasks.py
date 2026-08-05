#!/usr/bin/env python3
"""What each scenario asks for, and how it will be judged.

    python3 -m adherence.tasks            # one-shot text listing

Orientation, not results. An operator watching a grid sees scenario ids
and pass rates and has no way to tell what `cli-cli-13418` actually wants,
which files the answer lives in, or why one task is judged by unit tests
and the next at the command line. That gap is where a harness artifact
gets mistaken for a model failure -- the whole reason Amendment 2 exists.

Everything here is static: it reads the scenario directory, never a run.
Stdlib only.
"""
from __future__ import annotations

import json
from pathlib import Path

from adherence import REPO_ROOT
from adherence.runner import load_yamlish

# What each grader actually decides, in the terms an operator needs when
# reading a verdict rather than in the terms the code is written in.
GRADER_MEANING = {
    "unit": ("the PR's own unit tests",
             "The agent's tree is built and the PR's `_test.go` files are "
             "applied on top, then the affected packages run. The tests "
             "compile against any correct implementation, so internal "
             "naming does not matter."),
    "cli": ("the PR's own binary, flag-for-flag",
            "These tests name symbols the PR introduces, so they cannot "
            "compile against an implementation that chose different "
            "identifiers -- passing them would require guessing the "
            "maintainers' names. The merge commit is built as an oracle "
            "and the agent's binary is compared to it at the command "
            "line, where the contract is the one the PR body already "
            "gave the agent (Amendment 2)."),
}


def load(root: Path | None = None) -> list[dict]:
    """One record per scenario, sorted by id."""
    root = root or REPO_ROOT
    out = []
    for d in sorted((root / "scenarios").glob("*")):
        y = d / "scenario.yaml"
        if not y.is_file():
            continue
        try:
            meta = load_yamlish(y)
        except (OSError, ValueError):
            continue
        task = {}
        t = d / "task.json"
        if t.is_file():
            try:
                task = json.loads(t.read_text())
            except (OSError, json.JSONDecodeError):
                task = {}
        prompt = str(meta.get("prompt", ""))
        title = next((ln.strip() for ln in prompt.splitlines() if ln.strip()),
                     "(no title)")
        out.append({
            "id": d.name,
            "category": str(meta.get("category", "")),
            "fixture": str(meta.get("fixture", "")),
            "base_commit": str(meta.get("base_commit", ""))[:12],
            "timeout": int(meta.get("timeout", 0) or 0),
            "title": title,
            "prompt": prompt,
            "prompt_lines": len(prompt.splitlines()),
            "pr": task.get("pr", ""),
            "grader": task.get("grader", "unit" if task else ""),
            "invented": task.get("invented_symbols") or [],
            "code_files": task.get("code_files") or [],
            "test_files": task.get("test_files") or [],
            "test_cmd": task.get("test_cmd", ""),
            "dirs": sorted({str(Path(p).parent)
                            for p in (task.get("code_files") or [])}),
        })
    return out


def summarize(rows) -> dict:
    return {
        "n": len(rows),
        "unit": sum(1 for r in rows if r["grader"] == "unit"),
        "cli": sum(1 for r in rows if r["grader"] == "cli"),
        "synthetic": sum(1 for r in rows if not r["pr"]),
    }


def main():
    rows = load()
    if not rows:
        print("no scenarios found")
        return 0
    s = summarize(rows)
    print(f"{s['n']} scenarios — {s['unit']} unit-graded, {s['cli']} "
          f"cli-graded, {s['synthetic']} synthetic\n")
    print(f"{'scenario':<20}{'PR':>7}{'grader':>8}{'files':>7}{'dirs':>6}  what it asks for")
    for r in rows:
        print(f"{r['id']:<20}{str(r['pr'] or '—'):>7}{r['grader'] or '—':>8}"
              f"{len(r['code_files']):>7}{len(r['dirs']):>6}  {r['title'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
