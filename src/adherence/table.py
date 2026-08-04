#!/usr/bin/env python3
"""One-shot, pipe-friendly snapshot of the results matrix.

    python3 tools/table.py                     # every cell
    python3 tools/table.py 'a3/*'              # filtered
    make table                                 # same
    NOCOLOR=1 make table | pbcopy               # paste-friendly

Same loader as the TUI (tools/suitedata.py), so the two surfaces cannot
disagree about a number — that is the whole reason the loader is its own
module. Where `report.py` produces the publishable markdown scoreboard,
this is the thing you run while a battery is going. Stdlib only.
"""
from __future__ import annotations

import os
import sys

from adherence import suitedata as sd

NOCOLOR = bool(os.environ.get("NOCOLOR"))
G, Y, R, C0, DIM, B, X = ("\033[32m", "\033[33m", "\033[31m", "\033[36m",
                          "\033[2m", "\033[1m", "\033[0m")
if NOCOLOR:
    G = Y = R = C0 = DIM = B = X = ""


def pcol(p):
    return G if p >= 80 else Y if p >= 25 else R


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FILTER", "")
    ref = os.environ.get("REF", "a1")
    proxy = os.environ.get("PROXY")

    cells = sd.load_cells(pattern=pattern)
    if not cells:
        print("no results match" if pattern else
              "no results found — run the suite first (make all)")
        return 1

    print(f"{B}cells{X}  {DIM}(trials aggregated per arm/scenario){X}")
    print(f"{DIM}{'arm/scenario':<20}{'trials':>7}{'pass@1':>8}{'tok_in':>10}"
          f"{'if pass':>10}{'calls':>7}{'probes':>8}{'abnd':>6}  failing{X}")
    for c in cells:
        print(f"{c['tag']:<20}{c['trials']:>7}"
              f"{pcol(c['pass_rate'])}{c['pass_rate']:>7.0f}%{X}"
              f"{C0}{c['tok']:>10,.0f}{X}{DIM}{c['tok_won']:>10,.0f}{X}"
              f"{c['calls']:>7.0f}{c['probes']:>8.0f}"
              f"{(R if c['abandoned'] else DIM)}{c['abandoned']:>6}{X}"
              f"  {(R if c['fails'] else DIM)}{', '.join(c['fails']) or '—'}{X}")

    print(f"\n{B}arms{X}  {DIM}ratios paired on scenario, geometric, "
          f"vs {ref}{X}")
    print(f"{DIM}{'arm':<5}{'name':<20}{'scen':>5}{'pass@1':>8}{'tok_in':>10}"
          f"{'calls':>7}{'tok ratio':>11}{'call ratio':>11}   role{X}")
    for r in sd.arm_rollup(cells):
        tr, tn = sd.paired_ratio(cells, r["arm"], ref, "tok")
        cr, _ = sd.paired_ratio(cells, r["arm"], ref, "calls")
        if r["arm"] == ref:
            rt, rc = f"{'reference':>11}", f"{'':>11}"
        else:
            rt = f"{tr:>10.3f}×" if tr else f"{'—':>11}"
            rc = f"{cr:>10.3f}×" if cr else f"{'—':>11}"
        print(f"{B}{r['arm']:<5}{X}{r['name']:<20}{r['scenarios']:>5}"
              f"{pcol(r['pass_rate'])}{r['pass_rate']:>7.0f}%{X}"
              f"{C0}{r['tok']:>10,.0f}{X}{r['calls']:>7.0f}{rt}{rc}"
              f"   {DIM}{r['role']}{X}")

    front = sd.pareto_front(cells)
    if front:
        print(f"\n{B}Pareto frontier{X}  {DIM}nothing beats these on both "
              f"cost and pass rate (§5){X}")
        for c in sorted((c for c in cells if c["tag"] in front),
                        key=lambda c: -c["pass_rate"]):
            print(f"  {G}*{X} {c['tag']:<20}"
                  f"{pcol(c['pass_rate'])}{c['pass_rate']:>6.0f}%{X}"
                  f"{C0}{c['tok']:>10,.0f}{X} tokens"
                  f"{c['calls']:>6.0f} calls")

    if len({c["arm"] for c in cells}) > 1:
        print(f"\n{DIM}A ratio below 1.000 means cheaper than the reference. "
              f"Read it beside pass@1: a cost table without an adjacent "
              f"pass-rate column is not publishable (§5).{X}")

    if proxy:
        rows = sd.load_rows()
        cal = sd.calibration(rows, sd.load_proxy(
            proxy if os.path.isabs(proxy) else os.path.join(sd.ROOT, proxy)))
        if cal:
            worst = max(c["delta"] for c in cal)
            ta = sum(c["adapter_tok"] for c in cal)
            tp = sum(c["proxy_tok"] for c in cal)
            agg = abs(ta - tp) / tp if tp else 0
            ok = worst <= 0.02 and agg <= 0.02
            print(f"\n{B}H4 calibration{X}  runs={len(cal)}  "
                  f"aggregate={agg*100:.3f}%  worst={worst*100:.3f}%  "
                  f"{(G + 'PASS' if ok else R + 'FAIL')}{X}"
                  + ("" if len(cal) >= 20 else
                     f"  {Y}(inconclusive: gate specifies 20 runs){X}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
