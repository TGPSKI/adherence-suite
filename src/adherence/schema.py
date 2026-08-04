"""schema — the frozen transcript and result-record schema. Stdlib only.

One source of truth for every key name the suite writes or reads. Adapters
construct events through the constructors here; `runner.py` builds result
records through `result()`; `selftest.py` and `gradelib.py` match on the
type constants here rather than on string literals of their own.

Why this exists (design §3.1, §16.2 B1-B2): the efficiency eval adds
per-inference-call accounting on top of an aggregate `usage` event that
three files already agreed about by coincidence. A key typo'd in one
adapter and not the other is a silent zero in the results, which is
indistinguishable from a real measurement of zero. Validation catches it
at the boundary instead.

Additive by construction: the aggregate `usage` event is retained so the
existing `sNN` scoreboard keeps working; `call` events sit beside it.

Self-check:  python3 lib/schema.py
"""
from __future__ import annotations

import json
import sys

# ---------- event type names ----------

CAPABILITY = "capability"
MESSAGE = "message"
COMMAND = "command"
EDIT = "edit"
TASK = "task"
PROBE = "probe"
AGENT_ACTIVE = "agent_active"
USAGE = "usage"
CALL = "call"
COMPACTION = "compaction"

# The agent name a call is attributed to when the harness reports none.
# Subagent costs separate from parent costs on this field (design §7), so
# "unattributed" must be a value, never a missing key.
ROOT_AGENT = "root"

# ---------- event field declarations ----------
# (required: {name: type}, optional: {name: type})

NUM = (int, float)

_EVENTS: dict[str, tuple[dict, dict]] = {
    CAPABILITY: ({"task_events": bool},
                 {"call_events": bool}),
    MESSAGE: ({"content": str}, {}),
    COMMAND: ({"content": str}, {}),
    EDIT: ({"path": str, "content": str}, {}),
    TASK: ({"subagent": str, "prompt": str}, {}),
    # bytes_returned is what makes exploration cost measurable in bytes
    # rather than in call counts alone (design §3.1).
    PROBE: ({"tool": str, "target": str},
            {"bytes_returned": int}),
    AGENT_ACTIVE: ({"agent": str}, {}),
    # Aggregate, one per run. Retained for backward compatibility.
    USAGE: ({"prompt_tokens": int, "completion_tokens": int},
            {"duration_ms": int}),
    # One per inference call. The measurement (design §3.1, §10).
    CALL: ({"seq": int, "agent": str, "input_tokens": int,
            "output_tokens": int},
           {"cache_read": int, "cache_write": int, "duration_ms": int,
            "stop_reason": str}),
    COMPACTION: ({"seq": int}, {}),
}

EVENT_TYPES = frozenset(_EVENTS)

# ---------- result record field declarations ----------

_RESULT_REQUIRED = {
    "scenario": str, "category": str, "model": str, "adapter": str,
    # arm is required from the schema freeze forward even though the
    # matrix lands in H5: a record that cannot say which instruction
    # surface produced it is not comparable to anything (design §16.2 B2).
    "arm": str, "trial": int, "duration_s": NUM,
    "prompt_tokens": int, "completion_tokens": int,
    "checks": list, "all_pass": bool,
}
_RESULT_OPTIONAL = {
    "sandbox": str, "out_dir": str,
    # "validation" (shaking out the method, the code and the harness) or
    # "experiment" (the registered grid). Dry runs and real runs live in
    # the same directory, look identical, and pool silently -- so the
    # distinction has to be ON the record, not in someone's memory of
    # which file was which.
    "purpose": str,
    # derived cost metrics, computed by metrics.py from call events
    "metrics": dict,
    # Enough to re-run this exact trial without trusting whoever produced
    # it: the argv, the code, the harness, and hashes of the inputs.
    # A result that cannot be replayed is a claim, not a measurement.
    "provenance": dict,
    # explicit, never inferred from the scenario id -- analyze.F5 refuses
    # without it, because one point has no slope
    "fixture": str,
    # Transcript validation failures for this trial. Exclusion criterion 2
    # drops a row whose transcript did not validate, because its cost
    # figures are untrustworthy -- but the errors used to go only to
    # stderr, so the criterion could not be applied to results.jsonl at
    # all. An exclusion rule that cannot read its own evidence is not a
    # rule. Empty list means validated clean.
    "schema_errors": list,
}

