---
name: onboard-fixture
description: "Onboard a real repository as an eval fixture and build an eval on it: screen, vendor, prove offline, define arms and controls, freeze ground truth, pilot."
metadata:
  author: TGPSKI
  version: "1.0"
compatibility: "gh, git, python3, unshare (Linux), an OpenAI-compatible endpoint"
---

# Onboard a fixture and generate an eval

Turns an untouched open-source repository into a fixture this suite can run
arms against. Four phases, one PR each, days to weeks apart.

**The intervention is yours to choose.** This suite compares *arms* — whatever
you put in front of the model. An arm can be an instruction file, a tool
loadout, a retrieval strategy, a subagent topology, a system-prompt variant,
or a different model. `docs/EVAL.md` documents one instance (bounded contexts
vs a monolith) in full; treat it as a worked example of the shape, not as the
only thing this runs.

The order is not arbitrary. Every phase before the last exists to make the
last one honest: ground truth is frozen **before** any arm runs, and the
offline build is proven **before** anything is generated from the repo.

## Prerequisites

- A clean session. Sync and rebase on `main` before each phase — progress is
  detected from merged state, not from your working tree.
- `make check` green.
- Read [docs/EVAL.md](../../../docs/EVAL.md) §Fixtures and §Cost is meaningless
  unconditional. This workflow implements them; it does not re-argue them.

## Design principles

1. **The offline build is a veto, not a negotiation.** A fixture that needs
   the network during test does not become a fixture with a relaxed sandbox.
   It stops being a candidate.
2. **Ground truth is frozen before arms run.** Route ground truth and the PR
   set are committed in Phase 3. If they are still movable when arm results
   arrive, they will move.
3. **Generation is part of the treatment.** Whatever the intervention
   produces is produced cold, committed as produced, and never hand-corrected.
   A bad artifact is a result, not a bug to fix mid-run.
4. **A content-matched control or an admission.** If the treatment changes both
   what the model is told and how it is delivered, one control cannot separate
   them. Build the content-matched arm, or state in the record that the eval
   answers "should I adopt this" and not "does the mechanism do anything".
5. **Ask less, infer more.** Everything the screen already answered is on
   disk. Carry it forward; do not re-ask.
6. **The practical control is recovered, never authored.** It is whatever the
   repo already ships. If it ships nothing, say `n/a` and let the empty arm
   carry the floor. Writing the baseline yourself is how a benchmark gets
   accused of building it to lose.

## Entry point

**Ask the user exactly one thing:**

> Which repository? (`owner/name`, or "screen" to pick from candidates)

Everything else is derived from disk or from the previous phase.

## Progress detection

Read `docs/FIXTURES.md` on merged `main`.

| State on disk | Phase complete |
|---|---|
| No `### owner/name` section | none |
| Section exists with mirror URL + `base_commit` | 1 |
| Section also reads `offline build: PASS` with a test subset | 2 |
| `scenarios/<fixture>/ground-truth.jsonl` exists | 3 |
| Section records a kept-scenario count from the pilot | 4 — done |

Local uncommitted files do not count. An unmerged branch does not count.

## Determine phase

| Detected | Action |
|---|---|
| none | Phase 1 — Screen and vendor |
| 1 | Phase 2 — Prove it builds offline |
| 2 | Phase 3 — Arms and ground truth |
| 3 | Phase 4 — Floor and calibration pilot |
| 4 | Fixture is ready. Run the grid; see docs/EVAL.md §Scope |

Route to the **first** incomplete phase. Do not skip ahead.

## Phase files

| Phase | File | Produces |
|---|---|---|
| 1 | @references/phase-01-screen-vendor.md | a pinned mirror and a fixture record |
| 2 | @references/phase-02-prove-offline.md | the veto gate, passed or the fixture dropped |
| 3 | @references/phase-03-contexts-arms-truth.md | the intervention, its controls, arms, frozen ground truth |
| 4 | @references/phase-04-floor-pilot.md | per-arm floors and the kept scenario set |
