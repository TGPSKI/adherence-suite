#!/usr/bin/env python3
"""Per-arm instruction floor: what an arm costs before the agent does anything.

    python3 -m adherence.floors runs/val-grid.jsonl --arms-dir fixtures/cli-cli.arms

Every arm hands the model a different always-loaded instruction surface,
and that surface is billed on every call. A treatment that reads fewer
files but ships a larger preamble can lose on total tokens while winning on
everything the preamble was for -- and a total-token comparison alone
cannot tell those apart. docs/EVAL.md answers that with
`tok_in_marginal = tok_in_billed - floor x calls`, which is only correct
with a *measured* per-arm floor; exclusion criterion 4 refuses the whole
comparison when the floor comes out wrong. This is where that floor comes
from.

The floor is measured, not computed from file sizes. For a given scenario
the task prompt is byte-identical across arms, so the difference between
two arms on **call 1** is the instruction surface and nothing else: no
tool results yet, no exploration, no model choices. That is what
`first_call_input` records and what this reads.

The cross-check is the point of the tool. A measured floor has to track
the on-disk surface at a constant bytes-per-token rate; the tokenizer does
not care which arm produced the bytes. If two arms disagree on that rate,
then something other than the instruction surface varied on call 1 -- a
different prompt, a leaked tool result, an adapter that injects its own
preamble -- and the number is not a floor. The check is cheap and it fails
loudly, which is the only reason to trust the number it guards.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

# Files an agent is handed without asking. Anything under .subagents/ is
# loaded on demand, by routing, and so is not part of the floor -- that
# distinction is the whole claim under test, and folding it in here would
# assume the answer.
ALWAYS_LOADED = ("AGENTS.md", "CLAUDE.md")

# Agreement the bytes-per-token rate must hold across arms. Markdown
# tokenizes at roughly 3.5-4 bytes/token; what matters is not the value but
# that every arm reports the *same* one.
RATE_TOLERANCE = 0.05


def surface_bytes(arms_dir: Path, arm: str) -> int:
    d = arms_dir / arm
    if not d.is_dir():
        return 0
    return sum(p.stat().st_size for p in d.rglob("*")
               if p.is_file() and p.name in ALWAYS_LOADED)


def measure(rows) -> dict:
    """Median first_call_input per (arm, scenario)."""
    by = defaultdict(list)
    for r in rows:
        v = (r.get("metrics") or {}).get("first_call_input")
        if v:
            by[(r.get("arm", "-"), r["scenario"])].append(v)
    return {k: st.median(v) for k, v in by.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--arms-dir", help="cross-check floors against on-disk bytes")
    ap.add_argument("--ref", default="a1",
                    help="arm the deltas are taken against (default: the "
                         "practical control)")
    args = ap.parse_args()

    rows = []
    for f in args.files:
        for line in Path(f).read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))

    med = measure(rows)
    if not med:
        sys.exit("no first_call_input on any row. That metric was added after "
                 "some runs; re-run, or measure floors on a newer file.")

    arms = sorted({a for a, _ in med})
    scens = sorted({s for _, s in med})
    ref = args.ref
    if ref not in arms:
        sys.exit(f"reference arm {ref!r} not present; have {arms}")

    print(f"{'scenario':<20}" + "".join(f"{a:>10}" for a in arms))
    deltas = defaultdict(list)
    for s in scens:
        cells = {a: med.get((a, s)) for a in arms}
        print(f"{s:<20}" + "".join(
            f"{cells[a]:>10,.0f}" if cells[a] else f"{'-':>10}" for a in arms))
        for a in arms:
            if a != ref and cells[a] and cells[ref]:
                deltas[a].append(cells[a] - cells[ref])

    print(f"\nfloor delta vs {ref}, per scenario (median [min..max], n)")
    summary = {}
    for a in arms:
        if a == ref:
            continue
        d = deltas[a]
        if not d:
            print(f"  {a}: no scenario has both arms")
            continue
        summary[a] = st.median(d)
        print(f"  {a}: {st.median(d):>+9,.0f} tok  "
              f"[{min(d):+,.0f}..{max(d):+,.0f}]  n={len(d)}")

    if not args.arms_dir:
        print("\n(pass --arms-dir to cross-check against the on-disk surface)")
        return 0

    # --- the cross-check ------------------------------------------------
    arms_dir = Path(args.arms_dir)
    ref_b = surface_bytes(arms_dir, ref)
    print("\non-disk always-loaded surface, and implied bytes/token")
    print(f"  {ref}: {ref_b:>8,} B   (reference)")
    rates = []
    for a, dtok in summary.items():
        db = surface_bytes(arms_dir, a) - ref_b
        if not dtok:
            continue
        rate = db / dtok
        rates.append(rate)
        print(f"  {a}: {surface_bytes(arms_dir, a):>8,} B   "
              f"{db:>+8,} B / {dtok:>+8,.0f} tok = {rate:.2f} B/tok")

    if len(rates) < 2:
        print("\nonly one arm to compare; the rate check needs two.")
        return 0

    spread = (max(rates) - min(rates)) / st.mean(rates)
    if spread <= RATE_TOLERANCE:
        print(f"\nOK: arms agree on {st.mean(rates):.2f} B/tok to "
              f"{spread:.1%} (tolerance {RATE_TOLERANCE:.0%}). The measured "
              f"floor is the instruction surface.")
        return 0
    print(f"\nFAIL: bytes/token disagrees across arms by {spread:.1%} "
          f"(tolerance {RATE_TOLERANCE:.0%}): {[f'{r:.2f}' for r in rates]}.\n"
          f"Call 1 is carrying something besides the instruction surface -- "
          f"a differing prompt, an adapter preamble, or a tool result that "
          f"arrived before the first billed call. These are not floors.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
