#!/usr/bin/env python3
"""What is happening right now, read off disk without touching the run.

    python3 -m adherence.live            # one-shot text snapshot

A result row is written only after a trial finishes and is graded, so
`results.jsonl` cannot show work in flight -- and for a 900s timeout at
--jobs 3 that means a viewer sits at 0% for minutes while three agents are
very much doing something. The evidence is already on disk: the adapter
streams opencode's NDJSON to `<out-dir>/stdout.txt` as it arrives, and the
runner drops a marker naming the scenario, arm and trial. This joins them.

Strictly read-only, and deliberately tolerant: every file it reads is being
written concurrently by a process that does not know it is being watched.
A half-written final line is normal and is skipped, not raised. A viewer
that can crash a benchmark is worse than no viewer.

Stdlib only.
"""
from __future__ import annotations

import errno
import glob
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from adherence import REPO_ROOT
from adherence.runner import RUN_MARKER

# Runs whose stream has not moved in this long are shown as stalled rather
# than dropped: a hung adapter is exactly the thing you want to see.
STALE_S = 180
# Beyond this an out-dir is leftover debris from a killed run, not a run.
ABANDONED_S = 1800


def out_dirs(tmp: str | None = None):
    root = tmp or tempfile.gettempdir()
    return sorted(glob.glob(os.path.join(root, "adh-out-*")))


def _alive(pid: int) -> bool:
    """Does this pid exist? POSIX only -- see below.

    `os.kill(pid, 0)` is the standard existence probe on POSIX, where
    signal 0 is "check, do not send". On Windows it is not a probe at all:
    `signal.CTRL_C_EVENT == 0`, so the call delivers a real Ctrl-C to the
    target's console group. The selftest wrote a marker carrying its own
    pid, this function checked it, and the check raised KeyboardInterrupt
    inside an unrelated subprocess several tests later. Sixteen seconds
    into the Windows job, with a traceback pointing at code that had
    nothing to do with it.

    A liveness check must never be able to affect what it observes, so on
    Windows it declines to answer and the caller falls back to evidence
    that cannot misfire."""
    if not pid or os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM      # exists, not ours
    return True


def busy_out_dirs() -> set[str] | None:
    """Out-dirs named on the command line of some live process.

    The runner passes the out-dir to the adapter as an argument, so it is
    right there in `ps` for the whole life of the trial. That makes it a
    direct liveness signal, and a much better one than the fallback it
    replaces: without a marker there is no pid, so liveness was inferred
    from "the stream file moved recently", which reports a run killed ten
    seconds ago as still running for another three minutes. Observed
    exactly that after a teardown -- four rows claiming to run while `ps`
    showed nothing at all.

    None means ps was unavailable and the caller should fall back."""
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    busy = set()
    for line in out.stdout.splitlines():
        for tok in line.split():
            if "adh-out-" in tok:
                # prompt.txt lives inside the out-dir; take the directory.
                p = tok.rstrip("/")
                if not p.endswith("adh-out-") and "/adh-out-" in f"/{p}":
                    while p and "adh-out-" not in os.path.basename(p):
                        p = os.path.dirname(p)
                    if p:
                        busy.add(p)
    return busy


