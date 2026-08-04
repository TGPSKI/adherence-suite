"""metrics — derived cost metrics from a normalized transcript.

Design §10 and §16.3 C3: efficiency metrics do not belong in per-scenario
`grade.py`. Graders answer "did it do the job"; this answers "what did
that cost". Both read the same transcript and neither may depend on the
other. Stdlib only, pure functions, no I/O — so `selftest.py` can check
every one of them against a scripted transcript with no model in the
loop (H13).

Read cost from `call` events, never from the aggregate `usage` event:
`usage` mirrors opencode's own session total, which is root-session only
and excludes every dispatched subagent. Measured on s13: usage reports
98,962 input tokens where the calls sum to 307,495 across six agents.
"""
from __future__ import annotations

from adherence import schema

# Metered cache economics (design §10). Applied to cache_read/cache_write
# when they exist. On the local vLLM endpoint they are always 0 and
# `prompt_tokens_details` comes back null, so `tok_effective` degenerates
# to `tok_in_billed` here -- §16.1: E5 is not testable locally, and this
# number must not be presented as a caching result on local runs.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def calls(transcript, agent=None) -> list[dict]:
    out = [e for e in transcript if e.get("type") == schema.CALL]
    if agent is not None:
        out = [e for e in out if e.get("agent") == agent]
    return out


def root_calls(transcript) -> list[dict]:
    return calls(transcript, schema.ROOT_AGENT)


def subagent_calls(transcript) -> list[dict]:
    return [e for e in calls(transcript) if e.get("agent") != schema.ROOT_AGENT]


def probes_to_first_edit(transcript) -> int:
    """Read/glob/grep calls before the first edit.

    Read this as **locating latency** -- how long until the agent knew
    where to work -- which is what a router claims to shorten. Do not read
    it as total exploration cost, for two reasons:

    - It is gameable. An agent that edits first and investigates afterwards
      scores 0 and looks maximally efficient while being reckless. Always
      read it beside `probes_total`.
    - It covers only the first segment. Real work is explore, edit,
      explore, edit; everything after the first edit is invisible here and
      lives in `probes_after_first_edit`.

    A run that never edits scores all of its probes, because 'explored and
    gave up' is the behaviour this should expose, not a missing value."""
    n = 0
    for e in transcript:
        t = e.get("type")
        if t == schema.EDIT:
            return n
        if t == schema.PROBE:
            n += 1
    return n


def probe_trail(transcript, limit: int = 40) -> list[str]:
    """The targets probed before the first edit, in order.

    `probes_to_first_edit` counts exploration; this records where it went.
    Without it, route correctness -- a registered metric (docs/EVAL.md
    §Fixtures) -- is not answerable from a results file at all, and every
    routing claim would rest on a number with no evidence behind it.

    Truncated, because the point is the route taken, not a full log."""
    out = []
    for e in transcript:
        if e.get("type") == schema.EDIT:
            break
        if e.get("type") == schema.PROBE:
            t = e.get("target") or ""
            if t:
                out.append(t)
    return out[:limit]


def first_edit(transcript) -> str:
    for e in transcript:
        if e.get("type") == schema.EDIT:
            return e.get("path") or ""
    return ""


def edited_paths(transcript) -> list[str]:
    seen = []
    for e in transcript:
        if e.get("type") == schema.EDIT:
            p = e.get("path") or ""
            if p and p not in seen:
                seen.append(p)
    return seen


def handoff_construction_tokens(transcript) -> int:
    """Output tokens the parent spent on the call that emitted a dispatch.

    §7 names this as the thing a context *reference* is supposed to make
    near-zero, versus summarising the context into the subagent's prompt.
    Attributed to the most recent root call preceding each task event."""
    total, last_root_out = 0, 0
    for e in transcript:
        if e.get("type") == schema.CALL and e.get("agent") == schema.ROOT_AGENT:
            last_root_out = e.get("output_tokens", 0)
        elif e.get("type") == schema.TASK:
            total += last_root_out
    return total


def redundant_reads(transcript) -> int:
    """Re-probes of a target that has not changed since it was last probed.

    "Redundant" is a strong word and the naive version did not earn it: it
    counted every repeat of a (tool, target) pair, which scores as waste
    the single most correct thing an agent does -- re-reading a file it
    just edited to confirm the edit landed. That inverts the metric on
    exactly the careful behaviour it should reward.

    So an edit to a target clears it: the next probe of that target is a
    fresh read of changed content, not a repeat.

    What it still cannot see, and what therefore keeps it a heuristic:
    context compaction. A file re-read forty turns later because the
    harness dropped it from the window is not the agent being wasteful,
    and nothing in the transcript distinguishes that from carelessness
    until `compaction` events are actually observed."""
    seen, n = set(), 0
    for e in transcript:
        t = e.get("type")
        if t == schema.EDIT:
            path = e.get("path") or ""
            seen = {k for k in seen if k[1] != path}
            continue
        if t != schema.PROBE:
            continue
        key = (e.get("tool"), e.get("target"))
        if key in seen:
            n += 1
        else:
            seen.add(key)
    return n


