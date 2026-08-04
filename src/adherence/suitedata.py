#!/usr/bin/env python3
"""Shared loader for the scoreboard, the results table and the matrix TUI.

Adapted from `leather/examples/14-sig-triage/eval/scripts/matrixdata.py`.
That module's own porting note says four things there are eval-specific
and everything else is generic over "cells with an accuracy and a cost".
This is those four things replaced:

  1. scoring — sigeval's accuracy becomes **pass@1** over `all_pass`,
     which `grade.py` already decided deterministically. No scorer bridge
     and no subprocess: the verdict is in the result record.
  2. archives — leather's `results/runs/<tag>/` becomes the suite's
     `results*.jsonl`, one JSON object per (scenario, arm, trial).
  3. tags — `<rig>-<arm>-<draw>` becomes **`<arm>/<scenario>`**, the unit
     the paired analysis is paired on (§11).
  4. arms registry — `ablation/arms.json` becomes ARMS below, which is
     the same table as design §4.

Everything above those — the filter grammar, faceting, the Pareto
frontier, the ranking spread — is kept as-is, because it is generic over
cells that have a quality number and a cost number, which is exactly what
this eval produces.

One loader for every surface, so no two of them can disagree about a
number. Stdlib only.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import math
import os
import statistics as st
from collections import defaultdict

from adherence import REPO_ROOT

ROOT = str(REPO_ROOT)
# Anything a run wrote, not just files someone remembered to call
# "results". The viewers exist to be pointed at a run in progress; a glob
# that misses runs/ab.jsonl makes them useless exactly when they are wanted.
DEFAULT_GLOBS = ("results*.jsonl", "runs/*.jsonl")

REQUIRED_FIELDS = {"scenario", "checks", "model", "adapter", "trial", "all_pass"}

# design §4. `role` is what each arm is *for*; it is the thing readers of
# a scoreboard get wrong when it is not written down next to the number.
ARMS = {
    "a0": ("none", "floor: what does any instruction cost/buy"),
    "a1": ("monolith-realistic", "the practical control — the repo's own file"),
    "a2": ("monolith-matched", "the scientific control — same content, one file"),
    "a3": ("directed-inline", "the pattern as shipped"),
    "a4": ("directed-spawn", "context-scoped subagent (E3)"),
    "a5": ("minimal+tools", "the minimal-instruction alternative"),
    "-": ("unset", "no arm recorded"),
}


# ---------- loading ----------

def result_files(paths=None):
    if paths:
        return list(paths)
    out = []
    for g in DEFAULT_GLOBS:
        out += sorted(glob.glob(os.path.join(ROOT, g)))
    return [p for p in out if not p.endswith(".bak")]


def load_rows(paths=None):
    rows = []
    for p in result_files(paths):
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    # runs/ also holds proxy logs, whose lines are calls and
                    # marks rather than results. Require the fields a cell is
                    # built from, so a viewer pointed at a run in progress
                    # skips them instead of crashing on the first one.
                    if not r.keys() >= REQUIRED_FIELDS:
                        continue
                    r.setdefault("arm", "-")
                    r.setdefault("metrics", {})
                    r["_src"] = os.path.basename(p)
                    # File order is arrival order for an append-only log,
                    # which is the only "most recent" a viewer can know.
                    r["_seq"] = len(rows)
                    rows.append(r)
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def newest_mtime(paths=None):
    ts = [os.path.getmtime(p) for p in result_files(paths) if os.path.exists(p)]
    return max(ts, default=0.0)


# ---------- cells ----------

def _med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else 0


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0


def _p90(vals):
    """90th percentile, nearest-rank.

    Reported next to the median rather than instead of it. A cost
    comparison is made on medians because between-scenario variance is
    enormous, but a median hides the runs that actually hurt: the trial
    that explored for forty calls, the one that timed out. The gap between
    the two is the thing to look at -- a treatment that halves the median
    and doubles the tail has not made anything cheaper."""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0
    # Nearest-rank, not interpolation: with 5-7 trials per cell an
    # interpolated p90 invents a value no trial produced.
    k = max(1, math.ceil(0.9 * len(vals)))
    return vals[k - 1]


def load_cells(paths=None, pattern=None):
    """One cell per (model, adapter, arm, scenario), trials aggregated.

    Trials are aggregated rather than listed because the design is paired
    on scenario (§11) — the scenario is the cluster, and a single trial is
    not an independent unit of anything.
    """
    rows = load_rows(paths)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["adapter"], r["arm"], r["scenario"])].append(r)

    cells = []
    for (model, adapter, arm, scen), rs in groups.items():
        won = [r for r in rs if r["all_pass"]]
        toks = [(r["metrics"] or {}).get("tok_in_billed", 0) for r in rs]
        calls = [(r["metrics"] or {}).get("calls", 0) for r in rs]
        passes = [1.0 if r["all_pass"] else 0.0 for r in rs]
        fails = sorted({c["name"] for r in rs for c in r["checks"]
                        if c["status"] == "fail"})
        cells.append({
            "tag": f"{arm}/{scen}",
            "model": model, "adapter": adapter, "arm": arm, "scenario": scen,
            "category": rs[0].get("category", "uncategorized"),
            "trials": len(rs),
            "pass_rate": 100.0 * sum(passes) / len(passes),
            "spread": 100.0 * st.pstdev(passes) if len(passes) > 1 else 0.0,
            "tok": _med(toks),
            "ktok": _med(toks) / 1000.0,
            "tok_won": _med([(r["metrics"] or {}).get("tok_in_billed", 0)
                             for r in won]),
            "calls": _med(calls),
            "probes": _med([(r["metrics"] or {}).get("probes_to_first_edit", 0)
                            for r in rs]),
            "redundant": _med([(r["metrics"] or {}).get("redundant_reads", 0)
                               for r in rs]),
            "subagent_calls": sum((r["metrics"] or {}).get("subagent_calls", 0)
                                  for r in rs),
            "abandoned": sum(1 for r in rs
                             if (r["metrics"] or {}).get("abandoned")),
            "dur_s": _med([r.get("duration_s", 0) for r in rs]),
            "fails": fails,
            "rows": rs,
            # Averages and tails, alongside the medians above. The median
            # is what the analysis compares; the p90 is what says whether
            # the median is telling the whole story.
            "avg_tok": _avg(toks),
            "p90_tok": _p90(toks),
            # Cost with the give-ups removed. An abandoned trial spends a
            # fraction of a real attempt, so an arm that quits more often
            # looks CHEAPER on the unconditioned median -- measured at 23%
            # understatement for a1 on the validation grid, against the
            # very arm the treatment is compared to. The registered
            # analysis conditions on success; every surface that shows a
            # raw median has to be able to say so too.
            "tok_worked": _med([(r["metrics"] or {}).get("tok_in_billed", 0)
                                for r in rs
                                if not (r["metrics"] or {}).get("abandoned")]),
            "avg_calls": _avg(calls),
            "p90_calls": _p90(calls),
            "avg_dur": _avg([r.get("duration_s", 0) for r in rs]),
            "p90_dur": _p90([r.get("duration_s", 0) for r in rs]),
            "tools": _med([(r["metrics"] or {}).get("tool_calls", 0)
                           for r in rs]),
            "avg_tools": _avg([(r["metrics"] or {}).get("tool_calls", 0)
                               for r in rs]),
            "p90_tools": _p90([(r["metrics"] or {}).get("tool_calls", 0)
                               for r in rs]),
            "avg_probes": _avg([(r["metrics"] or {}).get(
                "probes_to_first_edit", 0) for r in rs]),
            "p90_probes": _p90([(r["metrics"] or {}).get(
                "probes_to_first_edit", 0) for r in rs]),
            "n_subagents": _med([(r["metrics"] or {}).get("n_subagents", 0)
                                 for r in rs]),
            "subagent_tok": _med([(r["metrics"] or {}).get("subagent_tok_in", 0)
                                  for r in rs]),
            "ungradeable": sum(1 for r in rs
                               if any(c.get("name") == "adapter"
                                      and c.get("status") != "pass"
                                      for c in r["checks"])),
        })
    cells.sort(key=lambda c: (c["arm"], c["scenario"]))
    if pattern:
        cells = [c for c in cells if matches(c["tag"], pattern)]
    return cells


# ---------- filter grammar (kept verbatim in spirit from matrixdata) ----------

def matches(tag, pattern):
    """Deliberately forgiving: bare prefix, glob, substring (case-
    insensitive), comma = OR, leading ! negates the whole expression."""
    if not pattern:
        return True
    pattern = pattern.strip()
    if pattern.startswith("!"):
        return not matches(tag, pattern[1:])
    for part in pattern.split(","):
        part = part.strip()
        if not part:
            continue
        if any(ch in part for ch in "*?["):
            if fnmatch.fnmatch(tag, part) or fnmatch.fnmatch(tag, part + "*"):
                return True
        elif tag.startswith(part) or part.lower() in tag.lower():
            return True
    return False


FACETS = ("model", "adapter", "arm", "scenario")


def facet_values(cells, facet):
    return sorted({c[facet] for c in cells})


# ---------- rollups and comparisons ----------

def arm_rollup(cells):
    """Per-arm aggregate. `pass_rate` is unweighted across scenarios so a
    scenario with more trials does not dominate — every scenario is one
    cluster (§11)."""
    by = defaultdict(list)
    for c in cells:
        by[c["arm"]].append(c)
    out = []
    for arm, cs in sorted(by.items()):
        out.append({
            "arm": arm,
            "name": ARMS.get(arm, ("?", ""))[0],
            "role": ARMS.get(arm, ("?", ""))[1],
            "scenarios": len(cs),
            "trials": sum(c["trials"] for c in cs),
            "pass_rate": sum(c["pass_rate"] for c in cs) / len(cs),
            "tok": _med([c["tok"] for c in cs]),
            "ktok": _med([c["tok"] for c in cs]) / 1000.0,
            "calls": _med([c["calls"] for c in cs]),
            "probes": _med([c["probes"] for c in cs]),
            "abandoned": sum(c["abandoned"] for c in cs),
            "subagent_calls": sum(c["subagent_calls"] for c in cs),
        })
    return out


def paired_ratio(cells, arm, ref, key="tok"):
    """Geometric mean of the per-scenario ratio arm/ref, over scenarios
    present in BOTH arms. Returns (ratio, n_paired) or (None, 0).

    Geometric, and paired on scenario, because between-scenario token
    variance dwarfs between-arm differences — a raw mean across
    heterogeneous scenarios is not a comparison, it is an artifact of the
    scenario mix (§11)."""
    a = {c["scenario"]: c[key] for c in cells if c["arm"] == arm and c[key]}
    b = {c["scenario"]: c[key] for c in cells if c["arm"] == ref and c[key]}
    both = sorted(set(a) & set(b))
    if not both:
        return None, 0
    lr = [math.log(a[s] / b[s]) for s in both]
    return math.exp(sum(lr) / len(lr)), len(both)


def pareto_front(cells, cost="ktok", quality="pass_rate"):
    """Tags that nothing else beats on BOTH axes.

    §5 control 2: the honest output is the (cost, success) plane, not a
    single number. A cell that is cheaper AND passes less is a trade, and
    the frontier is what says so where a ratio would not."""
    pts = [c for c in cells if c[cost] > 0]
    front = set()
    for c in pts:
        if not any(o[cost] <= c[cost] and o[quality] > c[quality] for o in pts):
            front.add(c["tag"])
    return front


# ---------- proxy / calibration ----------

def paired_proxy_log(paths):
    """The proxy log belonging to these results, if one exists.

    isolate.sh writes runs/probe.jsonl -> runs/probe.proxy.jsonl, so the
    pairing is derivable and the viewer should not need to be told. Asking
    the user to pass --proxy for a file the harness just wrote next to the
    results is the kind of step that guarantees the H4 tab stays empty."""
    for p in paths or []:
        cand = p[:-len(".jsonl")] + ".proxy.jsonl" if p.endswith(".jsonl") else ""
        if cand and os.path.exists(cand):
            return cand
    return ""


def load_proxy(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def calibration(rows, proxy_rows):
    """Per-run adapter-vs-proxy agreement — the H4 gate, live.

    The gate is not a one-time ceremony: an opencode upgrade, a config
    change, or a new adapter can break the agreement silently, and every
    cost number downstream would still render. Keeping it on a tab is the
    cheapest way to notice."""
    from adherence import metrics as M

    by_mark = M.split_by_mark(proxy_rows)
    out = []
    for r in rows:
        mark = f"{r['scenario']}|{r.get('arm', '-')}|{r['trial']}"
        pr = by_mark.get(mark)
        if pr is None:
            continue
        p = M.proxy_totals(pr)
        a_tok = (r["metrics"] or {}).get("tok_in_billed", 0)
        a_calls = (r["metrics"] or {}).get("calls", 0)
        d = (abs(a_tok - p["tok_in_billed"]) / p["tok_in_billed"]
             if p["tok_in_billed"] else 0.0)
        out.append({"mark": mark, "adapter_tok": a_tok,
                    "proxy_tok": p["tok_in_billed"], "delta": d,
                    "adapter_calls": a_calls, "proxy_calls": p["calls"],
                    "aux": p["aux_calls"]})
    return out


def fmt_duration(s):
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
