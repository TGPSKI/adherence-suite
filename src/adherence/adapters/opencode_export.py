#!/usr/bin/env python3
"""Convert `opencode export` JSON to the normalized transcript.

FALLBACK PATH. The primary converter is
`opencode_stream_to_transcript.py`, which reads `opencode run --format
json` and gets per-call tokens and cache fields directly. This one is
kept for opencode versions without `--format json`, and for reading
exports of sessions that were not captured live.

Defensive by design: opencode's export shape has changed across versions,
so this walks the whole JSON tree and pattern-matches recognizable parts
(bash tool inputs, edit/write tool inputs, task tool inputs, assistant
text, token usage) rather than assuming one exact schema. If the export
is missing entirely, it degrades to stdout-only: the final message is
whatever `opencode run` printed, and command/task checks report
ungradeable rather than fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from adherence import schema


def walk(node, out):
    if isinstance(node, dict):
        # Confirmed opencode export shape (v1.17.14):
        #   {"type": "tool", "tool": "bash"|"task"|"edit"|..., "state": {
        #       "status": "completed", "input": {...}, "output": "...",
        #       "metadata": {"exit": 0, ...}}}
        if node.get("type") == "tool":
            tool = str(node.get("tool", "")).lower()
            state = node.get("state") or {}
            inp = state.get("input") or {}
            if tool == "bash" and "command" in inp:
                out.append({"type": "command", "content": inp["command"]})
            elif tool in ("edit", "write", "patch") and ("filePath" in inp or "path" in inp):
                out.append({"type": "edit",
                            "path": inp.get("filePath") or inp.get("path"),
                            "content": str(inp.get("content", inp.get("newString", "")))[:4000]})
            elif tool == "task":
                out.append({"type": "task",
                            "subagent": inp.get("subagent_type", inp.get("subagent", "")),
                            "prompt": str(inp.get("prompt", ""))[:2000]})
            elif tool in ("read", "glob", "grep"):
                out.append({"type": "probe", "tool": tool,
                            "target": inp.get("filePath") or inp.get("pattern") or ""})

        # Fallback for older/alternate export shapes with a flat
        # tool/input on the node itself (kept for resilience across
        # opencode versions; the branch above is authoritative for the
        # confirmed 1.17.14 shape).
        tool = node.get("tool") or node.get("name") or node.get("toolName")
        flat_inp = node.get("input") or node.get("args") or node.get("arguments")
        if isinstance(flat_inp, str):
            try:
                flat_inp = json.loads(flat_inp)
            except Exception:
                flat_inp = {"raw": flat_inp}
        if tool and isinstance(flat_inp, dict) and node.get("type") != "tool":
            t = str(tool).lower()
            if t == "bash" and "command" in flat_inp:
                out.append({"type": "command", "content": flat_inp["command"]})
            elif t in ("edit", "write", "patch") and ("filePath" in flat_inp or "path" in flat_inp):
                out.append({"type": "edit",
                            "path": flat_inp.get("filePath") or flat_inp.get("path"),
                            "content": str(flat_inp.get("content", flat_inp.get("newString", "")))[:4000]})
            elif t == "task":
                out.append({"type": "task",
                            "subagent": flat_inp.get("subagent_type", flat_inp.get("subagent", "")),
                            "prompt": str(flat_inp.get("prompt", ""))[:2000]})

        # assistant text parts
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            role = node.get("role", "")
            out.append({"type": "message", "content": node["text"], "_role": role})
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def session_tokens(export: dict):
    """Prefer the session-level cumulative token count (info.tokens) over
    summing every message/part-level 'tokens' object -- the latter
    double/triple-counts in multi-round tool-loop sessions, since each
    round's message carries the context-so-far, not a per-round delta."""
    info = export.get("info") or {}
    tok = info.get("tokens") or {}
    pt = tok.get("input", 0) or 0
    ct = tok.get("output", 0) or 0
    if pt or ct:
        return pt, ct
    # Fallback: sum top-level message.info.tokens (one per message, not
    # per part). Measured on opencode 1.18.10, that sum equals
    # info.tokens exactly (93,519 over a 9-call session) -- one assistant
    # message per inference call. It is not an approximation.
    pt = ct = 0
    for msg in export.get("messages", []):
        t = (msg.get("info") or {}).get("tokens") or {}
        pt += t.get("input", 0) or 0
        ct += t.get("output", 0) or 0
    return pt, ct


def message_calls(export: dict) -> list[dict]:
    """One `call` event per assistant message. Verified on 1.18.10: each
    assistant message carries exactly one inference call's tokens, and
    their sum reconciles to info.tokens, which settles design §0.3 --
    the session scalar is billed input summed over calls, not the final
    context size.

    Ordering caveat: these are appended after the walked tool events
    rather than interleaved with them, because the tree walk does not
    preserve message boundaries. No metric depends on the relative order
    of `call` and tool events; `probes_to_first_edit` is computed from
    probe and edit events only."""
    out, seqs = [], {}
    for msg in export.get("messages", []):
        info = msg.get("info") or {}
        t = info.get("tokens") or {}
        if not (t.get("input") or t.get("output")):
            continue
        cache = t.get("cache") or {}
        agent = info.get("agent") or schema.ROOT_AGENT
        seq = seqs.get(agent, 0)
        seqs[agent] = seq + 1
        out.append(schema.call(
            seq=seq,
            input_tokens=t.get("input", 0) or 0,
            output_tokens=t.get("output", 0) or 0,
            agent=agent,
            cache_read=(cache.get("read", 0) or 0),
            cache_write=(cache.get("write", 0) or 0)))
    return out


def agent_switches(export: dict):
    """opencode's @-mention subagent dispatch (e.g. '@debug' typed in a
    message) is NOT represented as a discrete tool call -- it's an
    'agent' field switch on the session/message, confirmed from a real
    export where info.agent == 'build' with no mention present. Walk the
    session's own agent plus every message's agent field, in order,
    deduped, so a grader can check whether dispatch to a named subagent
    ever actually happened."""
    seen = []
    info_agent = (export.get("info") or {}).get("agent")
    if info_agent:
        seen.append(info_agent)
    for msg in export.get("messages", []):
        a = (msg.get("info") or {}).get("agent")
        if a and a not in seen:
            seen.append(a)
    return seen


def main():
    export_path, stdout_path, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    events = []
    have_export = False
    p = Path(export_path)
    pt = ct = 0
    agents = []
    calls = []
    if p.exists() and p.stat().st_size > 2:
        try:
            data = json.loads(p.read_text(errors="replace"))
            walk(data, events)
            have_export = True
            pt, ct = session_tokens(data)
            agents = agent_switches(data)
            calls = message_calls(data)
        except json.JSONDecodeError:
            pass

    # capability marker: with a parsed export we can see task events
    header = [schema.capability(task_events=have_export,
                                call_events=bool(calls))]
    for a in agents:
        header.append(schema.agent_active(a))

    for e in events:
        e.pop("_role", None)
    # final message: last assistant text from export, else raw stdout
    msgs = [e for e in events if e["type"] == schema.MESSAGE]
    final = msgs[-1]["content"] if msgs else Path(stdout_path).read_text(errors="replace")

    events += calls
    if pt or ct:
        events.append(schema.usage(pt, ct))

    out = header + events
    for e in schema.validate_transcript(out)[:10]:
        print(f"export-adapter: schema: {e}", file=sys.stderr)

    with open(out_dir / "transcript.jsonl", "w") as f:
        for e in out:
            f.write(json.dumps(e) + "\n")
    (out_dir / "final_message.txt").write_text(final)


if __name__ == "__main__":
    main()
