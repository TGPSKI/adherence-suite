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


# Per-ecosystem knowledge, in one table so a new fixture language is a
# data change rather than a rewrite. `verified` marks the ones actually
# exercised end to end -- the others are declared, not proven, and mkpr
# says so rather than implying support it has not demonstrated.
ECOSYSTEMS = {
    "go": {
        "marker": "go.mod",
        "is_test": lambda p: p.endswith("_test.go"),
        "is_code": lambda p: p.endswith(".go") and not p.endswith("_test.go"),
        "targets": lambda dirs: [f"./{d}/..." for d in dirs],
        "test_cmd": lambda t: "go test " + " ".join(t),
        "warm": ["go", "mod", "download", "all"],
        "path_roots": ("pkg", "internal", "cmd", "api"),
        "verified": True,
    },
    "rust": {
        "marker": "Cargo.toml",
        "is_test": lambda p: p.startswith("tests/") and p.endswith(".rs"),
        "is_code": lambda p: p.endswith(".rs") and not p.startswith("tests/"),
        "targets": lambda dirs: [],
        "test_cmd": lambda t: "cargo test",
        "warm": ["cargo", "fetch"],
        "path_roots": ("src", "crates", "tests"),
        "verified": False,
    },
    "python": {
        "marker": "pyproject.toml",
        "is_test": lambda p: (Path(p).name.startswith("test_")
                              or p.endswith("_test.py")) and p.endswith(".py"),
        "is_code": lambda p: p.endswith(".py") and not (
            Path(p).name.startswith("test_") or p.endswith("_test.py")),
        "targets": lambda dirs: sorted(dirs),
        "test_cmd": lambda t: "python -m pytest -q " + " ".join(t),
        "warm": ["python", "-m", "pip", "download", "-d", ".pipcache", "."],
        "path_roots": ("src", "lib", "tests"),
        "verified": False,
    },
    "node": {
        "marker": "package.json",
        "is_test": lambda p: (".test." in p or ".spec." in p) and p.endswith(
            (".js", ".ts", ".tsx", ".jsx")),
        "is_code": lambda p: p.endswith((".js", ".ts", ".tsx", ".jsx"))
                   and not (".test." in p or ".spec." in p),
        "targets": lambda dirs: sorted(dirs),
        "test_cmd": lambda t: "npm test --",
        "warm": ["npm", "ci", "--ignore-scripts"],
        "path_roots": ("src", "lib", "packages"),
        "verified": False,
    },
}


def detect_ecosystem(mirror: Path, commit: str) -> str:
    """Which ecosystem, from the marker file at the commit under test."""
    r = subprocess.run(["git", "ls-tree", "--name-only", commit],
                       cwd=mirror, capture_output=True, text=True)
    names = set(r.stdout.split())
    for key, eco in ECOSYSTEMS.items():
        if eco["marker"] in names:
            return key
    return ""


def packages_for(paths: list[str], eco: dict) -> list[str]:
    """Test targets for the directories the PR's tests touch."""
    dirs = sorted({str(Path(p).parent) for p in paths if eco["is_test"](p)})
    return eco["targets"]([d for d in dirs if d and d != "."])


def warm_at(d: str, env: dict, eco: dict | None = None) -> None:
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
    cmd = (eco or ECOSYSTEMS["go"])["warm"]
    subprocess.run(cmd, cwd=d, env=online,
                   capture_output=True, text=True, timeout=1800)


# A compiler complaining that the test names something that does not exist.
# Go's wording; the shape generalizes and each ecosystem can add its own.
_MISSING_SYMBOL = (
    re.compile(r"undefined:\s*([A-Za-z_][\w.]*)"),
    re.compile(r"unknown field (\w+) in struct literal"),
    re.compile(r"has no field or method (\w+)"),
    re.compile(r"undeclared name:\s*(\w+)"),
)


