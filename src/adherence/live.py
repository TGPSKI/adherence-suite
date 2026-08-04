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
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM      # exists, not ours
    return True


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
            "partial_lines": partial}


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

        # No marker means no pid to check, so liveness falls back to "the
        # stream moved recently", which is the same evidence a viewer would
        # use by eye.
        alive = (_alive(int(m["pid"])) if m.get("pid")
                 else age < STALE_S)
        if age > ABANDONED_S and not alive:
            continue                    # debris from a killed run

        st = _stream(stdout) if stdout.is_file() else _stream(Path(os.devnull))

        # Prefer the stream's own clock: it starts at the first inference
        # call and is present even when the marker is not.
        if st["first_ts"]:
            elapsed = max(elapsed, (now * 1000 - st["first_ts"]) / 1000.0)

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
            "info": _scenario_info(m.get("scenario", ""), root),
            **st,
        })
    runs.sort(key=lambda r: (r["state"] != "running", r["idle_s"]))
    return runs


def summarize(runs) -> dict:
    return {
        "running": sum(1 for r in runs if r["state"] == "running"),
        "grading": sum(1 for r in runs if r["state"] == "grading"),
        "stalled": sum(1 for r in runs if r["state"] in ("stalled", "dead")),
        "calls": sum(r["calls"] for r in runs),
        "tok_in": sum(r["tok_in"] for r in runs),
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


def main():
    runs = snapshot()
    if not runs:
        print("nothing in flight")
        return 0
    s = summarize(runs)
    print(f"{s['running']} running, {s['grading']} grading, "
          f"{s['stalled']} stalled — {s['calls']} calls, "
          f"{s['tok_in']:,} input tokens so far\n")
    print(f"{'scenario':<20}{'arm':>4}{'t':>3}{'state':>9}{'calls':>7}"
          f"{'tools':>7}{'tok_in':>11}{'elapsed':>9}{'budget':>8}  activity")
    for r in runs:
        print(f"{r['scenario'][:19]:<20}{r['arm']:>4}{r['trial']:>3}"
              f"{r['state']:>9}{r['calls']:>7}{r['tools']:>7}"
              f"{r['tok_in']:>11,}{fmt_age(r['elapsed_s']):>9}"
              f"{fmt_budget(r['budget']):>8}  {r['last_tool'][:50]}")
    if any(r["unlabelled"] for r in runs):
        print("\n? arm/trial: run started before the runner wrote its marker; "
              "the label is unknown and is not guessed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