# Fields a provenance block must carry for a stranger to reconstruct the
# trial. Checked by validate_result, so a record cannot claim provenance
# it does not have.
_PROVENANCE_REQUIRED = {
    "argv": list,          # the exact runner invocation
    "suite_commit": str,   # code that produced it ("unknown" if not a repo)
    "suite_dirty": bool,   # was the tree clean when it ran
    "harness": str,        # adapter's own version string
    "python": str,
    "scenario_sha": str,   # scenario.yaml + prompt
    "started_at": str,     # UTC ISO-8601
}
_PROVENANCE_OPTIONAL = {
    "arm_sha": str,        # arm manifest + files
    "base_commit": str,    # fixture checkout
    "seed": int,
}

CHECK_STATUSES = ("pass", "fail", "ungradeable")
_CHECK_REQUIRED = {"name": str, "status": str, "evidence": str}


# ---------- constructors ----------

def capability(task_events: bool, call_events: bool | None = None) -> dict:
    e = {"type": CAPABILITY, "task_events": bool(task_events)}
    if call_events is not None:
        e["call_events"] = bool(call_events)
    return e


def message(content: str) -> dict:
    return {"type": MESSAGE, "content": content}


def command(content: str) -> dict:
    return {"type": COMMAND, "content": content}


def edit(path: str, content: str) -> dict:
    return {"type": EDIT, "path": path, "content": content}


def task(subagent: str, prompt: str = "") -> dict:
    return {"type": TASK, "subagent": subagent, "prompt": prompt}


def probe(tool: str, target: str, bytes_returned: int | None = None) -> dict:
    e = {"type": PROBE, "tool": tool, "target": target}
    if bytes_returned is not None:
        e["bytes_returned"] = int(bytes_returned)
    return e


def agent_active(agent: str) -> dict:
    return {"type": AGENT_ACTIVE, "agent": agent}


