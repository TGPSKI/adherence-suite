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
import signal
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

# Written into every out-dir at run start so a trial in flight can say what
# it is. Read by adherence.live; nothing in the measurement path depends
# on it, so a viewer can never perturb a run.
RUN_MARKER = ".adh-run.json"

# Every adapter this process started and has not yet reaped. A Ctrl-C or a
# crash used to leave the whole tree running -- reparented to systemd,
# holding the GPU, invisible to the next run except as unexplained
# slowness. Reaped by _reap_all via atexit and the signal handlers.
_LIVE_PROCS: set = set()

# Each trial stamps this into the environment it hands the adapter, so
# every process it spawns inherits it. That makes a stray attributable to
# the exact (scenario, arm, trial) that leaked it, rather than to
# "something opencode-ish is running".
RUN_ID_VAR = "ADH_RUN_ID"

# No stream activity for this long means stuck, not slow. Generous enough
# that a long inference call is never mistaken for a hang.
DEFAULT_IDLE_TIMEOUT = 300


def _unescape(v: str) -> str:
    r"""Reverse mkscenarios.yaml_escape: \\, \" and \n.

    The escape and the unescape are a matched pair and belong in the same
    place. They were not: the writer escaped, and only the one call site
    that wrote prompt.txt unescaped, so `load_yamlish` handed every other
    caller a prompt with 168 literal backslash-n in it. The agent was fine;
    anything that displayed or measured the prompt was not."""
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


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
                data[k] = (int(v) if v.isdigit()
                           else _unescape(v.strip('"')))
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


def _reap_all(*_a):
    """Kill every adapter tree this process started, then exit.

    Without it, Ctrl-C killed the runner and left its opencode grandchildren
    running. `pkill -f adherence.runner` did the same thing, which is how
    two orphans came to be contending with three live trials for the GPU."""
    procs, _LIVE_PROCS_copy = list(_LIVE_PROCS), None
    for p in procs:
        if p.poll() is None:
            kill_process_group(p, grace=3.0)
    if _a:                                   # arrived as a signal
        sys.exit(130)


def install_reapers() -> None:
    import atexit
    import contextlib
    atexit.register(_reap_all)
    if os.name == "posix":
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            # A signal that cannot be installed (already handled, or not a
            # main thread) is not worth failing a run over.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, _reap_all)


def per_trial_bench_config(out_dir: Path, run_id: str) -> Path | None:
    """A copy of the harness config whose baseURL names this trial.

    The proxy is authoritative for token counts (H4), but it could only
    attribute calls to a trial via a single global mark -- so under
    --jobs>1 the runner skipped marking entirely rather than write an
    attribution it knew was wrong, and the gate could not be measured in
    parallel at all. That was a real limitation of the design, recorded as
    "the second cost of parallelism".

    It does not have to be. Nothing in an inference request body identifies
    its trial, but the request *path* is ours: routing this trial through
    `<proxy>/__run/<run_id>/v1` makes attribution arrive with the call.
    The proxy strips the prefix before forwarding, so upstream sees exactly
    what it saw before.

    Returns None when no proxy is in use, leaving the adapter on whatever
    config it would otherwise have picked."""
    base = os.environ.get("ADH_PROXY")
    src = os.environ.get("ADH_BENCH_CONFIG") or str(ROOT / "bench" /
                                                    "opencode-bench.json")
    if not base or not Path(src).is_file():
        return None
    try:
        cfg = json.loads(Path(src).read_text())
    except (OSError, json.JSONDecodeError):
        return None

    root = base.rsplit("/v1", 1)[0].rstrip("/")
    tagged = f"{root}/__run/{urllib.parse.quote(run_id, safe='')}/v1"
    changed = False
    for prov in (cfg.get("provider") or {}).values():
        opts = prov.get("options") if isinstance(prov, dict) else None
        if isinstance(opts, dict) and opts.get("baseURL"):
            opts["baseURL"] = tagged
            changed = True
    if not changed:
        return None
    dest = out_dir / "opencode-bench.json"
    dest.write_text(json.dumps(cfg, indent=2))
    return dest


