"""Grader for PR-derived scenarios. One implementation, every such task.

The protocol registered in docs/EVAL.md Amendment 1:

  1. The agent works from the PR's **parent** commit and never sees the
     tests.
  2. After it stops, the harness checks out **only** the PR's `_test.go`
     files from the merge commit and runs the affected packages.
  3. Those tests decide `task_pass`. They were written by the repository's
     maintainers, not by the experimenter, and mkpr already proved they
     fail at the parent and pass with the real fix.

The agent is not asked to write tests. Grading it on tests it authored
would measure test-writing confounded with the fix, and an agent that
writes a weak test would score as having fixed the bug.

Alongside correctness, two guardrails the cost numbers are meaningless
without:

  `diff_coverage`  — of the files the real PR changed, how many did the
                     agent touch. Under-coverage is how a token "win"
                     turns out to be an unfinished job.
  `scope`          — files touched outside the real diff, reported but
                     not failed: a different valid approach may touch
                     different files, and scoring that as a violation
                     would punish the arm that reasoned better.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from adherence.gradelib import Check, bad, git_changed_files, ok, sh, skip


def _task(sandbox):
    """The frozen task record the runner materialized alongside the tree."""
    p = sandbox / ".adh-task.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def grade(sandbox, transcript, final) -> list[Check]:
    checks: list[Check] = []
    task = _task(sandbox)
    if not task:
        return [skip("pr.task_record",
                     "no .adh-task.json in the sandbox; the runner did not "
                     "materialize a PR task here")]

    changed = set(git_changed_files(sandbox))
    real = set(task.get("code_files", []))
    cli_graded = task.get("grader") == "cli"

    # --- did it do the job the PR did ---------------------------------
    # Under the unit grader this is a real failure: the PR's own tests are
    # the verdict, and an agent that touched none of the PR's files did not
    # do the work. Under the CLI grader it is REPORTED, not failed --
    # `cli.surface` already proves the shipped behaviour flag-for-flag
    # against the PR's own binary, and insisting on the same file set
    # would smuggle back the "match the maintainers' structure" demand
    # that Amendment 2 exists to remove.
    hit = sorted(changed & real)
    coverage = len(hit) / len(real) if real else 0.0
    if hit:
        checks.append(ok("pr.diff_coverage",
                         f"touched {len(hit)}/{len(real)} of the real diff's "
                         f"files ({coverage:.0%}): {hit[:6]}"))
    elif cli_graded:
        checks.append(skip("pr.diff_coverage",
                           f"touched none of the {len(real)} files the real "
                           f"PR changed. Reported, not failed: this task is "
                           f"graded at the command line, where a different "
                           f"file layout with the same behaviour is a valid "
                           f"implementation"))
    else:
        checks.append(bad("pr.diff_coverage",
                          f"touched none of the {len(real)} files the real "
                          f"PR changed; agent touched {sorted(changed)[:6]}"))

    extra = sorted(changed - real)
    checks.append(skip("pr.scope",
                       f"also touched {len(extra)} file(s) outside the real "
                       f"diff: {extra[:6]}. Reported, not failed — a "
                       f"different valid fix may touch different files")
                  if extra else
                  ok("pr.scope", "touched nothing outside the real diff"))

    # --- route correctness (docs/EVAL.md §Fixtures) --------------------
    # Ground truth is the directories the real merge diff touched. The
    # question is whether the agent's exploration went there, and how much
    # of it went somewhere else. Reported, never failed: an agent that
    # reaches the right file by an unexpected path has still routed well.
    real_dirs = {str(Path(p).parent) for p in real}
    m = task.get("_metrics") or {}
    trail = m.get("probe_trail") or []
    first = m.get("first_edit", "")
    if trail:
        hit = [t for t in trail
               if any(t.startswith(d) or d in t for d in real_dirs if d)]
        checks.append(skip("pr.route",
                           f"{len(hit)}/{len(trail)} pre-edit probes landed in "
                           f"the real diff's directories {sorted(real_dirs)[:3]}"
                           f"; first edit: {first or '(none)'}; "
                           f"trail: {trail[:6]}"))
    else:
        checks.append(skip("pr.route",
                           "no probes recorded before the first edit; the "
                           "adapter may not emit probe events"))

    # --- CLI-boundary grading, when the unit tests cannot be fair ------
    # A feature PR's tests reference symbols the PR introduces, so they do
    # not compile against an agent that named its internals differently --
    # measured: 0-5% on every such task, on runs that had already passed
    # diff_coverage. mkpr marks those tasks, and they are graded against
    # what the PR publicly promises instead: the merge commit's own binary
    # is the oracle, and the comparison is flag-for-flag at the command
    # line. Nothing is added to the prompt; the grader stops asking a
    # question the agent was never given the means to answer.
    if task.get("grader") == "cli":
        from adherence.cligrade import grade_cli
        repo = task.get("repo") or ""
        merge = task.get("merge_commit", "")
        if not repo or not merge:
            checks.append(skip("cli.surface",
                               "task marked cli-graded but carries no repo "
                               "mirror or merge commit"))
            return checks
        mirror = Path(repo)
        if not mirror.is_absolute():
            from adherence import REPO_ROOT
            mirror = REPO_ROOT / mirror
        checks += grade_cli(sandbox, mirror, merge,
                            task.get("code_files") or [])
        return checks

    # --- the maintainers' own tests decide it -------------------------
    test_files = task.get("test_files") or []
    merge = task.get("merge_commit", "")
    if not test_files or not merge:
        checks.append(skip("pr.task_pass",
                           "task record carries no test files or merge "
                           "commit; cannot apply the grader"))
        return checks

    # The agent must not have been able to read these. They arrive now.
    r = sh(sandbox, "git checkout {} -- {}".format(
        shlex.quote(merge), " ".join(shlex.quote(t) for t in test_files)))
    if r.returncode != 0:
        checks.append(skip("pr.task_pass",
                           f"could not apply the PR's tests: "
                           f"{(r.stderr or r.stdout)[-160:]}"))
        return checks

    cmd = task.get("test_cmd") or ""
    if not cmd:
        checks.append(skip("pr.task_pass", "task record carries no test_cmd"))
        return checks

    t = sh(sandbox, cmd)
    if t.returncode == 0:
        checks.append(ok("pr.task_pass",
                         f"`{cmd}` passed with the PR's own tests applied"))
    else:
        checks.append(bad("pr.task_pass",
                          f"`{cmd}` failed: "
                          f"{(t.stdout + t.stderr).strip()[-300:]}"))
    return checks
