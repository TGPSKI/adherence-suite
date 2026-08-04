# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Pre-registration of the directed-contexts eval**, frozen at tag
  `prereg/directed-contexts/v1`. `docs/EVAL.md` now fixes the primary
  outcome, the reference arm, the power basis, the stopping rule, the
  exclusion criteria, and a deviation-disclosure policy before any arm runs.
- **`adherence.analyze`** — the analysis as committed code rather than a
  description, so it cannot be tuned to the data. Emits a Holm-corrected
  verdict per falsifier and reports **NOT TESTABLE** where a precondition is
  missing, never "not tripped". `make analyze`.
- `ruleset-prereg-tags-immutable.json` — `prereg/*` tags are immutable and
  signature-required, which is what makes the registration mean anything.

- `make ci-local` — extracts CI's own `run:` blocks from the workflow file and
  executes them under the shell GitHub uses. Added after two broken steps
  reached `main`: hand-copying a step into a terminal drops `set -euo
  pipefail`, which is precisely what the first failure depended on.

### Fixed

- **Graders could not run tests on Windows.** The interpreter path was
  quoted with `shlex.quote`, which is POSIX-only — on Windows it wraps
  `C:\...\python.exe` in single quotes that `cmd.exe` rejects. Replaced the
  shell string with a list-form `subprocess.run`, which needs no quoting at
  all. Caught by the cross-platform matrix, which is why it exists.

## [0.2.0] - 2026-08-03

The instrumentation release. v0 could tell you whether an agent obeyed; it could
not tell you what obeying cost. Adding that turned up four things the design was
wrong about, all recorded in [docs/EVAL.md](docs/EVAL.md).

### Added

- **Per-inference-call accounting.** `call` events carry input, output, cache
  read/write, duration, stop reason, and the agent they belong to. Sourced from
  `opencode run --format json`, which carries per-step tokens directly — no
  session-id resolution and no export-schema drift.
- **`adherence.proxy`** — a recording proxy in front of the inference endpoint,
  logging every request and response with its usage block. It counts round trips
  by construction and adds `stream_options.include_usage` so streaming responses
  carry usage at all.
- **`adherence.calibrate`** — the agreement gate. Proxy and adapter must match
  within 2%; on disagreement the proxy is authoritative and the adapter's
  figures are dropped. Current result over 20 runs: 0.000% aggregate, 0.000%
  worst run, call counts 189/189.
- **Subagent cost attribution.** `adherence.adapters.children` discovers
  sessions dispatched by a run and folds their calls in, per agent.
- **`adherence.schema`** — one frozen schema for transcripts and result records,
  with constructors, a validator, goldens, and mutations that must be rejected.
  Every adapter, the runner, and selftest build events through it.
- **`adherence.metrics`** — derived cost metrics as pure functions over a
  transcript: `calls`, `tok_in_billed`, `tok_in_marginal`, `tok_effective`,
  `probes_to_first_edit`, `redundant_reads`, per-agent split, `abandoned`.
- **Instruction-surface arms.** `adherence.mkarms` materializes A0–A5 from one
  source and asserts A2's content is byte-identical to what A3 exposes, checked
  in both drift directions. `--arms a1,a2,a3 --arms-dir DIR`.
- **`adherence.run_tests`** — stdlib test runner used when pytest is absent, so the
  suite genuinely needs nothing installed.
- **Results viewers.** `make table` (one-shot) and `make matrix` (interactive:
  cells, per-arm rollup with paired geometric ratios, the (cost, pass-rate)
  Pareto plane, and the calibration gate live), over one shared loader so no two
  surfaces can disagree about a number.
- **Real-repository fixtures.** `adherence.screen_repos` scores candidates
  against criteria fixed before screening and writes a rejection log;
  `bench/prewarm.sh` proves a fixture builds and tests with the network removed
  and its dependency cache read-only.
- **`--jobs N`** for parallel trials, and `--floor` for the per-arm
  harness floor that makes marginal tokens exact.
- Paired analysis in `report.py`: per-scenario log-ratios, geometric means,
  cluster-bootstrap CIs, success-conditioned and unconditional.
- **Two skills ship with the repo.** `.agents/skills/adherence-triage`
  separates a model failure from a grader bug, an adapter gap, or a
  coordinate mismatch, cheapest check first.
  `.agents/skills/onboard-fixture` is a four-phase workflow for turning a
  real repository into a fixture — screen, prove offline, freeze ground
  truth, pilot. Symlinked into `.claude/` and `.cursor/`.
