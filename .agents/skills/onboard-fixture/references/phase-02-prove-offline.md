---
name: phase-02-prove-offline
description: "Prove the fixture builds and tests with the network removed. This phase can end the fixture."
parent: onboard-fixture
---

# Phase 2 — Prove it builds offline

**Carry forward from Phase 1**: `{owner}/{name}`, mirror path, `base_commit`,
ecosystem.

This is the phase that vetoes. A fixture needing the network during test does
not get a relaxed sandbox — it gets dropped. Say so out loud if it happens;
"we made an exception for this one" is how a posture stops meaning anything.

## Step 1: Pick a test subset

**Inspect** the repo's own CI for what it runs, then choose a subset that is
deterministic and under 5 minutes.

| Signal | Action |
|---|---|
| repo has a fast/unit target | start there |
| tests hit the network | exclude those packages, and record why |
| tests assert on file permissions | exclude — see the uid-0 note below |
| no test subset under 5 min exists | the fixture is a candidate no longer |

## Step 2: Warm and verify

```bash
bench/prewarm.sh fixtures/{owner}-{name}.git {base_commit} "{test subset command}"
```

Two phases inside that script: warm the dependency cache once **with**
network, then re-materialize and run the subset in a network namespace with
the cache mounted **read-only**.

| Result | Verdict |
|---|---|
| exit 0 | passed the veto — continue |
| dependency resolution errors | the cache did not warm; check the ecosystem branch in `prewarm.sh` |
| connection errors from the tests themselves | see the two harness defects below before blaming the fixture |
| genuinely needs the network | **drop the fixture.** Return to Phase 1 |

## Two harness defects that look like fixture failures

Check both before concluding a fixture fails. Each one vetoed a good fixture
during development.

| Symptom | Cause | Fix |
|---|---|---|
| `httptest`/local server tests fail with connection errors | `unshare -rn` leaves loopback **down**, and that is indistinguishable from "needs the internet" | already fixed — `prewarm.sh` brings `lo` up. Confirm you are on current `main` |
| a test asserting an unwritable path *fails to fail* ("expected an error, got nil") | `unshare -r` maps you to uid 0; root ignores permission bits | exclude that package **and write down why** — an unexplained exclusion reads as cheating later |

## Step 3: Record honestly

**Generate** — update this fixture's section in `docs/FIXTURES.md`:

```markdown
offline build: **PASS** — {n} packages, {t}s, network removed, cache read-only

    bench/prewarm.sh fixtures/{owner}-{name}.git {sha} \
      "{test subset command}"

excluded from the subset:
- `{package}` — {the actual reason, in one line}
```

If nothing was excluded, say so. An empty exclusion list is a stronger claim
than a missing one.

## PR Checkpoint

**Title**: `fixtures: {owner}/{name} builds offline — phase 2`

**Files**:
- `docs/FIXTURES.md`

## Next

@phase-03-contexts-arms-truth.md
