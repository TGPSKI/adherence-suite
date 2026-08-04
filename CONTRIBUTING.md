# Contributing

## Setup

There isn't one. Clone it and run the checks — the suite is stdlib-only and
validates itself with no model, no GPU, and no network.

```bash
git clone git@github.com:TGPSKI/adherence-suite.git
cd adherence-suite
make check
```

`make help` lists everything. Targets are grouped by what they cost: validation
is free, viewing is read-only, and the two targets that spend GPU time say so.

## Development commands

| command | what it does |
|---|---|
| `make check` | compile + schema + selftest — exactly what CI gates on |
| `make ci-local [JOB=lint]` | CI's own steps, from the workflow file, under CI's shell |
| `make selftest` | every grader against compliant *and* violator actors, plus cost metrics, the noise filter, and the stdlib test runner |
| `make schema` | the frozen transcript/result schema against its goldens, including mutations that must be rejected |
| `make lint` | ruff; dev-time only, never a runtime import |
| `make table` / `make matrix` | read results; never spends a GPU-second |
| `make calibrate` | proxy-vs-adapter agreement — the gate on every token number |
| `make run S=s05` / `make all` | these spend GPU time |

CI additionally runs `selftest` with the `pytest` import broken, because the
"nothing to install" claim is invisible on a machine that has it.

**Changing `.github/workflows/ci.yml`? Run `make ci-local` first.** It extracts
CI's own `run:` blocks from the workflow file and executes them under the same
shell GitHub uses. Hand-copying a step into a terminal drops `set -euo
pipefail`, and that is how a broken step reached `main` twice — once on a
pipeline whose left side was *supposed* to exit non-zero, once on a path that
had moved.

## The rule everything else follows from

**No grader may call a model.** Every check inspects an observable artifact. If
a check can't be written deterministically it isn't a check, it's an opinion,
and it doesn't go in the scoring path. A judge hook exists as an extension
point; it is unscored and stays that way.

## Adding a scenario

```
scenarios/s14/
  scenario.yaml     category, prompt, timeout, expects_edit, optional repo/base_commit
  fixture/          the starting tree the agent sees
  grade.py          grade(sandbox, transcript, final) -> list[Check]
```

Register the id in `suite.yaml`. Then satisfy the bar:

**It isn't done until selftest catches a violator.** Add both actors to `ACTORS`
in `selftest.py` — one compliant, one reproducing the original failure — and
`make selftest` must print `compliant=pass violator=caught`. A grader that has
only ever seen correct input validates nothing, and the first time that matters
will be the first time it's tested.

**Ground it in something that happened.** Every scenario here is a trap a real
system fell into. A scenario invented to be hard measures puzzle-solving.

**Write against the schema, not against a harness.** Use `adherence.schema`'s
constants and `adherence.gradelib`'s helpers. If a check needs an event an adapter
may not emit, return `skip()` — ungradeable, never fail.

**Set `expects_edit: 0`** when the right answer is a report rather than a code
change. s04's correct behaviour is to stop and edit nothing; flagging that as
abandonment puts a red column on compliant work, and a flag that cries wolf is
one nobody reads later.

If your scenario runs tests the agent wrote, call
`gradelib.run_python_tests(sandbox, path)` rather than shelling out yourself. It
prefers pytest, falls back to bare execution only on "collected nothing", and
falls back to `adherence.run_tests` when pytest is missing. The subtle failure it
exists to prevent: running a pytest-style file as a plain script defines the
test functions without calling them, exits 0, and reports a green for a test
that never ran.

## Adding a metric

Metrics live in `src/adherence/metrics.py` as pure functions over a transcript, so they're
testable with no model. Add the expected value to `COST_EXPECTED` in
`selftest.py` **computed by hand**, and add a mutation that must change the
answer. The suite proves it catches an injected off-by-one in call counting
across seven independent metrics; keep that property.

## Adding an adapter

An adapter is any executable:

```
adapter <sandbox> <model> <prompt-file> <out-dir> [target-agent]
```

Run the harness with `cwd=sandbox`, write `<out-dir>/transcript.jsonl` and
`<out-dir>/final_message.txt`, exit 0. Build events through `adherence.schema`'s
constructors and validate before writing.

If the harness reports per-call usage, emit `call` events. If it dispatches
subagents into separate sessions, **find them** — reading cost off the parent
under-reports it (measured: 3.1×), and any "cheap subagent" claim measured that
way is confirmed by omission rather than by measurement.

## Cost numbers

Touch anything that produces a token count and the calibration gate gets re-run,
serially, with the result in the PR:

```bash
ADH_PROXY_LOG=runs/proxy.jsonl make all TRIALS=5 OUT=runs/cal.jsonl
make calibrate FILES=runs/cal.jsonl PROXY=runs/proxy.jsonl
```

Serially, because `--jobs > 1` cannot attribute proxy calls to trials — nothing
in an inference request identifies which trial made it. The proxy is
authoritative on disagreement.

## Pull requests

- `make check` passes. CI gates on it across Python 3.10 and 3.14.
- New or changed graders come with both selftest actors.
- Anything user-visible gets a `CHANGELOG.md` entry under `## [Unreleased]`.
- Add the `full-test` label to run the cross-platform matrix on your PR; it runs
  automatically on merge to `main`.

## Style

Match the file you're editing. Stdlib-only is not negotiable — it's what lets
this run on a benchmark box with nothing installed.

Comments explain *why*, especially where a measurement contradicted an
assumption. Several comments in this repo exist purely to stop someone
"fixing" a deliberate choice. Leave those in place, and add your own when you
find the next one.