def wait_or_kill(proc, out_dir: Path, hard_s: int, idle_s: int):
    """Wait for the adapter, killing it only for a reason worth killing for.

    A plain wall-clock deadline cannot tell a hung run from a working one.
    Measured: trials were being killed at 900s while 110 inference calls
    deep and still advancing -- productive work, discarded, and recorded as
    `calls=0` because the adapter writes its transcript at the end. The
    number in the results file said the run did nothing. It had done more
    than any run that completed.

    So there are two deadlines, and they mean different things:

      idle_s  no new bytes on the event stream for this long. The agent is
              genuinely stuck -- a hung request, a wedged tool -- and the
              wall clock is the right thing to stop.
      hard_s  a ceiling on total wall time, so a run that loops forever
              while looking busy still ends. Deliberately generous,
              because being wrong here throws away real work.

    On kill: SIGTERM to the whole process group first, which gives the
    adapter's trap a window to convert whatever the stream captured into a
    transcript, then SIGKILL for anything that ignored it."""
    stream = out_dir / "stdout.txt"
    # A subagent runs in its OWN opencode session, and the root stream
    # carries none of its events -- so a trial that dispatches one and
    # waits looks completely idle on stdout.txt for as long as the child
    # works. Observed live: parent 0 calls, subagent 5 calls and 206,689
    # tokens, forty tool calls deep, with the stream silent for 3m27s.
    # Watching only the stream would kill that run as hung, and it would do
    # it *more* to the arms that route to subagents -- which are the arms
    # under test. Progress is either file moving.
    store = out_dir / "xdg" / "data" / "opencode" / "opencode.db"
    # WAL mode: the main .db neither grows nor restamps between
    # checkpoints, so summing it alone made a busy subagent look idle.
    wal = Path(str(store) + "-wal")
    t0 = time.time()
    last_size, last_change = -1, t0

    def _activity_size():
        import contextlib
        total = 0
        for f in (stream, store, wal):
            # A file that is not there yet contributes nothing; the sum
            # only has to move, not be meaningful on its own.
            with contextlib.suppress(OSError):
                st = f.stat()
                # size AND mtime: a WAL that is being overwritten in place
                # can stay the same size while still carrying new writes.
                total += st.st_size + int(st.st_mtime)
        return total

    while True:
        try:
            return proc.communicate(timeout=2.0)[1], None
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        size = _activity_size()
        if size != last_size:
            last_size, last_change = size, now

        idle = now - last_change
        elapsed = now - t0
        why = None
        if idle_s and idle > idle_s and last_size >= 0:
            why = (f"adapter idle {int(idle)}s (no event-stream and no "
                   f"session-store activity; limit {idle_s}s) after "
                   f"{int(elapsed)}s and {last_size:,} bytes")
        elif hard_s and elapsed > hard_s:
            why = (f"adapter hit the hard ceiling of {hard_s}s while still "
                   f"active ({last_size:,} bytes of events, last advanced "
                   f"{int(idle)}s ago)")
        if not why:
            continue

        leaked = kill_process_group(proc)
        try:
            err = proc.communicate(timeout=30)[1]
        except subprocess.TimeoutExpired:
            err = ""
        if leaked:
            why += f"; {leaked} process(es) survived the group kill"
        return err, why


def kill_process_group(proc, grace: float = 10.0) -> int:
    """Kill the adapter and everything it spawned. Returns survivors.

    `subprocess.run(timeout=...)` kills only the process it started. The
    adapter is a shell script, so the thing actually holding the GPU is a
    *grandchild* -- and on timeout it was surviving, getting reparented to
    the user's systemd, and running to completion with nobody left to read
    its output.

    That is not a slow leak, it is a spiral: every timeout permanently
    removes a slot's worth of GPU from the pool, so the remaining trials
    get slower, so more of them time out. Measured on a --jobs 3 probe:
    three timeouts left two orphans still running twenty minutes later,
    against three live trials -- five processes contending for three
    slots, and the run was steadily starving itself.

    SIGTERM to the whole group first so opencode can flush, then SIGKILL
    to whatever ignored it."""
    if os.name != "posix":
        # No process groups: kill what we can and report honestly rather
        # than pretending the tree is gone.
        proc.kill()
        return 0
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return 0
        except OSError:
            break
        deadline = time.time() + (grace if sig == signal.SIGTERM else 5.0)
        while time.time() < deadline:
            proc.poll()                 # reap the direct child as it dies
            if not group_members(pgid):
                return 0
            time.sleep(0.2)
    proc.poll()
    return len(group_members(pgid))


