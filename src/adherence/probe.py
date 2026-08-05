#!/usr/bin/env python3
"""Read a difficulty probe and say whether the fixture can support an eval.

    python3 -m adherence.probe runs/probe.jsonl

The probe runs one arm — the repository's own instruction file — over the
candidate tasks. It needs no generated contexts and no arm materialization,
which is the point: it answers the question that would otherwise be
answered at the calibration gate, after the expensive work.

The registered band is [0.25, 0.80] pooled pass rate. Outside it a task
cannot show a cost/quality trade: at ceiling there is nothing to trade, at
floor there are no successful trials to condition cost on.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from adherence.suitedata import is_ungradeable

BAND = (0.25, 0.80)
MIN_USABLE = 6          # registered stopping rule in docs/EVAL.md


def main() -> int:
    paths = sys.argv[1:] or ["runs/probe.jsonl"]
    rows = []
    for p in paths:
        try:
            with open(p) as fh:
                rows += [json.loads(x) for x in fh if x.strip()]
        except OSError as e:
            print(f"probe: {e}", file=sys.stderr)
            return 1
    if not rows:
        print("probe: no results")
        return 1

    by = defaultdict(list)
    for r in rows:
        by[r["scenario"]].append(r)

    keep, ceiling, floor, broken = [], [], [], []
    for sid, rs in sorted(by.items()):
        # Two defects lived here, and together they kept a dead scenario.
        #
        # The adapter check reports a ceiling hit as `ungradeable`, not
        # `fail`, so matching on "fail" counted zero harness faults for a
        # run that had three -- the summary printed "0 harness" while three
        # trials had been killed at 2700s.
        #
        # And the rate divided by every trial, so those three counted as
        # model failures: cli-cli-13057 read 40% (2 of 5) and landed inside
        # the band, when its gradeable trials were 2 of 2 and it belongs
        # outside. The registration is explicit -- "harness faults are not
        # model failures" -- and this is the surface that prints the go/no-go
        # verdict, so it was the worst of the three places to get it wrong.
        broken_n = sum(1 for r in rs if is_ungradeable(r))
        graded = [r for r in rs if not is_ungradeable(r)]
        rate = (sum(1 for r in graded if r["all_pass"]) / len(graded)
                if graded else 0.0)
        row = (sid, rate, len(graded), broken_n)
        if not graded:
            broken.append(row)
        elif rate > BAND[1]:
            ceiling.append(row)
        elif rate < BAND[0]:
            floor.append(row)
        else:
            keep.append(row)

    print(f"{'scenario':<22}{'pass@1':>8}{'trials':>8}  verdict")
    for group, label in ((keep, "KEEP"), (ceiling, "ceiling — drop"),
                         (floor, "floor — drop"), (broken, "harness broke")):
        for sid, rate, n, _ in group:
            print(f"{sid:<22}{rate:>7.0%}{n:>8}  {label}")

    total = len(by)
    print(f"\n{len(keep)} of {total} tasks land in "
          f"[{BAND[0]:.2f}, {BAND[1]:.2f}]")
    print(f"  {len(ceiling)} ceiling · {len(floor)} floor · {len(broken)} harness")
    if broken:
        print("\nharness failures are not task difficulty — fix those and "
              "re-probe before drawing any conclusion about the fixture.")
    print()
    if len(keep) >= 10:
        print("VERDICT: proceed. Comfortable margin over the ~10 target.")
    elif len(keep) >= MIN_USABLE:
        print(f"VERDICT: proceed with thin margin ({len(keep)} usable). "
              f"Consider a second fixture before generating contexts — it "
              f"also unlocks the N-scaling claim a single fixture cannot reach.")
    else:
        print(f"VERDICT: stop. {len(keep)} usable is under the registered "
              f"minimum of {MIN_USABLE}. The registered response is to report "
              f"this, not to widen the band. Nothing expensive has been spent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
