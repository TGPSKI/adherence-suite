---
name: phase-04-floor-pilot
description: "Measure the per-arm harness floor, then pilot every scenario and keep only the ones that discriminate."
parent: onboard-fixture
---

# Phase 4 — Floor and calibration pilot

**Carry forward**: fixture, arms directory, `ground-truth.jsonl`, test subset.

## Step 1: Measure the per-arm floor

Every inference call carries the harness's fixed cost — system prompt, tool
schemas, and the arm's own instruction surface. Without measuring it,
`tok_in_marginal` is not marginal, it is a guess.

**Generate** — a no-op prompt per arm, recording input tokens on call 1:

```bash
for arm in a0 a1 a2 a3; do
  ADH_PROXY_LOG=runs/floor-$arm.jsonl \
    make run S=noop ARMS=$arm ARMSDIR=fixtures/{fixture}.arms OUT=runs/floor.jsonl
done
```

| Result | Meaning |
|---|---|
| the empty arm lowest, the content-matched control highest | expected — that arm carries the whole surface on every call |
| an arm you expected to be cheap is not | tool schemas and injected context inflate the floor on *every* call, not just the first. This is often the finding |
| floors within noise of each other | the arms did not apply. `--keep-sandbox`, then read the surface in the sandbox |

Record per-arm floors in `docs/FIXTURES.md`. Pass them to the runner with
`--floor`.

## Step 2: Calibration pilot

**Inspect** — the existing synthetic suite is at ceiling; PR-derived tasks
carry the opposite risk of flooring at 0.00. Both break the analysis: at
ceiling there is no cost/quality trade to observe, at floor there are no
successful trials to condition cost on.

**Generate** — pilot every scenario in the practical control and the treatment, 3 trials:

```bash
ADH_PROXY_LOG=runs/pilot-proxy.jsonl \
  make all TRIALS=3 ARMS=a1,a3 ARMSDIR=fixtures/{fixture}.arms OUT=runs/pilot.jsonl
make table FILTER='a1,a3'
```

**Keep only scenarios whose pooled pass rate lands in [0.25, 0.80].**

| Pooled pass rate | Action |
|---|---|
| 0.00 | drop — nothing to condition on. Log it |
| < 0.25 | drop — log it |
| 0.25–0.80 | keep |
| > 0.80 | drop — ceiling, no trade observable. Log it |
| any | **never re-author a dropped scenario to make it discriminate** |

That last row is the one that matters. Tuning a task until it discriminates
reintroduces exactly the experimenter bias PR-derivation was chosen to
remove. Expect to discard half or more; that is the protocol working.

The pilot also produces per-scenario token CV, which the power calculation
needs and which ranges from under 0.1% to ~34% depending on round count.

## Step 3: Confirm the gate still holds

Any change to the fixture, the adapter, or the endpoint can move the
calibration. Serially — proxy marks are meaningless under `--jobs`:

```bash
make calibrate FILES=runs/pilot.jsonl PROXY=runs/pilot-proxy.jsonl
```

| Result | Action |
|---|---|
| within 2% | the adapter's figures may be reported beside the proxy's |
| outside 2% | the proxy is authoritative. Drop the adapter figures and say so in the report |

## Step 4: Record

Update `docs/FIXTURES.md`:

```markdown
per-arm floor (tokens/call): a0 {n} · a1 {n} · a2 {n} · a3 {n}
pilot: {k}/{n} scenarios kept in [0.25, 0.80]; token CV {lo}%–{hi}%
dropped: {id} ({rate}) — {ceiling|floor}; never re-authored
calibration: {x.xxx}% over {n} runs, serial
```

## PR Checkpoint

**Title**: `fixtures: {owner}/{name} floors and pilot — phase 4`

**Files**:
- `docs/FIXTURES.md`
- `scenarios/{fixture}/ground-truth.jsonl` — dropped scenarios marked, not deleted

## Done

The fixture is ready. Run the grid — see [docs/EVAL.md](../../../../docs/EVAL.md)
§Scope for what the minimum viable version licenses you to claim, and what it
does not.
