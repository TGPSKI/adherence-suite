# The directed-contexts efficiency eval

Pre-registration and status for the experiment this harness was built to run.
Falsification criteria are fixed here *before* the numbers exist; where
measurement has already contradicted a premise, the premise is struck through
rather than quietly edited.

Artifact under test: [directed-contexts](https://github.com/TGPSKI/directed-contexts).

**Primary question:** does routing an agent to bounded contexts cost less than
injecting a monolith — in tokens, in round trips, and in subagent overhead — at
equal work delivered?

Origin: an internal platform-engineering discussion. The question put to the
tool's author was *"do you have evals?"* Correctness is enforced in Go
(`contextctl`); comparative evals on token spend and adherence did not exist. A
second participant asked how the pattern compares to a minimal `AGENTS.md` plus
LSPs and MCPs — that question becomes arm A5. Participants are unnamed
deliberately; the discussion was not public and the technical substance is what
carries over.

## Registration

**Registered 2026-08-04** at tag `prereg/directed-contexts/v1`, on a repository
whose committed ruleset makes `prereg/*` tags immutable and signature-required.
Nothing below was written with results in hand, because there are none.

What is frozen by that tag: the falsifiers and their thresholds, the primary
outcome, the arm that serves as reference, the analysis procedure, the power
basis, the stopping rule, and the exclusion criteria. The analysis itself is
committed as code — `adherence.analyze` — and validated against synthetic data
in `make selftest`, so the procedure cannot be tuned to the data later.

Deviations are not forbidden; **undisclosed** deviations are. Any departure
from this document gets stated in the results with what changed and why.

## Status

The harness is built and both instrumentation gates pass. The experiment has
not run.

| | |
|---|---|
| **Instrumentation** | done — per-call tokens, round trips, cache fields, subagent attribution |
| **Proxy-vs-adapter agreement** | **0.000%** over 20 runs, call counts 189/189 |
| **Self-validation** | 16/16 with no model in the loop, negative direction proven |
| **Arms A0–A5** | materialize from one source; A2 ≡ A3 asserted byte-for-byte |
| **Fixture** | `cli/cli` — 2 of 4 gates cleared, see below |
| **Context-set generation, PR sampling, the grid** | not started |

## The fixture: cli/cli, with three caveats

`cli/cli` is the registered fixture for the minimum viable version, pinned at
`e83adbc0642994fae7c39a9a012eb34b8c81f4f1`. Two of its four gates are cleared
and two are not:

| Gate | State |
|---|---|
| screened against criteria fixed before screening | **cleared** — MIT, 174 post-cutoff PRs, 12 subsystems, ships `AGENTS.md` (so A1 is recovered, not authored) and `CODEOWNERS` |
| hermetic offline build | **cleared** — 21 packages in 1.47 s, network namespace, cache read-only; materializes in 0.066 s with a clean `git status` at t=0 |
| context set generated cold and frozen | not started |
| PRs sampled, route ground truth frozen | not started |

Three things about this choice are worth stating before results exist, because
each of them is a reason the eval could come out uninformative:

**It is one fixture, so E4 is out of reach.** Effect versus achieved *N* needs
at least two repositories with different partitioning. One point has no slope.
`adherence.analyze` reports F5 as NOT TESTABLE on a single fixture rather than
letting it read as "no interaction found".

**Popularity cuts against the contamination control.** Post-cutoff PRs are
sampled precisely to avoid memorised solutions, and 174 is ample supply. But
`cli/cli` is a heavily-starred Go repository, so the *codebase* is
well-represented in training data even where a specific PR is not. Post-cutoff
sampling limits memorised patches; it does not make the repository unfamiliar.
The mutation pass exists for this, and if the pre/post split shows a large gap
that is a finding about contamination, not a nuisance to correct away.

**The floor risk is real and it is the biggest schedule risk here.** These are
genuine Go PRs graded by the repository's own tests, run against a local 35B
model. If pass rates floor near zero, the calibration gate drops nearly
everything and there is no success-conditioned subset to compare cost on. The
registered response is to report that outcome — "the model could not do enough
of this repository's work to compare arms" is a real result about model
capability — and **not** to widen the [0.25, 0.80] band until a grid appears.
A second, easier fixture would be a new pre-registration, not a repair of this
one.

## Claims

Efficiency claims are primary. Quality claims are guardrails — they exist to
make the efficiency numbers interpretable, not as the headline.

| ID | Claim | Operationalized as |
|---|---|---|
| **E1** | Bounded contexts cost fewer input tokens per task | Marginal + total billed input tokens vs both monolith arms |
| **E2** | Exploration reads and mis-routed turns burn tokens | Inference calls to completion; probes before first edit; redundant re-reads |
| **E3** | Subagent handoff is "0-cost" | Parent + all children `total_tokens` |
| **E4** | Advantage grows with repo partitioning | Effect size vs achieved context count *N* |
| **E5** | Advantage survives prompt caching | Cache-adjusted effective tokens |
| **E6** | Advantage survives session length | Per-task cost vs task index within one session |
| **G1** | Equal work delivered | Task pass rate must not drop |
| **G2** | No silent scope shrinkage | Files touched vs the real merge diff |

## Falsifiers

The efficiency claim is **not supported** if any of these hold:

- **F1.** Marginal input tokens in directed arms are not ≥20% below the
  content-matched monolith (A2), paired-log-ratio 95% CI excluding 0.
- **F2.** Cache-adjusted effective tokens land within ±20% of the realistic
  monolith (A1). A raw-token win with no cache-adjusted win is a negative
  result on cost and gets reported as one.
- **F3.** Inference calls to completion do not decrease. Tokens and calls can
  move in opposite directions and are reported separately.
- **F4.** Task pass rate drops ≥10pp in directed arms. Cheap and worse is not a
  win.
- **F5.** No interaction with *N*: effect at N≈4 equals effect at N≈15 within
  CI. The "monolith degrades as the repo grows" story would be wrong.
- **F6.** Subagent handoff shows total parent+child tokens no lower than a
  monolith parent doing the same work inline.

Tripping F5 or F6 while F1–F3 hold is the most likely realistic outcome, and is
still publishable: it bounds where the pattern applies.

## Arms

Six instruction surfaces, identical task prompts, identical tool permissions.
`adherence.mkarms` materializes all six from one source so no arm drifts.

| Arm | Surface | Isolates |
|---|---|---|
| **A0** | none | the floor: what does *any* instruction cost or buy |
| **A1** | the repo's own maintainer-written file, verbatim | the practical control |
| **A2** | router + every context concatenated, order seeded per trial | the **scientific** control |
| **A3** | router + `.subagents/`, as shipped | the pattern |
| **A4** | A3 plus context-scoped spawn | E3 |
| **A5** | ~40-line file + language tooling | the minimal-instruction alternative |

**A2 is the whole design.** Without it, a token win might only mean you shipped
fewer words. A2 is generated by concatenating exactly the files A3 exposes, and
the equivalence is asserted byte-for-byte from the written artifacts — asserted,
not assumed, and checked in both drift directions.

**A1 is recovered, never authored.** Fixtures are chosen partly because they
ship a maintainer-written instruction file; that file *is* A1. Where a repo
ships none, A1 is `n/a` and A0 carries the floor. Nobody can claim the baseline
was built to lose.

## Cost is meaningless unconditional

The cheapest possible agent does nothing, and this pattern is structurally
exposed to that: an agent that loads one bounded context and edits only that
context's files spends fewer tokens and touches fewer files than one that read
everything — whether that was correct scoping or an incomplete job.

Three controls, all mandatory:

1. **Report cost on the success-conditioned subset**, and report pass rate
   beside every cost figure. A cost table without an adjacent pass-rate column
   is not publishable.
2. **Plot the (cost, success) plane**, not a single number. If an arm is
   left-and-down from another, that is a trade, and the chart says so where a
   ratio would not. `make matrix` renders it live.
3. **Detect early abandonment.** Trials terminating with fewer than 2 tool
   calls, or with no edit where an edit was the job, are flagged.

## Metrics

Per scenario × arm × trial, computed by `adherence.metrics` from `call` events:

| Metric | Definition |
|---|---|
| `calls` | inference calls to completion — round trips |
| `tok_in_billed` | Σ input tokens over calls, **including subagents** |
| `tok_in_marginal` | `tok_in_billed − floor × calls` |
| `tok_effective` | uncached + 1.25×write + 0.10×read |
| `probes_to_first_edit` | read/glob/grep before the first edit |
| `redundant_reads` | the same target read more than once |
| `turns_until_first_compaction` | session-length pressure |
| `per_agent` | parent vs each child, separately |
| `abandoned` | gave up early |

## The primary outcome

One number, fixed now, so there is no menu to choose from later:

> **The geometric mean of the per-scenario paired log-ratio of
> `tok_in_marginal`, directed-inline (A3) against the content-matched
> monolith (A2), among trials that passed the fixture's own tests.**

Reference is **A2, not A1**. A win against the maintainer's own file cannot
separate "bounding helps" from "you shipped fewer words", and the second is
not a finding.

Everything else — calls, billed tokens, probes, the A1 comparison, the
unconditional versions — is secondary and reported, but does not decide E1.

## Power

Simulated from **measured** within-arm variance, not an assumed figure. On the
20-run calibration set, token CV was 0.0-0.3% on 2-3 call scenarios and
18.2-19.3% on 7- and 24-call scenarios. Token CV tracked call CV within about
a point on three of four, so the variance is round-count variance and does not
shrink with per-call care. PR-derived tasks are multi-round, so **19% is the
planning value**.

At 7 trials per cell, cluster-bootstrapped over scenarios:

| true effect | k=6 | k=8 | k=10 | k=12 |
|---|---|---|---|---|
| 5% | 29% | 27% | 32% | 36% |
| 10% | 62% | 71% | 77% | 83% |
| 15% | 89% | 96% | **98%** | 99% |
| 20% | 99% | 100% | **100%** | 100% |

**At the target of 10 scenarios, a 15% difference is detected 98% of the time
and the registered 20% threshold essentially always. A 5% difference is not
detectable and will be reported as "underpowered for small effects" rather
than as absence of an effect.**

The simulation assumes the true effect is the same in every scenario. Real
between-scenario heterogeneity would widen the interval, so these are optimistic
and are stated as an upper bound on power, not a promise.

## Stopping rule

**Run the full pre-registered grid, then analyse once.** No looking at
cross-arm results and deciding whether to continue — optional stopping is how
a null becomes a finding.

| Situation | Registered response |
|---|---|
| grid completes | analyse, report every cell |
| a cell fails to execute (adapter, endpoint, infrastructure) | re-run that cell up to 3 times; if still failing, report as missing with the reason |
| the calibration pilot yields fewer than 6 usable scenarios | **stop and report that**, do not relax the [0.25, 0.80] band to manufacture a grid |
| results look bad partway | irrelevant; finish the grid |

Interim looks at *harness health* (calibration gate, schema violations, crash
rates) are allowed and expected. Interim looks at *arm comparisons* are not.

## Exclusion criteria, fixed now

A trial or scenario is excluded only for a reason on this list, and every
exclusion is counted in the report:

1. **Adapter failure** — the harness did not complete a run (`adapter` check
   failed). Not a model result.
2. **Schema violation** — the transcript failed validation, so its cost
   figures are untrustworthy.
3. **Calibration gate** — a scenario whose pooled pilot pass rate falls
   outside [0.25, 0.80]. Dropped scenarios are logged and **never
   re-authored to discriminate**.
4. **Non-positive marginal tokens** — indicates the per-arm floor was
   mis-measured. `adherence.analyze` refuses the whole comparison rather than
   analysing survivors, because the survivors are biased toward high-token
   scenarios.

Not on the list, and therefore not permitted: excluding a trial for being an
outlier, for a surprising result, or for a scenario turning out to favour the
control.

## Statistical plan

- **Paired on scenario.** Between-scenario token variance dwarfs between-arm
  differences, so every arm sees every scenario and the scenario is the cluster.
- **Log-ratios**, geometric mean across scenarios, cluster-bootstrap 95% CI over
  scenarios. Never a raw mean of tokens across heterogeneous scenarios.
- **Medians and IQR** beside means. Token spend is skewed.
- **7 trials per cell**, justified by measured CV. A ~20% difference is
  detectable; ~5% is not, and that gets stated rather than glossed.
- **Holm** across the six pre-registered tests, implemented in
  `adherence.analyze.holm`. Tests that could not run do not consume alpha and
  are reported **NOT TESTABLE** — never as "not tripped", which reads as
  evidence for the treatment.
- **The analysis is code, not a description.** `python3 -m adherence.analyze
  results.jsonl` emits the verdict table. `make selftest` proves it detects a
  planted 40% effect, trips F1 when no effect exists, trips F4 on a 15pp
  pass-rate drop, and refuses when the floor is missing or mis-measured.

## Confounds

| Confound | Handling |
|---|---|
| Monolith is a strawman | Two monolith arms; A2 content-matched by construction |
| Harness floor inflates ratios | Per-arm floor calibration; report total *and* marginal |
| Adapter token accounting unverified | Recording proxy is authoritative; 2% agreement gate |
| Prompt caching | `tok_effective`; needs a metered API, not the local endpoint |
| Cheapness via doing less | Success-conditioning, Pareto plane, `abandoned` flag |
| Tokens vs calls move oppositely | F3 separates them; both reported |
| Position effects | A2 section order randomized per trial |
| Contamination | Post-cutoff PRs only; pre/post split; mutation pass |
| Generation quality | Generation is part of the treatment: run once, freeze, commit the output. A bad context set is a result, not a bug to fix mid-run |
| Harness capability skew | `ungradeable`, never `fail` |

Because fixtures are foreign, the baseline is maintainer-written, tasks are
maintainer-authored, correctness is graded by maintainer-written tests, token
counts come from a proxy rather than the tool's own reporting, and context sets
are whatever the generator produces cold — **the experimenter controls the
treatment and almost nothing else.** That is what would make a positive result
believable from the author of the tool.

## What measurement already contradicted

Four premises this design was written on turned out to be wrong. They are
recorded because a pre-registration that quietly absorbs its own corrections is
not a pre-registration.

**The session token scalar is billed tokens, not final context size.** This was
the open question that could have invalidated every cross-arm comparison in the
pattern's favour. Measured: `info.tokens.input` = 93,519 = the exact sum of 9
per-call inputs, where the final context was 11,137. Resolved in favour of the
existing numbers.

**~~The harness floor is ~17k per call and s05 is a single inference call.~~**
Both halves wrong. s05 is 2–3 calls at a **~9.5k** floor; the 17,046 figure was
one session *total* over two calls read as one call. Since `tok_in_marginal =
tok_in_billed − floor × calls`, the marginal decomposition is only correct with
a measured per-arm floor.

**Subagent cost was invisible.** A dispatched subagent runs in its own session,
and neither the root stream nor the root export contains one of its calls.
Measured on s13: the root reports 10 calls / 98,962 input tokens against a true
39 calls / 307,495 across six agents. **E3 is the claim that subagent handoff is
"0-cost" — reading it off the root session would have confirmed E3 by
omission.** Child sessions are now discovered and attributed per agent, and
`make selftest` fails if cost is ever read from the aggregate `usage` event again.

**Auxiliary calls exist.** The harness makes session-title-generation calls
carrying no tool schemas — 19 across 20 calibration runs, ~575 tokens each, and
absent entirely on some runs. Real spend, but not attributable to the
instruction surface, so they are excluded from arm comparisons and reported
separately. Stating the rule beats quietly picking a total.

## Two costs of parallelism

`--jobs > 1` buys throughput and spends two things:

- **Wall-clock stops being comparable across arms.** Latency becomes a function
  of GPU scheduling. Contended runs are stamped `contended: true` rather than
  left to be compared against serial ones later.
- **Per-trial proxy attribution becomes impossible.** The proxy mark is one
  piece of state and *nothing in an inference request identifies the trial* —
  measured: the sandbox path appears nowhere in the request body, not even in
  the system prompt. The runner skips marks and warns rather than writing an
  attribution that is wrong. **Calibration runs serially.**

Concurrent `opencode run` against one `XDG_DATA_HOME` also dies with `database
is locked`, so each parallel trial gets its own data home.

## Scope

The minimum viable version is a strict prefix of the full grid — same arms, same
protocol — so running it first costs only calendar time.

| | Minimum viable | Full grid |
|---|---|---|
| Runs | ≈210 | ≈4,000 |
| Fixtures | 1 | 4–5 |
| Arms | A1, A2, A3 | A0–A5 |
| Protocol | single-task | + multi-task, subagent, metered |
| Answers | E1, E2, G1, G2 | + E3, E4, E5, E6 |

**What the MVV licenses you to say:** *on a repository neither the tool nor its
author had seen, with the maintainers' own instruction file as baseline and the
maintainers' own tests as the grader, bounded contexts cost N% fewer input
tokens and M% fewer round trips than the identical content delivered as a
monolith, at no loss in task pass rate.*

**What it does not:** anything about *when* to adopt. No break-even, no
crossover, no guidance on repo size or session length. The three claims it
cannot reach (E4, E5, E6) are the most interesting ones, and two are where the
pattern is most likely to be found wanting. Publishing the MVV alone means
publishing only the claims most likely to come out favourable, which is a real
credibility cost even when every number in it is honest.

**The decision point is after the MVV, not now.** If it shows no token advantage
over the content-matched monolith, the honest move is falling back to the
ergonomic claims — deterministic validation, drift detection, `CODEOWNERS`
alignment — none of which need an eval. Saying so plainly beats a contested
benchmark.

## Known gaps

- **E5 is not testable locally.** vLLM returns `prompt_tokens_details: null`, so
  cache read/write are always 0 and `tok_effective` degenerates to
  `tok_in_billed`. Prompt-caching claims need a metered API. If that is
  unfunded, E5 comes out of the claim set rather than being argued from local
  numbers.
- **`compaction` events are unconfirmed.** The event name is present in the
  opencode binary but no session here has been long enough to trigger one, so
  the parsing path is written and untested.
- **Multi-task sessions have no driver yet.** E6 needs one; nothing else does.
- **Verified against one harness version** (opencode 1.18.10). The stream
  format is a moving target — which is exactly why the proxy exists and why it
  wins on disagreement.
