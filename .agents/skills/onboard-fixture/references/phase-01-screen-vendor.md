---
name: phase-01-screen-vendor
description: "Screen candidates mechanically, vendor a bare mirror, pin a base commit."
parent: onboard-fixture
---

# Phase 1 — Screen and vendor

## Step 1: Screen

**Inspect** — has the screen run recently?

| Status | Action |
|---|---|
| `docs/SCREENING.md` exists and is < 30 days old | reuse it |
| missing or stale | run `make screen` (needs `gh` authenticated) |
| user named a repo directly | still run the screen against it — the criteria are the point |

**Decide** — the screen returns candidates, not fixtures. Among the passes,
prefer in this order, and say which one decided it:

1. ships a maintainer instruction file — without one there is no A1
2. ships `CODEOWNERS` — route ground truth has something to check against
3. an ecosystem with a hermetic offline story (Go and Rust are easiest; npm
   and Python are doable; anything vendoring at test time is a fight)
4. moderate size — a 1.5 GB repo materializes fine but the mirror is a chore

**Generate** — nothing yet. Record the choice and the reason.

## Step 2: Vendor a mirror

**Inspect** — `fixtures/` is gitignored. The mirror is local; the *record* is
what gets committed.

**Generate**:

```bash
git clone --mirror https://github.com/{owner}/{name}.git fixtures/{owner}-{name}.git
git -C fixtures/{owner}-{name}.git rev-parse HEAD
```

**Decide** — which commit is the baseline?

| Situation | base_commit |
|---|---|
| onboarding only | current `HEAD`, recorded explicitly |
| a specific PR is the task | that PR's **parent** commit |

Never leave it implied. "Whatever HEAD was" is not reproducible, and the
runner refuses to materialize a `repo` without a `base_commit`.

## Step 3: Confirm it materializes

```bash
python3 - <<'PY'
import subprocess, tempfile, time
d = tempfile.mkdtemp()
t = time.time()
subprocess.run(["git","clone","--local","--shared","--quiet","--no-checkout",
                "fixtures/{owner}-{name}.git", d], check=True)
subprocess.run(["git","-C",d,"checkout","--detach","--quiet","{base_commit}"], check=True)
print(f"materialize: {time.time()-t:.3f}s")
print("git status:", subprocess.run(["git","-C",d,"status","--porcelain"],
      capture_output=True, text=True).stdout or "(clean)")
PY
```

| Result | Action |
|---|---|
| < 2 s and clean status | continue |
| slow | the mirror is not local, or `--shared` was dropped |
| **dirty status at t=0** | stop. Every scope check and every `diff_coverage` number depends on this being empty. Find what the checkout writes and add it to the fixture's `ignore` list before going further |

## Generate the record

Append to `docs/FIXTURES.md`:

```markdown
### {owner}/{name}

| | |
| --- | --- |
| mirror | `fixtures/{owner}-{name}.git` ({size}) |
| base commit | `{sha}` |
| license | {spdx} |
| post-cutoff merged PRs | {n} |
| A1 surface | ships `{AGENTS.md|CLAUDE.md}` — recovered verbatim / **n/a, A0 carries the floor** |
| CODEOWNERS | yes / no |
| ecosystem | {go|rust|node|python} |
| chosen because | {the one reason that decided it} |

materialize: {x.xxx}s, `git status` clean at t=0
offline build: not yet proven — Phase 2
```

## PR Checkpoint

**Title**: `fixtures: vendor {owner}/{name} — phase 1`

**Files**:
- `docs/FIXTURES.md`
- `docs/SCREENING.md` is gitignored; do not add it

## Next

@phase-02-prove-offline.md
