#!/usr/bin/env python3
"""What each arm is, how they differ, and the rules the run is bound by.

    python3 -m adherence.design           # one-shot text listing

Orientation, like adherence.tasks, and for the same reason: an operator
reading "a3 0.82x vs a2" has to know what a2 and a3 actually are before
the number means anything, and the answer currently lives in a 600-line
registration document. The arm surfaces are read off disk rather than
described, so this cannot drift from what a trial is actually handed.

Static: reads the arms directory and the registered constants. Never a run.
Stdlib only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from adherence import REPO_ROOT
from adherence.suitedata import ARMS

# Files an agent is handed without asking. Anything under .subagents/ is
# loaded on demand by routing, which is the claim under test.
ALWAYS_LOADED = ("AGENTS.md", "CLAUDE.md")

# Why each arm exists, in the terms a reader of a result needs. The
# registry in suitedata carries the one-line role; this is what the arm is
# FOR and what its number means when compared.
ARM_PURPOSE = {
    "a0": "The floor. No instruction surface at all, so it answers what any "
          "instruction costs and buys before asking which one is better. A "
          "treatment that cannot beat this is not worth its own bytes.",
    "a1": "The practical control: the repository's own AGENTS.md, recovered "
          "verbatim from the pinned commit and never authored here. This is "
          "what a real team already has, so it is the comparison anyone "
          "deciding whether to adopt the pattern actually cares about.",
    "a2": "The scientific control: the same content as the treatment, "
          "delivered as one file instead of a router plus modules. It "
          "isolates DELIVERY from CONTENT -- without it, a win could just "
          "mean the treatment's text is better written.",
    "a3": "The pattern as shipped: a small always-loaded router that points "
          "at bounded contexts loaded on demand. The primary outcome is its "
          "marginal input tokens against a2.",
    "a4": "The same context set delivered by spawning a context-scoped "
          "subagent rather than inline. Answers E3, the claim that subagent "
          "handoff is free, on parent+child totals rather than the parent's.",
    "a5": "The minimal alternative raised in the original discussion: a "
          "short AGENTS.md plus tools, no routing. Guards against the "
          "pattern being credited for what a few good sentences would do.",
}

# The rules the run is bound by, each with the failure it exists to
# prevent. Wording is deliberately close to docs/EVAL.md so the two cannot
# drift into saying different things.
GROUND_RULES = [
    ("Primary outcome",
     "Marginal input tokens in the directed arm against the content-matched "
     "monolith (a2), paired on scenario, geometric mean, 95% CI from a "
     "cluster bootstrap over scenarios."),
    ("Cost is meaningless alone",
     "No cost number is reportable without the pass rate beside it. Cheaper "
     "and worse is a trade, not a win, and the honest output is the (cost, "
     "success) plane."),
    ("Calibration band",
     "A scenario counts only if its pooled pass rate lands in [0.25, 0.80]. "
     "Outside that it cannot discriminate between arms. Dropped scenarios "
     "are logged and never re-authored to make them discriminate."),
    ("Harness faults are not model failures",
     "An adapter that did not complete, or a transcript that failed schema "
     "validation, is `ungradeable` and excluded -- never scored as a model "
     "that got it wrong."),
    ("Validation never becomes evidence",
     "Every record carries `purpose`. The registered analysis reads only "
     "rows marked `experiment`; anything unlabelled is treated as "
     "validation, because the safe reading of no label is not 'this is real "
     "data'."),
    ("Two grading tiers, never pooled",
     "Unit-graded tasks are the primary evidence. Tasks graded at the "
     "command line (Amendment 2) are reported as a separate tier, because "
     "a flag comparison is a weaker signal than a passing unit test."),
    ("The proxy is authoritative",
     "On any disagreement about tokens or round trips, the recording proxy "
     "wins over the adapter. It counts calls by construction: a round trip "
     "is a request it handled."),
    ("Deviations are disclosed, not forbidden",
     "Any departure from the registration is stated in the results with "
     "what changed and why. Undisclosed deviation is the only thing "
     "actually prohibited."),
]


def _files(d: Path):
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


def load(arms_dir: Path | None = None) -> list[dict]:
    """One record per materialized arm, measured off disk."""
    arms_dir = arms_dir or (REPO_ROOT / "fixtures" / "cli-cli.arms")
    out = []
    for key, (name, role) in ARMS.items():
        if key == "-":
            continue
        d = arms_dir / key
        files = _files(d)
        always = [f for f in files if f.name in ALWAYS_LOADED]
        ondemand = [f for f in files
                    if ".subagents" in f.as_posix() and f.suffix == ".md"]
        blob = b"".join(f.read_bytes() for f in always)
        out.append({
            "arm": key, "name": name, "role": role,
            "purpose": ARM_PURPOSE.get(key, ""),
            "present": bool(files),
            "always_bytes": sum(f.stat().st_size for f in always),
            "always_files": [f.name for f in always],
            "ondemand_files": [f.name for f in ondemand],
            "ondemand_bytes": sum(f.stat().st_size for f in ondemand),
            "sha8": hashlib.sha256(blob).hexdigest()[:8] if blob else "",
        })
    return out


def deltas(rows, ref="a1") -> dict:
    """Always-loaded bytes relative to the reference arm."""
    base = next((r["always_bytes"] for r in rows if r["arm"] == ref), 0)
    return {r["arm"]: r["always_bytes"] - base for r in rows}


def main():
    rows = load()
    d = deltas(rows)
    print(f"{'arm':<5}{'name':<22}{'always':>9}{'vs a1':>9}{'on demand':>11}"
          f"  role")
    for r in rows:
        if not r["present"]:
            print(f"{r['arm']:<5}{r['name']:<22}{'not built':>9}")
            continue
        print(f"{r['arm']:<5}{r['name']:<22}{r['always_bytes']:>9,}"
              f"{d[r['arm']]:>+9,}{r['ondemand_bytes']:>11,}  {r['role']}")
    print("\nground rules")
    for k, v in GROUND_RULES:
        print(f"  {k}\n      {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
