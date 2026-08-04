#!/usr/bin/env python3
"""tools/calibrate.py — the H4 gate: do the proxy and the adapter agree?

    tools/calibrate.py <results.jsonl> <proxy.jsonl> [--tolerance 0.02]

Design §3.2: "proxy-counted tokens and adapter-reported tokens must agree
within 2% on a 20-run calibration set before P2 opens. If they disagree,
the proxy is authoritative and the adapter number is dropped from the
report." This computes that check and prints the verdict; it does not
soften it.

Joins on the trial mark the runner POSTs to /__proxy/mark before each
trial, so per-run agreement is visible and not just the aggregate — an
aggregate can agree while two runs cancel each other out.

Auxiliary calls (opencode's session-title generation: inference calls
carrying no tool schemas) are excluded from the comparison and reported
separately. The adapter cannot see them and they are not attributable to
the instruction surface under test. Excluding them is a stated choice,
not a rounding decision — see lib/metrics.is_auxiliary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adherence import metrics


def load_jsonl(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("proxy")
    ap.add_argument("--tolerance", type=float, default=0.02)
    args = ap.parse_args()

    results = load_jsonl(args.results)
    by_mark = metrics.split_by_mark(load_jsonl(args.proxy))

    print("# H4 calibration — proxy vs adapter\n")
    print("| run | adapter calls | proxy calls | adapter tok_in | proxy tok_in "
          "| delta | aux calls |")
    print("|---|---|---|---|---|---|---|")

    tot_a = tot_p = 0
    tot_ac = tot_pc = 0
    worst = 0.0
    unmatched = []
    n = 0
    for r in results:
        mark = f"{r['scenario']}|{r['arm']}|{r['trial']}"
        rows = by_mark.get(mark)
        m = r.get("metrics") or {}
        if rows is None:
            unmatched.append(mark)
            continue
        p = metrics.proxy_totals(rows)
        a_tok, p_tok = m.get("tok_in_billed", 0), p["tok_in_billed"]
        d = abs(a_tok - p_tok) / p_tok if p_tok else (0.0 if not a_tok else 1.0)
        worst = max(worst, d)
        tot_a += a_tok
        tot_p += p_tok
        tot_ac += m.get("calls", 0)
        tot_pc += p["calls"]
        n += 1
        print(f"| {mark} | {m.get('calls', 0)} | {p['calls']} | {a_tok} "
              f"| {p_tok} | {d*100:.2f}% | {p['aux_calls']} |")

    agg = abs(tot_a - tot_p) / tot_p if tot_p else 1.0
    print(f"\nruns compared: **{n}**")
    print(f"adapter Σ input tokens: **{tot_a}**  ·  "
          f"proxy Σ input tokens: **{tot_p}**")
    print(f"aggregate delta: **{agg*100:.3f}%**  ·  "
          f"worst single run: **{worst*100:.3f}%**")
    print(f"call counts: adapter **{tot_ac}** vs proxy **{tot_pc}**")
    if unmatched:
        print(f"\n**unmatched runs (no proxy mark): {len(unmatched)}** "
              f"— {unmatched[:5]}")

    ok = (agg <= args.tolerance and worst <= args.tolerance
          and tot_ac == tot_pc and not unmatched)
    print()
    if n < 20:
        print(f"**INCONCLUSIVE — {n} runs, the gate specifies 20.**")
    if ok:
        print(f"**H4 PASS** — agreement within {args.tolerance*100:.0f}% on "
              f"every run and on call counts. The adapter's per-call figures "
              f"may be reported alongside the proxy's.")
    else:
        print(f"**H4 FAIL** — the proxy is authoritative. Drop the adapter "
              f"token figures from the report and re-derive every cost "
              f"number from {args.proxy}.")
    return 0 if (ok and n >= 20) else 1


if __name__ == "__main__":
    sys.exit(main())