def probes_total(transcript) -> int:
    return sum(1 for e in transcript if e.get("type") == schema.PROBE)


def probes_after_first_edit(transcript) -> int:
    return probes_total(transcript) - probes_to_first_edit(transcript)


def probe_bytes(transcript, before_first_edit: bool = False) -> int:
    """Bytes exploration pulled into the context.

    This is the number E2 is actually about. "Exploration reads are
    burning tokens" is a claim about volume, and a probe *count* does not
    measure volume: three reads returning 200 KB cost far more than ten
    returning 2 KB. Count answers "how many steps"; bytes answers "how
    much did it cost", and only the second is the claim."""
    total = 0
    for e in transcript:
        if before_first_edit and e.get("type") == schema.EDIT:
            break
        if e.get("type") == schema.PROBE:
            total += e.get("bytes_returned", 0) or 0
    return total


def tool_calls(transcript) -> int:
    return sum(1 for e in transcript
               if e.get("type") in (schema.COMMAND, schema.EDIT,
                                    schema.TASK, schema.PROBE))


def abandoned(transcript, expects_edit: bool = True,
              completed: bool = True, observed_edits: int = -1) -> bool:
    """Design §5 control 3: flag trials that terminate with fewer than 2
    tool calls, or with no edit where an edit was the job. An arm whose
    token advantage comes from giving up sooner has to be caught here,
    because in the aggregate it looks exactly like efficiency.

    `expects_edit` is per scenario. Several scenarios are correctly
    answered by a report and nothing else — s04's right answer is to STOP
    and edit nothing at all. Flagging those as abandoned would put a red
    column on compliant behaviour, and a flag that cries wolf is a flag
    nobody reads by the time the PR-derived tasks arrive.

    `completed=False` means the harness stopped the trial -- a deadline, a
    crashed adapter -- and the flag is withheld. Giving up and being cut
    off look identical in a transcript that ends early, and conflating
    them puts a give-up flag on the harness's own decision. Observed on a
    45-minute trial killed at its ceiling with 27 MB of events: recorded
    as abandoned, which is the opposite of what happened."""
    if not completed:
        return False
    if tool_calls(transcript) < 2:
        return True
    if not expects_edit:
        return False
    # `observed_edits` is git's answer, and git outranks the transcript
    # here. EDIT events are emitted only for tools that name a file in
    # their input; an agent that writes through the shell -- a heredoc,
    # `sed -i` -- changes files that git sees and the transcript does not.
    # Measured: a trial with 26 calls, 35 tool calls and every check
    # passing was flagged as having given up, because it did its editing
    # in bash. -1 means nobody looked, so fall back to the transcript.
    if observed_edits >= 0:
        return observed_edits == 0
    return not any(e.get("type") == schema.EDIT for e in transcript)


def turns_until_first_compaction(transcript) -> int | None:
    """Calls made before the harness first auto-compacted, or None if it
    never did. §6's counter-prediction: monolith sessions carry a larger
    baseline and should hit compaction sooner, which is a directed-
    contexts advantage invisible in single-task protocols."""
    n = 0
    for e in transcript:
        if e.get("type") == schema.CALL:
            n += 1
        elif e.get("type") == schema.COMPACTION:
            return n
    return None