- Full repository frame: `AGENTS.md`, `CHANGELOG.md`, `CODE_OF_CONDUCT.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`, CODEOWNERS, four committed
  rulesets, an issue template, `ruff.toml`, and label-gated CI with SHA-pinned
  actions across Python 3.10 and 3.14 plus a cross-platform matrix.

### Changed

- **Cost is read from `call` events, never from the aggregate `usage` event.**
  `usage` mirrors the harness's own session total, which is root-session only.
  Measured on s13: 10 calls / 98,962 input tokens reported against a true 39
  calls / 307,495 across six agents — a 3.1× under-report. `usage` is retained
  unchanged so the v0 scoreboard stays comparable.
- **Fixture materialization** uses `git clone --local --shared` from a bare
  mirror at a pinned commit instead of `copytree` + `git init` + `git add -A`.
  A 164 MB repository materializes in 0.066 s with a clean `git status` at t=0.
- **Harness noise filtering** moved from a hardcoded set in `gradelib` to a
  per-fixture ignore set installed in the sandbox's own `.git/info/exclude`, so
  `git status` applies it uniformly and it never appears in the agent's tree.
  The old set knew only about `opencode.json` and Python caches; a real repo's
  `node_modules/` or `target/` would have read as agent edits.
- **Session id comes from the run's own event stream**, not from
  `opencode session list | head -1`, which is ordered by global recency and
  under parallelism hands back another trial's transcript.
- **`bench/isolate.sh` starts the recording proxy** when `ADH_PROXY_LOG` is set,
  absorbing the separate wrapper that used to do it.
- **Graders shell out through `sys.executable`.** `python3` is not a command on
  Windows and is not necessarily the right interpreter anywhere else.
- **All Python moved under `src/adherence/`** as a real package. Modules are
  run, not files: `PYTHONPATH=src python3 -m adherence.selftest`.
  `bench/isolate.sh` exports `PYTHONPATH` itself, so anything under it just
  works — a caller cannot prefix `PYTHONPATH=src` on a command it runs,
  because `isolate.sh` execs `"$@"` and a `VAR=value` prefix would be argv[0].
- **`bench/isolate.sh` is reentrant.** `make all` wraps itself in it, so a
  caller who also wraps nests two instances; the inner one no longer fights
  the outer for the proxy port.
- Documentation consolidated: the experiment design and its execution graph
  merged into `docs/EVAL.md`, fixture screening and records into
  `docs/FIXTURES.md`.

### Fixed

- **A missing `pytest` scored as a model failure.** s12 fell back to bare
  execution only on pytest's "collected nothing" exit code (5); a missing pytest
  exits 1, so every s12 run on a box without it failed and blamed the agent.
  Now detected explicitly, with a stdlib fallback — and CI runs the whole
  self-test with the `pytest` import broken to keep it that way.
- **`abandoned` flagged compliant behaviour.** As specified ("<2 tool calls or
  no edit") it fired on 8 of 13 scenarios whose correct outcome is a report;
  s04's right answer is to stop and edit nothing. Gated on `expects_edit`.
- **Parallel trials died with `database is locked`.** Concurrent `opencode run`
  against one `XDG_DATA_HOME` contends on a single sqlite store; each parallel
  trial now gets its own data home.
- **Streaming calls lost their usage block** when the client hung up early — the
  proxy now drains the upstream response so the call is recorded rather than
  counted as zero tokens.

### Removed

- `bench/with-proxy.sh` and `bench/verify-agents.sh`, folded in or orphaned.
- The internal handoff document, superseded by `AGENTS.md` and `docs/EVAL.md`.

## [0.1.0] - 2026-07-07

### Added

- 13 adherence scenarios (`s01`–`s13`), each a trap recovered from a real
  production failure, each with a deterministic grader.
- `selftest.py` — validates every grader against a scripted compliant actor and
  a scripted violator, with no model in the loop.
- Harness-agnostic adapter contract and normalized `transcript.jsonl`; adapters
  for opencode and for any OpenAI-compatible endpoint.
- `bench/` sandbox layer: benchmark-only opencode config with no `ask` rules,
  `XDG_CONFIG_HOME` isolation, and a preflight that spends one turn before a
  full run.
- `report.py` scoreboard with pass@1, per-check adherence, and variance across
  trials.
