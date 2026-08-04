---
name: adherence-triage
description: "Diagnose a failing or suspicious adherence-suite result: is it the model, the grader, the adapter, the proxy, or the coordinate system?"
metadata:
  author: TGPSKI
  version: "1.0"
compatibility: "git, python3, curl; a results.jsonl and ideally a proxy log"
---

# Triaging an adherence-suite result

A red cell in the scoreboard has five plausible authors: the model, the
grader, the adapter, the harness, and the person reading it. Only one of
those is the thing the suite exists to measure. This workflow separates
them, cheapest check first.

**Do Tier 1 before reading any source.** In this repo the most common root
cause is not a bug — it is reading the wrong number. There are three
legitimate token totals and three copies of the config, and picking the
wrong one produces a confident, wrong conclusion.

---

## Tier 1 — Coordinates

### The coordinate systems that bite

| Coordinate | The values | The confusion |
|---|---|---|
| **Token total** | `usage` event · sum of `call` events · proxy log | All three are real and they differ. `usage` is **root-session only** and excludes subagents — measured 3.1× low on s13. |
| **Config copy** | committed `bench/opencode-bench.json` · patched `bench/.xdg/opencode/opencode.json` · the sandbox's own `opencode.json` | The proxy rewrite touches the middle one; `ADH_BENCH_CONFIG` propagates it to the third. Miss that and the run bypasses the proxy while the log looks merely empty. |
| **Session** | root session · child sessions per subagent | The root stream and root export contain *none* of a child's calls. |
| **Sandbox** | `/tmp/adh-<sid>-*` (tree) · `/tmp/adh-out-<sid>-*` (transcript) | Different dirs. `--keep-sandbox` prints both. |
| **Interpreter** | `python3` on PATH · `sys.executable` | asdf re-prepends its shim dir, so a PATH shim you installed may never fire. |
| **Arm** | `-` (unset) · `a0`–`a5` | Records predating the arm dimension carry `-` and no `metrics`. |
| **Concurrency** | serial · `--jobs N` | Proxy marks are only valid serially. Under `--jobs` attribution is skipped, not wrong — but a stale log from a parallel run *is* wrong. |

### Intake

| Fact | Source | Tag |
|---|---|---|
| {exact failing check name and evidence string} | results.jsonl | STATED |
| {which arm, scenario, trial} | results.jsonl | STATED |
| {was a proxy log written for this run} | runs/ | STATED/MISSING |
| {serial or --jobs} | invocation | STATED/MISSING |

Any MISSING that could close the case alone — resolve it before proceeding.

### The gate

```
REPORTED: {the number or verdict the reporter is quoting}
SOURCE:   {which of the three totals / which config copy it came from}
```

- **Wrong source** → `COORDINATE MISMATCH`. The suite is fine. Re-read from
  the right one and stop.
- **Right source** → `COORDINATES VERIFIED`. Continue.

### Ground truth, non-destructive

```bash
make selftest                      # no model: is the grader itself sound?
make schema                        # is the transcript shape legal?
curl -s localhost:8010/__proxy/health   # is a proxy actually up?
```

`make selftest` is the single highest-value check in this repo. It needs no
model, no GPU and no network, and it answers "is this a grader bug?"
outright. Run it first, every time.

---

## Tier 2 — Investigation

Only after `COORDINATES VERIFIED`.

### The recurring hypotheses

| Hypothesis | Suspect when | Discriminating check | Cost |
|---|---|---|---|
| **Grader bug, not model failure** | any red cell | `make selftest` — violator must be caught | seconds |
| **Adapter capability gap** | a dispatch/task check reads `fail` | is there a `capability` event, and does `has_task_events()` return true? A gap must render `ungradeable`, never `fail` | seconds |
| **Reading root-only tokens** | adapter and proxy differ by 2–4× | group `call` events by `agent`; compare the root subtotal against `usage` | seconds |
| **Proxy bypassed** | proxy log empty, or holds only no-tool calls | `grep baseURL` the sandbox's `opencode.json` (`--keep-sandbox`) | seconds |
| **Nested isolate** | "already listening on 8010" | `make all` wraps itself in isolate; a caller who also wraps nests two | seconds |
| **Schema violation** | runner printed a `schema:` warning; metrics are zero | `schema.validate_transcript` on the transcript | seconds |
| **Dirty fixture at t=0** | *every* scope check fails at once | materialize, make zero edits, `git status --porcelain` — must be empty | a minute |
| **Parallel-only** | passes serially, fails under `--jobs` | look for `database is locked` in the adapter's stderr | a minute |
| **Real model failure** | everything above is clean | re-run N trials; is it stochastic or deterministic? | GPU time |

**Model failure is the last hypothesis, not the first.** It is the only one
that costs GPU time to confirm and the only one that is not a bug.

### The cheapest discriminating check

Almost always this, and it costs nothing:

```bash
make selftest && make schema
```

- **selftest red** → grader bug. The model is irrelevant. Fix the grader,
  and add the actor that would have caught it.
- **schema red** → adapter bug. Cost metrics from that run are not
  trustworthy; do not quote them.
- **both green** → the harness is sound. Now the run is worth investigating.

### Reading a transcript

```bash
make run S=s05 MODEL=... TRIALS=1     # note the [out_dir=...] on the trial line
python3 - <<'PY'
import json, sys
ev = [json.loads(l) for l in open("<out_dir>/transcript.jsonl")]
calls = [e for e in ev if e["type"] == "call"]
print("types:", sorted({e["type"] for e in ev}))
print("calls:", len(calls), "by agent:",
      {a: sum(1 for c in calls if c["agent"] == a) for a in {c["agent"] for c in calls}})
print("tok_in from calls:", sum(c["input_tokens"] for c in calls))
print("tok_in from usage:", next((e["prompt_tokens"] for e in ev if e["type"] == "usage"), None))
PY
```

If those last two differ, that is not a bug — it is the root-only/all-agents
distinction, working as designed. Quote the first one.

### Calibration disagreement

The gate is 2%; it currently reads 0.000%. If it moves:

1. Was the run **serial**? Marks are meaningless under `--jobs`. A parallel
   log is not evidence of disagreement.
2. Are the extra proxy calls **auxiliary** (no tool schemas)? Session-title
   generation is real spend the adapter cannot see, and it is excluded by
   design — `metrics.is_auxiliary`.
3. Did every trial get a mark? Unmatched runs are reported separately.

If it survives all three: **the proxy is authoritative.** Drop the adapter's
figures from the report and say so in writing. That is the documented
resolution, not a fallback.

---

## Artifact

Write findings to `sessions/{date}-{slug}.md`:

1. **Verdict** — one of: coordinate mismatch · grader bug · adapter gap ·
   harness bug · real model failure.
2. **Evidence** — labelled DEFINITIVE (system output) or ANECDOTAL. A
   scoreboard cell someone remembers is not evidence; the results line is.
3. **What changes** — a grader bug means a new selftest actor. An adapter gap
   means an `ungradeable`, never a `fail`. A real model failure means nothing
   changes in this repo; that was the measurement working.

---

## Anti-patterns

- **Never conclude "the model failed" before `make selftest`.** It is free
  and it is the check that most often ends the investigation.
- **Never compare a token number without saying which of the three it is.**
- **Never treat a `--jobs` proxy log as calibration evidence.**
- **Never fix a grader without adding the actor that would have caught it.**
  A grader bug that reaches `main` twice is a missing test, not bad luck.
- **Never widen a check to make a red cell green.** The red cell may be the
  finding.
