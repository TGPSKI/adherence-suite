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

    # --- did it do the job the PR did ---------------------------------
    hit = sorted(changed & real)
    coverage = len(hit) / len(real) if real else 0.0
    checks.append(ok("pr.diff_coverage",
                     f"touched {len(hit)}/{len(real)} of the real diff's "
                     f"files ({coverage:.0%}): {hit[:6]}")
                  if hit else
                  bad("pr.diff_coverage",
                      f"touched none of the {len(real)} files the real PR "
                      f"changed; agent touched {sorted(changed)[:6]}"))

    extra = sorted(changed - real)
    checks.append(skip("pr.scope",
                       f"also touched {len(extra)} file(s) outside the real "
                       f"diff: {extra[:6]}. Reported, not failed — a "
                       f"different valid fix may touch different files")
                  if extra else
                  ok("pr.scope", "touched nothing outside the real diff"))

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
