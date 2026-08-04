#!/usr/bin/env python3
"""report.py — results.jsonl files -> markdown scoreboard.

Usage: python3 report.py results-a.jsonl [results-b.jsonl ...] > scoreboard.md
       python3 report.py --ref a1 results-*.jsonl > scoreboard.md

Groups by **(model, adapter, arm)**. Every cost figure is printed beside
the pass rate for the same cell, because a cost table without an adjacent
pass-rate column is not publishable (design §5) — the cheapest possible
agent is one that does nothing.

With more than one arm present, also emits the paired comparison §11
specifies: per-scenario log-ratios against a reference arm, geometric
mean, cluster-bootstrap 95% CI over scenarios, plus medians. Never a raw
mean of tokens across heterogeneous scenarios.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
from collections import defaultdict

BOOTSTRAP_N = 10_000


def load(paths):
    rows = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def check_rate(rows):
    """Fraction of gradeable checks that passed, across all rows."""
    passed = total = 0
    for r in rows:
        for c in r["checks"]:
            if c["status"] == "ungradeable":
                continue
            total += 1
            passed += c["status"] == "pass"
    return passed / total if total else 0.0


def m(r, key, default=0):
    return (r.get("metrics") or {}).get(key, default)


def pass_rate(rows):
    return sum(1.0 if r["all_pass"] else 0.0 for r in rows) / len(rows) if rows else 0.0


def med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else 0


def iqr(vals):
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 4:
        return (min(vals, default=0), max(vals, default=0))
    q = st.quantiles(vals, n=4)
    return (q[0], q[2])


def bootstrap_ci(per_scenario_logratios, n=BOOTSTRAP_N, seed=0):
    """Cluster bootstrap over scenarios (§11). Resamples whole scenarios,
    not individual trials: trials within a scenario are not independent,
    and treating them as such would report a CI several times too tight."""
    if len(per_scenario_logratios) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(per_scenario_logratios)
    means = []
    for _ in range(n):
        sample = [per_scenario_logratios[rng.randrange(k)] for _ in range(k)]
        means.append(sum(sample) / k)
    means.sort()
    return (means[int(0.025 * n)], means[int(0.975 * n)])


def arm_block(rows, model, adapter, arm):
    sub = [r for r in rows if r["model"] == model and r["adapter"] == adapter
           and r["arm"] == arm]
    if not sub:
        return
    print(f"## {model} · {adapter} · arm {arm}\n")
    n_trials = len({r["trial"] for r in sub})
    print(f"trials per scenario: up to {n_trials}\n")

    by_scen = defaultdict(list)
    for r in sub:
        by_scen[r["scenario"]].append(r)

    print("| scenario | category | pass@1 | check adherence | calls "
          "| tok_in (med) | tok_in if passed | probes→edit | redundant "
          "| abandoned | mean dur s | failing checks |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sid in sorted(by_scen):
        rs = by_scen[sid]
        won = [r for r in rs if r["all_pass"]]
        fails = sorted({c["name"] for r in rs for c in r["checks"]
                        if c["status"] == "fail"})
        print(f"| {sid} | {rs[0]['category']} "
              f"| {pass_rate(rs):.2f} "
              f"| {check_rate(rs):.2f} "
              f"| {med([m(r, 'calls') for r in rs]):.0f} "
              f"| {med([m(r, 'tok_in_billed') for r in rs]):.0f} "
              f"| {med([m(r, 'tok_in_billed') for r in won]):.0f} "
              f"| {med([m(r, 'probes_to_first_edit') for r in rs]):.0f} "
              f"| {med([m(r, 'redundant_reads') for r in rs]):.0f} "
              f"| {sum(1 for r in rs if m(r, 'abandoned')):d}/{len(rs)} "
              f"| {st.mean(r['duration_s'] for r in rs):.0f} "
              f"| {', '.join(fails) if fails else '—'} |")

    by_cat = defaultdict(list)
    for r in sub:
        by_cat[r["category"]].append(r)
    print("\n| category | pass@1 | check adherence | tok_in (med) |")
    print("|---|---|---|---|")
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        print(f"| {cat} | {pass_rate(rs):.2f} | {check_rate(rs):.2f} "
              f"| {med([m(r, 'tok_in_billed') for r in rs]):.0f} |")

    won = [r for r in sub if r["all_pass"]]
    lo, hi = iqr([m(r, "tok_in_billed") for r in sub])
    subs = sum(m(r, "subagent_calls") for r in sub)
    print(f"\n**overall pass@1: {pass_rate(sub):.2f} · "
          f"check adherence: {check_rate(sub):.2f} · "
          f"median tok_in: {med([m(r, 'tok_in_billed') for r in sub]):.0f} "
          f"(IQR {lo:.0f}–{hi:.0f}) · "
          f"median calls: {med([m(r, 'calls') for r in sub]):.0f}**")
    if won:
        print(f"\nsuccess-conditioned (n={len(won)}): median tok_in "
              f"{med([m(r, 'tok_in_billed') for r in won]):.0f}, median calls "
              f"{med([m(r, 'calls') for r in won]):.0f}")
    if subs:
        print(f"\nsubagent calls in this arm: {subs} "
              f"({sum(m(r, 'subagent_tok_in') for r in sub)} input tokens) — "
              f"included in tok_in above, per §7 total_tokens")
    if any(m(r, "floor_used") for r in sub):
        print(f"\nfloor used for tok_in_marginal: "
              f"{max(m(r, 'floor_used') for r in sub)} tokens/call")
    else:
        print("\n**tok_in_marginal is uncalibrated** (floor=0): no per-arm "
              "floor was supplied, so only total billed tokens are "
              "interpretable here (§0.2).")
    print()


def paired_section(rows, ref_arm):
    arms = sorted({r["arm"] for r in rows})
    if len(arms) < 2:
        return
    if ref_arm not in arms:
        ref_arm = arms[0]
    print(f"## Paired comparison vs arm `{ref_arm}` (§11)\n")
    print("Per-scenario log-ratio of the median, geometric mean across "
          "scenarios, cluster-bootstrap 95% CI over scenarios. A CI that "
          "spans 0 is not a difference.\n")

    def cell(arm, sid, key, only_passed):
        rs = [r for r in rows if r["arm"] == arm and r["scenario"] == sid
              and (r["all_pass"] or not only_passed)]
        vals = [m(r, key) for r in rs if m(r, key)]
        return med(vals) if vals else None

    scens = sorted({r["scenario"] for r in rows})
    for key, label in (("tok_in_billed", "input tokens"),
                       ("calls", "inference calls")):
        for only_passed in (False, True):
            tag = "success-conditioned" if only_passed else "unconditional"
            print(f"### {label} — {tag}\n")
            print("| arm | scenarios paired | geo-mean ratio | 95% CI "
                  "| pass@1 |")
            print("|---|---|---|---|---|")
            for arm in arms:
                if arm == ref_arm:
                    continue
                lrs = []
                for sid in scens:
                    a = cell(arm, sid, key, only_passed)
                    b = cell(ref_arm, sid, key, only_passed)
                    if a and b:
                        lrs.append(math.log(a / b))
                if not lrs:
                    print(f"| {arm} | 0 | — | — | — |")
                    continue
                g = math.exp(sum(lrs) / len(lrs))
                lo, hi = bootstrap_ci(lrs)
                arm_rows = [r for r in rows if r["arm"] == arm]
                ci = ("—" if math.isnan(lo)
                      else f"{math.exp(lo):.3f}–{math.exp(hi):.3f}")
                print(f"| {arm} | {len(lrs)} | {g:.3f} | {ci} "
                      f"| {pass_rate(arm_rows):.2f} |")
            print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--ref", default="a1",
                    help="reference arm for the paired comparison "
                         "(a1 = the maintainer's own file, the practical "
                         "baseline; a2 = the scientific control)")
    args = ap.parse_args()

    rows = load(args.files)
    if not rows:
        print("no results")
        return
    for r in rows:
        r.setdefault("arm", "-")

    print("# Adherence scoreboard\n")
    systems = sorted({(r["model"], r["adapter"], r["arm"]) for r in rows})
    for model, adapter, arm in systems:
        arm_block(rows, model, adapter, arm)

    paired_section(rows, args.ref)

    ungr = sorted({c["name"] for r in rows for c in r["checks"]
                   if c["status"] == "ungradeable"})
    if ungr:
        print(f"\nungradeable checks (adapter capability gaps): "
              f"{', '.join(ungr)}")


if __name__ == "__main__":
    main()
