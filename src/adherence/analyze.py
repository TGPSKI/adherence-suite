#!/usr/bin/env python3
"""Pre-specified analysis: turn results into a verdict on each falsifier.

    python3 -m adherence.analyze results.jsonl [--ref a2] [--floor-map f.json]

This module exists **before the data**. That is its whole point: if the
analysis is chosen after seeing results, the falsifiers are decorative.
Everything that could be tuned to flatter an outcome is fixed here — which
arm is the reference, which metric answers which falsifier, the CI method,
the multiplicity correction, and what happens when a precondition is
missing.

It refuses rather than substitutes. If the per-arm floor was never
measured, F1 is reported NOT TESTABLE — it does not quietly fall back to
billed tokens, because billed and marginal answer different questions and
only one of them is F1.

Registered thresholds (docs/EVAL.md §Falsifiers):

    F1  marginal input tokens, treatment vs content-matched control,
        not >=20% lower with a CI excluding 0
    F2  cache-adjusted effective tokens within +/-20% of the practical
        control                                    (metered endpoint only)
    F3  inference calls to completion do not decrease
    F4  task pass rate drops >=10pp in the treatment arm
    F5  no interaction with achieved N             (>=2 fixtures required)
    F6  parent+child total tokens not lower than an inline monolith parent

Holm-corrected across the six. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
import sys

# A cluster bootstrap resamples scenarios, so it needs scenarios. Below
# this the CI is nan and any verdict derived from it is a verdict from a
# test that never ran -- the single failure mode this whole design exists
# to prevent. F1 already guarded on it; F3 and F6 did not, and reported
# TRIPPED off nan on a one-scenario run.
MIN_PAIRED = 4
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260804          # fixed here, not chosen at analysis time
ALPHA = 0.05

# Registered arm roles. The reference for F1/F3 is the CONTENT-MATCHED
# control, not the practical one: a win against a1 alone cannot separate
# "bounding helps" from "you shipped fewer words".
TREATMENT = "a3"
CONTENT_MATCHED = "a2"
PRACTICAL = "a1"
SPAWN = "a4"


def load(paths) -> list[dict]:
    rows = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    r.setdefault("arm", "-")
                    r.setdefault("metrics", {})
                    rows.append(r)
    return rows


def _m(r, key, default=None):
    return (r.get("metrics") or {}).get(key, default)


def cell(rows, arm, scen, key, success_only):
    """Median of `key` for one (arm, scenario) cell.

    Success-conditioned by default: cost among trials that passed. The
    cheapest agent does nothing, so unconditional cost is not a result on
    its own (docs/EVAL.md §Cost is meaningless unconditional)."""
    rs = [r for r in rows if r["arm"] == arm and r["scenario"] == scen
          and (r["all_pass"] or not success_only)]
    vals = [_m(r, key) for r in rs if _m(r, key) is not None]
    return st.median(vals) if vals else None


def paired_log_ratios(rows, arm, ref, key, success_only):
    """Per-scenario log-ratio arm/ref, plus an explicit account of what
    was dropped and why.

    A log-ratio needs both cells positive, and `tok_in_marginal` can be
    <= 0 when the measured floor times the call count exceeds billed
    tokens — which means the floor was mis-measured for that arm, not
    that the scenario is uninteresting. Dropping those silently biases
    the survivor set toward high-token scenarios and moves the estimate.
    Callers must surface `dropped`; `evaluate()` refuses when it is large.
    """
    scens = sorted({r["scenario"] for r in rows})
    out, dropped = [], []
    for s in scens:
        a = cell(rows, arm, s, key, success_only)
        b = cell(rows, ref, s, key, success_only)
        if a is None or b is None:
            dropped.append((s, "missing in one arm"))
        elif a <= 0 or b <= 0:
            dropped.append((s, f"non-positive {key} ({a}, {b})"))
        else:
            out.append((s, math.log(a / b)))
    return out, dropped


def bootstrap_ci(lrs, n=BOOTSTRAP_N, seed=BOOTSTRAP_SEED):
    """Cluster bootstrap over scenarios. Resamples whole scenarios because
    trials within a scenario are not independent; treating them as such
    reports a CI several times too tight."""
    if len(lrs) < 2:
        return (float("nan"), float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(lrs)
    means = sorted(sum(lrs[rng.randrange(k)] for _ in range(k)) / k
                   for _ in range(n))
    point = sum(lrs) / k
    return (point, means[int(0.025 * n)], means[int(0.975 * n)])


def p_from_ci(lrs, n=BOOTSTRAP_N, seed=BOOTSTRAP_SEED):
    """Two-sided bootstrap p: the proportion of resamples on the wrong
    side of 0, doubled. Coarse by construction; reported to two places
    and never as '< 0.001'."""
    if len(lrs) < 2:
        return float("nan")
    rng = random.Random(seed + 1)
    k = len(lrs)
    means = [sum(lrs[rng.randrange(k)] for _ in range(k)) / k for _ in range(n)]
    frac = sum(1 for m in means if m >= 0) / n
    return max(1.0 / n, 2 * min(frac, 1 - frac))


def holm(pvals: dict[str, float], alpha=ALPHA) -> dict[str, bool]:
    """Holm-Bonferroni. Returns {name: rejected}. NaN p-values are tests
    that could not run; they do not consume alpha."""
    live = {k: v for k, v in pvals.items() if not math.isnan(v)}
    order = sorted(live, key=lambda k: live[k])
    m, out, blocked = len(order), {}, False
    for i, k in enumerate(order):
        thresh = alpha / (m - i)
        if blocked or live[k] > thresh:
            blocked, out[k] = True, False
        else:
            out[k] = True
    for k in pvals:
        out.setdefault(k, False)
    return out


def pass_rate(rows, arm):
    rs = [r for r in rows if r["arm"] == arm]
    return sum(1.0 for r in rs if r["all_pass"]) / len(rs) if rs else float("nan")


def experiment_rows(rows) -> tuple[list[dict], int]:
    """Only rows produced by the registered grid.

    Validation runs exist to shake out the method, the code and the
    harness. They are the same shape as real results, land in the same
    directory, and would pool into a verdict without anyone deciding to.
    Rows with no `purpose` predate the field and are treated as validation:
    the safe reading of an unlabelled row is not "this is experiment data".
    """
    keep = [r for r in rows if r.get("purpose") == "experiment"]
    return keep, len(rows) - len(keep)


def evaluate(rows) -> dict:
    """Every falsifier, with an explicit NOT TESTABLE where a precondition
    is missing. A verdict of 'not tripped' from a test that never ran is
    the failure mode this guards against."""
    arms = {r["arm"] for r in rows}
    # Fixture must be explicit. Inferring it from the scenario id counted
    # every scenario as its own fixture, which made F5 look testable on a
    # single-fixture run — the exact false "no interaction found" this
    # verdict exists to prevent.
    fixtures = {r["fixture"] for r in rows if r.get("fixture")}
    floors = {_m(r, "floor_used", 0) for r in rows}
    have_floor = any(f for f in floors)
    have_cache = any(_m(r, "cache_read", 0) or _m(r, "cache_write", 0) for r in rows)

    F, p = {}, {}

    def record(name, verdict, detail, pval=float("nan")):
        F[name] = {"verdict": verdict, "detail": detail}
        p[name] = pval

    # ---- F1: marginal input tokens vs the content-matched control -------
    if not have_floor:
        record("F1", "NOT TESTABLE",
               "no per-arm floor measured; tok_in_marginal is uncalibrated. "
               "Billed tokens are NOT substituted — they answer a different "
               "question (docs/EVAL.md §What measurement already contradicted)")
    elif not {TREATMENT, CONTENT_MATCHED} <= arms:
        record("F1", "NOT TESTABLE",
               f"needs arms {TREATMENT} and {CONTENT_MATCHED}; have {sorted(arms)}")
    else:
        lrs, dropped = paired_log_ratios(rows, TREATMENT, CONTENT_MATCHED,
                                         "tok_in_marginal", True)
        total = len(lrs) + len(dropped)
        if len(lrs) < 4 or len(dropped) > total / 4:
            record("F1", "NOT TESTABLE",
                   f"{len(dropped)}/{total} scenarios have no usable "
                   f"marginal figure ({dropped[:3]}). A non-positive "
                   f"marginal means floor x calls exceeded billed tokens — "
                   f"the floor is wrong for that arm. Re-measure it rather "
                   f"than analysing the survivors, which are biased toward "
                   f"high-token scenarios")
        else:
            pt, lo, hi = bootstrap_ci([x for _, x in lrs])
            pv = p_from_ci([x for _, x in lrs])
            ratio = math.exp(pt)
            tripped = not (ratio <= 0.80 and hi < 0)
            record("F1", "TRIPPED" if tripped else "not tripped",
                   f"marginal tokens {ratio:.3f}x vs {CONTENT_MATCHED} "
                   f"(95% CI {math.exp(lo):.3f}-{math.exp(hi):.3f}, "
                   f"k={len(lrs)}, dropped={len(dropped)}); registered "
                   f"threshold <=0.80 with CI excluding 1", pv)

    # ---- F2: cache-adjusted effective tokens ---------------------------
    if not have_cache:
        record("F2", "NOT TESTABLE",
               "endpoint reports no cache read/write, so tok_effective "
               "degenerates to tok_in_billed. Requires a metered API")
    else:
        lrs, dropped = paired_log_ratios(rows, TREATMENT, PRACTICAL, "tok_effective", True)
        pt, lo, hi = bootstrap_ci([x for _, x in lrs])
        pv = p_from_ci([x for _, x in lrs])
        ratio = math.exp(pt)
        tripped = 0.80 <= ratio <= 1.20
        record("F2", "TRIPPED" if tripped else "not tripped",
               f"effective tokens {ratio:.3f}x vs {PRACTICAL} "
               f"(95% CI {math.exp(lo):.3f}-{math.exp(hi):.3f})", pv)

    # ---- F3: round trips -----------------------------------------------
    if not {TREATMENT, CONTENT_MATCHED} <= arms:
        record("F3", "NOT TESTABLE", f"needs {TREATMENT} and {CONTENT_MATCHED}")
    else:
        lrs, dropped = paired_log_ratios(rows, TREATMENT, CONTENT_MATCHED, "calls", True)
        if len(lrs) < MIN_PAIRED:
            record("F3", "NOT TESTABLE",
                   f"only {len(lrs)} scenario(s) paired; a cluster bootstrap "
                   f"over scenarios needs at least {MIN_PAIRED}. Trials within "
                   f"one scenario are not independent and cannot stand in "
                   f"for scenarios")
        else:
            pt, lo, hi = bootstrap_ci([x for _, x in lrs])
            pv = p_from_ci([x for _, x in lrs])
            ratio = math.exp(pt)
            tripped = not (ratio < 1.0 and hi < 0)
            record("F3", "TRIPPED" if tripped else "not tripped",
                   f"calls {ratio:.3f}x vs {CONTENT_MATCHED} "
                   f"(95% CI {math.exp(lo):.3f}-{math.exp(hi):.3f}, "
                   f"k={len(lrs)})", pv)

    # ---- F4: pass rate guardrail ---------------------------------------
    if not {TREATMENT, PRACTICAL} <= arms:
        record("F4", "NOT TESTABLE", f"needs {TREATMENT} and {PRACTICAL}")
    else:
        t, r = pass_rate(rows, TREATMENT), pass_rate(rows, PRACTICAL)
        drop = r - t
        n_scen = len({x["scenario"] for x in rows})
        note = "" if n_scen >= MIN_PAIRED else (
            f" -- on {n_scen} scenario(s), so this is a description of one "
            f"task, not a guardrail verdict")
        record("F4", "TRIPPED" if drop >= 0.10 else "not tripped",
               f"pass rate {t:.2f} vs {r:.2f} ({drop*100:+.1f}pp); "
               f"registered threshold -10pp{note}")

    # ---- F5: interaction with achieved N -------------------------------
    if len(fixtures) < 2:
        record("F5", "NOT TESTABLE",
               f"needs >=2 fixtures with different achieved N; have "
               f"{len(fixtures) or 'none recorded'}. One point has no slope")
    else:
        record("F5", "REQUIRES MANUAL READ",
               f"{len(fixtures)} fixtures: plot effect vs achieved N with CIs "
               f"and state direction. With <5 points, fit nothing")

    # ---- F6: subagent handoff ------------------------------------------
    if SPAWN not in arms:
        record("F6", "NOT TESTABLE", f"arm {SPAWN} not run")
    else:
        lrs, dropped = paired_log_ratios(rows, SPAWN, CONTENT_MATCHED, "tok_in_billed", True)
        if len(lrs) < MIN_PAIRED:
            record("F6", "NOT TESTABLE",
                   f"only {len(lrs)} scenario(s) paired; needs at least "
                   f"{MIN_PAIRED}")
        else:
            pt, lo, hi = bootstrap_ci([x for _, x in lrs])
            pv = p_from_ci([x for _, x in lrs])
            ratio = math.exp(pt)
            tripped = not (ratio < 1.0 and hi < 0)
            record("F6", "TRIPPED" if tripped else "not tripped",
                   f"parent+child total {ratio:.3f}x vs inline "
                   f"{CONTENT_MATCHED} (95% CI {math.exp(lo):.3f}-"
                   f"{math.exp(hi):.3f}, k={len(lrs)})", pv)

    rejected = holm(p)
    for k, v in F.items():
        v["p"] = p[k]
        v["holm_significant"] = rejected.get(k, False)
    return F


def render(F: dict) -> str:
    out = ["# Falsifier verdicts", "",
           "Pre-specified in docs/EVAL.md before any arm ran. Holm-corrected",
           "across the six tests; NOT TESTABLE does not consume alpha.", "",
           "| # | verdict | Holm sig. | p | detail |", "|---|---|---|---|---|"]
    for k in sorted(F):
        v = F[k]
        pv = "—" if math.isnan(v["p"]) else f"{v['p']:.3f}"
        sig = "yes" if v["holm_significant"] else ("—" if math.isnan(v["p"]) else "no")
        out.append(f"| {k} | **{v['verdict']}** | {sig} | {pv} | {v['detail']} |")
    live = [v for v in F.values() if v["verdict"] in ("TRIPPED", "not tripped")]
    nt = [k for k, v in F.items() if v["verdict"] == "NOT TESTABLE"]
    out += ["", f"{len(live)} of {len(F)} falsifiers were testable on this data."]
    if nt:
        out.append(f"**Not testable: {', '.join(sorted(nt))}** — stated rather "
                   f"than reported as 'not tripped'.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()
    rows = load(args.results)
    if not rows:
        print("no results", file=sys.stderr)
        return 1
    rows, dropped = experiment_rows(rows)
    if dropped:
        print(f"excluded {dropped} row(s) not marked purpose=experiment "
              f"(validation runs, or predating the field)", file=sys.stderr)
    if not rows:
        print("no rows marked purpose=experiment. These are validation runs; "
              "the registered analysis does not read them.", file=sys.stderr)
        return 1
    F = evaluate(rows)
    print(json.dumps(F, indent=2) if args.json else render(F))
    return 0


if __name__ == "__main__":
    sys.exit(main())