def compute(transcript, floor: int = 0, duration_s: float | None = None,
            expects_edit: bool = True, completed: bool = True,
            observed_edits: int = -1) -> dict:
    """All per-run cost metrics.

    `floor` is the per-arm harness floor from E5's calibration: the input
    tokens on call 1 of a no-op prompt in this arm (system prompt + tool
    schemas + injected instruction surface). `tok_in_marginal` subtracts
    floor × calls, which is the only form in which a cross-arm ratio is
    interpretable — §0.2. With floor=0 the marginal figure equals the
    billed figure and must be labelled as uncalibrated, not reported as
    marginal.
    """
    cs = calls(transcript)
    subs = subagent_calls(transcript)
    tok_in = sum(c.get("input_tokens", 0) for c in cs)
    tok_out = sum(c.get("output_tokens", 0) for c in cs)
    c_read = sum(c.get("cache_read", 0) for c in cs)
    c_write = sum(c.get("cache_write", 0) for c in cs)
    uncached = tok_in - c_read - c_write

    agents = sorted({c.get("agent", schema.ROOT_AGENT) for c in cs})
    per_agent = {a: {"calls": len(calls(transcript, a)),
                     "tok_in": sum(c.get("input_tokens", 0)
                                   for c in calls(transcript, a)),
                     "tok_out": sum(c.get("output_tokens", 0)
                                    for c in calls(transcript, a))}
                 for a in agents}

    m = {
        # Call 1's input is system prompt + tool schemas + the arm's
        # instruction surface + the task prompt. The task prompt is
        # identical across arms for a given scenario, so the difference
        # between arms here IS the arm surface -- the per-arm floor (E5),
        # measured under real conditions instead of from a separate no-op
        # run that would not carry the same tool schemas.
        "first_call_input": (cs[0].get("input_tokens", 0) if cs else 0),
        "calls": len(cs),
        "tok_in_billed": tok_in,
        "tok_in_marginal": tok_in - floor * len(cs),
        "tok_effective": round(uncached + CACHE_WRITE_MULT * c_write
                               + CACHE_READ_MULT * c_read, 1),
        "tok_out": tok_out,
        "cache_read": c_read,
        "cache_write": c_write,
        "floor_used": floor,
        "tool_calls": tool_calls(transcript),
        "probes_to_first_edit": probes_to_first_edit(transcript),
        "probes_total": probes_total(transcript),
        "probes_after_first_edit": probes_after_first_edit(transcript),
        "probe_bytes": probe_bytes(transcript),
        "probe_bytes_to_first_edit": probe_bytes(transcript, True),
        "redundant_reads": redundant_reads(transcript),
        "compactions": sum(1 for e in transcript
                           if e.get("type") == schema.COMPACTION),
        "turns_until_first_compaction": turns_until_first_compaction(transcript),
        "abandoned": abandoned(transcript, expects_edit, completed,
                               observed_edits),
        # What git saw, beside what the transcript claims. A gap between
        # them means the agent edited through a tool the stream does not
        # mark as an edit, which silently empties first_edit, probe_trail
        # and every routing metric built on them.
        "observed_edits": observed_edits,
        # §7: total_tokens is the only figure quotable as a saving;
        # per-agent numbers are diagnostics. Both are reported, and the
        # total leads.
        # Evidence for route correctness, not just its count. A number
        # without the trail behind it cannot be audited by anyone who did
        # not run it.
        "probe_trail": probe_trail(transcript),
        "first_edit": first_edit(transcript),
        "edited_paths": edited_paths(transcript),
        "handoff_construction_tokens": handoff_construction_tokens(transcript),
        "n_subagents": len({c.get("agent") for c in subs}),
        "subagent_calls": len(subs),
        "subagent_tok_in": sum(c.get("input_tokens", 0) for c in subs),
        "per_agent": per_agent,
    }
    if duration_s is not None:
        m["wall_clock_s"] = duration_s
    return m


# ---------- proxy-side accounting (design §3.2) ----------

def is_auxiliary(proxy_row: dict) -> bool:
    """Whether a proxy-observed call is harness overhead rather than task
    work.

    opencode makes a session-title-generation call that carries no tool
    schemas and does not participate in the agent loop. Measured: 2 such
    calls per s13 trial, ~575 input tokens each, and they are not
    emitted at all on some runs. They are real spend, so they are
    recorded — but they are not attributable to the instruction surface
    under test, so they are excluded from arm comparisons and reported
    separately. Stating the rule beats quietly picking one total."""
    return bool(proxy_row.get("inference")) and not proxy_row.get("n_tools")


def proxy_totals(rows: list[dict]) -> dict:
    """Split proxy call records into task work and harness overhead."""
    inf = [r for r in rows if r.get("type") == "call" and r.get("inference")]
    aux = [r for r in inf if is_auxiliary(r)]
    task = [r for r in inf if not is_auxiliary(r)]
    return {
        "calls": len(task),
        "tok_in_billed": sum(r.get("input_tokens", 0) for r in task),
        "tok_out": sum(r.get("output_tokens", 0) for r in task),
        "aux_calls": len(aux),
        "aux_tok_in": sum(r.get("input_tokens", 0) for r in aux),
        "usage_missing": sum(1 for r in inf if r.get("usage_missing")),
    }


def split_by_mark(rows: list[dict]) -> dict:
    """Group proxy records by trial.

    Two mechanisms, and the per-record one wins wherever it is present:

    `run_id` is carried on the request path, so it is correct no matter how
    many trials are in flight. `mark` is a single piece of proxy state set
    between trials -- fine serially, meaningless under --jobs>1, where two
    trials interleave into whichever label was set last.

    Records with neither land under ''."""
    out, cur = {}, ""
    for r in rows:
        if r.get("type") == "mark":
            cur = r.get("label", "")
            out.setdefault(cur, [])
            continue
        key = trial_key(r.get("run_id") or r.get("mark") or cur)
        out.setdefault(key, []).append(r)
    return out


def trial_key(run_id: str) -> str:
    """`scenario|arm|trial`, the unit a result row is joined on.

    A run id carries a fourth field so two out-dirs for the same trial
    (a retry, a re-run) stay distinct as processes. Calibration joins on
    the trial, so the suffix is dropped here rather than at every call
    site that would otherwise have to know about it."""
    parts = (run_id or "").split("|")
    return "|".join(parts[:3]) if len(parts) >= 3 else (run_id or "")
