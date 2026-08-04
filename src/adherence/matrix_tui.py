#!/usr/bin/env python3
"""Interactive results matrix for the adherence suite.

  python3 tools/suite-tui.py                # all cells, opens on the picker
  python3 tools/suite-tui.py 'a3/*'         # start filtered
  make matrix                               # same
  make watch                                # live, refreshes as results land

Views ([tab] cycles):
  cells        every (arm, scenario) cell, trials aggregated — [space] detail
  arms         per-arm rollup + paired geometric-mean ratios vs the reference
  cost         pass rate vs input tokens; ★ marks the Pareto frontier
  calib        adapter vs recording proxy per run — the H4 gate, live

Filter grammar (shared with table.py, see suitedata.matches): bare prefix
('a3'), glob ('a*/s1?'), substring, comma-OR, '!' negates.

Adapted from leather's sig-triage matrix TUI; all data flows through
tools/suitedata.py so this and `make table` cannot disagree about a
number. Stdlib only; curses primitives vendored under tools/tui/.
"""
from __future__ import annotations

import os
import sys
import time

from adherence import live as lv
from adherence import suitedata as sd
from adherence.tui.framework import TuiApp, curses_main

VIEWS = ("live", "cells", "arms", "cost", "calib")
VIEW_HELP = {
    "live": "runs in flight, read off their streams — [space] opens detail",
    "cells": "every (arm, scenario) cell — [space] opens detail",
    "arms": "per-arm rollup; ratios are paired on scenario, geometric",
    "cost": "pass rate vs tokens — ★ is the Pareto frontier (§5)",
    "calib": "adapter vs proxy per run; >2% fails the H4 gate (§3.2)",
}
SORTS = ("tag", "pass", "tok")
GATE = 0.02          # H4 tolerance


