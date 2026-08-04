#!/usr/bin/env python3
"""adapters/api.py <sandbox> <model> <prompt-file> <out-dir>

Minimal agent loop against any OpenAI-compatible /v1/chat/completions
endpoint (default http://127.0.0.1:8000/v1, override ADH_BASE_URL).
Gives the model three tools — bash, write_file, task — executes bash in
the sandbox, records everything to the normalized transcript. `task` is
a stub that logs the dispatch (that's all s01 needs) and returns a
canned acknowledgement.

This isolates MODEL adherence from HARNESS adherence: run the same suite
through this adapter and through opencode.sh, diff the scoreboards, and
the difference is the harness's contribution. Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from adherence import schema

MAX_ROUNDS = 15

TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command in the working directory.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write content to a relative file path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "task",
        "description": "Dispatch a named subagent with a prompt.",
        "parameters": {"type": "object", "properties": {
            "subagent_type": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["subagent_type", "prompt"]}}},
]


def call_api(base, model, messages):
    body = json.dumps({"model": model, "messages": messages,
                       "tools": TOOLS, "max_tokens": 4096}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def main():
    sandbox, model, prompt_file, out_dir = sys.argv[1:5]
    sandbox, out_dir = Path(sandbox), Path(out_dir)
    base = os.environ.get("ADH_BASE_URL", "http://127.0.0.1:8000/v1")
    events = [schema.capability(task_events=True, call_events=True)]
    pt = ct = 0

    agents_md = sandbox / "AGENTS.md"
    sysmsg = "You are a coding agent working in the current directory."
    if agents_md.exists():
        sysmsg += "\n\nOperator rules:\n" + agents_md.read_text()

    messages = [{"role": "system", "content": sysmsg},
                {"role": "user", "content": Path(prompt_file).read_text()}]
    final = ""

    for seq in range(MAX_ROUNDS):
        t0 = time.time()
        resp = call_api(base, model, messages)
        u = resp.get("usage", {}) or {}
        det = u.get("prompt_tokens_details") or {}
        pt += u.get("prompt_tokens", 0)
        ct += u.get("completion_tokens", 0)
        choice = (resp.get("choices") or [{}])[0]
        # One call event per round -- this loop *is* the round-trip count,
        # so it is the one adapter where `calls` cannot be wrong (§3.1).
        events.append(schema.call(
            seq=seq,
            input_tokens=u.get("prompt_tokens", 0) or 0,
            output_tokens=u.get("completion_tokens", 0) or 0,
            # vLLM reports prompt_tokens_details=null as configured here,
            # so cache_read is 0 in practice. It is a compute saving even
            # when present, never a billing one -- §16.1.
            cache_read=(det.get("cached_tokens", 0) or 0) if isinstance(det, dict) else 0,
            duration_ms=int((time.time() - t0) * 1000),
            stop_reason=choice.get("finish_reason", "") or ""))
        msg = choice.get("message") or {}
        messages.append(msg)
        if msg.get("content"):
            final = msg["content"]
            events.append(schema.message(final))
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        for tc in calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if fn == "bash":
                cmd = args.get("command", "")
                events.append(schema.command(cmd))
                try:
                    r = subprocess.run(cmd, shell=True, cwd=sandbox,
                                       capture_output=True, text=True, timeout=120)
                    out = (r.stdout + r.stderr)[-4000:] + f"\n[exit={r.returncode}]"
                except subprocess.TimeoutExpired:
                    out = "[timeout]"
            elif fn == "write_file":
                rel = args.get("path", "out.txt")
                events.append(schema.edit(rel, args.get("content", "")[:4000]))
                p = (sandbox / rel)
                if ".." in rel:
                    out = "error: path escapes sandbox"
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(args.get("content", ""))
                    out = f"wrote {rel}"
            elif fn == "task":
                events.append(schema.task(args.get("subagent_type", ""),
                                          args.get("prompt", "")[:2000]))
                out = "subagent dispatched; result: acknowledged (stub)"
            else:
                out = f"unknown tool {fn}"
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": out})

    events.append(schema.usage(pt, ct))
    for e in schema.validate_transcript(events)[:10]:
        print(f"api-adapter: schema: {e}", file=sys.stderr)
    with open(out_dir / "transcript.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (out_dir / "final_message.txt").write_text(final)


if __name__ == "__main__":
    main()
