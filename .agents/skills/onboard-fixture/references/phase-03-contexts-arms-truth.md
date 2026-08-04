---
name: phase-03-arms-and-truth
description: "Define the intervention and its controls, materialize arms, and freeze ground truth before any arm runs."
parent: onboard-fixture
---

# Phase 3 — Arms and frozen ground truth

**Carry forward**: `{owner}/{name}`, mirror, `base_commit`, test subset.

This phase is intervention-agnostic. The suite compares *arms* — whatever you
put in front of the model — and directed-contexts is one instance. An arm can
be an instruction file, a tool loadout, a system-prompt variant, a model, a
harness setting, a retrieval strategy, or a subagent topology.

## Step 1: Name the intervention and its controls

**Decide** — answer these three before generating anything:

| Question | Why it decides the arm set |
|---|---|
| What is the **treatment**? | the one thing you are claiming helps |
| What is the **practical control**? | what a real user has today, recovered from the repo — never authored by you |
| What is the **scientific control**? | the treatment with its *mechanism* removed but its *content* held constant |

The third one is the one people skip, and skipping it is what makes an eval
unfalsifiable. If the treatment changes both what the model is told and how
it is delivered, a win could be either. The content-matched control separates
them.

| Intervention | Practical control | Content-matched control |
|---|---|---|
| bounded contexts + a router | the repo's own instruction file | the same contexts concatenated into one file |
| a curated tool loadout | the harness default loadout | the same tools, undifferentiated |
| retrieval over a knowledge base | no retrieval | the retrieved chunks pasted in statically |
| a subagent topology | inline single agent | the same prompts run inline |
| a smaller model with better scaffolding | the big model, bare | the small model, bare |

**If you cannot construct a content-matched control, say so in the record.**
An eval with only a practical control answers "should I adopt this", not
"does the mechanism do anything". Both are legitimate; conflating them is not.

## Step 2: Produce the treatment cold, then freeze it

**Inspect** — the fixture has never had this intervention applied. That is
the point: what is measured is what the *generator* produces on code it has
never seen, including its setup cost.

**Generate** — run the intervention's own tooling once, against a checkout at
`base_commit`. Record tokens and wall-clock for the run.

**Then stop touching it.** Commit what came out.

| Temptation | Why not |
|---|---|
| "this output is obviously wrong" | that is a finding about the generator, and the eval reports it |
| "one small hand-fix" | hand-correcting makes the treatment arm a hand-tuned artifact no adopter will have |
| "regenerate with a better prompt" | allowed — but the prompt is now part of the treatment, and gets reported as such |

Record any achieved parameter the generator chose rather than you (context
count, chunk count, tool count). It is an outcome, not a knob.

## Step 3: Materialize arms

The shipped materializer, `adherence.mkarms`, implements the
directed-contexts arm set (A0–A5) and asserts the content-matched control
byte-for-byte:

```bash
python3 -m adherence.mkarms --repo {checkout} --out fixtures/{fixture}.arms --seed 0
```

| Output | Meaning |
|---|---|
| `"a2_matches_a3": true` | the content-matched control is valid |
| `"problems": [...]` | **stop** — the control has drifted and a win would prove nothing |

**For a different intervention, write a materializer that honours the same
two properties**, and reuse the arm-overlay contract rather than inventing one:

1. **One source of truth.** Every arm is generated from the same input in one
   run, so no arm can silently drift from another.
2. **The content-matched control is asserted, not assumed** — from the written
   artifacts, in both drift directions (content changed, and content missing).

The contract the runner expects is a directory per arm containing `_arm.json`:

```json
{"arm": "a2", "remove": ["AGENTS.md", ".subagents"], "files": ["AGENTS.md"],
 "note": "router + N contexts concatenated, seed=0"}
```

`remove` clears whatever surface the repo ships **before** the arm's own files
are written, so no arm inherits another's. The overlay is applied before the
baseline commit, so an arm's files never read as agent edits.

## Step 4: Sample tasks and freeze ground truth

**Inspect** — tasks come from the repo's own merged history, not from you.
Only work merged **after the model's training cutoff** is usable for the
primary result: a memorized task is solved with fewer exploration tokens,
which is a confound on the *primary* metric, not just on correctness.

**Generate** — sample ~40, expect to keep ~10.

Prompt = title + issue text with **all file paths and subsystem names
stripped**. The agent must locate the work; for most interventions worth
testing, locating the work is exactly what the treatment claims to help with.

Classify — do not author to a quota:

| Class | Note |
|---|---|
| single-domain | the easy case |
| cross-domain | where the treatment should cost more |
| **adversarial to the treatment** | **report separately, never averaged in.** For bounded contexts that is the hidden-constraint task; for retrieval it is the query whose answer is not in the corpus; for a tool loadout it is the task needing the tool you removed |
| ambiguous / decomposable | record the natural distribution; it is itself information |

Every intervention has a class of task it is *supposed* to lose on. Find it
and keep it. An eval whose task mix cannot produce a loss is a demo. If fewer
than ~3 turn up naturally, do not manufacture them — say the mix is thin.

**Write** `scenarios/{fixture}/ground-truth.jsonl`:

```json
{"task": 1234, "base_commit": "...", "class": "cross-domain",
 "prompt": "...", "test_cmd": "...",
 "expected_surface": ["src/api", "src/db"], "diff_files": ["..."]}
```

`expected_surface` is whatever "it went to the right place" means for this
intervention — directories in the real diff, documents that should have been
retrieved, tools that should have been called. Hand-reviewed **once**, here.

**This file is committed before any arm runs.** That ordering is the entire
reason it counts as ground truth.

## PR Checkpoint

**Title**: `fixtures: {owner}/{name} arms and ground truth — phase 3`

**Files**:
- `scenarios/{fixture}/treatment/` — the frozen generated artifact
- `scenarios/{fixture}/ground-truth.jsonl`
- `docs/FIXTURES.md` — the intervention, its two controls, generation cost, class distribution

## Next

@phase-04-floor-pilot.md
