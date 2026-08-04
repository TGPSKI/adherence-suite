#!/usr/bin/env python3
"""Convert `opencode run --format json` NDJSON to the normalized transcript.

    opencode_stream_to_transcript.py <stream.jsonl> <export.json> \
        <stdout.txt> <out-dir>

Why this replaces the export walker as the primary path (verified against
opencode 1.18.10):

- `--format json` emits one `step_finish` part **per inference call**,
  carrying `tokens: {input, output, reasoning, cache: {read, write}}` and
  a `reason`. That is the per-call accounting design §3.1 requires, cache
  fields included — the export's `info.tokens` is only the session total.
- It is a live stream, so no session-id resolution and no export-shape
  drift, which `opencode_export_to_transcript.py` documents fighting.
- Tool parts arrive with full `input`, `output`, and timing, so commands,
  edits, tasks and probes all come from the same stream.

Measured on a 9-call session: Σ per-call `tokens.input` = 93,519 =
`info.tokens.input` exactly. **The session scalar the suite has always
recorded is billed input summed over calls, not final context size** —
design §0.3's open question, resolved in favour of the existing numbers.
The aggregate `usage` event is still emitted from that sum so the `sNN`
scoreboard keeps working.

The export is still read, for one thing the stream does not carry: the
per-message `agent` field that `agents_seen()` needs to tell whether
@-mention dispatch to a named subagent actually happened. Missing export
degrades that check to ungradeable, never to fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from adherence import schema

# Probe tools are read/glob/grep only, matching gradelib.probes()'s
# documented meaning. `list` is arguably exploration too, but widening
# the set would silently change `probes_to_first_edit` for every
# scenario already measured with it.
PROBE_TOOLS = ("read", "glob", "grep")
EDIT_TOOLS = ("edit", "write", "patch")


def _read_stream(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # progress noise, not an event
    return out


def _agent_for(session_id: str, root_session: str, sub_names: list[str],
               sub_index: dict) -> str:
    """Attribute a call to an agent. The stream carries sessionID, not an
    agent name: a dispatched subagent runs in its own session. Root
    session -> ROOT_AGENT; any other session -> the subagent named by the
    Nth `task` dispatch seen so far, which is the best attribution
    available without joining against child exports (see H12/E11)."""
    if not session_id or session_id == root_session:
        return schema.ROOT_AGENT
    if session_id not in sub_index:
        n = len(sub_index)
        sub_index[session_id] = (sub_names[n] if n < len(sub_names)
                                 else f"sub{n}")
    return sub_index[session_id]


def _child_calls(child_dir: Path) -> tuple[list[dict], int, int]:
    """Call events for every dispatched subagent, read from per-child
    exports written by adapters/opencode.sh.

    Without these, `tok_in_billed` is root-session-only and E3's
    "0-cost subagent handoff" is confirmed by omission rather than by
    measurement (§7). Attribution is the child's own `agent` field, so
    parent and child costs separate on `call.agent` exactly as §3.1
    specifies."""
    events, tin, tout = [], 0, 0
    if not child_dir or not child_dir.is_dir():
        return events, tin, tout
    for f in sorted(child_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        info = data.get("info") or {}
        agent = info.get("agent") or f.name.split(".")[0] or "sub"
        seq = 0
        for msg in data.get("messages", []):
            t = ((msg.get("info") or {}).get("tokens")) or {}
            if not (t.get("input") or t.get("output")):
                continue
            cache = t.get("cache") or {}
            events.append(schema.call(
                seq=seq, input_tokens=t.get("input", 0) or 0,
                output_tokens=t.get("output", 0) or 0, agent=agent,
                cache_read=(cache.get("read", 0) or 0),
                cache_write=(cache.get("write", 0) or 0)))
            tin += t.get("input", 0) or 0
            tout += t.get("output", 0) or 0
            seq += 1
    return events, tin, tout


def convert(stream_events: list[dict], export: dict | None,
            stdout_text: str, child_dir: Path | None = None
            ) -> tuple[list[dict], str]:
    events: list[dict] = []
    calls: dict[str, int] = {}            # per-agent seq counter
    sub_names: list[str] = []
    sub_index: dict[str, str] = {}
    root_session = ""
    tok_in = tok_out = 0
    step_started: dict[str, int] = {}

    for ev in stream_events:
        sid = ev.get("sessionID") or ""
        if not root_session and sid:
            root_session = sid
        part = ev.get("part") or {}
        ptype = part.get("type") or ""
        etype = ev.get("type") or ""

        if ptype == "step-start":
            step_started[sid] = ev.get("timestamp") or 0
            continue

        if "compact" in etype or "compact" in ptype:
            # Unconfirmed against a real compaction (needs a session long
            # enough to trigger one); the name is present in the binary.
            # Recorded rather than silently dropped -- an uncounted
            # compaction is a large call attributed to nothing (§6).
            agent = _agent_for(sid, root_session, sub_names, sub_index)
            events.append(schema.compaction(seq=calls.get(agent, 0)))
            continue

        if ptype == "step-finish":
            agent = _agent_for(sid, root_session, sub_names, sub_index)
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            seq = calls.get(agent, 0)
            calls[agent] = seq + 1
            t0 = step_started.get(sid) or ev.get("timestamp") or 0
            events.append(schema.call(
                seq=seq,
                input_tokens=tok.get("input", 0) or 0,
                output_tokens=tok.get("output", 0) or 0,
                agent=agent,
                cache_read=(cache.get("read", 0) or 0),
                cache_write=(cache.get("write", 0) or 0),
                duration_ms=max(0, (ev.get("timestamp") or 0) - t0),
                stop_reason=part.get("reason", "") or ""))
            tok_in += tok.get("input", 0) or 0
            tok_out += tok.get("output", 0) or 0
            continue

        if ptype == "text" and isinstance(part.get("text"), str):
            events.append(schema.message(part["text"]))
            continue

        if ptype == "tool":
            tool = str(part.get("tool", "")).lower()
            state = part.get("state") or {}
            inp = state.get("input") or {}
            output = state.get("output")
            if tool == "bash" and "command" in inp:
                events.append(schema.command(inp["command"]))
            elif tool in EDIT_TOOLS and ("filePath" in inp or "path" in inp):
                events.append(schema.edit(
                    inp.get("filePath") or inp.get("path"),
                    str(inp.get("content", inp.get("newString", "")))[:4000]))
            elif tool == "task":
                name = inp.get("subagent_type") or inp.get("subagent") or ""
                sub_names.append(name)
                events.append(schema.task(name, str(inp.get("prompt", ""))[:2000]))
            elif tool in PROBE_TOOLS:
                events.append(schema.probe(
                    tool,
                    inp.get("filePath") or inp.get("pattern") or
                    inp.get("path") or "",
                    bytes_returned=len(output) if isinstance(output, str) else 0))
            continue

    # agent_active comes from the export only (see module docstring).
    agents = []
    if export:
        info_agent = (export.get("info") or {}).get("agent")
        if info_agent:
            agents.append(info_agent)
        for msg in export.get("messages", []):
            a = (msg.get("info") or {}).get("agent")
            if a and a not in agents:
                agents.append(a)

    msgs = [e for e in events if e["type"] == schema.MESSAGE]
    final = msgs[-1]["content"] if msgs else stdout_text

    child_events, _ctin, _ctout = _child_calls(child_dir)
    events += child_events

    header = [schema.capability(task_events=bool(stream_events),
                                call_events=any(e["type"] == schema.CALL
                                                for e in events))]
    header += [schema.agent_active(a) for a in agents]

    # The aggregate `usage` event stays ROOT-SESSION ONLY, matching
    # opencode's own info.tokens and every number already in
    # results-clean.jsonl -- changing it would silently redefine the
    # existing scoreboard's column. Subagent cost is not lost: it is in
    # the `call` events, and lib/metrics.py sums those for
    # `tok_in_billed`. Read cost from calls, never from usage.
    if tok_in or tok_out:
        events.append(schema.usage(tok_in, tok_out))

    return header + events, final


def main():
    stream_path, export_path, stdout_path, out_dir = sys.argv[1:5]
    child_dir = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    out_dir = Path(out_dir)

    stream_events = _read_stream(Path(stream_path))
    export = None
    p = Path(export_path)
    if p.exists() and p.stat().st_size > 2:
        try:
            export = json.loads(p.read_text(errors="replace"))
        except json.JSONDecodeError:
            export = None
    stdout_text = ""
    if Path(stdout_path).exists():
        stdout_text = Path(stdout_path).read_text(errors="replace")
    # Under --format json, stdout IS the event stream. Falling back to it
    # as the final message would hand every format grader a wall of JSON
    # and score it as a formatting failure by the agent.
    if stdout_text.lstrip().startswith('{"type"'):
        stdout_text = ""

    events, final = convert(stream_events, export, stdout_text, child_dir)

    errs = schema.validate_transcript(events)
    if errs:
        for e in errs[:10]:
            print(f"stream-adapter: schema: {e}", file=sys.stderr)

    with open(out_dir / "transcript.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (out_dir / "final_message.txt").write_text(final)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