class SuiteTui(TuiApp):
    def __init__(self, stdscr, pattern="", ref="a1", proxy=None,
                 files=None, expect=0):
        super().__init__(stdscr)
        # Scope to one run. Without this the loader globs every *.jsonl in
        # runs/, so a live probe renders pooled with every smoke test and
        # calibration run that ever landed there -- and the numbers on
        # screen belong to no single experiment.
        self.files = files or None
        self.expect = expect     # runs this batch will produce, for progress
        self.pattern = pattern
        self.ref = ref
        self.proxy_path = proxy
        self.view = 0
        self.sort = 0
        self.editing = False
        self.buf = ""
        self.cursor = 0
        self.detail = False
        self.cells = []
        self.rows = []
        self.live = []
        self.live_cursor = 0
        self.live_sel = ""   # out_dir the cursor is anchored to
        self.proxy_rows = []
        self.stamp = 0.0
        self.seen_mtime = 0.0
        self.sel = {f: set() for f in sd.FACETS}
        # The picker narrows an existing dataset. With nothing loaded yet --
        # a run that just started -- it is an empty box over the one thing
        # worth seeing, which is the progress bar.
        self.reload_live()
        # Open on whatever there is to see: if something is running, that is
        # the news; otherwise the results are.
        self.view = 0 if self.live else VIEWS.index("cells")
        self.picking = not pattern and bool(self.cells) and not self.live
        self.pick_i = 0
        self.reload()
        self.curses.halfdelay(20)     # 2s tick, so a running battery shows up

    # ---- data ------------------------------------------------------------

    def reload(self):
        self.cells = sd.load_cells(paths=self.files)
        self.rows = sd.load_rows(self.files)
        self.proxy_rows = (sd.load_proxy(self.proxy_path)
                           if self.proxy_path else [])
        self.stamp = time.time()
        self.seen_mtime = sd.newest_mtime(self.files)

    def reload_live(self):
        """Separate from reload(): in-flight state changes every second and
        has no mtime to gate on, while re-parsing every results file on a 2s
        tick would be wasteful. Failures are swallowed -- a viewer must not
        die because a run cleaned up its directory mid-read."""
        try:
            self.live = lv.snapshot()
        except Exception:
            self.live = []
        # Re-anchor to the run the cursor was on. Trials finish and new ones
        # start on every tick, so a positional cursor points at a different
        # trial each time the list changes length -- worst in the detail
        # pane, which would swap out from under you mid-read. out_dir is
        # unique and outlives the run, so it is the identity to hold.
        if self.live_sel:
            idx = next((i for i, r in enumerate(self.live)
                        if r["out_dir"] == self.live_sel), None)
            if idx is not None:
                self.live_cursor = idx
            elif self.live:
                # The selected run ended. Stay in range and re-anchor
                # rather than silently tracking whatever slid into its slot.
                self.live_cursor = min(self.live_cursor, len(self.live) - 1)
                self.live_sel = self.live[self.live_cursor]["out_dir"]
        if not self.live_sel and self.live:
            self.live_sel = self.live[min(self.live_cursor,
                                          len(self.live) - 1)]["out_dir"]

    def _anchor_live(self):
        """Pin the selection to a run, not to a row number."""
        if self.live:
            self.live_cursor = max(0, min(self.live_cursor,
                                          len(self.live) - 1))
            self.live_sel = self.live[self.live_cursor]["out_dir"]

    def visible(self):
        cs = [c for c in self.cells if sd.matches(c["tag"], self.pattern)]
        for f, want in self.sel.items():
            if want:
                cs = [c for c in cs if c[f] in want]
        if SORTS[self.sort] == "pass":
            cs = sorted(cs, key=lambda c: (-c["pass_rate"], c["tag"]))
        elif SORTS[self.sort] == "tok":
            cs = sorted(cs, key=lambda c: (-c["tok"], c["tag"]))
        else:
            cs = sorted(cs, key=lambda c: c["tag"])
        return cs

    # ---- chrome ----------------------------------------------------------

    def pass_attr(self, p):
        C = self.curses
        return (C.color_pair(1) if p >= 80 else
                C.color_pair(3) if p >= 25 else C.color_pair(4))

    def header(self, max_x):
        C = self.curses
        v = VIEWS[self.view]
        self._put(0, 1, "adherence-suite", C.A_BOLD)
        x = 17
        for i, name in enumerate(VIEWS):
            attr = C.color_pair(5) if i == self.view else C.A_DIM
            self._put(0, x, f" {name} ", attr)
            x += len(name) + 3
        filt = self.buf if self.editing else (self.pattern or "all")
        tail = f"filter:{filt}  sort:{SORTS[self.sort]}  ref:{self.ref}"
        self._put(0, max(x + 2, max_x - len(tail) - 2), tail,
                  C.A_BOLD if self.editing else C.A_DIM)
        self._put(1, 1, VIEW_HELP[v], C.A_DIM)

        # Live progress. `runs` is what has actually landed on disk, which
        # is the only thing a viewer can honestly claim: a run in flight has
        # not been written yet and must not be counted as done.
        n = len(self.rows)
        if self.expect:
            pct = 100.0 * n / self.expect
            bar_w = 18
            fill = int(bar_w * min(n / self.expect, 1.0))
            prog = (f"{'#' * fill}{'.' * (bar_w - fill)} "
                    f"{n}/{self.expect} {pct:.0f}%")
            done = n >= self.expect
            self._put(1, max(len(VIEW_HELP[v]) + 3, max_x - len(prog) - 2),
                      prog, C.color_pair(2 if done else 5))
        else:
            tail = f"{n} run(s)" + (f" from {len(self.files)} file(s)"
                                    if self.files else "")
            self._put(1, max(len(VIEW_HELP[v]) + 3, max_x - len(tail) - 2),
                      tail, C.A_DIM)

    def footer(self, max_y, max_x, total, avail):
        C = self.curses
        n_live = sum(1 for r in self.live if r["state"] == "running")
        keys = [("[tab] view", C.A_DIM),
                (f"[L] live·{n_live}", C.color_pair(2) if n_live else C.A_DIM),
                ("[s] sort", C.A_DIM),
                ("[/] filter", C.A_DIM), ("[F] clear", C.A_DIM),
                ("[p] pick", C.A_DIM), ("[space] detail", C.A_DIM),
                ("[r] reload", C.A_DIM), ("[q] quit", C.A_DIM)]
        if self.editing:
            keys = [("type a filter · [enter] apply · [esc] cancel", C.A_BOLD)]
        self.render_footer_items(max_y, keys)
        if total > avail > 0:
            end = min(self.scroll + avail, total)
            self._put(max_y - 1, max_x - 14, f"{self.scroll+1}-{end}/{total}",
                      C.A_DIM)

    # ---- views -----------------------------------------------------------

    def render(self, max_y, max_x):
        self.header(max_x)
        body = max_y - 3
        if self.picking:
            total, avail = self.view_picker(body, max_x)
            self.footer(max_y, max_x, total, avail)
            return
        cs = self.visible()
        total = avail = 0
        v = VIEWS[self.view]
        if v == "live":
            # Deliberately not filtered by `pattern`: that filter is a
            # results-side idea (arm/scenario cells), and silently hiding a
            # running job because of it would be the worst possible
            # behaviour for a monitor.
            if self.detail and self.live:
                self.live_cursor = max(0, min(self.live_cursor,
                                              len(self.live) - 1))
                self.view_live_detail(self.live[self.live_cursor],
                                      max_y, max_x)
            else:
                total, avail = self.view_live(body, max_x)
            self.footer(max_y, max_x, total, avail)
            return
        if not cs and v != "calib":
            # Nothing on screen has two very different causes, and blaming
            # the filter when the run simply has not written a result yet
            # sends you hunting for a filter you never set.
            if not self.cells:
                msg = ("waiting for the first result — the run has not "
                       "finished a trial yet" if self.expect else
                       "no results in the file(s) named")
            else:
                msg = "no cells match this filter — [F] clears it"
            self._put(3, 3, msg, self.curses.A_DIM)
        elif self.detail and v == "cells":
            self.cursor = max(0, min(self.cursor, len(cs) - 1))
            self.view_detail(cs[self.cursor], max_y, max_x)
        elif v == "cells":
            total, avail = self.view_cells(cs, body, max_x)
        elif v == "arms":
            self.view_arms(cs, body, max_x)
        elif v == "cost":
            self.view_cost(cs, body, max_x)
        elif v == "calib":
            total, avail = self.view_calib(body, max_x)
        self.footer(max_y, max_x, total, avail)

    STATE_COLOR = {"running": 2, "grading": 5, "stalled": 4, "dead": 4}

    def view_live(self, body, max_x):
        C = self.curses
        runs = self.live
        if not runs:
            self._put(3, 3, "nothing in flight — start a run, or [tab] to "
                            "the results views", C.A_DIM)
            return 0, 0
        s = lv.summarize(runs)
        self._put(3, 1, f"{s['running']} running · {s['grading']} grading · "
                        f"{s['stalled']} stalled · {s['calls']} calls · "
                        f"{s['tok_in']:,} input tokens", C.A_BOLD)
        self._put(4, 1, f"{'scenario':<20}{'arm':>4}{'t':>3}{'state':>9}"
                        f"{'calls':>7}{'tools':>7}{'sub':>5}{'tok_in':>12}"
                        f"{'elapsed':>9}{'budget':>8}  doing", C.A_DIM)
        avail = max(1, body - 3)
        self.live_cursor = max(0, min(self.live_cursor, len(runs) - 1))
        for i, r in enumerate(runs[:avail]):
            y = 5 + i
            base = C.color_pair(5) if i == self.live_cursor else 0
            self._put(y, 1, f"{r['scenario'][:19]:<20}", base)
            self._put(y, 21, f"{r['arm']:>4}", C.A_DIM if r["unlabelled"]
                      else 0)
            self._put(y, 25, f"{r['trial'] if r['trial'] >= 0 else '?':>3}",
                      C.A_DIM)
            self._put(y, 28, f"{r['state']:>9}",
                      C.color_pair(self.STATE_COLOR.get(r["state"], 0)))
            self._put(y, 37, f"{r['calls']:>7}", 0)
            self._put(y, 44, f"{r['tools']:>7}", C.A_DIM)
            self._put(y, 51, f"{r['sessions'] - 1 if r['sessions'] else 0:>5}",
                      C.A_DIM)
            self._put(y, 56, f"{r['tok_in']:>12,}", C.color_pair(2))
            self._put(y, 68, f"{lv.fmt_age(r['elapsed_s']):>9}", C.A_DIM)
            # Red once the run is close enough to its timeout that it is
            # probably going to be killed rather than finish.
            b = r["budget"]
            self._put(y, 77, f"{lv.fmt_budget(b):>8}",
                      C.color_pair(4) if b and b > 0.8 else C.A_DIM)
            self._put(y, 87, r["last_tool"][:max(0, max_x - 89)], C.A_DIM)
        return len(runs), avail

    def view_live_detail(self, r, max_y, max_x):
        C = self.curses
        w = max_x - 4
        y = 3

        def line(label, value, attr=0):
            nonlocal y
            if y >= max_y - 2:
                return
            self._put(y, 1, f"{label:<16}", C.A_DIM)
            self._put(y, 18, str(value)[:max(0, w - 18)], attr)
            y += 1

        def wrapped(label, text, limit=6):
            """Wrap for the terminal while keeping the author's own line
            breaks. A task prompt is a PR body -- headings, tables, bullet
            lists -- and flattening it to one whitespace-separated blob
            destroys exactly the structure that makes it readable."""
            nonlocal y
            self._put(y, 1, f"{label:<16}", C.A_DIM)
            out, width = [], max(20, w - 18)
            for para in str(text).replace("\r", "").split("\n"):
                if not para.strip():
                    out.append("")
                    continue
                cur = ""
                for word in para.split():
                    if len(cur) + len(word) + 1 > width:
                        out.append(cur)
                        cur = word
                    else:
                        cur = f"{cur} {word}".strip()
                if cur:
                    out.append(cur)
            # Leading/trailing blanks carry no information on a fixed budget.
            while out and not out[0]:
                out.pop(0)
            for ln in out[:limit]:
                if y >= max_y - 2:
                    return
                self._put(y, 18, ln, 0)
                y += 1
            if len(out) > limit and y < max_y - 2:
                self._put(y, 18, f"… {len(out) - limit} more line(s)", C.A_DIM)
                y += 1

        info = r["info"]
        line("scenario", r["scenario"], C.A_BOLD)
        line("arm / trial", f"{r['arm']} / "
                            f"{r['trial'] if r['trial'] >= 0 else '?'}"
                            + ("   (started before the runner wrote its "
                               "marker; not guessed)" if r["unlabelled"]
                               else ""),
             C.A_DIM if r["unlabelled"] else 0)
        line("state", f"{r['state']}   elapsed {lv.fmt_age(r['elapsed_s'])}"
                      f"   budget {lv.fmt_budget(r['budget'])}"
                      f"   idle {lv.fmt_age(r['idle_s'])}",
             C.color_pair(self.STATE_COLOR.get(r["state"], 0)))
        line("cost so far", f"{r['calls']} calls · {r['tok_in']:,} in · "
                            f"{r['tok_out']:,} out · cache "
                            f"{r['cache_read']:,}r/{r['cache_write']:,}w")
        if r["sessions"] > 1:
            line("subagents", f"{r['sessions'] - 1} session(s), "
                              f"{r['subagent_calls']} of the calls above")
        if info.get("pr"):
            line("source PR", f"#{info['pr']}   base "
                              f"{info.get('base_commit', '')[:12]}")
        y += 1

        wrapped("task", info.get("prompt", "(no prompt recorded)"), limit=8)
        y += 1

        # What the grader will do when the agent stops. Shown while the run
        # is live because it is the only way to judge, in the moment,
        # whether the agent is working in the right place at all.
        if info.get("code_files"):
            line("PR touched", ", ".join(info["code_files"])[:w - 18])
        if info.get("test_files"):
            line("grades with", ", ".join(info["test_files"])[:w - 18])
        if info.get("test_cmd"):
            line("test cmd", info["test_cmd"])
        y += 1

        if r["tool_counts"]:
            top = sorted(r["tool_counts"].items(), key=lambda kv: -kv[1])
            line("tools used", "  ".join(f"{k}×{v}" for k, v in top[:8]))
        if r["spawns"]:
            line("spawned", f"{len(r['spawns'])} subagent task(s)")
            for sp in r["spawns"][:3]:
                line("", f"· {sp}", C.A_DIM)
        y += 1
        line("last tool", r["last_tool"] or "—")
        wrapped("last message", r["last_text"] or "—", limit=4)
        y += 1
        line("sandbox", r["sandbox"] or "—", C.A_DIM)
        line("out dir", r["out_dir"], C.A_DIM)
        if r["partial_lines"]:
            line("note", f"{r['partial_lines']} unparsed line(s) — normal "
                         f"while the stream is being written", C.A_DIM)

    def view_cells(self, cs, body, max_x):
        C = self.curses
        self._put(3, 1, f"{'arm/scenario':<20}{'trials':>7}{'pass@1':>8}"
                        f"{'±':>6}{'tok_in':>9}{'if pass':>9}{'calls':>7}"
                        f"{'probes':>8}{'redun':>7}{'abnd':>6}  failing",
                  C.A_DIM)
        avail = max(1, body - 2)
        self.scroll = max(0, min(self.scroll, max(0, len(cs) - avail)))
        for i, c in enumerate(cs[self.scroll:self.scroll + avail]):
            y = 4 + i
            idx = self.scroll + i
            base = C.color_pair(5) if idx == self.cursor else 0
            self._put(y, 1, f"{c['tag']:<20}", base)
            self._put(y, 21, f"{c['trials']:>7}", C.A_DIM)
            self._put(y, 28, f"{c['pass_rate']:>7.0f}%",
                      self.pass_attr(c["pass_rate"]))
            self._put(y, 36, f"{c['spread']:>5.0f}", C.A_DIM)
            self._put(y, 41, f"{c['tok']:>9,.0f}", C.color_pair(2))
            self._put(y, 50, f"{c['tok_won']:>9,.0f}", C.A_DIM)
            self._put(y, 59, f"{c['calls']:>7.0f}", C.A_DIM)
            self._put(y, 66, f"{c['probes']:>8.0f}", C.A_DIM)
            self._put(y, 74, f"{c['redundant']:>7.0f}", C.A_DIM)
            self._put(y, 81, f"{c['abandoned']:>6}",
                      C.color_pair(4) if c["abandoned"] else C.A_DIM)
            self._put(y, 89, ", ".join(c["fails"])[:max(0, max_x - 91)],
                      C.color_pair(4) if c["fails"] else C.A_DIM)
        return len(cs), avail

    def view_arms(self, cs, body, max_x):
        C = self.curses
        rolls = sd.arm_rollup(cs)
        self._put(3, 1, f"{'arm':<5}{'name':<20}{'scen':>5}{'trials':>7}"
                        f"{'pass@1':>8}{'tok_in':>10}{'calls':>7}"
                        f"{'tok ratio':>11}{'call ratio':>11}   role", C.A_DIM)
        y = 4
        for r in rolls:
            tr, tn = sd.paired_ratio(cs, r["arm"], self.ref, "tok")
            cr, _ = sd.paired_ratio(cs, r["arm"], self.ref, "calls")
            self._put(y, 1, f"{r['arm']:<5}", C.A_BOLD)
            self._put(y, 6, f"{r['name']:<20}")
            self._put(y, 26, f"{r['scenarios']:>5}", C.A_DIM)
            self._put(y, 31, f"{r['trials']:>7}", C.A_DIM)
            self._put(y, 38, f"{r['pass_rate']:>7.0f}%",
                      self.pass_attr(r["pass_rate"]))
            self._put(y, 46, f"{r['tok']:>10,.0f}", C.color_pair(2))
            self._put(y, 56, f"{r['calls']:>7.0f}", C.A_DIM)
            if r["arm"] == self.ref:
                self._put(y, 63, f"{'reference':>11}", C.A_DIM)
            else:
                for col, val in ((63, tr), (74, cr)):
                    if val is None:
                        self._put(y, col, f"{'—':>11}", C.A_DIM)
                    else:
                        attr = (C.color_pair(1) if val < 0.95 else
                                C.color_pair(4) if val > 1.05 else C.A_DIM)
                        self._put(y, col, f"{val:>10.3f}×", attr)
            self._put(y, 88, r["role"][:max(0, max_x - 90)], C.A_DIM)
            y += 1

        y += 1
        self._put(y, 1, f"ratios are paired on scenario and geometric, vs "
                        f"arm {self.ref} ({sd.ARMS.get(self.ref, ('?',''))[0]}); "
                        f"n paired shown per arm below", C.A_DIM)
        y += 1
        for r in rolls:
            if r["arm"] == self.ref:
                continue
            _, tn = sd.paired_ratio(cs, r["arm"], self.ref, "tok")
            self._put(y, 3, f"{r['arm']}: {tn} scenario(s) paired"
                            + (f"  ·  {r['subagent_calls']} subagent calls "
                               f"included in tok_in (§7)"
                               if r["subagent_calls"] else ""), C.A_DIM)
            y += 1
        if y + 2 < body:
            self._put(y + 1, 1, "a ratio below 1.000 means this arm is "
                                "cheaper than the reference; read it next to "
                                "pass@1, never alone (§5)", C.A_DIM)

    def view_cost(self, cs, body, max_x):
        """Pass rate vs cost with the Pareto frontier marked.

        Design §5 control 2 asks for exactly this plane per fixture: arms
        as points, cost on x, pass rate on y. If an arm is left-and-down
        from another that is a trade, not a win, and the chart says so
        where a ratio would not."""
        C = self.curses
        pts = [c for c in cs if c["ktok"] > 0]
        if not pts:
            self._put(3, 3, "no cells with token telemetry in this selection",
                      C.A_DIM)
            return
        front = sd.pareto_front(pts)
        lo_p = min(c["pass_rate"] for c in pts)
        hi_p = max(c["pass_rate"] for c in pts)
        lo_k = min(c["ktok"] for c in pts)
        hi_k = max(c["ktok"] for c in pts)
        ph = max(6, min(16, body - 8))
        pw = max(20, min(max_x - 22, 70))
        self._put(3, 1, f"pass@1 vs input tokens — {len(front)} cell(s) on "
                        f"the frontier (★)", C.A_BOLD)
        for i in range(ph):
            self._put(4 + i, 8, "│", C.A_DIM)
        self._put(4 + ph, 8, "└" + "─" * pw, C.A_DIM)
        self._put(4, 2, f"{hi_p:4.0f}%", C.A_DIM)
        self._put(4 + ph - 1, 2, f"{lo_p:4.0f}%", C.A_DIM)
        self._put(4 + ph + 1, 9, f"{lo_k:.0f}k", C.A_DIM)
        self._put(4 + ph + 1, 9 + pw - 6, f"{hi_k:.0f}k", C.A_DIM)
        for c in sorted(pts, key=lambda c: c["tag"] not in front):
            py = (c["pass_rate"] - lo_p) / (hi_p - lo_p) if hi_p > lo_p else 0.5
            px = (c["ktok"] - lo_k) / (hi_k - lo_k) if hi_k > lo_k else 0.5
            y = 4 + int((1 - py) * (ph - 1))
            x = 9 + int(px * (pw - 2))
            if c["tag"] in front:
                self._put(y, x, "★", C.color_pair(1) | C.A_BOLD)
            else:
                self._put(y, x, "·", C.A_DIM)
        y0 = 4 + ph + 2
        self._put(y0, 1, f"{'frontier':<20}{'pass@1':>8}{'tok_in':>10}"
                         f"{'calls':>7}{'dur':>8}", C.A_DIM)
        best = sorted((c for c in pts if c["tag"] in front),
                      key=lambda c: -c["pass_rate"])
        for i, c in enumerate(best[:max(0, body - ph - 6)]):
            yy = y0 + 1 + i
            self._put(yy, 1, f"{c['tag']:<20}")
            self._put(yy, 21, f"{c['pass_rate']:>7.0f}%",
                      self.pass_attr(c["pass_rate"]))
            self._put(yy, 29, f"{c['tok']:>10,.0f}", C.color_pair(2))
            self._put(yy, 39, f"{c['calls']:>7.0f}", C.A_DIM)
            self._put(yy, 46, f"{sd.fmt_duration(c['dur_s']):>8}",
                      C.color_pair(6))

    def view_calib(self, body, max_x):
        C = self.curses
        if not self.proxy_rows:
            # The old text pointed at bench/with-proxy.sh, which does not
            # exist and never did, and omitted the reason this tab is empty
            # for most runs: the gate cannot be measured in parallel at all.
            self._put(3, 3, "no proxy log loaded. The recording proxy runs "
                            "only when ADH_PROXY_LOG is set:", C.A_DIM)
            self._put(4, 5, "ADH_PROXY_LOG=runs/proxy.jsonl make all "
                            "TRIALS=5 JOBS=1", C.A_BOLD)
            self._put(5, 5, "make matrix PROXY=runs/proxy.jsonl", C.A_BOLD)
            self._put(7, 3, "JOBS=1 is not optional. The proxy carries one "
                            "trial mark at a time and nothing in an "
                            "inference request", C.A_DIM)
            self._put(8, 3, "identifies its trial, so under --jobs>1 the "
                            "runner skips the mark rather than writing a "
                            "wrong attribution.", C.A_DIM)
            self._put(9, 3, "The H4 gate therefore has to be measured "
                            "serially — that is the second cost of "
                            "parallelism (§16.4).", C.A_DIM)
            self._put(11, 3, "Without it the adapter's token counts are "
                             "unverified: design §3.2 makes the proxy "
                             "authoritative, not the adapter.", C.A_DIM)
            return 0, 0
        cal = sd.calibration(self.rows, self.proxy_rows)
        if not cal:
            self._put(3, 3, "proxy log has no runs matching these results "
                            "(no trial marks?)", C.A_DIM)
            return 0, 0
        worst = max(c["delta"] for c in cal)
        tot_a = sum(c["adapter_tok"] for c in cal)
        tot_p = sum(c["proxy_tok"] for c in cal)
        agg = abs(tot_a - tot_p) / tot_p if tot_p else 0.0
        ok = worst <= GATE and agg <= GATE and len(cal) >= 20
        verdict = ("H4 PASS" if ok else
                   "H4 INCONCLUSIVE — fewer than 20 runs"
                   if worst <= GATE and agg <= GATE else "H4 FAIL")
        self._put(3, 1, f"{verdict}   runs={len(cal)}  aggregate "
                        f"delta={agg*100:.3f}%  worst={worst*100:.3f}%  "
                        f"tolerance={GATE*100:.0f}%",
                  (C.color_pair(1) if ok else C.color_pair(3)
                   if worst <= GATE else C.color_pair(4)) | C.A_BOLD)
        if not ok and worst > GATE:
            self._put(4, 1, "the proxy is authoritative — drop the adapter "
                            "figures and re-derive cost from the proxy log",
                      C.color_pair(4))
        self._put(5, 1, f"{'run':<26}{'adapter tok':>13}{'proxy tok':>12}"
                        f"{'delta':>9}{'a.calls':>9}{'p.calls':>9}{'aux':>6}",
                  C.A_DIM)
        avail = max(1, body - 4)
        self.scroll = max(0, min(self.scroll, max(0, len(cal) - avail)))
        for i, c in enumerate(cal[self.scroll:self.scroll + avail]):
            y = 6 + i
            bad = c["delta"] > GATE or c["adapter_calls"] != c["proxy_calls"]
            self._put(y, 1, f"{c['mark']:<26}")
            self._put(y, 27, f"{c['adapter_tok']:>13,}", C.A_DIM)
            self._put(y, 40, f"{c['proxy_tok']:>12,}", C.color_pair(2))
            self._put(y, 52, f"{c['delta']*100:>8.2f}%",
                      C.color_pair(4) if bad else C.color_pair(1))
            self._put(y, 61, f"{c['adapter_calls']:>9}", C.A_DIM)
            self._put(y, 70, f"{c['proxy_calls']:>9}", C.A_DIM)
            self._put(y, 79, f"{c['aux']:>6}", C.A_DIM)
        return len(cal), avail

    def view_detail(self, c, max_y, max_x):
        C = self.curses
        self._put(3, 1, f"{c['tag']}   {c['category']}   "
                        f"{c['model']} · {c['adapter']}", C.A_BOLD)
        arm_name, arm_role = sd.ARMS.get(c["arm"], ("?", ""))
        self._put(4, 1, f"arm {c['arm']} — {arm_name}: {arm_role}", C.A_DIM)
        lines = [
            ("trials", f"{c['trials']}"),
            ("pass@1", f"{c['pass_rate']:.0f}%  (± {c['spread']:.0f})"),
            ("median tok_in", f"{c['tok']:,.0f}"),
            ("median tok_in | passed", f"{c['tok_won']:,.0f}"),
            ("median calls", f"{c['calls']:.0f}"),
            ("probes to first edit", f"{c['probes']:.0f}"),
            ("redundant reads", f"{c['redundant']:.0f}"),
            ("subagent calls", f"{c['subagent_calls']}"),
            ("abandoned trials", f"{c['abandoned']}/{c['trials']}"),
            ("median duration", sd.fmt_duration(c["dur_s"])),
            ("failing checks", ", ".join(c["fails"]) or "—"),
        ]
        for i, (k, v) in enumerate(lines):
            self._put(6 + i, 3, f"{k:<26}", C.A_DIM)
            self._put(6 + i, 29, v)
        y = 6 + len(lines) + 1
        self._put(y, 1, "per-trial", C.A_DIM)
        for i, r in enumerate(c["rows"][:max(0, max_y - y - 4)]):
            m = r.get("metrics") or {}
            self._put(y + 1 + i, 3,
                      f"trial {r['trial']}  {'pass' if r['all_pass'] else 'FAIL'}"
                      f"  calls={m.get('calls', 0)}"
                      f"  tok_in={m.get('tok_in_billed', 0):,}"
                      f"  dur={r.get('duration_s', 0)}s"
                      f"  src={r.get('_src', '')}",
                      C.color_pair(1) if r["all_pass"] else C.color_pair(4))

    def view_picker(self, body, max_x):
        C = self.curses
        opts = []
        for f in sd.FACETS:
            for v in sd.facet_values(self.cells, f):
                opts.append((f, v))
        self._put(3, 1, "pick facets — [space] toggles, [enter] applies, "
                        "values within a facet OR, facets AND", C.A_DIM)
        avail = max(1, body - 2)
        self.pick_i = max(0, min(self.pick_i, max(0, len(opts) - 1)))
        self.scroll = max(0, min(self.scroll, max(0, len(opts) - avail)))
        if self.pick_i < self.scroll:
            self.scroll = self.pick_i
        elif self.pick_i >= self.scroll + avail:
            self.scroll = self.pick_i - avail + 1
        for i, (f, v) in enumerate(opts[self.scroll:self.scroll + avail]):
            y = 4 + i
            idx = self.scroll + i
            on = v in self.sel[f]
            mark = "[x]" if on else "[ ]"
            attr = C.color_pair(5) if idx == self.pick_i else 0
            self._put(y, 1, f"{mark} {f:<10}{v}", attr)
            n = sum(1 for c in self.cells if c[f] == v)
            self._put(y, 45, f"{n} cell(s)", C.A_DIM)
        self._picker_opts = opts
        return len(opts), avail

    # ---- input -----------------------------------------------------------

    def handle_key(self, key) -> bool:
        C = self.curses
        if key == -1:                     # halfdelay tick
            # Live state has no mtime to gate on and changes constantly, so
            # it refreshes every tick; results only when a file moved.
            self.reload_live()
            if sd.newest_mtime(self.files) > self.seen_mtime:
                self.reload()
            return False
        if self.editing:
            if key in (10, 13):
                self.pattern, self.editing = self.buf.strip(), False
                self.scroll = self.cursor = 0
            elif key == 27:
                self.editing = False
            elif key in (C.KEY_BACKSPACE, 127, 8):
                self.buf = self.buf[:-1]
            elif 32 <= key < 127:
                self.buf += chr(key)
            return False
        if self.picking:
            opts = getattr(self, "_picker_opts", [])
            if key in (ord("j"), C.KEY_DOWN):
                self.pick_i += 1
            elif key in (ord("k"), C.KEY_UP):
                self.pick_i = max(0, self.pick_i - 1)
            elif key == ord(" ") and opts:
                f, v = opts[self.pick_i]
                self.sel[f] ^= {v}
            elif key in (10, 13):
                self.picking = False
                self.scroll = 0
            elif key in (ord("q"), ord("Q"), 27):
                if any(self.sel.values()):
                    self.sel = {f: set() for f in sd.FACETS}
                    return False
                return True
            return False

        if key in (ord("q"), ord("Q"), 27):
            if self.detail:
                self.detail = False
                return False
            return True
        if key == 9:                                  # tab
            self.view = (self.view + 1) % len(VIEWS)
            self.scroll = 0
        elif key == ord("s"):
            self.sort = (self.sort + 1) % len(SORTS)
        elif key == ord("/"):
            self.editing, self.buf = True, self.pattern
        elif key == ord("F"):
            self.pattern = ""
            self.sel = {f: set() for f in sd.FACETS}
            self.scroll = 0
        elif key == ord("p"):
            self.picking, self.scroll, self.pick_i = True, 0, 0
        elif key == ord("r"):
            self.reload()
            self.reload_live()
        elif key == ord("L"):
            self.view = VIEWS.index("live")
            self.scroll = 0
        elif key == ord(" "):
            self.detail = not self.detail
        elif key in (ord("j"), C.KEY_DOWN):
            if VIEWS[self.view] == "live":
                self.live_cursor += 1
                self._anchor_live()
            else:
                self.cursor += 1
                self.scroll += 1
        elif key in (ord("k"), C.KEY_UP):
            if VIEWS[self.view] == "live":
                self.live_cursor = max(0, self.live_cursor - 1)
                self._anchor_live()
            else:
                self.cursor = max(0, self.cursor - 1)
                self.scroll = max(0, self.scroll - 1)
        elif key == C.KEY_NPAGE:
            self.scroll += 10
        elif key == C.KEY_PPAGE:
            self.scroll = max(0, self.scroll - 10)
        return False


