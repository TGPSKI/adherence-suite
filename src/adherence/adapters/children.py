#!/usr/bin/env python3
"""Print the session ids dispatched as subagents by a root session.

    opencode_children.py <root-session-id>   -> one id per line on stdout

Why this exists: a subagent runs in its OWN opencode session. Neither
`opencode run --format json` (root stream) nor `opencode export <root>`
shows a single one of its inference calls. Measured on s13 at opencode
1.18.10: the root export reports 10 calls / 98,962 input tokens while the
recording proxy counted 29 inference calls for the same trial — the
missing 19 are four dispatched subagents.

That gap is not cosmetic. E3 is the claim that subagent handoff is
"0-cost", and §7 names parent+all-children `total_tokens` as **the only
number that can be quoted as a saving**. Reading subagent cost off the
root session would confirm E3 by construction, because the child's tokens
are not in the number being read.

There is no CLI for this: `opencode session list` filters to the current
project and exposes no parent/child flag. So this reads opencode's own
sqlite store, which is private and may move between versions. It is
deliberately best-effort — on any failure it prints nothing and the
caller records root-session cost only, with the proxy still holding the
authoritative total (H4).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path


def db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
    return Path(base) / "opencode" / "opencode.db"


def children(root: str, attempts: int = 5, delay: float = 0.6
             ) -> list[tuple[str, str]]:
    """[(session_id, agent_name)] for every descendant of `root`, in
    creation order. Recursive: a subagent may dispatch its own."""
    # Retried, because this runs the instant `opencode run` returns and a
    # child session written moments earlier may not be visible yet. A
    # single miss is not a small error: measured against the recording
    # proxy, a trial reporting 26 calls had made 66, and two whole
    # subagent sessions -- 40 calls, ~1.5M input tokens -- were absent
    # from the transcript while the proxy had them all. E3 is a claim
    # about exactly those tokens.
    for attempt in range(attempts):
        found = _query(root)
        if found or attempt == attempts - 1:
            return found
        time.sleep(delay)
    return []


def _query(root: str) -> list[tuple[str, str]]:
    p = db_path()
    if not p.exists():
        return []
    try:
        # read-only, and never block on opencode's own writer
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA query_only=ON")
        rows = list(con.execute(
            "SELECT id, parent_id, COALESCE(agent,''), time_created "
            "FROM session WHERE parent_id IS NOT NULL "
            "ORDER BY time_created ASC"))
        con.close()
    except Exception:
        return []

    by_parent: dict[str, list] = {}
    for sid, parent, agent, _t in rows:
        by_parent.setdefault(parent, []).append((sid, agent))

    out, stack = [], [root]
    seen = {root}
    while stack:
        cur = stack.pop(0)
        for sid, agent in by_parent.get(cur, []):
            if sid in seen:
                continue
            seen.add(sid)
            out.append((sid, agent))
            stack.append(sid)
    return out


def main():
    if len(sys.argv) < 2 or not sys.argv[1]:
        return 0
    for sid, agent in children(sys.argv[1]):
        print(f"{sid}\t{agent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