def names_the_test_requires(output: str) -> list[str]:
    """Identifiers the PR's own tests reference that do not exist yet.

    The fail-before check proves a task goes red at the parent commit. It
    does not ask *why*, and there are two very different reasons:

      behaviour  the tests compile and assert the wrong answer. An agent
                 that implements the behaviour passes, whatever it names
                 its internals. This is a real task.
      compile    the tests do not build, because they reference a symbol
                 the fix introduces -- `undefined: findCopilotBinaryFunc`,
                 `unknown field IssueType in struct literal`. Passing
                 requires independently choosing the maintainers' exact
                 identifier. That measures name-guessing, not the work,
                 and no arm can do it reliably.

    Measured on the validation grid: all four compile-class scenarios
    scored 0%, 5%, 0%, 0% -- none within the [0.25, 0.80] calibration band
    -- while every in-band and every ceiling scenario was behaviour-class.
    A perfect separation, and the dominant cause of the floor problem the
    registration names as its biggest schedule risk.

    This is why SWE-bench-style grading works for bug fixes and not for
    feature PRs: a bug fix's tests call API that already exists, while a
    feature PR's tests call API the PR is adding."""
    found, seen = [], set()
    for pat in _MISSING_SYMBOL:
        for m in pat.finditer(output or ""):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def verify(mirror: Path, parent: str, merge: str, test_files: list[str],
           pkgs: list[str], env: dict, eco: dict) -> tuple[bool, str]:
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
            warm_at(d, env, eco)
            # the PR's tests, on the pre-fix tree
            sh(["git", "checkout", merge, "--"] + test_files, cwd=d)
            cmd = eco["test_cmd"](pkgs)
            red = subprocess.run(cmd, shell=True, cwd=d, env=env,
                                 capture_output=True, text=True, timeout=900)
            if red.returncode == 0:
                return False, "tests already pass at the parent commit"
            invented = names_the_test_requires(red.stdout + red.stderr)
            if invented:
                # Not a broken task -- a task the UNIT grader cannot judge
                # fairly. It is handed to the CLI grader instead, which
                # compares against the merge commit's own binary at the
                # command line, where the contract is the one the PR body
                # already gave the agent. Recorded so the choice is on the
                # record rather than in a heuristic nobody can see.
                return True, ("cli-graded: the PR's tests name "
                              + ", ".join(invented[:4])
                              + ", which the fix introduces, so they cannot "
                                "compile against any implementation that "
                                "chose different identifiers")
            # The full post-fix state. Checking out `merge -- .` instead
            # would restore files the merge has but NOT remove files it
            # deleted, so a PR that deletes or renames a source file leaves
            # the old one behind -- duplicate declarations, a compile error,
            # and a task dropped as "tests still fail with the real fix".
            # Same failure class as a cold cache: a harness defect wearing
            # the costume of an ungradeable task.
            sh(["git", "checkout", "--detach", "--quiet", merge], cwd=d)
            warm_at(d, env, eco)
            green = subprocess.run(cmd, shell=True, cwd=d, env=env,
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
                    help="DANGEROUS: skip the fail-before/pass-after check. "
                         "An unverified task whose tests already pass at the "
                         "parent commit is scored as a success for an agent "
                         "that did nothing. Only for a fast dry run")
    args = ap.parse_args()
    if args.no_verify:
        print("WARNING: --no-verify. Tasks will NOT be proven to fail at the "
              "parent commit, so a task whose tests already pass scores an "
              "agent that did nothing as a success. Do not run a grid on "
              "this output.", file=sys.stderr)

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

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mirror,
                          capture_output=True, text=True).stdout.strip()
    kind = detect_ecosystem(mirror, head)
    if not kind:
        sys.exit(f"no ecosystem marker at {head[:10]} — expected one of: "
                 + ", ".join(e["marker"] for e in ECOSYSTEMS.values()))
    eco = ECOSYSTEMS[kind]
    print(f"ecosystem: {kind}", file=sys.stderr)
    if not eco["verified"]:
        print(f"WARNING: the '{kind}' adapter is declared but has never been "
              f"exercised end to end. Run with --limit 3 first and read "
              f"dropped.jsonl: an adapter bug looks exactly like an "
              f"ungradeable task, which is how a good fixture gets thrown "
              f"away.", file=sys.stderr)

    # Resume: verification costs two full test runs plus a network warm per
    # task, so re-running 174 PRs to recover from an interruption is real
    # money. Anything already decided is kept.
    done = {}
    for name in ("ground-truth.jsonl", "dropped.jsonl"):
        f = out / name
        if f.is_file():
            for line in f.read_text().splitlines():
                if line.strip():
                    done[json.loads(line)["pr"]] = name
    if done:
        print(f"resuming: {len(done)} PRs already decided", file=sys.stderr)

    env = dict(os.environ)
    cache = mirror.parent / f"{mirror.stem}.cache" / "gomod"
    if cache.is_dir():
        env.update(GOMODCACHE=str(cache), GOFLAGS="-mod=mod",
                   GOPROXY="off", GOTOOLCHAIN="local")

    kept, dropped = [], []
    gt_f = (out / "ground-truth.jsonl").open("a")
    dr_f = (out / "dropped.jsonl").open("a")

    def keep(rec):
        kept.append(rec)
        gt_f.write(json.dumps(rec) + "\n")
        gt_f.flush()

    def drop(n, why):
        dropped.append((n, why))
        dr_f.write(json.dumps({"pr": n, "reason": why}) + "\n")
        dr_f.flush()

    for n in nums:
        if n in done:
            continue
        if args.limit and len(kept) >= args.limit:
            break
        pr = gh_json(f"repos/{args.repo}/pulls/{n}")
        files = gh_json(f"repos/{args.repo}/pulls/{n}/files?per_page=100")
        if not pr or not files:
            drop(n, "api")
            continue
        paths = [f["filename"] for f in files]
        tests = [p for p in paths if eco["is_test"](p)]
        code = [p for p in paths if eco["is_code"](p)]
        if not tests or not code:
            drop(n, f"no {kind} change with its own tests")
            continue

        prompt = strip_paths(f"{pr['title']}\n\n{pr.get('body') or ''}")
        if leaks_path(prompt):
            drop(n, "prompt still names a path after stripping")
            continue
        if len(prompt) < 40:
            drop(n, "prompt too thin once stripped")
            continue

        pkgs = packages_for(tests, eco)
        ok, why = (True, "unverified") if args.no_verify else verify(
            mirror, pr["base"]["sha"], pr["merge_commit_sha"], tests, pkgs, env, eco)
        if not ok:
            drop(n, why)
            continue

        # Which grader can judge this task fairly. `verify` says so: a
        # task whose tests do not compile at the parent because they name
        # what the fix introduces cannot be judged by those tests against
        # an agent that chose different identifiers.
        cli_graded = why.startswith("cli-graded:")
        keep({
            "pr": n, "ecosystem": kind, "title": pr["title"], "base_commit": pr["base"]["sha"],
            "merge_commit": pr["merge_commit_sha"], "prompt": prompt,
            "test_files": tests, "test_cmd": eco["test_cmd"](pkgs),
            "code_files": code, "additions": pr.get("additions", 0),
            "verified": why,
            "grader": "cli" if cli_graded else "unit",
            "invented_symbols": (names_the_test_requires(why)
                                 if cli_graded else []),
        })
        print(f"  kept #{n} (+{pr.get('additions',0)}) "
              f"[{'cli' if cli_graded else 'unit'}] {pr['title'][:52]}",
              file=sys.stderr)

    gt_f.close()
    dr_f.close()
    print(f"\nDONE. kept {len(kept)}, dropped {len(dropped)} this run "
          f"-> {out}", file=sys.stderr)
    print(f"every drop is recorded in {out/'dropped.jsonl'} with its reason",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
