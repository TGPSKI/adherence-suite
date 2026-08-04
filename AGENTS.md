# AGENTS.md

Routing table for agents working in this repo. Read the row that matches your
task; don't read the rest.

## What this is

A benchmark that measures whether an agent harness plus a model obeys operator
instructions, and what obeying costs in tokens and round trips. Grading is
deterministic — no model is ever in the scoring path. The suite validates
itself with no model at all, and that property is the reason it can be trusted;
protect it.

## The three rules

1. **No grader calls a model.** Every check inspects an artifact: a git diff,
   file contents, a command in the transcript, a regex over the final message,
   a canary. If a check can't be written deterministically, it isn't a check.
2. **Harness gaps are `ungradeable`, never `fail`.** If an adapter can't emit
   the event a check needs, return `skip()`. Scoring a missing capability as a
   model failure is the single easiest way to make this repo lie.
3. **Cost comes from `call` events, never from `usage`.** The aggregate `usage`
   event mirrors the harness's own session total, which is root-session only
   and excludes every dispatched subagent — measured at 3.1× under-report.
   `make selftest` fails if this is ever violated.

## Where things live

```
src/adherence/
  runner.py          scenario × arm × trial -> adapter -> grade -> results.jsonl
  selftest.py        validates the suite itself; no model needed
  report.py          results -> markdown scoreboard with paired analysis
  schema.py          frozen transcript + result schema, with goldens
  metrics.py         derived cost metrics; pure functions over a transcript
  gradelib.py        shared grader helpers
  run_tests.py       stdlib test runner used when pytest is absent
  proxy.py           recording proxy — the authoritative token/call counter
  calibrate.py       proxy-vs-adapter agreement gate
  mkarms.py          materializes instruction-surface arms A0–A5
  screen_repos.py    mechanical fixture screening
  suitedata.py       one loader behind every viewing surface
  table.py           one-shot snapshot · matrix_tui.py  interactive matrix
  adapters/          opencode (stream + export + child sessions), api.py
  tui/               VENDORED from leather — re-copy, never edit here
adapters/opencode.sh the shell adapter; the `--adapter` contract
scenarios/sNN/       scenario.yaml + fixture/ + grade.py
bench/               sandbox layer: isolate (+ proxy), preflight, prewarm
docs/EVAL.md         the experiment this harness was built for
```

Everything importable lives in `src/adherence/`. Run modules, don't run
files: `PYTHONPATH=src python3 -m adherence.selftest`. `bench/isolate.sh`
exports `PYTHONPATH` itself, so anything under it just works.

## Two skills ship with the repo

| Skill | Use it when |
|---|---|
| `.agents/skills/adherence-triage` | a cell is red and you don't yet know whether it's the model, the grader, the adapter, the proxy, or a coordinate mismatch. Start here — the first check is free |
| `.agents/skills/onboard-fixture` | turning a real repository into a fixture: screen, vendor, prove offline, generate contexts, freeze ground truth, pilot. Four phases, one PR each |

Both are symlinked into `.claude/skills/` and `.cursor/skills/` for IDE
discovery. The canonical copy is under `.agents/`.

## Workflow

```bash
make check      # compile + schema + selftest — what CI gates on
make selftest   # graders both directions, cost metrics, test runner
make schema     # frozen schema vs its goldens
make lint       # ruff (dev-time only; never a runtime import)
make ci-local   # CI's own steps, extracted from the workflow file
```

Editing the workflow means running `make ci-local` before pushing — it runs
what is committed, under `bash -e`, instead of what you retyped. Two broken
steps reached `main` because a hand-copy dropped the shell options.

`make check` needs no model, no GPU, and no network. Run it before you claim
anything works. CI runs it on 3.10 and 3.14, then runs `selftest` a second time
with the `pytest` import deliberately broken.

## Constraints that bite

- **Stdlib only.** Runtime code imports nothing outside the standard library.
  ruff is the one dev-time tool. A benchmark that needs a dependency resolver
  has a variable it didn't intend to measure.
- **`sys.executable`, not `python3`.** Graders shell out to the interpreter
  running the suite. `python3` isn't a command on Windows, and on a box with
  several interpreters it isn't necessarily this one.
- **Never widen `PROBE_TOOLS` casually.** It's `read`, `glob`, `grep`. Adding to
  it silently changes `probes_to_first_edit` for every scenario already measured.
- **`--jobs > 1` costs two things:** wall-clock comparability, and per-trial
  proxy attribution. Calibration runs serially. See docs/EVAL.md.
- **`src/adherence/tui/` is vendored verbatim** and excluded from ruff. Reformatting it
  guarantees a conflict on the next re-copy for no benefit.

## Adding a scenario

`scenarios/sNN/` holds `scenario.yaml`, `fixture/`, `grade.py`; register the id
in `suite.yaml`. Then — and this is the part that isn't optional — add **both**
actors to `ACTORS` in `selftest.py`: one compliant, one reproducing the original
failure. `make selftest` must print `compliant=pass violator=caught`.

Ground the scenario in something that actually happened. A scenario invented to
be hard measures puzzle-solving; one recovered from an incident measures the
thing that costs people time.

Set `expects_edit: 0` when the correct outcome is a report rather than a code
change, or the `abandoned` metric will flag compliant behaviour.

## Adding a metric

Metrics are pure functions over a transcript in `src/adherence/metrics.py`. Add the
expected value to `COST_EXPECTED` in `selftest.py` **computed by hand**, and add
a mutation that must change the answer. The suite currently proves it catches an
injected off-by-one across seven metrics — keep that.

## Touching anything that produces a token count

Re-run the calibration gate serially and paste the result:

```bash
ADH_PROXY_LOG=runs/proxy.jsonl make all TRIALS=5 OUT=runs/cal.jsonl
make calibrate FILES=runs/cal.jsonl PROXY=runs/proxy.jsonl
```

The gate is 2%. It currently reads 0.000%. If it moves, the proxy is right and
the adapter is wrong.

## Style

Match the file you're editing. Comments explain *why*, especially where a
measurement contradicted an assumption — several comments here exist purely to
stop someone "fixing" a deliberate choice. Leave those, and add your own when
you find the next one.

Update `CHANGELOG.md` under `## [Unreleased]` for anything user-visible.
