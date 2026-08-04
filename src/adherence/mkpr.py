#!/usr/bin/env python3
"""Turn merged PRs into scenarios, and prove each one is gradeable.

    python3 -m adherence.mkpr --repo cli/cli --mirror fixtures/cli-cli.git \\
        --since 2026-05-01 --out scenarios/cli-cli

Two jobs, in this order, because the second is what makes the eval
affordable:

1. **Extract.** For each PR: the parent commit is the fixture state, the
   title and body with every path and subsystem name stripped is the
   prompt, and the PR's own `_test.go` changes are the grader.

2. **Verify before spending anything on a model.** A task counts only if
   its tests **fail at the parent commit and pass with the real fix**.
   That is a pure git-and-compiler check — no inference, no GPU — and it
   removes tasks that cannot be graded at all before they can waste a
   grid slot.

The agent never sees the tests. It is asked to make the change; the
harness applies the PR's test files afterwards and runs the affected
packages (docs/EVAL.md, Amendment 1).

Path stripping is deliberate and it is the measurement: the router's
claim is that it helps an agent *locate* work, so a prompt naming the
file answers the question for it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Anything that would hand the agent the answer. Applied to the prompt,
# never to the diff.
_PATHISH = re.compile(
    r"""(?xi)
    (?:^|(?<=[\s(\[`'"]))          # at a boundary
    (?:
        [\w./-]+/[\w./-]+\.\w+     # a/b/c.go
      | [\w-]+\.go\b               # file.go
      | \b(?:pkg|internal|cmd|api)/[\w./-]+
    )
    """)


def sh(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          check=check, timeout=600)


def strip_paths(text: str) -> str:
    """Remove file paths and package prefixes from a prompt.

    Conservative on purpose: it is better to leave a task out for being
    unstrippable than to leak a location and quietly make the routing
    question trivial for one arm."""
    out = _PATHISH.sub("<path>", text or "")
    out = re.sub(r"\n#+\s*Checklist.*", "", out, flags=re.S | re.I)
    out = re.sub(r"<!--.*?-->", "", out, flags=re.S)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def leaks_path(text: str) -> bool:
    return bool(_PATHISH.search(text or ""))


def gh_json(path: str):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def packages_for(paths: list[str]) -> list[str]:
    """Go packages touched by the tests, as `./dir/...` targets."""
    dirs = sorted({str(Path(p).parent) for p in paths if p.endswith("_test.go")})
    return [f"./{d}/..." for d in dirs if d and d != "."]


def warm_at(d: str, env: dict) -> None:
    """Populate the shared module cache for THIS commit, with network.

    Measured on cli/cli: 125 lines of go.mod/go.sum drift between HEAD and
    a base three months earlier. A cache warmed at one commit does not
    serve tasks at another, and the shortfall surfaces later as
    "module lookup disabled by GOPROXY=off" — which reads as a broken
    task rather than a cold cache, and silently deletes usable tasks from
    the eval.

    Warm with network here, once per commit. The cache is shared, so
    overlapping dependency sets are downloaded once across the whole task
    set. The agent's own runs stay offline; this is setup, not measurement.
    """
    online = {k: v for k, v in env.items()
              if k not in ("GOPROXY", "GOFLAGS", "GOTOOLCHAIN")}
    online["GOFLAGS"] = "-mod=mod"
    subprocess.run(["go", "mod", "download", "all"], cwd=d, env=online,
                   capture_output=True, text=True, timeout=1800)


def verify(mirror: Path, parent: str, merge: str, test_files: list[str],
           pkgs: list[str], env: dict) -> tuple[bool, str]:
    """Fail-before / pass-after, without a model.

    A task that does not go red at the parent commit is not measuring the
    fix — the tests already passed, so any agent 'succeeds' by doing
    nothing. A task that does not go green with the real patch is broken
    or environment-dependent. Both are dropped here, for free."""
    with tempfile.TemporaryDirectory() as d:
        try:
            sh(["git", "clone", "--local", "--shared", "--quiet",
                "--no-checkout", str(mirror), d])
            sh(["git", "checkout", "--detach", "--quiet", parent], cwd=d)
            # Dependencies for THIS commit, before anything runs offline.
            warm_at(d, env)
            # the PR's tests, on the pre-fix tree
            sh(["git", "checkout", merge, "--"] + test_files, cwd=d)
            red = subprocess.run(["go", "test", *pkgs], cwd=d, env=env,
                                 capture_output=True, text=True, timeout=900)
            if red.returncode == 0:
                return False, "tests already pass at the parent commit"
            # now the real fix as well -- and its own dependency state,
            # since the merge commit may move go.mod
            sh(["git", "checkout", merge, "--", "."], cwd=d)
            warm_at(d, env)
            green = subprocess.run(["go", "test", *pkgs], cwd=d, env=env,
                                   capture_output=True, text=True, timeout=900)
            if green.returncode != 0:
                return False, f"tests still fail with the real fix: " \
                              f"{(green.stdout + green.stderr)[-200:]}"
            return True, "fails at parent, passes with the fix"
        except subprocess.CalledProcessError as e:
            return False, f"git: {(e.stderr or '')[-160:]}"
        except subprocess.TimeoutExpired:
            return False, "test run exceeded the timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--mirror", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD")
    ap.add_argument("--until", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-verify", action="store_true",
                    help="extract without the fail-before/pass-after check "
                         "(faster, but the tasks are unproven)")
    args = ap.parse_args()

    mirror, out = Path(args.mirror).resolve(), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    window = f"{args.since}..{args.until or '2100-01-01'}"

    nums = []
    for page in range(1, 6):
        d = gh_json(f"search/issues?q=repo:{args.repo}+is:pr+is:merged+"
                    f"merged:{window}&per_page=100&page={page}")
        if not d or not d.get("items"):
            break
        nums += [i["number"] for i in d["items"]]
    print(f"{len(nums)} merged PRs in {window}", file=sys.stderr)

    env = dict(os.environ)
    cache = mirror.parent / f"{mirror.stem}.cache" / "gomod"
    if cache.is_dir():
        env.update(GOMODCACHE=str(cache), GOFLAGS="-mod=mod",
                   GOPROXY="off", GOTOOLCHAIN="local")

    kept, dropped = [], []
    for n in nums:
        if args.limit and len(kept) >= args.limit:
            break
        pr = gh_json(f"repos/{args.repo}/pulls/{n}")
        files = gh_json(f"repos/{args.repo}/pulls/{n}/files?per_page=100")
        if not pr or not files:
            dropped.append((n, "api"))
            continue
        paths = [f["filename"] for f in files]
        tests = [p for p in paths if p.endswith("_test.go")]
        code = [p for p in paths if p.endswith(".go") and p not in tests]
        if not tests or not code:
            dropped.append((n, "no Go change with its own tests"))
            continue

        prompt = strip_paths(f"{pr['title']}\n\n{pr.get('body') or ''}")
        if leaks_path(prompt):
            dropped.append((n, "prompt still names a path after stripping"))
            continue
        if len(prompt) < 40:
            dropped.append((n, "prompt too thin once stripped"))
            continue

        pkgs = packages_for(tests)
        ok, why = (True, "unverified") if args.no_verify else verify(
            mirror, pr["base"]["sha"], pr["merge_commit_sha"], tests, pkgs, env)
        if not ok:
            dropped.append((n, why))
            continue

        kept.append({
            "pr": n, "title": pr["title"], "base_commit": pr["base"]["sha"],
            "merge_commit": pr["merge_commit_sha"], "prompt": prompt,
            "test_files": tests, "test_cmd": "go test " + " ".join(pkgs),
            "code_files": code, "additions": pr.get("additions", 0),
            "verified": why,
        })
        print(f"  kept #{n} (+{pr.get('additions',0)}) {pr['title'][:56]}",
              file=sys.stderr)

    (out / "ground-truth.jsonl").write_text(
        "".join(json.dumps(k) + "\n" for k in kept))
    (out / "dropped.jsonl").write_text(
        "".join(json.dumps({"pr": n, "reason": r}) + "\n" for n, r in dropped))
    print(f"\nkept {len(kept)}, dropped {len(dropped)} -> {out}", file=sys.stderr)
    print(f"every drop is recorded in {out/'dropped.jsonl'} with its reason",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
