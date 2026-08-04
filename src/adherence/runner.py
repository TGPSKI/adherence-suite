#!/usr/bin/env python3
"""runner.py — execute adherence scenarios against a harness adapter.

For each (scenario, trial):
  1. Copy fixture to a fresh sandbox, `git init && git add -A && git commit`.
  2. Invoke the adapter:  adapter <sandbox> <model> <prompt-file> <out-dir>
     Adapter contract: run the harness with cwd=sandbox, write
     out-dir/transcript.jsonl and out-dir/final_message.txt, exit 0.
  3. Import the scenario's grade.py, run checks, append one JSON line to
     the results file.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from adherence import REPO_ROOT, gradelib, metrics, schema

ROOT = REPO_ROOT


def load_yamlish(path: Path) -> dict:
    """Minimal YAML subset loader (flat keys, str/int values, one-level
    lists of strings under 'scenarios:'). Avoids a pyyaml dependency."""
    data, cur_list = {}, None
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and cur_list is not None:
            data[cur_list].append(line[4:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if v == "":
                data[k] = []
                cur_list = k
            else:
                cur_list = None
                data[k] = int(v) if v.isdigit() else v.strip('"')
    return data


def _sha8(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
    return h.hexdigest()[:8]


def _harness_version(adapter: Path) -> str:
    """The adapter's own version string. Recorded because the stream format
    this suite reads is a moving target; a cost number from an unknown
    harness version is not reproducible."""
    for probe in (["opencode", "--version"],):
        try:
            r = subprocess.run(probe, capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return f"{probe[0]} {r.stdout.strip().splitlines()[0]}"
        except (OSError, subprocess.SubprocessError):
            pass
    return f"unknown ({adapter.name})"


def provenance(scen_dir: Path, meta: dict, arm: str, arms_dir, adapter: Path,
               harness: str) -> dict:
    """Everything a stranger needs to re-run this exact trial.

    The replay invariant: a node is reconstructible by someone who does not
    trust the author, from the record alone. Without this block a result is
    a claim about a measurement, not the measurement."""
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        commit = rev.stdout.strip() or "unknown"
        st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
        dirty = bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unknown", True

    scen_yaml = (scen_dir / "scenario.yaml")
    grade = (scen_dir / "grade.py")
    prov = {
        "argv": list(sys.argv),
        "suite_commit": commit,
        "suite_dirty": dirty,
        "harness": harness,
        "python": sys.version.split()[0],
        # scenario.yaml carries the prompt; grade.py decides the verdict.
        # Both must be pinned or "same scenario" means nothing.
        "scenario_sha": _sha8(
            scen_yaml.read_text() if scen_yaml.exists() else "",
            grade.read_text() if grade.exists() else ""),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if arms_dir and arm and arm != "-":
        d = Path(arms_dir) / arm
        if d.is_dir():
            prov["arm_sha"] = _sha8(*(f.read_text(errors="replace")
                                      for f in sorted(d.rglob("*")) if f.is_file()))
    if meta.get("base_commit"):
        prov["base_commit"] = str(meta["base_commit"])
    return prov


def proxy_mark(label: str, parallel: bool = False) -> None:
    """Tell the recording proxy which trial the next calls belong to.

    Without it, proxy lines are one undifferentiated stream and the H4
    agreement check has nothing to join on.

    **This only works serially.** The mark is a single piece of state on
    the proxy, and nothing in an inference request identifies the trial
    that made it -- measured: the sandbox path appears nowhere in the
    request body, not even in the system prompt. Under --jobs>1 two
    trials interleave into one mark, so rather than writing an
    attribution that is wrong, the mark is skipped and H4 must be run
    serially. Losing per-trial proxy attribution is the second cost of
    parallelism, alongside the latency metric §16.4 already names."""
    base = os.environ.get("ADH_PROXY")
    if not base:
        return
    if parallel:
        return
    root = base.rsplit("/v1", 1)[0]
    try:
        req = urllib.request.Request(
            root + "/__proxy/mark",
            data=json.dumps({"label": label}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass          # measurement bookkeeping must never fail a trial


def git(cwd: Path, args: str, check: bool = True):
    return subprocess.run(f"git {args}", shell=True, cwd=cwd, check=check,
                          capture_output=True, text=True)


def materialize(scen_dir: Path, sandbox: Path, meta: dict) -> None:
    """Put the scenario's starting tree into the sandbox.

    Two paths, chosen by whether the scenario names a repository:

    **Mirror path** (design §16.2 B3) — `repo` is a local bare mirror and
    `base_commit` is the PR's parent. `git clone --local --shared` hard-
    links object storage instead of copying it, so a 100+ MB repository
    materializes in well under a second and ~2,100 trials stay tractable.
    The parent commit is also the *correct* baseline: `git status` is
    clean at t=0, so `git_changed_files` reports the agent's work and
    only the agent's work.

    **Fixture path** — the synthetic sNN scenarios, unchanged: copy the
    fixture tree and init a repo around it. At 8-32 KB the copy is free,
    and there is no upstream commit to check out.
    """
    repo = meta.get("repo", "")
    if repo:
        mirror = Path(repo)
        if not mirror.is_absolute():
            mirror = ROOT / mirror
        if not mirror.exists():
            raise SystemExit(f"{scen_dir.name}: mirror {mirror} does not "
                             f"exist — vendor it first (design §8.1)")
        # --shared keeps objects in the mirror; the mirror must outlive
        # every sandbox cloned from it, which is why it lives under
        # fixtures/ rather than in a temp dir.
        subprocess.run(
            ["git", "clone", "--local", "--shared", "--quiet",
             "--no-checkout", str(mirror), str(sandbox)],
            check=True, capture_output=True)
        base = meta.get("base_commit", "")
        if not base:
            raise SystemExit(f"{scen_dir.name}: repo set but base_commit is "
                             f"missing — the baseline would be arbitrary")
        git(sandbox, f"checkout --detach --quiet {base}")
        return

    fixture = scen_dir / "fixture"
    if fixture.exists():
        shutil.copytree(fixture, sandbox, dirs_exist_ok=True)
    git(sandbox, "init -q")


def apply_arm(sandbox: Path, arms_dir: Path, arm: str) -> str:
    """Overlay one instruction-surface arm onto a materialized sandbox.

    Every arm first removes whatever instruction surface the repo ships
    (the manifest's `remove` list) and then writes its own. Skipping the
    removal is how an arm silently inherits another's surface — A0 would
    stop being a floor and A5 would quietly be A1+A5.

    Returns a human-readable note for the log. A missing arms directory
    is not an error: the synthetic sNN scenarios carry no instruction
    surface, so `arm` is a label on those runs and nothing else."""
    if not arms_dir:
        return "no arms dir; arm is a label only"
    d = arms_dir / arm
    manifest = d / "_arm.json"
    if not manifest.is_file():
        raise SystemExit(f"arm {arm!r} not found under {arms_dir} — run "
                         f"tools/mkarms.py first")
    spec = json.loads(manifest.read_text())
    for rel in spec.get("remove", []):
        target = sandbox / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    n = 0
    for src in sorted(d.rglob("*")):
        if not src.is_file() or src.name == "_arm.json":
            continue
        dst = sandbox / src.relative_to(d)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    return f"{arm}: removed {spec.get('remove', [])}, wrote {n} file(s)"


def load_grader(scen_dir: Path):
    spec = importlib.util.spec_from_file_location(
        f"grade_{scen_dir.name}", scen_dir / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_one(scen_dir: Path, adapter: Path, model: str, keep: bool,
            arm: str = "-", trial: int = 0, floor: int = 0,
            arms_dir: Path | None = None, parallel: bool = False,
            timeout_override: int = 0, purpose: str = "validation") -> dict:
    meta = load_yamlish(scen_dir / "scenario.yaml")
    sandbox = Path(tempfile.mkdtemp(prefix=f"adh-{scen_dir.name}-"))
    out_dir = Path(tempfile.mkdtemp(prefix=f"adh-out-{scen_dir.name}-"))

    materialize(scen_dir, sandbox, meta)
    # PR-derived scenarios ship a task record the grader needs and the
    # agent must not have: test files, the merge commit, the test command.
    # It lands before the baseline commit so it is tracked-and-clean and
    # never shows up as an agent edit, and it names no source path the
    # prompt withheld.
    task_json = scen_dir / "task.json"
    if task_json.is_file():
        (sandbox / ".adh-task.json").write_text(task_json.read_text())
    # Per-fixture ignore set, installed before anything can dirty the
    # tree. `ignore:` in scenario.yaml lists whatever this repo's own
    # test run writes and its .gitignore misses (H9).
    gradelib.write_harness_excludes(sandbox, meta.get("ignore", []))
    apply_arm(sandbox, arms_dir, arm)
    # Baseline commit LAST, after the arm overlay is in place. Committing
    # first would leave every arm's own instruction files sitting in
    # `git status`, and `check_no_extra_changes` would read the harness's
    # setup as the agent editing files outside its surface.
    git(sandbox, "add -A")
    git(sandbox, "-c user.email=a@b -c user.name=adh commit -qm baseline "
                 "--allow-empty")

    prompt_file = out_dir / "prompt.txt"
    prompt_file.write_text(meta["prompt"].replace("\\n", "\n"))

    proxy_mark(f"{scen_dir.name}|{arm}|{trial}", parallel)
    t0 = time.time()
    timeout = timeout_override or int(meta.get("timeout", 300))
    target_agent = meta.get("agent", "")

    harness = _harness_version(adapter)
    env = dict(os.environ)
    if parallel:
        # Measured: concurrent `opencode run` against one XDG_DATA_HOME
        # dies with "database is locked" -- opencode keeps sessions in a
        # single sqlite store and does not serialize writers. Give each
        # trial its own data/state/cache home. XDG_CONFIG_HOME is
        # deliberately NOT redirected: that is the isolation boundary
        # bench/isolate.sh establishes, and it is read-only here.
        for var, sub in (("XDG_DATA_HOME", "data"),
                         ("XDG_STATE_HOME", "state"),
                         ("XDG_CACHE_HOME", "cache")):
            d = out_dir / "xdg" / sub
            d.mkdir(parents=True, exist_ok=True)
            env[var] = str(d)
    try:
        proc = subprocess.run(
            [str(adapter), str(sandbox), model, str(prompt_file), str(out_dir),
             target_agent],
            capture_output=True, text=True, timeout=timeout, env=env)
        adapter_ok = proc.returncode == 0
        adapter_err = proc.stderr[-2000:]
    except subprocess.TimeoutExpired:
        adapter_ok, adapter_err = False, f"adapter timeout after {timeout}s"
    duration = time.time() - t0

    transcript = gradelib.load_transcript(out_dir / "transcript.jsonl")
    schema_errs = schema.validate_transcript(transcript)
    final_path = out_dir / "final_message.txt"
    final = final_path.read_text(errors="replace") if final_path.exists() else ""

    if adapter_ok:
        # PR graders need the derived route evidence; stash it where the
        # grader can reach it without changing the grade() signature every
        # scenario depends on.
        tj = sandbox / ".adh-task.json"
        if tj.is_file():
            try:
                rec = json.loads(tj.read_text())
                rec["_metrics"] = metrics.compute(
                    transcript, expects_edit=bool(int(meta.get("expects_edit", 1))))
                tj.write_text(json.dumps(rec))
            except (json.JSONDecodeError, OSError):
                pass
        checks = [c.d() for c in load_grader(scen_dir).grade(sandbox, transcript, final)]
    else:
        checks = [gradelib.bad("adapter", adapter_err).d()]

    usage = next((e for e in transcript if e.get("type") == schema.USAGE), {})
    result = schema.result(
        scenario=scen_dir.name,
        category=meta.get("category", "uncategorized"),
        model=model,
        adapter=adapter.name,
        arm=arm,
        trial=trial,
        duration_s=round(duration, 1),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        checks=checks,
        all_pass=all(c["status"] == "pass" for c in checks
                     if c["status"] != "ungradeable") and
                 any(c["status"] == "pass" for c in checks),
        sandbox=str(sandbox) if keep else "",
        out_dir=str(out_dir) if keep else "",
        # Cost lives here, not in prompt_tokens: the aggregate `usage`
        # event is root-session only and misses subagents entirely.
        metrics=metrics.compute(transcript, floor=floor,
                                duration_s=round(duration, 1),
                                expects_edit=bool(int(
                                    meta.get("expects_edit", 1)))),
        provenance=provenance(scen_dir, meta, arm, arms_dir, adapter, harness),
        fixture=str(meta.get("fixture", "")),
        purpose=purpose,
    )
    if not keep:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    return result, schema_errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="suite.yaml")
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--keep-sandbox", action="store_true")
    ap.add_argument("--arm", default="-",
                    help="single instruction-surface arm (a0..a5); "
                         "'-' means unset")
    ap.add_argument("--arms",
                    help="comma-separated arms to run as a matrix, e.g. "
                         "a1,a2,a3 — every arm sees every scenario, which "
                         "is what makes the paired analysis possible (§11)")
    ap.add_argument("--arms-dir",
                    help="directory of materialized arms from "
                         "tools/mkarms.py (a0/ .. a5/)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="run N trials concurrently. Token and call counts "
                         "are unaffected by contention; wall_clock_s becomes "
                         "a function of GPU scheduling and is NOT comparable "
                         "across arms (§16.4) — it is recorded but stamped "
                         "contended=true")
    ap.add_argument("--purpose", default="validation",
                    choices=("validation", "experiment"),
                    help="'validation' shakes out the method and the code; "
                         "'experiment' is the registered grid. Defaults to "
                         "validation so nothing is labelled experiment data "
                         "by omission")
    ap.add_argument("--timeout", type=int, default=0,
                    help="override every scenario's timeout, in seconds. The "
                         "feasibility probe wants a tighter bound than the "
                         "grid: a task that has not converged in a few "
                         "minutes is telling you it floors")
    ap.add_argument("--floor", type=int, default=0,
                    help="measured per-arm harness floor in input tokens "
                         "(E5); tok_in_marginal is meaningless without it")
    ap.add_argument("--strict-schema", action="store_true",
                    help="abort on the first schema violation instead of "
                         "warning; use in CI")
    args = ap.parse_args()

    suite = load_yamlish(ROOT / args.suite)
    ids = suite.get("scenarios", [])
    if args.only:
        wanted = set(args.only.split(","))
        ids = [i for i in ids if i in wanted]

    adapter = Path(args.adapter).resolve()
    arms = [a.strip() for a in args.arms.split(",")] if args.arms else [args.arm]
    arms_dir = Path(args.arms_dir).resolve() if args.arms_dir else None
    n_schema_bad = 0
    # scenario-major, then arm, then trial: an interrupted run still has
    # every arm for the scenarios it finished, so a partial grid is
    # analyzable as a paired design instead of being thrown away.
    work = [(ROOT / "scenarios" / sid, arm, trial)
            for sid in ids for arm in arms for trial in range(args.trials)]

    def one(item):
        scen_dir, arm, trial = item
        r, errs = run_one(scen_dir, adapter, args.model, args.keep_sandbox,
                          arm, trial, args.floor, arms_dir, args.jobs > 1,
                          args.timeout, args.purpose)
        if args.jobs > 1:
            # Latency is not recoverable after the fact, so say so in the
            # record rather than letting a contended number be compared
            # against a serial one later (§16.4).
            r["metrics"]["contended"] = True
        return item, r, errs + schema.validate_result(r)

    def emit(out, item, r, errs):
        nonlocal n_schema_bad
        scen_dir, arm, trial = item
        sid = scen_dir.name
        if errs:
            n_schema_bad += 1
            for e in errs[:5]:
                print(f"  schema: {sid} arm={arm} trial={trial}: {e}",
                      file=sys.stderr)
            if args.strict_schema:
                sys.exit(f"aborting: schema violation in {sid} trial={trial}")
        out.write(json.dumps(r) + "\n")
        out.flush()
        failed = [c["name"] for c in r["checks"] if c["status"] == "fail"]
        mt = r.get("metrics") or {}
        print(f"{sid} arm={r['arm']} trial={trial} "
              f"all_pass={r['all_pass']} dur={r['duration_s']}s "
              f"calls={mt.get('calls', 0)} tok_in={mt.get('tok_in_billed', 0)}"
              + (f" FAILED:{failed}" if failed else "")
              + (f"  [out_dir={r['out_dir']}]" if r.get("out_dir") else ""))

    if args.jobs > 1 and os.environ.get("ADH_PROXY"):
        print("WARNING: --jobs>1 with a recording proxy — per-trial proxy "
              "attribution is unavailable and trial marks are skipped. Run "
              "the H4 calibration serially.", file=sys.stderr)

    with open(args.out, "a") as out:
        if args.jobs > 1:
            # Threads, not processes: every trial is dominated by waiting
            # on a subprocess and on the endpoint, so the GIL is not the
            # constraint and the shared output file needs no IPC.
            from concurrent.futures import ThreadPoolExecutor
            lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                for item, r, errs in pool.map(one, work):
                    with lock:
                        emit(out, item, r, errs)
        else:
            for item in work:
                emit(out, *one(item))
    if n_schema_bad:
        print(f"WARNING: {n_schema_bad} run(s) violated the frozen schema "
              f"(lib/schema.py); cost metrics from them are not trustworthy",
              file=sys.stderr)


if __name__ == "__main__":
    main()