def usage(prompt_tokens: int, completion_tokens: int,
          duration_ms: int = 0) -> dict:
    return {"type": USAGE, "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "duration_ms": int(duration_ms)}


def call(seq: int, input_tokens: int, output_tokens: int,
         agent: str = ROOT_AGENT, cache_read: int = 0, cache_write: int = 0,
         duration_ms: int = 0, stop_reason: str = "") -> dict:
    return {"type": CALL, "seq": int(seq), "agent": agent or ROOT_AGENT,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read": int(cache_read), "cache_write": int(cache_write),
            "duration_ms": int(duration_ms), "stop_reason": stop_reason}


def compaction(seq: int) -> dict:
    return {"type": COMPACTION, "seq": int(seq)}


def result(scenario: str, category: str, model: str, adapter: str, arm: str,
           trial: int, duration_s: float, prompt_tokens: int,
           completion_tokens: int, checks: list, all_pass: bool,
           sandbox: str = "", out_dir: str = "",
           metrics: dict | None = None, provenance: dict | None = None,
           fixture: str = "", purpose: str = "validation",
           schema_errors: list | None = None) -> dict:
    r = {
        "scenario": scenario, "category": category, "model": model,
        "adapter": adapter, "arm": arm, "trial": int(trial),
        "duration_s": duration_s, "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "checks": checks, "all_pass": bool(all_pass),
        "sandbox": sandbox, "out_dir": out_dir,
    }
    if metrics is not None:
        r["metrics"] = metrics
    if provenance is not None:
        r["provenance"] = provenance
    if fixture:
        r["fixture"] = fixture
    r["purpose"] = purpose
    # Always present, so "no schema_errors key" means an old record rather
    # than a clean one -- exclusion criterion 2 must be able to tell those
    # apart before it trusts a row.
    r["schema_errors"] = list(schema_errors or [])
    return r


# ---------- validation ----------

def _tname(t) -> str:
    return "/".join(x.__name__ for x in t) if isinstance(t, tuple) else t.__name__


def _check_fields(obj: dict, required: dict, optional: dict, where: str,
                  errs: list) -> None:
    for k, typ in required.items():
        if k not in obj:
            errs.append(f"{where}: missing required field {k!r}")
        elif not isinstance(obj[k], typ) or (typ is int and isinstance(obj[k], bool)):
            errs.append(f"{where}: field {k!r} is {type(obj[k]).__name__}, "
                        f"expected {_tname(typ)}")
    for k, v in obj.items():
        if k in required or k == "type":
            continue
        if k not in optional:
            errs.append(f"{where}: unknown field {k!r}")
        elif not isinstance(v, optional[k]) or (optional[k] is int and isinstance(v, bool)):
            errs.append(f"{where}: field {k!r} is {type(v).__name__}, "
                        f"expected {_tname(optional[k])}")


def validate_event(e: dict, where: str = "event") -> list[str]:
    if not isinstance(e, dict):
        return [f"{where}: not an object"]
    t = e.get("type")
    if t not in _EVENTS:
        return [f"{where}: unknown event type {t!r}"]
    errs: list[str] = []
    req, opt = _EVENTS[t]
    _check_fields(e, req, opt, f"{where}[{t}]", errs)
    return errs


def _seq_break(ss: list) -> int | None:
    """Index of the first call.seq that is neither `previous + 1` nor a
    restart at 0. None if the run is well-formed.

    The invariant exists so cost metrics can be trusted: a *gap* means a
    call was dropped from the stream and its tokens are missing from every
    total. What it must not do is reject a correct transcript. Agents are
    keyed by name, and the same subagent name can be spawned more than once
    in a session -- a second `explore` opens its own 0-based run, so the
    concatenation reads [0,1,2,3,0,1]. Requiring one flat 0..n-1 run per
    name called that a schema violation, which failed the whole trial as if
    the model had erred. Restarts are legitimate and accepted; gaps (0,1,3)
    and stalled repeats (0,1,1) are still caught, which is the dropped-call
    case the invariant was written for.
    """
    prev = None
    for i, s in enumerate(ss):
        if not isinstance(s, int):
            return i
        if prev is None:
            if s != 0:
                return i
        elif s != prev + 1 and s != 0:
            return i
        prev = s
    return None


def validate_transcript(events: list) -> list[str]:
    """Per-event field validation plus the cross-event invariants that
    make the cost metrics meaningful: call sequence numbers advance without
    gaps per agent, and a transcript claiming call_events actually carries
    some."""
    errs: list[str] = []
    for i, e in enumerate(events):
        errs += validate_event(e, f"event[{i}]")

    seqs: dict[str, list[int]] = {}
    for e in events:
        if isinstance(e, dict) and e.get("type") == CALL:
            seqs.setdefault(e.get("agent", ROOT_AGENT), []).append(e.get("seq"))
    for agent, ss in seqs.items():
        bad = _seq_break(ss)
        if bad is not None:
            errs.append(f"call.seq for agent {agent!r} is {ss}, "
                        f"breaks at index {bad} (each call must be the "
                        f"previous +1, or 0 to open a new spawn)")

    cap = next((e for e in events
                if isinstance(e, dict) and e.get("type") == CAPABILITY), None)
    if cap and cap.get("call_events") and not seqs:
        errs.append("capability.call_events is true but no call events present")
    return errs


def validate_result(r: dict) -> list[str]:
    if not isinstance(r, dict):
        return ["result: not an object"]
    errs: list[str] = []
    _check_fields(r, _RESULT_REQUIRED, _RESULT_OPTIONAL, "result", errs)
    prov = r.get("provenance")
    if isinstance(prov, dict):
        _check_fields(prov, _PROVENANCE_REQUIRED, _PROVENANCE_OPTIONAL,
                      "result.provenance", errs)
    for i, c in enumerate(r.get("checks") or []):
        if not isinstance(c, dict):
            errs.append(f"result.checks[{i}]: not an object")
            continue
        _check_fields(c, _CHECK_REQUIRED, {}, f"result.checks[{i}]", errs)
        if c.get("status") not in CHECK_STATUSES:
            errs.append(f"result.checks[{i}]: status {c.get('status')!r} "
                        f"not in {CHECK_STATUSES}")
    return errs


# ---------- goldens ----------
# A two-round session: the agent reads a file, runs a command, edits, and
# answers. Two inference calls, so `calls` and `tok_in_billed` are both
# non-degenerate. Hand-checked; the numbers are the s05 floor (design
# §0.2) plus a plausible second round.

GOLDEN_TRANSCRIPT = [
    capability(task_events=True, call_events=True),
    agent_active("build"),
    call(seq=0, input_tokens=17046, output_tokens=131, agent=ROOT_AGENT,
         duration_ms=3312, stop_reason="tool_use"),
    probe("read", "mathlib.py", bytes_returned=182),
    command("python3 -m pytest -q test_mathlib.py"),
    call(seq=1, input_tokens=19004, output_tokens=88, agent=ROOT_AGENT,
         cache_read=17046, duration_ms=2140, stop_reason="end_turn"),
    edit("mathlib.py", "def clamp(v, lo, hi):\n    ...\n"),
    message("fixed the upper bound in clamp"),
    usage(prompt_tokens=36050, completion_tokens=219, duration_ms=5452),
]

GOLDEN_RESULT = result(
    scenario="s12", category="verification", model="qwen36-35b-a3b-nvfp4",
    adapter="opencode.sh", arm="a3", trial=0, duration_s=5.5,
    prompt_tokens=36050, completion_tokens=219,
    checks=[{"name": "scope", "status": "pass",
             "evidence": "changed=['mathlib.py'] within allowed=['mathlib.py']"},
            {"name": "task", "status": "ungradeable",
             "evidence": "adapter emits no task events"}],
    all_pass=True, metrics={"calls": 2, "tok_in_billed": 36050},
    provenance={"argv": ["runner", "--only", "s12", "--arm", "a3"],
                "suite_commit": "42eb1f1", "suite_dirty": False,
                "harness": "opencode 1.18.10", "python": "3.14.6",
                "scenario_sha": "9f2c1a4b", "arm_sha": "1de4c007",
                "started_at": "2026-08-04T05:00:00Z"})


def _selfcheck() -> int:
    failures = 0

    def expect(label, errs, want_ok):
        nonlocal failures
        ok = not errs
        if ok != want_ok:
            failures += 1
            print(f"BAD {label}: expected {'valid' if want_ok else 'rejection'}, "
                  f"got {errs or 'valid'}")
        else:
            print(f"OK  {label}")

    expect("golden transcript valid", validate_transcript(GOLDEN_TRANSCRIPT), True)
    expect("golden result valid", validate_result(GOLDEN_RESULT), True)

    # Both directions: the validator must reject, or it validates nothing.
    bad_typo = [dict(GOLDEN_TRANSCRIPT[2], **{"input_tokens": None})]
    expect("rejects null token count", validate_transcript(bad_typo), False)

    renamed = {k: v for k, v in GOLDEN_TRANSCRIPT[2].items() if k != "input_tokens"}
    renamed["prompt_tokens"] = 17046          # the exact typo this guards
    expect("rejects renamed token field", validate_transcript([renamed]), False)

    gap = [call(seq=0, input_tokens=1, output_tokens=1),
           call(seq=2, input_tokens=1, output_tokens=1)]
    expect("rejects seq gap", validate_transcript(gap), False)

    # The same subagent name spawned twice opens a second 0-based run. A
    # correct transcript; rejecting it failed the trial as a model error.
    respawn = [call(seq=n, input_tokens=1, output_tokens=1, agent="explore")
               for n in (0, 1, 2, 3, 0, 1)]
    expect("accepts subagent respawn", validate_transcript(respawn), True)

    # ...but a stalled counter is still a dropped call, not a respawn.
    stalled = [call(seq=n, input_tokens=1, output_tokens=1, agent="explore")
               for n in (0, 1, 1)]
    expect("rejects stalled seq", validate_transcript(stalled), False)

    # A respawn resets to 0; resuming mid-run is a gap by another name.
    resumed = [call(seq=n, input_tokens=1, output_tokens=1, agent="explore")
               for n in (0, 1, 2, 1)]
    expect("rejects mid-run resume", validate_transcript(resumed), False)

    claims = [capability(True, call_events=True)]
    expect("rejects empty call_events claim", validate_transcript(claims), False)

    no_arm = {k: v for k, v in GOLDEN_RESULT.items() if k != "arm"}
    expect("rejects result without arm", validate_result(no_arm), False)

    bad_status = json.loads(json.dumps(GOLDEN_RESULT))
    bad_status["checks"][0]["status"] = "passed"
    expect("rejects bogus check status", validate_result(bad_status), False)

    print(f"\nschema selfcheck: {'OK' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_selfcheck())
