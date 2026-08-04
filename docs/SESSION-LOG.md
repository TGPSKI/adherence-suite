# Building adherence-suite: a session log

A narrative record of how this harness was built, what it is for, and — mostly
— what it got wrong along the way and how each error surfaced. Written while
the work was happening, so the defect accounts are first-hand rather than
reconstructed.

The short version: **a benchmark's hardest problem is not measuring the thing.
It is noticing when you are measuring something else.** Almost every defect
below was silent, and several were directional — they would have produced a
clean, publishable, wrong answer rather than an obvious failure.

---

## 1. What this is and why it exists

`adherence-suite` is a deterministic harness for measuring agent behaviour
against a repository. No LLM sits anywhere in the scoring path; every verdict
comes from a grader that a person can read and re-run.

It was built to answer one question, which arrived from an internal
platform-engineering discussion about
[directed-contexts](https://github.com/TGPSKI/directed-contexts) — a pattern
where a small always-loaded `AGENTS.md` "router" points an agent at bounded
context modules loaded on demand.

The question put to the tool's author was blunt: **"do you have evals?"**

Correctness was already enforced in Go (`contextctl`). Comparative evidence on
token spend and adherence did not exist. A second participant asked how the
pattern compares to a minimal `AGENTS.md` plus LSPs and MCPs — that question
became arm A5.

So the primary question became:

> Does routing an agent to bounded contexts cost less than injecting a
> monolith — in tokens, in round trips, and in subagent overhead — **at equal
> work delivered**?

The last clause is the whole difficulty. Cost without an adjacent success rate
is not a result; it is a way of being cheap by doing less.

---

## 2. The design, and what it refuses to do

### Arms

Six instruction surfaces, materialized from one source so they cannot drift:

| arm | always-loaded | vs a1 | on demand | what it is for |
|---|---|---|---|---|
| a0 | 0 B | −6,518 | 0 | the floor: what does *any* instruction cost or buy |
| a1 | 6,518 B | — | 0 | the practical control — the repo's own `AGENTS.md`, recovered verbatim |
| a2 | 18,060 B | +11,542 | 0 | the scientific control — same content as a3, one file |
| a3 | 3,474 B | −3,044 | 13,850 | the pattern as shipped |
| a4 | 3,289 B | −3,229 | 13,850 | the same set delivered by spawning a subagent (E3) |
| a5 | 417 B | −6,101 | 0 | minimal `AGENTS.md` + tools |

A2 exists because without it, a win for A3 could just mean "the generated text
is better written." A2 is content-matched to A3 by construction and the
equality is asserted byte-for-byte.

### The rules that make the numbers mean something

- **Falsifiers F1–F6 fixed before data**, with thresholds, and each one carries
  what its death would name next.
- **`ungradeable` is not `fail`.** A harness capability gap must never be
  scored as a model failure.
- **`purpose` on every record.** The registered analysis reads only rows marked
  `experiment`; anything unlabelled is treated as validation, because the safe
  reading of no label is not "this is real data."
- **The proxy is authoritative.** On disagreement about tokens or round trips,
  the recording proxy wins over the adapter. It counts calls by construction: a
  round trip is a request it handled.
- **Cost is meaningless unconditional.** No cost number is reportable without
  the pass rate beside it.
- **Deviations are disclosed, not forbidden.** Undisclosed deviation is the only
  thing actually prohibited.

Registered at tag `prereg-directed-contexts-v1` on a repository whose ruleset
makes `prereg-*` tags immutable and signature-required. The first attempt at
that tag was **not** actually immutable — the ruleset pattern did not match a
nested tag path, and a tamper test overwrote the registration message. Flattened
and re-verified: both delete and force-move are now refused.

---

## 3. The fixture, and the supply problem

`cli/cli` (the GitHub CLI), pinned at `e83adbc0`. Chosen against criteria fixed
before screening: MIT, post-cutoff PR supply, multiple subsystems, ships its own
`AGENTS.md` (so A1 is *recovered*, not authored) and a `CODEOWNERS`.

Three things about the fixture were stated up front as reasons the eval could
come out uninformative: it is one fixture so E4 is out of reach; it is a
heavily-starred Go repo so the codebase is familiar to the model even where a
specific PR is not; and the floor risk is real.

**The supply collapsed twice under inspection.**

The E1 screen counted 174 post-cutoff merges. Classifying them by what they
actually change:

| bucket | n | share |
|---|---|---|
| CI / agent-config (`.github/`, workflows, skill files) | 83 | 47.7% |
| dependency bumps | 42 | 24.1% |
| **Go change with its own tests** | **32** | **18.4%** |
| Go change, no tests | 13 | 7.5% |
| docs / other | 4 | 2.3% |

Nearly half of this repository's recent history is agentic-workflow
configuration, which is a fact about `cli/cli` in 2026 rather than a defect.

Then extraction reported **3 usable tasks**. The cause was a harness bug, not
the fixture: the Go module cache was warmed at `HEAD` while tasks run at their
PR's parent commit, and 125 lines of `go.mod`/`go.sum` drift meant almost every
task failed to build offline. Warming per-commit took it to **34**.

That number was one command away from being published as the fixture's supply.

---

## 4. Validation: the runs that were never meant to be results

### The n=7 single-task run

42 runs across all six arms on one scenario. Useful mainly for shaking out
plumbing, and for one exchange worth recording: a token ratio was narrated as a
verdict without the pass rate beside it — violating the suite's own rule — and
the pass figure quoted alongside it was wrong as well. The correction is why
`tok_won` and the Pareto plane exist on every surface now.

### The 210-run validation grid

10 tasks × 3 arms × 7 trials. Explicitly **not** experimental data — the framing
was stated at the time:

> *"these aren't experimental runs, this is validating the method, the process,
> the code, and the results, before we actually run the experiment."*

That sentence produced the `purpose` field. Nothing on disk distinguished a
shakedown from real data — same shape, same directory, separated only by
someone's memory of which file was which.

**What the grid actually found:**

Only **2 of 10 scenarios** landed in the registered calibration band
[0.25, 0.80]. Six floored, two hit the ceiling. `MIN_PAIRED=4` was not met, so
the registered analysis correctly refused to produce verdicts.

The diagnosis mattered more than the count: **agents located the right files
95–100% of the time and then failed the maintainers' tests.** Routing was not
the bottleneck. That has a direct implication for the eval's premise — with this
model on this fixture, `task_pass` is dominated by fix quality, so a routing
treatment has little room to move correctness and the differences must appear in
cost.

---

## 5. The defect catalogue

Grouped by what they would have done to a published result. This is the part of
the log worth reading.

### 5.1 Defects that would have produced a wrong answer, quietly

**Orphaned processes → a compounding, invisible slowdown.**
`subprocess.run(timeout=)` kills the process it started. The adapter is a shell
script, so the thing holding the GPU is a *grandchild*, and it survived every
timeout — reparented to systemd, running to completion, output read by nobody.
Measured live: two orphans still going twenty minutes later against three live
trials. Five processes contending for three slots. Not a leak but a spiral —
each timeout permanently shrinks the pool, so more trials time out. Fixed with
process-group kills, `ADH_RUN_ID` attribution via `/proc/<pid>/environ`, atexit
and signal reapers, and a startup refusal when unowned harness processes exist.

**A wall-clock deadline that could not tell a hung run from a working one.**
Trials were being killed at 900s while 110 calls deep and still advancing, and
recorded as `calls=0` because the transcript is written at the end. The results
file said those runs did nothing; they had done more than any run that finished.
Split into `--idle-timeout` (no stream activity: stuck) and `--timeout` (a
generous ceiling).

**A delegating trial read as a stalled one — twice.**
A subagent runs in its own opencode session and the root stream carries none of
its events, so a trial that dispatches one and waits is silent by construction.
Observed: parent 0 calls, subagent 5 calls and 206,689 tokens, forty tool calls
deep, stream untouched for 3m27s — reported "stalled", and `--idle-timeout 300`
would have killed it. **It would have done that more to the arms that route to
subagents, which are the arms under test.**

The first fix added opencode's session store to the activity check. That was the
right idea and the wrong file: the store runs SQLite in **WAL mode**, so live
writes land in `opencode.db-wal` and the main `.db` mtime only moves on
checkpoint. Measured 282s stale on the main file while `-wal` was 29s fresh. The
half-fix made it a coin flip against checkpoint timing — and it was caught only
because the TUI flickered.

**Subagent cost read as zero.**
The live view counted distinct session ids in the root stream, which is always
1. A trial that had dispatched two `explore` agents showed `sub=0`. Reading
totals from the root session under-reports by 3.1×, measured on s13 — and **E3
is the claim that subagent handoff is "0-cost"**, so a root-only total would
have confirmed it by construction. Child sessions and their tokens are now read
live from the store; totals are parent + children, and the split is displayed
because E3 is a claim about the child half specifically.

**Results buffered and lost.**
`ThreadPoolExecutor.map` yields in *submission* order, so a single slow trial
buffered every later result inside the executor. Caught live: a trial completed,
its out-dir was cleaned up, and its row sat unwritten behind two trials still 6+
minutes from finishing — invisible to the live view, missing from the count, and
**lost outright if the runner were killed.**

**A liveness check that killed the process it checked.**
`os.kill(pid, 0)` is the standard existence probe on POSIX. On Windows
`signal.CTRL_C_EVENT == 0`, so the call delivers a real Ctrl-C. The selftest
wrote a marker carrying its own pid, checked it, and raised `KeyboardInterrupt`
inside an unrelated subprocess several tests later — sixteen seconds into the
job, with a traceback pointing at code that had nothing to do with it. CI was
red on exactly one platform.

### 5.2 Verdicts from tests that never ran

**F3 and F6 reported TRIPPED off a `nan` CI** with k=1. A verdict from a test
that never ran reads as evidence, which is worse than no result.

**F3 reported `TRIPPED, k=4` while silently discarding 6 of 10 scenarios** —
every one dropped because neither arm ever succeeded on it, so the survivors
were exactly the tasks the model could already do. `paired_log_ratios`' own
docstring says callers must surface `dropped`; F1 did, F2/F3/F6 bound the
variable and never read it. Six of ten is also past F1's own refusal threshold.

**Exclusion criteria 1 and 2 were registered and unimplemented.** An adapter
fault was graded `fail` — indistinguishable downstream from a model that failed
the task — and schema errors went to stderr and never reached the record, so
criterion 2 could not read its own evidence. Writing an exclusion rule down is
not the same as having one.

### 5.3 Metrics that overclaimed

**`redundant_reads` counted re-read-after-edit.** The naive version scored as
waste the single most correct thing an agent does: re-reading a file it just
changed.

**`probes_to_first_edit` was gameable** and counted steps rather than bytes.
Replaced with a family — `probes_total`, `probes_after_first_edit`,
`probe_bytes`, `probe_trail` — and route correctness became measurable rather
than asserted.

**Per-arm floors were never measured.** `tok_in_marginal = tok_in_billed −
floor × calls`, and exclusion criterion 4 refuses the whole comparison when the
floor is wrong — but nothing computed one. The number was going to be typed in
by hand. `make floors` derives it from `first_call_input`, where the task prompt
is byte-identical across arms so the call-1 difference is the instruction
surface and nothing else. The cross-check is the point: a measured floor must
track the on-disk surface at one constant bytes-per-token rate. A2 and A3
independently imply 3.72 and 3.73 B/tok — agreement to 0.4%.

### 5.4 Arms and fixtures that were not what they claimed

**A1 was the router I generated, not the maintainers' file.** Generation
overwrites `AGENTS.md`, so the "practical control" had silently become the
treatment's own text. Fixed with `--a1-ref` recovery and a refusal that inspects
for generated markers.

**A4 was byte-identical to A3.** The spawn arm — the one E3 depends on — was a
copy of the inline arm, so E3's question would have been answered by an arm that
never spawns.

**`.adh-task.json` was reported as an agent edit.** The runner writes it, then
rewrites it after the agent stops to hand metrics to the grader — which turned
it into a modified tracked file. Observed as *"agent touched
['.adh-task.json']"* on a run that touched nothing.

### 5.5 The two findings that changed the experiment

**Feature PRs are unpassable by construction.**

SWE-bench-style grading applies the PR's own tests to the agent's independent
implementation. That is sound for a bug fix, whose tests call API that already
exists. It is not sound for a feature PR, whose tests call API the PR is adding:

```
create_test.go:695:5: unknown field IssueType in struct literal of type CreateOptions
```

A **compile error in the test file** — on a trial that had already passed
`diff_coverage` with 13/13 pre-edit probes inside the real diff's directories.
The agent located the work, implemented it, and failed because it did not
independently choose the maintainers' internal field name.

The separation on the validation grid was perfect:

| class | scenarios | pass rates | in band |
|---|---|---|---|
| tests name a symbol the fix introduces | 4 | 0%, 5%, 0%, 0% | **0/4** |
| tests compile; assertions disagree | 6 | 5%, 19%, 71%, 76%, 90%, 95% | 2/6 |

The tempting repair — state the required API surface in the prompt — **was
rejected**, and the reasoning is the most important paragraph in this log: a
symbol list *is* routing information. `CreateOptions.IssueType` names its
subsystem. Supplying it hands every arm a piece of exactly what the treatment is
supposed to supply, biasing the primary outcome toward the null *inside the
treatment's own channel*, and making a null result uninterpretable.

Grading at the public interface was preferred but has no supply here: of 34
tasks, 30 are unit-test-only and none acceptance-only.

**Amendment 2** registers CLI-boundary grading instead. The oracle is the PR's
own binary:

```
reference = build(merge commit)      what the PR actually shipped
candidate = build(the agent's tree)  what the agent produced
compare flag-for-flag at the CLI
```

`gh issue create --type` is a user-facing contract already present in the PR
body the agent receives, so **nothing is added to the prompt** — the grader
stops demanding an identifier it never disclosed. Assignment is by compiler:
`adherence.classify` checks each PR's tests onto its parent tree and builds
them. Result across 34 tasks: **16 unit-graded, 18 cli-graded.**

A declaration-scan heuristic was tried first and rejected for false negatives —
it missed `13675` (a field on an existing struct) and `13624`, neither of which
adds a top-level declaration.

**The prompt never asked for anything.**

The deepest defect, found last. `mkpr` uses the PR body as the prompt, and a PR
body is not reliably a task. Bodies describing the *solution* read as changelog
entries. The model replied, verbatim:

> *"I see you've shared a PR description about migrating GitHub database IDs
> from `int` to `int64`. **What would you like me to do with this?**"*

One inference call, zero tool calls, six seconds. That accounted for **8 of the
14 abandoned trials** in the validation grid. Bodies describing a *problem*
("`gh pr checks` prints a blank summary when…") were attempted normally.

It was arm-dependent — **a1 12%, a2 1%** — so the instruction surface was being
credited for whether the agent guessed the prompt was a task at all. A confound
sitting directly on the primary outcome, flattering the quitting arm twice over:
a refusal costs one call, so a1's median token count read **23% below** the work
it actually did, and **467× below** on `a1/cli-cli-13403`, where four of seven
trials refused:

```
   12,547  ABANDONED
   12,554  ABANDONED
   12,554  ABANDONED
   12,556  ABANDONED
2,725,240  worked
5,869,686  worked
6,393,551  worked
```

The fix is a fixed imperative on every prompt, naming no file, package, symbol
or API, identical for every arm. Verified by reproducing the failure and then
removing it — same scenario, same arm, same model:

| | calls | tools | tokens | behaviour |
|---|---|---|---|---|
| before | 1 | 0 | 12,553 | asks what to do |
| after | 21 | 45 | 1,180,052 | editing files |

---

## 6. The TUI, and why it was the best debugging tool built

A results matrix was adapted from `leather`'s sig-triage eval. It grew into a
live dashboard, and the pattern that emerged is the single most useful lesson of
the session:

> **Every display feature requested surfaced a real defect in the measurement
> path.** Not coincidence — putting a number on screen forces the question "is
> that true?", and each time the answer was no.

| what was asked for | what it exposed |
|---|---|
| see runs in progress | orphaned processes compounding into a death spiral |
| show subagent counts | root-only totals that would have confirmed E3 by construction |
| why does this say stalled | an idle timeout that would preferentially kill the treatment |
| why is this task 0/3 | grading that demanded identifiers never given to the agent |
| why is this already graded | completed results buffered, invisible, and lost on kill |
| where do these numbers come from | a WAL-mode staleness check watching the wrong file |

Three of those are *directional* — they would have biased toward a specific
conclusion rather than adding noise. Noise you notice.

The dashboard now carries: a live section (per-trial state, calls, tools,
subagent sessions, tokens, elapsed, budget-against-deadline, current tool), a
graded section with full check evidence, a per-(arm, scenario) rollup with
median/p90 and abandonment, an activity feed reading tool *output* from the
session store, and two reference tabs — `tasks` (what each scenario asks and how
it is judged) and `design` (what each arm is, measured off disk, plus the ground
rules) — separated by a divider from the run-data views.

Its own crop of bugs, all found by use: a cursor that walked off the end of the
list and opened a detail pane for a row never on screen; a positional cursor
that let new events replace the one being read; a phantom nested level that took
two backs to leave; sections silently truncated to what fit; `q` meaning both
"back" and "quit"; sorts that belonged to a different tab; a progress bar that
rendered as an ellipsis; JSON collapsed into one unreadable line.

---

## 7. Current status

**Branch** `harden/live-tui`, 26 commits, PR #1 open and deliberately unlabelled
so the cross-platform matrix stays out until the stack is stable.

| | |
|---|---|
| selftest | **24/24**, several checks encoding specific past failures |
| lint / `ci-local` | green |
| proxy-vs-adapter agreement | **0.000%**, live, under `--jobs 3` |
| per-trial proxy attribution in parallel | working — previously declared impossible |
| task supply | 34 verified, 16 unit-graded / 18 cli-graded |
| per-arm floors | measured, arms agree on 3.73 B/tok to 0.4% |
| registration | `prereg-directed-contexts-v1`, immutable, + Amendments 1 and 2 |
| validation run | 120 trials in flight — the first end-to-end test of the whole stack |

### Known gaps, unfixed and disclosed

- **A0, A4 and A5 have never run on a PR task.** A4 is the arm E3 depends on and
  has never been observed spawning anything.
- **`cli.surface` verifies flags exist and are accepted, not that they work.**
  Weaker than a passing unit test, which is why Amendment 2 reports CLI-graded
  tasks as a separate tier, never pooled.
- **E5 / F2 (prompt caching) is dead locally.** vLLM returns
  `prompt_tokens_details: null`, so `tok_effective` degenerates. Needs a metered
  API or the claim leaves the set.
- **Cost profile may not be affordable.** `cli-cli-13057` burned 18M tokens and
  23 minutes per trial. The registered grid at 34 tasks × 6 arms × 7 trials
  needs computing from real numbers before it is committed to.
- **A2 section ordering** is fixed across trials; the registration wants
  per-trial re-seeding.
- **Compaction events** are parsed but never observed.

---

## 8. Meta-highlights

**The suite tests itself more rigorously than it tests anything else.** 24
selftest checks, several written to encode a specific past failure so it cannot
return: the harness-fault-is-not-a-model-failure check, the live cursor
anchoring check, the proxy attribution-under-concurrency check, the
process-hygiene check that kills a grandchild and verifies it died.

**It caught its author twice.** Changing the budget semantics broke a selftest
assertion that had been correct against the old contract — the suite noticed
before CI did. And a stray-process guard now refuses to start a run when unowned
harness processes are alive, which is a rule written after watching those
orphans distort a run.

**Refusing beats guessing, everywhere.** `analyze` reports NOT TESTABLE rather
than "not tripped". `floors` exits non-zero rather than print a number it cannot
defend. The live view shows arm `?` rather than infer one. `make probe` refuses
when the arm cannot be applied. `mkscenarios` refuses unverified tasks. The
registered analysis refuses rows not marked `experiment`. Every one of those
refusals exists because the alternative once produced a confident wrong answer.

**The most dangerous defects were all silent and most were directional.** A
harness that kills the treatment for delegating, a metric that credits it for
not spawning, a grader that fails it for naming things differently, a prompt
that makes the control quit and look cheap — none of these announce themselves.
They produce a clean result with a plausible story.

**Destructive convenience was deliberately declined.** A one-command
reset-and-clear was built and then split: `make stop` reclaims processes and
sweeps temp directories but never deletes results, and prints the `rm` for you
to type. Bundling "kill the processes" with "remove the data" is how a habit
formed for the safe purpose performs the destructive one, and a `--force` flag
exists to be used.

**What the whole exercise is really about.** The eval has not run. What exists
is a harness that has been argued with, caught out, and corrected roughly thirty
times — and the argument is the artifact. A number produced by an instrument
nobody tried to break is not evidence.
