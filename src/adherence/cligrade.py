#!/usr/bin/env python3
"""Grade a feature PR at the command-line boundary, not the Go boundary.

The problem this exists for: applying a PR's own unit tests to an agent's
independent implementation requires the agent to have chosen the
maintainers' exact internal identifiers. Measured on the validation grid,
every task whose tests referenced new symbols scored 0-5% -- while the
agent had already passed `diff_coverage` with 13/13 pre-edit probes in the
right directories. It did the work and failed on a name.

The obvious repair -- tell the agent the required API surface -- is worse
than the disease. A symbol list *is* routing information (`CreateOptions.
IssueType` names the subsystem), so it would hand every arm a piece of
exactly what the treatment is supposed to supply, and bias the primary
outcome toward the null in the treatment's own channel. A null result
would then be uninterpretable.

So grade what the PR *publicly promises* instead. `gh issue create --type`
is a user-facing contract: it appears in the PR body the agent already
receives, and it is identical whatever the agent names its internals.
Nothing here is added to the prompt; the grader simply stops asking a
question it never gave the agent the information to answer.

The oracle is the merge commit's own binary, so the expectations are still
the maintainers' and not the experimenter's:

    reference = build(merge commit)      what the PR actually shipped
    candidate = build(agent's tree)      what the agent produced
    compare observable CLI behaviour

Offline and deterministic -- `--help` and argument validation need no
network, which is what makes this runnable inside the sandbox.

Stdlib only.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from adherence.gradelib import Check, bad, ok, skip

BUILD_TIMEOUT = 600
RUN_TIMEOUT = 60

# A flag line in Go/cobra help output: two spaces, optional -x, then --name.
_FLAG = re.compile(r"^\s+(?:-\w,\s+)?--([a-z0-9][\w-]*)")


def command_path(code_files: list[str]) -> list[str]:
    """The CLI subcommand a PR touches, from its file paths.

    `pkg/cmd/issue/create/create.go` -> ["issue", "create"]. Mechanical, so
    the battery is derived rather than authored -- an experimenter choosing
    which commands to test is an experimenter choosing the result."""
    best: list[str] = []
    for f in code_files or []:
        parts = Path(f).parts
        if len(parts) < 3 or parts[0] != "pkg" or parts[1] != "cmd":
            continue
        words = [p for p in parts[2:-1] if p and not p.endswith(".go")]
        if len(words) > len(best):
            best = words
    return best


def build(tree: Path, out: Path, env=None) -> tuple[bool, str]:
    try:
        r = subprocess.run(["go", "build", "-o", str(out), "./cmd/gh"],
                           cwd=tree, capture_output=True, text=True,
                           timeout=BUILD_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[-400:]
    return r.returncode == 0, (r.stderr or r.stdout)[-800:]


def flags(binary: Path, argv: list[str], env=None) -> set[str]:
    """Flag names a command advertises. Empty on any failure -- the caller
    distinguishes 'no flags' from 'could not ask'."""
    try:
        r = subprocess.run([str(binary), *argv, "--help"],
                           capture_output=True, text=True,
                           timeout=RUN_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {m.group(1) for line in (r.stdout + r.stderr).splitlines()
            if (m := _FLAG.match(line))}


def grade_cli(sandbox: Path, mirror: Path, merge: str,
              code_files: list[str], env=None) -> list[Check]:
    """Differential CLI grading against the PR's own merge commit.

    Returns `ungradeable` rather than `fail` for anything that is the
    harness's problem -- a toolchain that will not build the reference, a
    PR that touches no command -- because a grader that cannot run must
    never be scored as a model that got it wrong."""
    checks: list[Check] = []
    argv = command_path(code_files)
    if not argv:
        return [skip("cli.surface",
                     "PR touches no pkg/cmd/... command, so it has no "
                     "command-line surface to compare")]

    # --- the candidate: whatever the agent produced --------------------
    cand = sandbox / ".adh-gh-candidate"
    built, err = build(sandbox, cand, env)
    if not built:
        # This IS a model result: the agent left the tree not compiling.
        return [bad("cli.builds",
                    f"the agent's tree does not build: {err[-300:]}")]
    checks.append(ok("cli.builds", "the agent's tree compiles"))

    # --- the oracle: the merge commit, built the same way ---------------
    import tempfile
    with tempfile.TemporaryDirectory(prefix="adh-ref-") as d:
        ref_tree = Path(d)
        try:
            subprocess.run(["git", "clone", "--local", "--shared", "--quiet",
                            "--no-checkout", str(mirror), str(ref_tree)],
                           check=True, capture_output=True, timeout=300)
            subprocess.run(["git", "checkout", "--detach", "--quiet", merge],
                           cwd=ref_tree, check=True, capture_output=True,
                           timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            checks.append(skip("cli.surface",
                               f"could not materialize the reference: {e}"))
            return checks
        ref = ref_tree / "gh-reference"
        ok_ref, ref_err = build(ref_tree, ref, env)
        if not ok_ref:
            checks.append(skip("cli.surface",
                               f"the reference commit does not build here, "
                               f"so there is nothing to compare against: "
                               f"{ref_err[-200:]}"))
            return checks

        want = flags(ref, argv, env)
        got = flags(cand, argv, env)
        if not want:
            checks.append(skip("cli.surface",
                               f"reference `gh {' '.join(argv)} --help` "
                               f"advertised no flags; nothing to compare"))
            return checks

        missing = sorted(want - got)
        checks.append(
            ok("cli.surface",
               f"`gh {' '.join(argv)}` advertises all {len(want)} flag(s) "
               f"the PR's own binary does")
            if not missing else
            bad("cli.surface",
                f"missing {len(missing)} flag(s) the PR shipped: "
                f"{missing[:8]}. Compared against the merge commit's own "
                f"binary, so these are the maintainers' names, not the "
                f"experimenter's"))

        # Flags the agent added beyond the PR: reported, never failed. A
        # different valid implementation may expose more.
        extra = sorted(got - want)
        checks.append(skip("cli.extra_surface",
                           f"also advertises {len(extra)} flag(s) the PR "
                           f"does not: {extra[:8]}")
                      if extra else
                      ok("cli.extra_surface", "no flags beyond the PR's"))
    return checks