def group_members(pgid: int) -> list[int]:
    """Live, non-zombie pids in a process group.

    `os.killpg(pgid, 0)` is the obvious check and it is wrong here: a
    zombie is still a group member, so an un-reaped direct child makes a
    fully-killed group look alive. The suite's own hygiene test caught
    exactly that, reporting a leak that was really just a corpse waiting
    to be waited on."""
    out = []
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        # No procfs: fall back to the coarse check, zombies and all.
        try:
            os.killpg(pgid, 0)
            return [pgid]
        except OSError:
            return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        # comm can contain spaces and parens; everything after the last
        # ')' is positional. state is the first field after it, pgrp the
        # third.
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 3:
            continue
        if tail[0] == "Z":              # a corpse holds no GPU
            continue
        try:
            if int(tail[2]) == pgid:
                out.append(pid)
        except ValueError:
            continue
    return out


def strays(pattern: str = "opencode run") -> list[tuple[int, str]]:
    """Processes matching `pattern` that no live adapter owns.

    A previous run's leaked children are indistinguishable from load: the
    GPU is busy, trials are slow, and nothing in the results says why.
    Checked at startup so a poisoned run is refused rather than measured."""
    if os.name != "posix":
        return []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,etimes,args"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    live_pids = set()
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etimes, args = parts
        try:
            pid, ppid, etimes = int(pid), int(ppid), int(etimes)
        except ValueError:
            continue
        live_pids.add(pid)
        rows.append((pid, ppid, etimes, args))
    out_rows = []
    for pid, ppid, etimes, args in rows:
        # `--adapter adapters/opencode.sh` on the runner's own command line
        # matches a bare "opencode"; require the harness invocation itself.
        if pattern not in args or " -eo " in args:
            continue
        if "adherence.runner" in args or "isolate.sh" in args:
            continue
        rid = run_id_of(pid)
        # Owned by a live adapter/runner? then it is someone's current work.
        parent = next((r for r in rows if r[0] == ppid), None)
        if parent and ("adapters/" in parent[3] or "adherence.runner" in parent[3]):
            continue
        if ppid in live_pids and parent and pattern in parent[3]:
            continue                      # a child of another matching proc
        # The run id says which trial leaked it, which is the difference
        # between "kill something opencode-ish" and "this a3/trial-2 run
        # from the probe never died".
        label = f"{etimes}s"
        if rid:
            label += f"  run={rid}"
        out_rows.append((pid, f"{label}  {args[:60]}"))
    return out_rows