# The stream nests everything one level down under "part"; the useful
# fields are part.tokens for a step_finish and part.tool / part.state.input
# for a tool_use. Read off opencode 1.18.10 -- the same moving target the
# adapter warns about, which is why a missing field degrades to zero here
# instead of raising.
def _target(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in ("filePath", "path", "pattern", "command", "description",
              "query"):
        v = inp.get(k)
        if v:
            return str(v)
    return ""


def _stream(path: Path) -> dict:
    """Fold the in-flight NDJSON into counters.

    The file is open for append in another process. The last line is
    routinely a partial write, so a JSON error means "not finished yet",
    never "corrupt"."""
    calls = tools = tok_in = tok_out = text = 0
    cache_r = cache_w = 0
    last_tool = last_text = ""
    tool_counts: dict[str, int] = {}
    sessions: dict[str, int] = {}
    spawns: list[str] = []
    first_ts = last_ts = 0
    partial = 0
    root_sid = ""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    partial += 1
                    continue
                part = e.get("part") or {}
                ts = e.get("timestamp") or 0
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                sid = e.get("sessionID") or ""
                if sid and not root_sid:
                    root_sid = sid
                t = e.get("type")
                if t == "step_finish":
                    calls += 1
                    if sid:
                        sessions[sid] = sessions.get(sid, 0) + 1
                    tk = part.get("tokens") or {}
                    tok_in += tk.get("input") or 0
                    tok_out += tk.get("output") or 0
                    ch = tk.get("cache") or {}
                    cache_r += ch.get("read") or 0
                    cache_w += ch.get("write") or 0
                elif t == "tool_use":
                    tools += 1
                    name = part.get("tool") or ""
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    inp = (part.get("state") or {}).get("input") or {}
                    last_tool = f"{name} {_target(inp)[:70]}".strip()
                    # `task` is a subagent spawn: its description is the
                    # clearest statement of what the agent thinks it is
                    # doing, and E3 is a claim about exactly these.
                    if name == "task":
                        d = inp.get("description") or ""
                        if d:
                            spawns.append(str(d)[:70])
                elif t == "text":
                    body = (part.get("text") or "").strip().replace("\n", " ")
                    if body:
                        text += 1
                        last_text = body[:200]
    except OSError:
        pass
    return {"calls": calls, "tools": tools, "tok_in": tok_in,
            "tok_out": tok_out, "cache_read": cache_r, "cache_write": cache_w,
            "texts": text, "last_tool": last_tool, "last_text": last_text,
            "tool_counts": tool_counts, "spawns": spawns,
            # More than one session id means the agent dispatched a
            # subagent, which the root session's own totals would miss.
            "sessions": len(sessions), "subagent_calls":
                sum(sorted(sessions.values())[:-1]) if len(sessions) > 1 else 0,
            "first_ts": first_ts, "last_ts": last_ts,
            "root_session": root_sid, "partial_lines": partial}


def _subagents(out_dir: Path, root_session: str) -> dict:
    """Child sessions this trial has dispatched, read live.

    A subagent runs in its OWN opencode session, and neither the root
    stream nor the root export carries a single one of its inference calls
    -- measured at 3.1x under-report on s13. So the stream this view reads
    cannot see subagent cost at all, and reporting its totals without
    saying so would understate exactly the number E3 is a claim about.

    The runner gives every trial its own XDG_DATA_HOME under the out-dir,
    so opencode's store is right here while the run is live. Read-only and
    best-effort: the writer is active, the schema is private and may move
    between versions, and a viewer must never block a run. On any failure
    this reports nothing rather than a number it cannot stand behind."""
    db = out_dir / "xdg" / "data" / "opencode" / "opencode.db"
    empty = {"sessions": [], "root": root_session, "readable": False,
             "calls": 0, "tok_in": 0, "tok_out": 0}
    if not db.exists():
        return dict(empty)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.execute("PRAGMA query_only=ON")
        rows = list(con.execute(
            "SELECT id, parent_id, COALESCE(agent,'') FROM session "
            "WHERE parent_id IS NOT NULL ORDER BY time_created ASC"))
        kids = [(sid, agent) for sid, _parent, agent in rows]
        calls = tok_in = tok_out = 0
        if kids:
            ids = [s for s, _a in kids]
            placeholders = ",".join("?" * len(ids))
            q = f"SELECT data FROM part WHERE session_id IN ({placeholders})"
            for (data,) in con.execute(q, ids):
                try:
                    d = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                # Same event the root stream calls step_finish; the store
                # spells it with a hyphen.
                if d.get("type") == "step-finish":
                    calls += 1
                    tk = d.get("tokens") or {}
                    tok_in += tk.get("input") or 0
                    tok_out += tk.get("output") or 0
        con.close()
    except Exception:
        return dict(empty)
    return {"sessions": kids, "root": root_session, "readable": True,
            "calls": calls, "tok_in": tok_in, "tok_out": tok_out}


def activity(out_dir: Path, limit: int = 60) -> list[dict]:
    """The trial's recent events, with tool output.

    The NDJSON stream carries a tool call's *input* and status but not
    what it returned -- so a viewer built on the stream alone can say "it
    ran go test" and never what go test said, which is the one thing worth
    knowing while a run is in flight. opencode's store keeps the output,
    and the runner puts that store in the out-dir.

    Read-only and best-effort, like everything else here: the writer is
    live and the schema is private."""
    db = out_dir / "xdg" / "data" / "opencode" / "opencode.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.execute("PRAGMA query_only=ON")
        kids = {s for (s,) in con.execute(
            "SELECT id FROM session WHERE parent_id IS NOT NULL")}
        rows = list(con.execute(
            "SELECT session_id, data, time_created FROM part "
            "ORDER BY time_created DESC LIMIT ?", (limit * 6,)))
        con.close()
    except Exception:
        return []

    out = []
    for sid, data, ts in rows:
        try:
            d = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        kind = d.get("type")
        who = "subagent" if sid in kids else "agent"
        if kind == "tool":
            st = d.get("state") or {}
            inp = st.get("input") or {}
            body = st.get("error") or st.get("output") or ""
            out.append({
                "ts": ts, "who": who, "kind": "tool",
                "name": d.get("tool") or "?",
                "target": _target(inp),
                "status": st.get("status") or "",
                "text": " ".join(str(body).split())[:400],
                "failed": bool(st.get("error")),
            })
        elif kind == "text":
            body = " ".join((d.get("text") or "").split())
            if body:
                out.append({"ts": ts, "who": who, "kind": "text",
                            "name": "", "target": "", "status": "",
                            "text": body[:400], "failed": False})
    out.sort(key=lambda e: e["ts"])
    return out[-limit:]


def _scenario_info(scenario: str, root: Path) -> dict:
    """Static facts about the task, from the repo rather than the run."""
    d = root / "scenarios" / scenario
    info = {"prompt": "", "category": "", "timeout": 0, "pr": "",
            "code_files": [], "test_files": [], "test_cmd": "",
            "base_commit": ""}
    y = d / "scenario.yaml"
    if y.is_file():
        try:
            from adherence.runner import load_yamlish
            meta = load_yamlish(y)
            info["prompt"] = str(meta.get("prompt", ""))
            info["category"] = str(meta.get("category", ""))
            info["timeout"] = int(meta.get("timeout", 0) or 0)
            info["base_commit"] = str(meta.get("base_commit", ""))
        except (OSError, ValueError):
            pass
    t = d / "task.json"
    if t.is_file():
        try:
            rec = json.loads(t.read_text())
            info["pr"] = str(rec.get("pr", ""))
            info["code_files"] = rec.get("code_files") or []
            info["test_files"] = rec.get("test_files") or []
            info["test_cmd"] = rec.get("test_cmd") or ""
        except (OSError, json.JSONDecodeError):
            pass
    return info


def snapshot(tmp: str | None = None, root: Path | None = None,
             now: float | None = None) -> list[dict]:
    """One record per run currently in flight, newest activity first."""
    root = root or REPO_ROOT
    now = now or time.time()
    busy = busy_out_dirs()
    runs = []
    for d in out_dirs(tmp):
        p = Path(d)
        marker = p / RUN_MARKER
        m = {}
        if marker.is_file():
            try:
                m = json.loads(marker.read_text())
            except (OSError, json.JSONDecodeError):
                m = {}
        if not m:
            # A run started before the marker existed, or one whose marker
            # was lost. The directory name still carries the scenario, and
            # the stream still carries the work -- so degrade to that
            # rather than pretending nothing is happening. Arm and trial
            # are genuinely unknown here and are shown as unknown, never
            # guessed: a wrong arm label is worse than a missing one.
            name = p.name[len("adh-out-"):]
            m = {"scenario": name.rsplit("-", 1)[0], "arm": "?", "trial": -1,
                 "unlabelled": True}

        stdout = p / "stdout.txt"
        # transcript.jsonl is written after the harness returns, so its
        # presence means the agent is done and grading is under way.
        graded = (p / "transcript.jsonl").is_file()
        try:
            touched = max(os.path.getmtime(x) for x in
                          (stdout, p) if os.path.exists(x))
        except (OSError, ValueError):
            touched = 0.0
        age = now - touched if touched else 1e9
        started = m.get("started_at", "")
        try:
            elapsed = now - os.path.getmtime(marker)
        except OSError:
            elapsed = 0.0

        # Liveness, best evidence first: a process still naming this
        # out-dir, then the runner's recorded pid, and only if `ps` is
        # unavailable the weak "stream moved recently" guess.
        if busy is not None:
            alive = d in busy
        elif m.get("pid") and os.name == "posix":
            alive = _alive(int(m["pid"]))
        else:
            alive = age < STALE_S
        if age > ABANDONED_S and not alive:
            continue                    # debris from a killed run

        st = _stream(stdout) if stdout.is_file() else _stream(Path(os.devnull))

        # Prefer the stream's own clock: it starts at the first inference
        # call and is present even when the marker is not.
        if st["first_ts"]:
            elapsed = max(elapsed, (now * 1000 - st["first_ts"]) / 1000.0)

        kids = _subagents(p, st.get("root_session", ""))
        timeout = int(m.get("timeout") or 0)
        if graded:
            state = "grading"
        elif not alive:
            state = "dead"
        elif age > STALE_S:
            state = "stalled"
        else:
            state = "running"

        runs.append({
            "out_dir": d,
            "scenario": m.get("scenario", Path(d).name),
            "arm": m.get("arm", "-"),
            "trial": m.get("trial", 0),
            "model": m.get("model", ""),
            "sandbox": m.get("sandbox", ""),
            "started_at": started,
            "elapsed_s": elapsed,
            "timeout_s": timeout,
            # Fraction of the timeout burned -- the only honest progress
            # number for a run, since nothing on disk knows how near done
            # it is. None when the marker is missing: the runner's
            # --timeout overrides scenario.yaml, so substituting the
            # scenario's value would overstate the remaining budget on
            # exactly the runs closest to being killed.
            "budget": (elapsed / timeout) if timeout else None,
            "idle_s": age,
            "state": state,
            "unlabelled": bool(m.get("unlabelled")),
            # Dispatched subagents, from opencode's own store. `tok_in`
            # above is ROOT ONLY -- their calls are in separate sessions
            # the root stream never sees.
            "child_sessions": len(kids["sessions"]),
            "child_agents": [a for _s, a in kids["sessions"] if a],
            "children_readable": kids["readable"],
            "child_calls": kids["calls"],
            "child_tok_in": kids["tok_in"],
            "child_tok_out": kids["tok_out"],
            # The number E3 is actually a claim about: parent + every
            # child. Quoting the root total alone would confirm "subagent
            # handoff is 0-cost" by leaving the cost out of the number.
            "total_calls": st["calls"] + kids["calls"],
            "total_tok_in": st["tok_in"] + kids["tok_in"],
            "info": _scenario_info(m.get("scenario", ""), root),
            **st,
        })
    # Stable ordering. Sorting by activity (state, then idle time) put the
    # busiest run on top, which reads well and is unusable: idle_s changes
    # every second, so rows reorder under the cursor and a detail pane
    # silently swaps to a different trial while you are reading it.
    # Identity, not liveness, decides position.
    runs.sort(key=lambda r: (r["scenario"], r["arm"], r["trial"],
                             r["out_dir"]))
    return runs


def summarize(runs) -> dict:
    return {
        "running": sum(1 for r in runs if r["state"] == "running"),
        "grading": sum(1 for r in runs if r["state"] == "grading"),
        "stalled": sum(1 for r in runs if r["state"] in ("stalled", "dead")),
        "calls": sum(r["total_calls"] for r in runs),
        "tok_in": sum(r["total_tok_in"] for r in runs),
        "child_calls": sum(r["child_calls"] for r in runs),
    }


def fmt_budget(b) -> str:
    return "?" if b is None else f"{b * 100:.0f}%"


def fmt_age(s) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def sweep(tmp: str | None = None, dry: bool = False) -> tuple[int, int]:
    """Remove sandboxes and out-dirs no live process is using.

    A trial that completes cleans up after itself; one that is killed does
    not, and the leftovers are not harmless. They are what the live view
    reads, so a torn-down run keeps rendering as rows long after it is
    gone, and at ~30MB of git checkout apiece they are not free either.

    Refuses while anything is running, because the only way to decide a
    directory is dead is to know nothing is using it."""
    import shutil
    busy = busy_out_dirs()
    if busy is None:
        raise RuntimeError("cannot enumerate processes; refusing to delete "
                           "directories that might be in use")
    if busy:
        raise RuntimeError(
            f"{len(busy)} run(s) still active. Stop them first -- deleting a "
            f"sandbox under a running agent produces a garbage transcript "
            f"and a result row that looks real.")
    root = tmp or tempfile.gettempdir()
    freed = n = 0
    for d in sorted(glob.glob(os.path.join(root, "adh-*"))):
        try:
            size = sum(f.stat().st_size for f in Path(d).rglob("*")
                       if f.is_file())
        except OSError:
            size = 0
        n += 1
        freed += size
        if not dry:
            shutil.rmtree(d, ignore_errors=True)
    return n, freed


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true",
                    help="delete sandboxes and out-dirs from finished or "
                         "killed runs; refuses while anything is running")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.clean:
        try:
            n, freed = sweep(dry=a.dry_run)
        except RuntimeError as e:
            print(e)
            return 1
        verb = "would remove" if a.dry_run else "removed"
        print(f"{verb} {n} director{'y' if n == 1 else 'ies'}, "
              f"{freed / 1e6:.0f} MB")
        return 0

    runs = snapshot()
    if not runs:
        print("nothing in flight")
        return 0
    s = summarize(runs)
    print(f"{s['running']} running, {s['grading']} grading, "
          f"{s['stalled']} stalled — {s['calls']} calls "
          f"({s['child_calls']} in subagents), "
          f"{s['tok_in']:,} input tokens so far\n"
          f"calls and tokens are parent + children (§7)\n")
    print(f"{'scenario':<20}{'arm':>4}{'t':>3}{'state':>9}{'calls':>7}"
          f"{'tools':>7}{'sub':>5}{'tok_in':>12}{'elapsed':>9}"
          f"{'budget':>8}  activity")
    for r in runs:
        print(f"{r['scenario'][:19]:<20}{r['arm']:>4}{r['trial']:>3}"
              f"{r['state']:>9}{r['total_calls']:>7}{r['tools']:>7}"
              f"{r['child_sessions']:>5}"
              f"{r['total_tok_in']:>12,}{fmt_age(r['elapsed_s']):>9}"
              f"{fmt_budget(r['budget']):>8}  {r['last_tool'][:50]}")
    if any(r["unlabelled"] for r in runs):
        print("\n? arm/trial: run started before the runner wrote its marker; "
              "the label is unknown and is not guessed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