def main():
    pattern = ""
    ref = os.environ.get("REF", "a1")
    proxy = os.environ.get("PROXY") or None
    files = [f for f in os.environ.get("FILES", "").split() if f]
    expect = int(os.environ.get("EXPECT", "0") or 0)
    args = [a for a in sys.argv[1:]]
    for i, a in enumerate(args):
        if a == "--ref" and i + 1 < len(args):
            ref = args[i + 1]
        elif a == "--proxy" and i + 1 < len(args):
            proxy = args[i + 1]
        elif a == "--files" and i + 1 < len(args):
            files = args[i + 1].split(",")
        elif a == "--expect" and i + 1 < len(args):
            expect = int(args[i + 1])
        elif not a.startswith("--") and (i == 0 or args[i - 1] not in
                                         ("--ref", "--proxy", "--files",
                                          "--expect")):
            pattern = a
    if proxy and not os.path.isabs(proxy):
        proxy = os.path.join(sd.ROOT, proxy)
    files = [f if os.path.isabs(f) else os.path.join(sd.ROOT, f)
             for f in files]
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        print(f"no such results file: {', '.join(missing)}", file=sys.stderr)
        return 1
    # An explicitly named file that exists but is empty is the normal state
    # of a run that just started, and watching it fill is the whole reason
    # to point the viewer at one. Only the unscoped case -- nothing named,
    # nothing on disk -- is a "you have not run anything yet" error.
    if not files and not sd.load_rows():
        print("no results found — run the suite first "
              "(make all), or pass FILES=<results.jsonl>", file=sys.stderr)
        return 1
    curses_main(lambda scr: SuiteTui(scr, pattern, ref, proxy, files, expect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