def run_id_of(pid: int) -> str:
    """The trial that spawned this process, from its own environment.

    Children inherit ADH_RUN_ID, so this attributes a stray to an exact
    (scenario, arm, trial) rather than leaving it as an anonymous process
    someone has to guess about. Linux-only; absent elsewhere, which costs
    a label and nothing else."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if kv.startswith(RUN_ID_VAR.encode() + b"="):
                    return kv.split(b"=", 1)[1].decode("utf-8", "replace")
    except OSError:
        pass
    return ""


def require_arms_dir(scen_dir: Path, meta: dict, arm: str, arms_dir) -> None:
    """A fixture-backed scenario with a named arm needs the arm's files.

    `apply_arm` treats a missing arms directory as "arm is a label only",
    which is right for the synthetic sNN scenarios -- they ship no
    instruction surface, so there is nothing to overlay. On a fixture it is
    catastrophic and silent: the sandbox gets whatever the repo happened to
    carry at that commit, and the record still says `arm=a1`.

    That is not hypothetical. Every one of the 34 cli/cli PR tasks has a
    base commit that predates the repo's AGENTS.md, so `make probe --arm a1`
    without an arms dir handed the model *no instruction surface at all* --
    arm A0, the floor, written to disk labelled A1. A calibration measured
    on the floor would then have set the difficulty band for an experiment
    run on a different surface entirely.

    A run that cannot apply the arm it claims must not start."""
    if arms_dir or not arm or arm == "-":
        return
    if meta.get("repo"):
        raise SystemExit(
            f"{scen_dir.name}: --arm {arm!r} needs --arms-dir. This scenario "
            f"is backed by a fixture repo, so the arm is an instruction "
            f"surface that has to be overlaid onto the checkout -- without "
            f"it the model gets whatever the repo shipped at that commit "
            f"and the record still claims arm={arm!r}. Pass --arms-dir "
            f"(e.g. fixtures/<name>.arms), or --arm '-' if you genuinely "
            f"mean 'whatever is in the tree'.")


def apply_arm(sandbox: Path, arms_dir: Path, arm: str) -> str:
    """Overlay one instruction-surface arm onto a materialized sandbox.

    Every arm first removes whatever instruction surface the repo ships
    (the manifest's `remove` list) and then writes its own. Skipping the
    removal is how an arm silently inherits another's surface — A0 would
    stop being a floor and A5 would quietly be A1+A5.

    Returns a human-readable note for the log. A missing arms directory
    is not an error for the synthetic sNN scenarios: they carry no
    instruction surface, so `arm` is a label on those runs and nothing
    else. For a fixture-backed scenario it is always an error -- see
    `require_arms_dir`."""
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
            timeout_override: int = 0, purpose: str = "validation",
            idle_timeout: int = DEFAULT_IDLE_TIMEOUT) -> dict:
    meta = load_yamlish(scen_dir / "scenario.yaml")
    sandbox = Path(tempfile.mkdtemp(prefix=f"adh-{scen_dir.name}-"))
    out_dir = Path(tempfile.mkdtemp(prefix=f"adh-out-{scen_dir.name}-"))
    # Identifies every process this trial spawns. Derived from the out-dir
    # name, which is already unique, so it survives in ps output and in
    # the marker without a second source of truth.
    run_id = f"{scen_dir.name}|{arm}|{trial}|{out_dir.name.rsplit('-', 1)[-1]}"

    # Declare the run before doing any of it. Sandbox and out-dir get
    # independent random suffixes, and neither name carries the arm or the
    # trial -- so until this file exists a run in flight is anonymous, and
    # a viewer can see that *something* is happening but not what. Written
    # first, so it covers a run that dies during setup.
    (out_dir / RUN_MARKER).write_text(json.dumps({
        "scenario": scen_dir.name, "arm": arm, "trial": trial,
        "run_id": run_id,
        "model": model, "adapter": adapter.name,
        "sandbox": str(sandbox), "pid": os.getpid(),
        "timeout": int(timeout_override or meta.get("timeout", 1800)),
        "started_at": datetime.now(timezone.utc)
                          .replace(microsecond=0).isoformat(),
    }, indent=2) + "\n")

    materialize(scen_dir, sandbox, meta)
    # Per-fixture ignore set, installed before anything can dirty the
    # tree. `ignore:` in scenario.yaml lists whatever this repo's own
    # test run writes and its .gitignore misses (H9).
    gradelib.write_harness_excludes(sandbox, meta.get("ignore", []))
    # PR-derived scenarios ship a task record the grader needs and the
    # agent must not have: test files, the merge commit, the test command.
    # Written AFTER the excludes so it is ignored rather than committed:
    # the runner rewrites it once the agent stops, and a tracked file that
    # the harness modifies is a file the grader will attribute to the
    # agent. It names no source path the prompt withheld.
    task_json = scen_dir / "task.json"
    if task_json.is_file():
        (sandbox / ".adh-task.json").write_text(task_json.read_text())
    require_arms_dir(scen_dir, meta, arm, arms_dir)
    apply_arm(sandbox, arms_dir, arm)
    # Baseline commit LAST, after the arm overlay is in place. Committing
    # first would leave every arm's own instruction files sitting in
    # `git status`, and `check_no_extra_changes` would read the harness's
    # setup as the agent editing files outside its surface.
    git(sandbox, "add -A")
    git(sandbox, "-c user.email=a@b -c user.name=adh commit -qm baseline "
                 "--allow-empty")

    prompt_file = out_dir / "prompt.txt"
    # load_yamlish already unescaped; doing it again here would corrupt a
    # prompt that legitimately contains a backslash.
    prompt_file.write_text(meta["prompt"])

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
    # start_new_session puts the adapter in its own process group so a
    # deadline can kill everything it spawned. See kill_process_group.
    env["ADH_RUN_ID"] = run_id
    cfg = per_trial_bench_config(out_dir, run_id)
    if cfg:
        env["ADH_BENCH_CONFIG"] = str(cfg)
    proc = subprocess.Popen(
        [str(adapter), str(sandbox), model, str(prompt_file), str(out_dir),
         target_agent],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        **({"start_new_session": True} if os.name == "posix" else {}))
    _LIVE_PROCS.add(proc)
    try:
        err, why = wait_or_kill(proc, out_dir, timeout, idle_timeout)
    finally:
        _LIVE_PROCS.discard(proc)
    adapter_ok = why is None and proc.returncode == 0
    adapter_err = why or (err or "")[-2000:]
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
        # docs/EVAL.md exclusion criterion 1: "the harness did not complete a
        # run. Not a model result." Grading this `fail` made it exactly that
        # -- all_pass went False and every downstream pass rate counted a
        # harness fault against the model. `ungradeable` is the status the
        # design reserves for a harness gap, so the row can be excluded and
        # counted instead of silently scored.
        checks = [gradelib.skip("adapter", adapter_err).d()]

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
        schema_errors=schema_errs,
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
    ap.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
                    help="kill a trial after this many seconds with NO event "
                         "stream activity (default: %(default)s). This is "
                         "the deadline that means 'stuck'; --timeout is a "
                         "ceiling on total wall time and should be generous, "
                         "because a run that is still making calls is doing "
                         "work that a kill throws away. 0 disables.")
    ap.add_argument("--allow-strays", action="store_true",
                    help="start even when unowned harness processes are "
                         "running. They contend for the GPU, so durations "
                         "and timeouts measured alongside them are not "
                         "comparable to a clean run")
    args = ap.parse_args()

    suite = load_yamlish(ROOT / args.suite)
    ids = suite.get("scenarios", [])
    if args.only:
        wanted = set(args.only.split(","))
        ids = [i for i in ids if i in wanted]

    adapter = Path(args.adapter).resolve()
    arms = [a.strip() for a in args.arms.split(",")] if args.arms else [args.arm]
    arms_dir = Path(args.arms_dir).resolve() if args.arms_dir else None

    # A previous run's leaked children look exactly like load: the GPU is
    # busy, trials are slow, and the results record none of it. Every
    # number this run produces would be contaminated by contention it
    # cannot see, so refuse rather than measure.
    left = strays()
    if left and not args.allow_strays:
        lines = "\n".join(f"    {pid}  {d}" for pid, d in left[:8])
        sys.exit(
            f"refusing to start: {len(left)} harness process(es) are running "
            f"that no adapter owns:\n{lines}\n\n"
            f"These are almost always orphans from a timed-out or killed "
            f"run. They hold the GPU, so every duration and every timeout "
            f"measured now would reflect contention this run did not cause "
            f"and does not record.\n\n"
            f"    kill {' '.join(str(p) for p, _ in left)}\n\n"
            f"Then re-run. Pass --allow-strays to measure anyway.")
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
                          args.timeout, args.purpose, args.idle_timeout)
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
        print("note: --jobs>1 with a recording proxy. Trial marks are "
              "skipped (they are one piece of shared state and two trials "
              "would interleave), but each trial routes through its own "
              "/__run/<id> path, so per-trial attribution is exact and the "
              "H4 gate is measurable in parallel.", file=sys.stderr)

    install_reapers()
    with open(args.out, "a") as out:
        # One writer per results file. Two runners appending to the same
        # file interleave silently into something that looks like one
        # coherent run -- two models, two arms sets, two grids, pooled by
        # accident and indistinguishable afterwards. flock is advisory and
        # released with the fd, so a crashed runner does not wedge it.
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(out.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                sys.exit(
                    f"refusing to start: another runner is already writing "
                    f"{args.out}. Two runners appending to one file "
                    f"interleave into a single file that reads as one run "
                    f"and is not. Use --out with a different path.")
        if args.jobs > 1:
            # Threads, not processes: every trial is dominated by waiting
            # on a subprocess and on the endpoint, so the GIL is not the
            # constraint and the shared output file needs no IPC.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = [pool.submit(one, it) for it in work]
                # as_completed, NOT map: map yields in SUBMISSION order, so
                # one slow trial buffers every later result inside the
                # executor. Observed on the probe -- a trial finished, its
                # out-dir was cleaned up, and its row stayed unwritten
                # behind two 20-minute runs: invisible to the live view,
                # missing from the progress count, and lost outright if the
                # runner were killed. A finished trial is written now.
                for fut in as_completed(futures):
                    try:
                        item, r, errs = fut.result()
                    except Exception as e:          # noqa: BLE001
                        print(f"  trial raised: {e}", file=sys.stderr)
                        continue
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
