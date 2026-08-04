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
from pathlib import Path

from adherence import design as dz
from adherence import live as lv
from adherence import suitedata as sd
from adherence import tasks as tk
from adherence.tui.framework import TuiApp, curses_main

# Run data first, then the reference pages. They are separated in the tab
# bar because they answer different questions: everything left of the
# divider changes as the grid runs, everything right of it is ground truth
# that does not move, and mixing them invites reading a static table as a
# live one.
VIEWS = ("live", "cells", "arms", "cost", "calib", "tasks", "design")
REFERENCE_FROM = VIEWS.index("tasks")
VIEW_HELP = {
    "live": "running · graded · per arm×scenario   [h/l] section  "
            "[j/k] row  [space] detail",
    "tasks": "what each scenario asks for and how it is judged — no run "
             "data   [j/k] row  [space] detail",
    "design": "what each arm is, how they differ, and the run's ground "
              "rules   [j/k] row  [space] detail",
    "cells": "every (arm, scenario) cell   [j/k] row  [space] detail "
             "(median/mean/p90)",
    "arms": "per-arm rollup; ratios are paired on scenario, geometric",
    "cost": "pass rate vs tokens — ★ is the Pareto frontier (§5)",
    "calib": "adapter vs proxy per run; >2% fails the H4 gate (§3.2)",
}
SORTS = ("tag", "pass", "tok")
SECTIONS = ("running", "summary", "graded")
# Per-section sorts, cycled with [s] on whichever section has focus.
GRADED_SORTS = ("recent", "verdict", "tok", "dur")
SUM_SORTS = ("tag", "pass", "tok", "dur", "left")
TASK_SORTS = ("id", "grader", "files", "dirs", "pr")
DESIGN_SORTS = ("arm", "always", "ondemand")
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
        self.act_cursor = 0  # selected activity event, newest-first index
        self.act_sel = ""    # its stable id -- see _anchor_act
        self.act_scroll = 0
        self.act_follow = True   # stay pinned to newest until you move
        self.act_open = False
        # Which of the live view's three tables has the cursor. [space]
        # opens the selected row of whichever one is focused.
        self.live_section = 0            # 0 running, 1 graded, 2 summary
        self.graded_cursor = 0
        self.sum_cursor = 0
        # Each section scrolls independently. They used to be truncated to
        # whatever fit, with no scroll and no indication -- so 25 graded
        # rows showed 6 and the cursor could sit on one of the 19 nobody
        # could see.
        self.live_scroll = 0
        self.graded_scroll = 0
        self.sum_scroll = 0
        self.graded_sort = 0
        self.sum_sort = 0
        self._tasks = None       # static; loaded once, never per tick
        self._design = None
        self.task_sort = 0
        self.task_desc = False
        self.design_sort = 0
        self.design_desc = False
        self.pane_scroll = 0     # shared by the scrollable detail panes
        # Direction is separate from key, so [S] flips without losing the
        # column you picked. Defaults are the useful end: newest graded
        # first, cheapest cell first.
        self.graded_desc = True
        self.sum_desc = False
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

    def scrollbar(self, y0, rows, x, total, offset):
        """A track with a proportional thumb, drawn down column `x`.

        Rendered whenever the list is longer than the window, so it is
        possible to tell 'there is nothing more' from 'you are at the top
        of a long list' -- which a plain truncated list cannot say."""
        C = self.curses
        if rows <= 0:
            return
        if total <= rows:
            for i in range(rows):
                self._put(y0 + i, x, "│", C.A_DIM)
            return
        size = max(1, int(rows * rows / total))
        span = rows - size
        pos = int(span * offset / max(1, total - rows)) if span else 0
        for i in range(rows):
            inside = pos <= i < pos + size
            self._put(y0 + i, x, "█" if inside else "│",
                      0 if inside else C.A_DIM)

    def section_lengths(self):
        """Rows in each live section, in display order."""
        return [len(self.live), len(self.sum_cells()),
                len(self.graded_rows())]

    def _move_live(self, step):
        """Move one row, crossing into the next section at the edges.

        The three tables read as one column on screen, so [j] at the last
        row of `running` should land on the first row of `graded` rather
        than stop dead and wait for an [h]/[l] nobody thinks to press.
        [h]/[l] still jump whole sections."""
        lens = self.section_lengths()
        cursors = [self.live_cursor, self.sum_cursor, self.graded_cursor]
        sec = self.live_section
        cur = cursors[sec] + step
        if cur < 0:
            # off the top: previous non-empty section, at its last row
            prev = next((i for i in range(sec - 1, -1, -1) if lens[i]), None)
            if prev is None:
                cur = 0
            else:
                sec, cur = prev, lens[prev] - 1
        elif cur >= lens[sec]:
            nxt = next((i for i in range(sec + 1, 3) if lens[i]), None)
            if nxt is None:
                cur = max(0, lens[sec] - 1)
            else:
                sec, cur = nxt, 0
        self.live_section = sec
        cursors[sec] = max(0, cur)
        self.live_cursor, self.sum_cursor, self.graded_cursor = cursors
        if sec == 0:
            self._anchor_live()

    def _move_rows(self, step):
        """Move the results cursor, clamped to the rows that exist.

        It was unbounded, and `view_cells` clamps `scroll` but not
        `cursor` -- so holding [j] walked the selection off the end of the
        list, the highlight vanished, and [space] then opened a detail
        pane for a row that was never on screen."""
        v = VIEWS[self.view]
        n = (len(self.task_rows()) if v == "tasks"
             else len(self.design_rows()) if v == "design"
             else len(self.visible()))
        if not n:
            self.cursor = self.scroll = 0
            return
        self.cursor = max(0, min(self.cursor + step, n - 1))
        # Keep the cursor in view without yanking the window around.
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        self.scroll = max(0, min(self.scroll, max(0, n - 1)))

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
            if i == REFERENCE_FROM:
                self._put(0, x, " │ ", C.A_DIM)
                x += 3
            attr = C.color_pair(5) if i == self.view else C.A_DIM
            self._put(0, x, f" {name} ", attr)
            x += len(name) + 3
        filt = self.buf if self.editing else (self.pattern or "all")
        # The sort shown must be the one [s] would change here. It used to
        # be the cells sort on every tab, so `sort:tok` sat above a task
        # list that [s] did not touch.
        if v == "tasks":
            sort = f"{TASK_SORTS[self.task_sort]} " \
                   f"{'v' if self.task_desc else '^'}"
        elif v == "design":
            sort = f"{DESIGN_SORTS[self.design_sort]} " \
                   f"{'v' if self.design_desc else '^'}"
        elif v == "live":
            sort = SECTIONS[self.live_section]
        else:
            sort = SORTS[self.sort]
        tail = f"filter:{filt}  sort:{sort}  ref:{self.ref}"
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
            # Bracketed, and a track character that cannot be mistaken for
            # an ellipsis: an empty bar rendered as "................"
            # reads as truncated output, not as 0%.
            prog = (f"[{'█' * fill}{'·' * (bar_w - fill)}] "
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
        live_v = VIEWS[self.view] == "live"
        # Depth-accurate: advertise only what the current level responds
        # to. A legend listing keys that do nothing here is worse than no
        # legend, because it is checked once and then trusted.
        if self.act_open:
            keys = [("[j/k] scroll", C.A_DIM), ("[space] back to activity",
                                                C.A_BOLD),
                    ("[esc] root", C.A_DIM), ("[Q] quit", C.A_DIM)]
        elif self.detail and live_v and self.live_section == 0:
            keys = [("[j/k] event", C.A_DIM), ("[space] expand", C.A_DIM),
                    ("[q] back to list", C.A_BOLD), ("[esc] root", C.A_DIM),
                    ("[Q] quit", C.A_DIM)]
        elif self.detail:
            keys = [("[q] back", C.A_BOLD), ("[esc] root", C.A_DIM),
                    ("[Q] quit", C.A_DIM)]
        elif live_v:
            keys = [("[tab/shift-tab] view", C.A_DIM),
                    (f"[h/l] section: {SECTIONS[self.live_section]}",
                     C.color_pair(5)),
                    ("[j/k] row", C.A_DIM), ("[s] sort [S] asc/desc",
                                             C.A_DIM),
                    ("[space] detail", C.A_DIM), ("[r] reload", C.A_DIM),
                    ("[q] quit", C.A_DIM)]
        else:
            keys = [("[tab/shift-tab] view", C.A_DIM),
                    (f"[L] live·{n_live}",
                     C.color_pair(2) if n_live else C.A_DIM),
                    ("[s] sort [S] asc/desc", C.A_DIM),
                    ("[/] filter", C.A_DIM),
                    ("[F] clear", C.A_DIM), ("[p] pick", C.A_DIM),
                    ("[space] detail", C.A_DIM), ("[r] reload", C.A_DIM),
                    ("[q] quit", C.A_DIM)]
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
        # Two rows more than the chrome needs: one for the scrolling
        # section's range indicator, one blank, so neither the last row of
        # content nor the "N of M" line sits flush against the legend.
        body = max_y - 5
        if self.picking:
            total, avail = self.view_picker(body, max_x)
            self.footer(max_y, max_x, total, avail)
            return
        cs = self.visible()
        total = avail = 0
        v = VIEWS[self.view]
        if v == "tasks":
            rows = self.task_rows()
            if self.detail and rows:
                self.cursor = max(0, min(self.cursor, len(rows) - 1))
                self.view_task_detail(rows[self.cursor], max_y, max_x)
            else:
                total, avail = self.view_tasks(rows, body, max_x)
            self.footer(max_y, max_x, total, avail)
            return
        if v == "design":
            rows = self.design_rows()
            if self.detail and rows:
                self.cursor = max(0, min(self.cursor, len(rows) - 1))
                self.view_design_detail(rows[self.cursor], max_y, max_x)
            else:
                total, avail = self.view_design(rows, body, max_x)
            self.footer(max_y, max_x, total, avail)
            return
        if v == "live":
            # Deliberately not filtered by `pattern`: that filter is a
            # results-side idea (arm/scenario cells), and silently hiding a
            # running job because of it would be the worst possible
            # behaviour for a monitor.
            if self.detail and self.live_section == 2 and self.rows:
                rows = self.graded_rows()
                self.graded_cursor = max(0, min(self.graded_cursor,
                                                len(rows) - 1))
                self.view_result_detail(rows[self.graded_cursor],
                                        max_y, max_x)
            elif self.detail and self.live_section == 1 and self.cells:
                cs = self.sum_cells()
                self.sum_cursor = max(0, min(self.sum_cursor, len(cs) - 1))
                self.view_detail(cs[self.sum_cursor], max_y, max_x)
            elif self.detail and self.live:
                self.live_cursor = max(0, min(self.live_cursor,
                                              len(self.live) - 1))
                self.view_live_detail(self.live[self.live_cursor],
                                      max_y, max_x)
            else:
                total, avail = self.view_live(body, max_x, max_y)
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

    STATE_COLOR = {"running": 2, "grading": 5, "done": 1,
                   "stalled": 4, "dead": 4}

    def view_live(self, body, max_x, max_y=0):
        """Three stacked sections: what is running, what has just been
        graded, and the per-(arm, scenario) rollup that says whether the
        grid is on pace. A single flat table cannot answer 'is it working'
        and 'how long will this take' at the same time."""
        C = self.curses
        max_y = max_y or (body + 3)
        runs = self.live
        y = 3
        # Priority, not proportion. Running and summary are bounded --
        # one row per concurrent trial, one per (arm, scenario) cell -- so
        # they fit and should simply be shown whole. Graded grows without
        # bound for the life of the run, so it is the section that absorbs
        # the scrolling. Sharing the space proportionally instead gave
        # graded 22 rows it did not need while truncating a 3-row running
        # table, and on a 24-line terminal pushed graded off the screen
        # entirely -- absent, not truncated, with nothing saying so.
        HEAD = 3
        n_run, n_sum, n_grad = len(runs), len(self.cells), len(self.rows)
        overhead = 2 + HEAD * (bool(n_run) + bool(n_sum) + bool(n_grad))
        spare = max(1, body - overhead)
        self.run_budget = min(n_run, spare)
        spare -= self.run_budget
        self.sum_budget = min(n_sum, spare)
        spare -= self.sum_budget
        # Graded takes the remainder, and at least one row so the section
        # never disappears without a scrollbar to explain itself.
        self.sec_budget = max(1, spare)

        s = lv.summarize(runs)
        done = len(self.rows)
        self._put(y, 1, f"{s['running']} running · {s['grading']} grading · "
                        f"{s['stalled']} stalled · {done} graded"
                        + (f"/{self.expect}" if self.expect else "")
                        + f" · {s['calls']} calls · {s['tok_in']:,} in",
                  C.A_BOLD)
        y += 2

        # ---- running ---------------------------------------------------
        if runs:
            self._put(y, 1, "running" + ("  ◀" if self.live_section == 0
                                          else ""),
                      C.color_pair(5) if self.live_section == 0 else C.A_DIM)
            y += 1
            self._put(y, 1, f"{'scenario':<20}{'arm':>4}{'t':>3}{'state':>9}"
                            f"{'calls':>7}{'tools':>6}{'sub':>5}{'tok_in':>12}"
                            f"{'elapsed':>9}{'budget':>8}  doing", C.A_DIM)
            y += 1
            self.live_cursor = max(0, min(self.live_cursor, len(runs) - 1))
            rows_r = max(1, min(len(runs), self.run_budget))
            if self.live_cursor < self.live_scroll:
                self.live_scroll = self.live_cursor
            elif self.live_cursor >= self.live_scroll + rows_r:
                self.live_scroll = self.live_cursor - rows_r + 1
            self.live_scroll = max(0, min(self.live_scroll,
                                          max(0, len(runs) - rows_r)))
            top = y
            shown = runs[self.live_scroll:self.live_scroll + rows_r]
            for i_off, r in enumerate(shown):
                i = self.live_scroll + i_off
                base = (C.color_pair(5)
                        if i == self.live_cursor and self.live_section == 0
                        else 0)
                self._put(y, 1, f"{r['scenario'][:19]:<20}", base)
                self._put(y, 21, f"{r['arm']:>4}",
                          C.A_DIM if r["unlabelled"] else 0)
                self._put(y, 25,
                          f"{r['trial'] if r['trial'] >= 0 else '?':>3}",
                          C.A_DIM)
                self._put(y, 28, f"{r['state']:>9}",
                          C.color_pair(self.STATE_COLOR.get(r["state"], 0)))
                self._put(y, 37, f"{r['total_calls']:>7}", 0)
                self._put(y, 44, f"{r['tools']:>6}", C.A_DIM)
                # Child SESSIONS, from opencode's own store -- the root
                # stream carries none of a subagent's calls, so counting
                # session ids in the stream always answered 0.
                self._put(y, 50, f"{r['child_sessions']:>5}",
                          C.color_pair(3) if r["child_sessions"] else C.A_DIM)
                self._put(y, 55, f"{r['total_tok_in']:>12,}",
                          C.color_pair(2))
                self._put(y, 67, f"{lv.fmt_age(r['elapsed_s']):>9}", C.A_DIM)
                b = r["budget"]
                self._put(y, 76, f"{lv.fmt_budget(b):>8}",
                          C.color_pair(4) if b and b > 0.8 else C.A_DIM)
                self._put(y, 86, r["last_tool"][:max(0, max_x - 88)], C.A_DIM)
                y += 1
            if len(runs) > rows_r:
                self.scrollbar(top, rows_r, max_x - 2, len(runs),
                               self.live_scroll)
                self._put(y, 1,
                          f"  {self.live_scroll + 1}-"
                          f"{self.live_scroll + rows_r} of {len(runs)}"
                          f"   [j/k] scrolls", C.A_DIM)
                y += 1
        else:
            self._put(y, 3, "nothing in flight", C.A_DIM)
            y += 1
        y += 1

        cells = self.sum_cells()
        # ---- per (arm, scenario) rollup --------------------------------
        if not cells:
            self._put(y, 1, "summary — per arm x scenario"
                            + ("  ◀" if self.live_section == 1 else ""),
                      C.color_pair(5) if self.live_section == 1 else C.A_BOLD)
            self._put(y + 1, 3, "nothing graded yet — a cell appears once a "
                                "trial finishes", C.A_DIM)
            y += 3
        if cells:
            self._put(y, 1, f"summary — per arm x scenario, sorted by "
                            f"{SUM_SORTS[self.sum_sort]} "
                            f"{'v' if self.sum_desc else '^'}"
                            + ("  ◀" if self.live_section == 1 else ""),
                      C.color_pair(5) if self.live_section == 1 else C.A_BOLD)
            y += 1
            self._put(y, 1, f"{'arm/scenario':<24}{'done':>6}{'ung':>5}"
                            f"{'pass':>6}{'med tok':>11}{'p90 tok':>11}"
                            f"{'calls':>7}{'tools':>7}{'sub':>5}{'abnd':>6}"
                            f"{'med dur':>9}{'left':>5}{'eta':>8}", C.A_DIM)
            y += 1
            # Trials per cell, from the batch size when it is known.
            per_cell = 0
            if self.expect and cells:
                per_cell = max(1, round(self.expect / max(1, len(cells))))
            self.sum_cursor = max(0, min(self.sum_cursor, len(cells) - 1))
            rows_s = max(1, min(len(cells), self.sum_budget))
            if self.sum_cursor < self.sum_scroll:
                self.sum_scroll = self.sum_cursor
            elif self.sum_cursor >= self.sum_scroll + rows_s:
                self.sum_scroll = self.sum_cursor - rows_s + 1
            self.sum_scroll = max(0, min(self.sum_scroll,
                                         max(0, len(cells) - rows_s)))
            top_s = y
            for ci_off, c in enumerate(cells[self.sum_scroll:
                                             self.sum_scroll + rows_s]):
                ci = self.sum_scroll + ci_off
                hl = (C.color_pair(5)
                      if ci == self.sum_cursor and self.live_section == 1
                      else 0)
                left = max(0, per_cell - c["trials"]) if per_cell else 0
                eta = left * c["dur_s"]
                self._put(y, 1, f"{c['tag'][:23]:<24}", hl)
                self._put(y, 25, f"{c['trials']:>6}", C.A_DIM)
                self._put(y, 31, f"{c['ungradeable']:>5}",
                          C.color_pair(3) if c["ungradeable"] else C.A_DIM)
                self._put(y, 36, f"{c['pass_rate']:>5.0f}%",
                          self.pass_attr(c["pass_rate"]))
                skewed = (c["abandoned"] and c["tok_worked"]
                          and c["tok_worked"] > c["tok"] * 1.05)
                self._put(y, 42, f"{c['tok']:>11,.0f}"
                          + ("*" if skewed else " "),
                          C.color_pair(4) if skewed else C.color_pair(2))
                # p90 in red when the tail is far past the middle: a median
                # that halves while the tail doubles is not a saving.
                tail = (c["p90_tok"] / c["tok"]) if c["tok"] else 0
                self._put(y, 53, f"{c['p90_tok']:>11,.0f}",
                          C.color_pair(4) if tail >= 2 else C.A_DIM)
                self._put(y, 64, f"{c['calls']:>7.0f}", C.A_DIM)
                self._put(y, 71, f"{c['avg_tools']:>7.1f}", C.A_DIM)
                self._put(y, 78, f"{c['n_subagents']:>5.0f}",
                          C.color_pair(3) if c["n_subagents"] else C.A_DIM)
                # Abandons in red: they never pass, and they drag the
                # median token count down, so a cheap-looking cell with
                # abandons is not cheap.
                self._put(y, 83, f"{c['abandoned']:>6}",
                          C.color_pair(4) if c["abandoned"] else C.A_DIM)
                self._put(y, 89, f"{lv.fmt_age(c['dur_s']):>9}", C.A_DIM)
                self._put(y, 98, f"{left:>5}" if per_cell else f"{'—':>5}",
                          C.A_DIM)
                self._put(y, 103, f"{lv.fmt_age(eta):>8}" if eta
                          else f"{'—':>8}", C.A_DIM)
                y += 1
            if len(cells) > rows_s:
                self.scrollbar(top_s, rows_s, max_x - 2, len(cells),
                               self.sum_scroll)
                self._put(y, 1,
                          f"  {self.sum_scroll + 1}-"
                          f"{self.sum_scroll + rows_s} of {len(cells)}"
                          f"   [j/k] scrolls", C.A_DIM)
                y += 1
            y += 1          # separator; the block swap dropped it
        # ---- graded ----------------------------------------------------
        # Newest first: a run in progress is judged by what just landed,
        # not by what landed an hour ago.
        recent = self.graded_rows()
        if not recent:
            self._put(y, 1, "graded"
                            + ("  ◀" if self.live_section == 2 else ""),
                      C.color_pair(5) if self.live_section == 2 else C.A_BOLD)
            self._put(y + 1, 3, "no results yet — the first row lands when a "
                                "trial finishes and is graded", C.A_DIM)
            y += 3
        if recent:
            self._put(y, 1, f"graded — sorted by "
                            f"{GRADED_SORTS[self.graded_sort]} "
                            f"{'v' if self.graded_desc else '^'}"
                            + ("  ◀" if self.live_section == 2 else ""),
                      C.color_pair(5) if self.live_section == 2 else C.A_BOLD)
            y += 1
            self._put(y, 1, f"{'scenario':<20}{'arm':>4}{'t':>3}"
                            f"{'verdict':>13}{'calls':>7}{'tok_in':>12}"
                            f"{'dur':>9}  failing", C.A_DIM)
            y += 1
            self.graded_cursor = max(0, min(self.graded_cursor,
                                            len(recent) - 1))
            rows_g = max(1, min(len(recent), self.sec_budget))
            if self.graded_cursor < self.graded_scroll:
                self.graded_scroll = self.graded_cursor
            elif self.graded_cursor >= self.graded_scroll + rows_g:
                self.graded_scroll = self.graded_cursor - rows_g + 1
            self.graded_scroll = max(0, min(self.graded_scroll,
                                            max(0, len(recent) - rows_g)))
            window_g = recent[self.graded_scroll:self.graded_scroll + rows_g]
            top_g = y
            for gi_off, r in enumerate(window_g):
                gi = self.graded_scroll + gi_off
                hl = (C.color_pair(5)
                      if gi == self.graded_cursor and self.live_section == 2
                      else 0)
                m = r.get("metrics") or {}
                ung = [c for c in r["checks"]
                       if c.get("name") == "adapter"
                       and c.get("status") != "pass"]
                verdict = ("ungradeable" if ung else
                           "pass" if r["all_pass"] else "fail")
                col = (C.color_pair(3) if ung else
                       C.color_pair(1) if r["all_pass"] else C.color_pair(4))
                fails = ", ".join(c["name"] for c in r["checks"]
                                  if c["status"] == "fail") or "—"
                self._put(y, 1, f"{r['scenario'][:19]:<20}", hl)
                self._put(y, 21, f"{r.get('arm', '-'):>4}", C.A_DIM)
                self._put(y, 25, f"{r['trial']:>3}", C.A_DIM)
                self._put(y, 28, f"{verdict:>13}", col)
                self._put(y, 41, f"{m.get('calls', 0):>7}", C.A_DIM)
                self._put(y, 48, f"{m.get('tok_in_billed', 0):>12,}", C.A_DIM)
                self._put(y, 60, f"{lv.fmt_age(r.get('duration_s', 0)):>9}",
                          C.A_DIM)
                self._put(y, 71, fails[:max(0, max_x - 73)], C.A_DIM)
                y += 1
            if len(recent) > rows_g:
                self.scrollbar(top_g, rows_g, max_x - 2, len(recent),
                               self.graded_scroll)
                self._put(y, 1,
                          f"  {self.graded_scroll + 1}-"
                          f"{self.graded_scroll + rows_g} of {len(recent)}"
                          f"   [j/k] scrolls", C.A_DIM)
                y += 1
            y += 1

        return 0, 0

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
        if self.act_open:
            # The expanded event gets the whole pane. Keeping the run block
            # on screen left about eight lines for the thing being read.
            self.view_event(r, max_y, max_x)
            return
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
        # parent + children, which is the only total §7 permits quoting as
        # a cost. The split is shown because E3 is a claim about the child
        # half specifically.
        line("cost so far", f"{r['total_calls']} calls · "
                            f"{r['total_tok_in']:,} in · "
                            f"{r['tok_out'] + r['child_tok_out']:,} out · "
                            f"cache {r['cache_read']:,}r/"
                            f"{r['cache_write']:,}w")
        line("  parent", f"{r['calls']} calls · {r['tok_in']:,} in")
        if r["child_sessions"]:
            line("  subagents", f"{r['child_calls']} calls · "
                                f"{r['child_tok_in']:,} in   "
                                f"({r['child_sessions']} session(s): "
                                f"{', '.join(r['child_agents'][:4]) or '?'})",
                 C.color_pair(3))
            share = (r["child_tok_in"] / r["total_tok_in"]
                     if r["total_tok_in"] else 0)
            line("", f"subagents are {share:.0%} of this trial's input "
                     f"tokens — read live from opencode's store, since the "
                     f"root stream carries none of them", C.A_DIM)
        elif not r["children_readable"] and r["spawns"]:
            line("subagents", f"{len(r['spawns'])} spawn(s) seen in the "
                              f"stream; opencode's store was not readable",
                 C.A_DIM)
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
        line("sandbox", r["sandbox"] or "—", C.A_DIM)
        line("out dir", r["out_dir"], C.A_DIM)
        if r["partial_lines"]:
            line("note", f"{r['partial_lines']} unparsed line(s) — normal "
                         f"while the stream is being written", C.A_DIM)
        y += 1

        y += 1

        # ---- activity ---------------------------------------------------
        # Newest first, indexed by position in the run, and deliberately
        # one line each: showing every tool's output inline turned the pane
        # into a wall nobody could scan. [space] opens the selected one.
        room = max_y - y - 2
        if room < 4:
            return
        try:
            ev = lv.activity(Path(r["out_dir"]))
        except Exception:
            ev = []
        ev = ev[::-1]                     # newest first
        self._anchor_act(ev)

        if not ev:
            self._put(y, 1, "activity", C.A_BOLD)
            self._put(y + 1, 3, "nothing recorded yet", C.A_DIM)
            return

        self._put(y, 1, f"activity — newest first, {len(ev)} events",
                  C.A_BOLD)
        hint = "[j/k] move  [space] open  [q] back"
        self._put(y, max(0, max_x - len(hint) - 3), hint, C.A_DIM)
        y += 1
        rows = max_y - y - 2
        # Keep the cursor inside the window without yanking the view.
        if self.act_cursor < self.act_scroll:
            self.act_scroll = self.act_cursor
        elif self.act_cursor >= self.act_scroll + rows:
            self.act_scroll = self.act_cursor - rows + 1
        self.act_scroll = max(0, min(self.act_scroll, max(0, len(ev) - rows)))
        for i, e in enumerate(ev[self.act_scroll:self.act_scroll + rows]):
            idx = self.act_scroll + i
            base = C.color_pair(5) if idx == self.act_cursor else 0
            self._put(y + i, 1, f"{e['n']:>5}", C.A_DIM)
            self._put(y + i, 7, "sub" if e["who"] == "subagent" else "   ",
                      C.color_pair(3) if e["who"] == "subagent" else C.A_DIM)
            if e["kind"] == "tool":
                label = f"{e['name']} {e['target']}"
                mark = "!" if e["failed"] else " "
                self._put(y + i, 11, f"{mark}{label}"[:max_x - 15],
                          C.color_pair(4) if e["failed"] else base)
            else:
                self._put(y + i, 11,
                          f" say  {lv.preview(e, max_x - 20)}",
                          C.A_DIM if idx != self.act_cursor else base)
        self.scrollbar(y, rows, max_x - 2, len(ev), self.act_scroll)

    def _anchor_act(self, ev):
        """Resolve the activity cursor against a list that grows at the top.

        The cursor is a position, and new events arrive at position 0, so
        without this the selection walks backwards one row per event and an
        expanded view silently swaps to a different tool call while you are
        reading it. The id is the store's primary key, so it survives.

        `act_follow` keeps the default behaviour useful: before you move,
        the cursor tracks the newest event, which is what "what is it doing"
        means. The first [j]/[k] pins it."""
        if not ev:
            self.act_cursor, self.act_sel = 0, ""
            return
        if self.act_follow:
            self.act_cursor, self.act_sel = 0, ev[0]["id"]
            return
        idx = next((i for i, e in enumerate(ev)
                    if e["id"] == self.act_sel), None)
        if idx is not None:
            self.act_cursor = idx
        else:
            # The selected event rolled out of the window entirely.
            self.act_cursor = max(0, min(self.act_cursor, len(ev) - 1))
            self.act_sel = ev[self.act_cursor]["id"]

    def graded_rows(self):
        """Graded results in the order the table shows them."""
        def verdict_rank(r):
            ung = any(c.get("name") == "adapter" and c.get("status") != "pass"
                      for c in r["checks"])
            return (0 if ung else 1 if r["all_pass"] else 2)

        mode = GRADED_SORTS[self.graded_sort]
        keys = {
            "recent": lambda r: r.get("_seq", 0),
            "verdict": lambda r: (verdict_rank(r), r.get("_seq", 0)),
            "tok": lambda r: (r.get("metrics") or {}).get("tok_in_billed", 0),
            "dur": lambda r: r.get("duration_s", 0),
        }
        return sorted(self.rows, key=keys[mode], reverse=self.graded_desc)

    def sum_cells(self):
        """Per-(arm, scenario) cells in the order the table shows them."""
        cs = sd.load_cells(paths=self.files)
        mode = SUM_SORTS[self.sum_sort]
        keys = {
            "tag": lambda c: c["tag"],
            "pass": lambda c: (c["pass_rate"], c["tag"]),
            "tok": lambda c: c["tok"],
            "dur": lambda c: c["dur_s"],
            "left": lambda c: c["dur_s"] * c["trials"],
        }
        return sorted(cs, key=keys[mode], reverse=self.sum_desc)

    def view_result_detail(self, r, max_y, max_x):
        """One graded trial: every check with its evidence.

        The graded table can only fit check *names*; the evidence is where
        a fail is actually explained -- which test command ran, what it
        printed, which files the agent touched against the real diff."""
        C = self.curses
        m = r.get("metrics") or {}
        self._put(3, 1, f"{r['scenario']}   arm {r.get('arm', '-')}   "
                        f"trial {r['trial']}", C.A_BOLD)
        ung = any(c.get("name") == "adapter" and c.get("status") != "pass"
                  for c in r["checks"])
        verdict = ("ungradeable — harness fault, excluded from pass rates"
                   if ung else "pass" if r["all_pass"] else "fail")
        self._put(4, 1, verdict, C.color_pair(3) if ung
                  else C.color_pair(1) if r["all_pass"] else C.color_pair(4))
        facts = [
            ("duration", sd.fmt_duration(r.get("duration_s", 0))),
            ("calls", f"{m.get('calls', 0)}  "
                      f"({m.get('subagent_calls', 0)} in subagents)"),
            ("tok_in billed", f"{m.get('tok_in_billed', 0):,}"),
            ("tok_in marginal", f"{m.get('tok_in_marginal', 0):,}"),
            ("first call input", f"{m.get('first_call_input', 0):,}"),
            ("probes to 1st edit", f"{m.get('probes_to_first_edit', 0)}"),
            ("first edit", str(m.get("first_edit") or "—")[:max_x - 32]),
            ("purpose", r.get("purpose", "(unlabelled)")),
            ("schema errors", ", ".join(r.get("schema_errors") or []) or "none"),
        ]
        y = 6
        for k, v in facts:
            self._put(y, 3, f"{k:<22}", C.A_DIM)
            self._put(y, 26, v)
            y += 1
        y += 1
        self._put(y, 1, "checks", C.A_BOLD)
        y += 1
        for c in r["checks"]:
            if y >= max_y - 2:
                break
            col = (C.color_pair(1) if c["status"] == "pass" else
                   C.color_pair(4) if c["status"] == "fail" else C.A_DIM)
            self._put(y, 3, f"{c['status']:<12}", col)
            self._put(y, 16, c["name"])
            y += 1
            ev = " ".join(str(c.get("evidence", "")).split())
            width = max_x - 22
            while ev and y < max_y - 2:
                self._put(y, 20, ev[:width], C.A_DIM)
                ev = ev[width:]
                y += 1

    def view_event(self, r, max_y, max_x):
        """One activity event, full pane, structure intact.

        Wraps long lines but keeps the author's own line breaks and their
        leading indentation: a continuation is indented past the original
        so a wrapped line is never mistaken for a new one. JSON arrives
        pre-indented from live._pretty."""
        C = self.curses
        try:
            ev = lv.activity(Path(r["out_dir"]))[::-1]
        except Exception:
            ev = []
        if not ev:
            self._put(3, 3, "event no longer available", C.A_DIM)
            return
        self._anchor_act(ev)
        e = ev[self.act_cursor]

        head = (f"#{e['n']}  {e['name']} {e['target']}"
                if e["kind"] == "tool" else f"#{e['n']}  message")
        self._put(3, 1, head[:max_x - 2],
                  C.color_pair(4) if e["failed"] else C.A_BOLD)
        meta = (f"{r['scenario']} {r['arm']}/{r['trial']}   {e['who']}"
                f"   {e['status']}   [j/k] scroll  [space] back")
        self._put(4, 1, meta[:max_x - 2], C.A_DIM)

        width = max_x - 6
        lines = []
        for raw in e["text"].splitlines():
            indent = len(raw) - len(raw.lstrip())
            pad = " " * min(indent, 12)
            body = raw.strip()
            if not body:
                lines.append("")
                continue
            cur = pad
            for word in body.split():
                if len(cur) + len(word) + 1 > width and cur.strip():
                    lines.append(cur)
                    cur = pad + "  " + word      # continuation, indented
                else:
                    cur = f"{cur} {word}" if cur.strip() else pad + word
            if cur.strip():
                lines.append(cur)

        y0 = 6
        rows = max_y - y0 - 2
        self.act_scroll = max(0, min(self.act_scroll,
                                     max(0, len(lines) - rows)))
        for i, ln in enumerate(lines[self.act_scroll:self.act_scroll + rows]):
            self._put(y0 + i, 3, ln[:width],
                      C.color_pair(4) if e["failed"] else 0)
        self.scrollbar(y0, rows, max_x - 2, len(lines), self.act_scroll)
        pos = f"{self.act_scroll + 1}-{min(self.act_scroll + rows, len(lines))}" \
              f"/{len(lines)} lines"
        self._put(max_y - 2, max(0, max_x - len(pos) - 3), pos, C.A_DIM)

    def task_rows(self):
        """Scenario descriptions, filtered by the same grammar as cells."""
        if self._tasks is None:
            try:
                self._tasks = tk.load()
            except Exception:
                self._tasks = []
        rows = [t for t in self._tasks
                if sd.matches(t["id"], self.pattern)]
        mode = TASK_SORTS[self.task_sort]
        keys = {
            "id": lambda t: t["id"],
            # cli-graded first when descending: it is the exception and the
            # thing worth finding.
            "grader": lambda t: (t["grader"], t["id"]),
            "files": lambda t: (len(t["code_files"]), t["id"]),
            "dirs": lambda t: (len(t["dirs"]), t["id"]),
            "pr": lambda t: (str(t["pr"]), t["id"]),
        }
        return sorted(rows, key=keys[mode], reverse=self.task_desc)

    def view_tasks(self, rows, body, max_x):
        C = self.curses
        if not rows:
            self._put(3, 3, "no scenarios found", C.A_DIM)
            return 0, 0
        s = tk.summarize(rows)
        self._put(3, 1, f"{s['n']} scenarios — {s['unit']} judged by the "
                        f"PR's unit tests, {s['cli']} at the command line, "
                        f"{s['synthetic']} synthetic", C.A_BOLD)
        self._put(4, 1, f"{'scenario':<20}{'PR':>7}{'grader':>8}{'files':>7}"
                        f"{'dirs':>6}  what it asks for", C.A_DIM)
        avail = max(1, body - 3)
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + avail:
            self.scroll = self.cursor - avail + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - avail)))
        for i, t in enumerate(rows[self.scroll:self.scroll + avail]):
            y, idx = 5 + i, self.scroll + i
            base = C.color_pair(5) if idx == self.cursor else 0
            self._put(y, 1, f"{t['id'][:19]:<20}", base)
            self._put(y, 21, f"{str(t['pr'] or '—'):>7}", C.A_DIM)
            # cli-graded is the exception and worth spotting at a glance.
            self._put(y, 28, f"{t['grader'] or '—':>8}",
                      C.color_pair(3) if t["grader"] == "cli" else C.A_DIM)
            self._put(y, 36, f"{len(t['code_files']):>7}", C.A_DIM)
            self._put(y, 43, f"{len(t['dirs']):>6}", C.A_DIM)
            self._put(y, 51, t["title"][:max(0, max_x - 53)], base)
        self.scrollbar(5, avail, max_x - 2, len(rows), self.scroll)
        return len(rows), avail

    def view_task_detail(self, t, max_y, max_x):
        """One scenario: what it asks, where the answer lives, how it is
        judged, and what a verdict from that grader does and does not
        mean."""
        C = self.curses
        w = max_x - 4
        self._put(3, 1, f"{t['id']}"
                        + (f"   PR #{t['pr']}" if t["pr"] else "")
                        + f"   base {t['base_commit']}", C.A_BOLD)
        self._put(4, 1, t["title"][:w], 0)

        y = 6
        label, meaning = tk.GRADER_MEANING.get(
            t["grader"], ("(no PR grader)", "Synthetic scenario: its own "
                                            "grade.py decides."))
        self._put(y, 1, "judged by", C.A_DIM)
        self._put(y, 16, label,
                  C.color_pair(3) if t["grader"] == "cli" else 0)
        y += 1
        for ln in self._wrap(meaning, w - 16):
            if y >= max_y - 4:
                break
            self._put(y, 16, ln, C.A_DIM)
            y += 1
        if t["invented"]:
            self._put(y, 16, "forced by: " + ", ".join(t["invented"][:6]),
                      C.A_DIM)
            y += 1
        y += 1

        for label, value in (
            ("answer lives in", ", ".join(t["dirs"][:6]) or "—"),
            ("files the PR hit", f"{len(t['code_files'])}: "
                                 + ", ".join(t["code_files"][:4])),
            ("checked with", t["test_cmd"] or "CLI flag comparison"),
            ("timeout", f"{t['timeout']}s"),
        ):
            self._put(y, 1, f"{label:<15}", C.A_DIM)
            self._put(y, 16, str(value)[:w - 16])
            y += 1
        y += 1

        self._put(y, 1, f"prompt ({t['prompt_lines']} lines) — this is all "
                        f"the agent is given", C.A_BOLD)
        y += 1
        # Keep the author's line breaks; a PR body is headings and tables.
        lines = []
        for raw in t["prompt"].splitlines():
            lines += self._wrap(raw, w - 4) or [""]
        self._pane(lines, y, max_y, max_x)

    def _pane(self, lines, y0, max_y, max_x, x=3):
        """Draw a list of lines in the space that is left, with a scrollbar.

        Detail panes used to render until they ran out of screen and then
        simply stop -- on a short terminal the prompt was cut with nothing
        saying so and no way to see the rest."""
        rows = max(1, max_y - y0 - 2)
        self.pane_scroll = max(0, min(self.pane_scroll,
                                      max(0, len(lines) - rows)))
        for i, ln in enumerate(lines[self.pane_scroll:
                                     self.pane_scroll + rows]):
            self._put(y0 + i, x, ln[:max_x - x - 3], ln.attr
                      if hasattr(ln, "attr") else 0)
        if len(lines) > rows:
            self.scrollbar(y0, rows, max_x - 2, len(lines), self.pane_scroll)
            pos = (f"{self.pane_scroll + 1}-"
                   f"{min(self.pane_scroll + rows, len(lines))}"
                   f"/{len(lines)}   [j/k] scrolls")
            self._put(max_y - 2, max(0, max_x - len(pos) - 3), pos,
                      self.curses.A_DIM)

    def design_rows(self):
        if self._design is None:
            try:
                self._design = dz.load()
            except Exception:
                self._design = []
        mode = DESIGN_SORTS[self.design_sort]
        keys = {
            "arm": lambda r: r["arm"],
            "always": lambda r: r["always_bytes"],
            "ondemand": lambda r: r["ondemand_bytes"],
        }
        return sorted(self._design, key=keys[mode], reverse=self.design_desc)

    def view_design(self, rows, body, max_x):
        C = self.curses
        if not rows:
            self._put(3, 3, "no arms materialized — run `make trees` or "
                            "point ARMSDIR at a built arms directory",
                      C.A_DIM)
            return 0, 0
        d = dz.deltas(rows)
        self._put(3, 1, "arms — instruction surface measured off disk, not "
                        "described", C.A_BOLD)
        self._put(4, 1, f"{'arm':<5}{'name':<22}{'always':>9}{'vs a1':>9}"
                        f"{'on demand':>11}{'sha':>10}  role", C.A_DIM)
        avail = max(1, body - 3)
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        for i, r in enumerate(rows[:avail]):
            y = 5 + i
            base = C.color_pair(5) if i == self.cursor else 0
            self._put(y, 1, f"{r['arm']:<5}", base)
            self._put(y, 6, f"{r['name']:<22}", base)
            if not r["present"]:
                self._put(y, 28, f"{'not built':>9}", C.A_DIM)
                continue
            self._put(y, 28, f"{r['always_bytes']:>9,}", C.color_pair(2))
            self._put(y, 37, f"{d[r['arm']]:>+9,}", C.A_DIM)
            self._put(y, 46, f"{r['ondemand_bytes']:>11,}",
                      C.color_pair(3) if r["ondemand_bytes"] else C.A_DIM)
            self._put(y, 57, f"{r['sha8']:>10}", C.A_DIM)
            self._put(y, 69, r["role"][:max(0, max_x - 71)], C.A_DIM)
        y = 5 + min(len(rows), avail) + 1
        self._put(y, 1, "ground rules — [space] on an arm for its purpose "
                        "and files", C.A_BOLD)
        y += 1
        width = max(len(k) for k, _ in dz.GROUND_RULES) + 2
        for k, v in dz.GROUND_RULES:
            if y >= body + 2:
                break
            self._put(y, 3, k[:width - 1], 0)
            for ln in self._wrap(v, max_x - width - 6)[:1]:
                self._put(y, 3 + width, ln, C.A_DIM)
            y += 1
        return 0, 0

    def view_design_detail(self, r, max_y, max_x):
        C = self.curses
        self._put(3, 1, f"{r['arm']} — {r['name']}", C.A_BOLD)
        self._put(4, 1, r["role"], C.A_DIM)
        lines = []
        lines += self._wrap(r["purpose"], max_x - 8) + [""]
        lines.append(f"always loaded   {r['always_bytes']:,} B   "
                     f"{', '.join(r['always_files']) or '(none)'}")
        lines.append(f"on demand       {r['ondemand_bytes']:,} B   "
                     f"{len(r['ondemand_files'])} file(s)")
        if r["ondemand_files"]:
            lines += ["    " + f for f in r["ondemand_files"][:12]]
        lines.append(f"surface sha     {r['sha8'] or '—'}")
        lines += ["", "ground rules this run is bound by", ""]
        for k, v in dz.GROUND_RULES:
            lines.append(k)
            lines += ["    " + x for x in self._wrap(v, max_x - 12)]
            lines.append("")
        self._pane(lines, 6, max_y, max_x)

    @staticmethod
    def _wrap(text, width):
        out, cur = [], ""
        for word in str(text).split():
            if len(cur) + len(word) + 1 > width and cur:
                out.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            out.append(cur)
        return out

    def view_cells(self, cs, body, max_x):
        C = self.curses
        self._put(3, 1, f"{'arm/scenario':<20}{'trials':>7}{'pass@1':>8}"
                        f"{'±':>6}{'tok_in':>9}{'if pass':>9}{'calls':>7}"
                        f"{'probes':>8}{'redun':>7}{'abnd':>6}  failing",
                  C.A_DIM)
        avail = max(1, body - 2)
        self.cursor = max(0, min(self.cursor, max(0, len(cs) - 1)))
        self.scroll = max(0, min(self.scroll, max(0, len(cs) - avail)))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + avail:
            self.scroll = self.cursor - avail + 1
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
            self._put(3, 3, "no proxy log for this run.", C.A_DIM)
            self._put(5, 3, "The proxy now runs by default and writes "
                            "alongside the results — runs/probe.jsonl "
                            "pairs with", C.A_DIM)
            self._put(6, 3, "runs/probe.proxy.jsonl, which this view finds "
                            "on its own. A run started before that change, "
                            "or one", C.A_DIM)
            self._put(7, 3, "started with ADH_NO_PROXY=1, has no log to "
                            "find.", C.A_DIM)
            self._put(9, 3, "Without it the adapter's token counts are "
                            "unverified: design §3.2 makes the proxy "
                            "authoritative,", C.A_DIM)
            self._put(10, 3, "not the adapter, and the 2% H4 agreement "
                             "gate has nothing to compare.", C.A_DIM)
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
        self._put(6, 3, f"{'':<26}{'median':>14}{'mean':>14}{'p90':>14}"
                        f"{'p90/med':>10}", C.A_DIM)
        # median, mean and p90 side by side. The analysis compares medians;
        # the p90 column is what says whether the median is representative,
        # and the ratio is the fastest way to see a cell whose tail is
        # carrying the cost.
        dist = [
            ("tok_in", c["tok"], c["avg_tok"], c["p90_tok"], "{:,.0f}"),
            ("calls", c["calls"], c["avg_calls"], c["p90_calls"], "{:,.1f}"),
            ("tool calls", c["tools"], c["avg_tools"], c["p90_tools"],
             "{:,.1f}"),
            ("probes to first edit", c["probes"], c["avg_probes"],
             c["p90_probes"], "{:,.1f}"),
            ("duration s", c["dur_s"], c["avg_dur"], c["p90_dur"], "{:,.0f}"),
        ]
        y = 7
        for name, med, avg, p90, fmt in dist:
            ratio = (p90 / med) if med else 0
            self._put(y, 3, f"{name:<26}", C.A_DIM)
            self._put(y, 29, f"{fmt.format(med):>14}")
            self._put(y, 43, f"{fmt.format(avg):>14}", C.A_DIM)
            self._put(y, 57, f"{fmt.format(p90):>14}",
                      C.color_pair(4) if ratio >= 2 else 0)
            self._put(y, 71, f"{ratio:>9.1f}x" if ratio else f"{'—':>10}",
                      C.color_pair(4) if ratio >= 2 else C.A_DIM)
            y += 1
        y += 1
        lines = [
            ("trials", f"{c['trials']}"),
            ("pass@1", f"{c['pass_rate']:.0f}%  (± {c['spread']:.0f})"),
            ("median tok_in | passed", f"{c['tok_won']:,.0f}"),
            ("median tok_in | worked", f"{c['tok_worked']:,.0f}"
                                       + ("   (abandoned trials excluded — "
                                          "they never pass and spend a "
                                          "fraction of a real attempt)"
                                          if c["abandoned"] else "")),
            ("ungradeable (harness)", f"{c['ungradeable']}/{c['trials']}"),
            ("redundant reads", f"{c['redundant']:.0f}"),
            ("subagents dispatched", f"{c['n_subagents']:.0f} "
                                     f"(median), {c['subagent_calls']} "
                                     f"child calls total"),
            ("subagent tok_in", f"{c['subagent_tok']:,.0f} (median)"),
            ("abandoned trials", f"{c['abandoned']}/{c['trials']}"),
            ("failing checks", ", ".join(c["fails"]) or "—"),
        ]
        for i, (k, v) in enumerate(lines):
            self._put(y + i, 3, f"{k:<26}", C.A_DIM)
            self._put(y + i, 29, v)
        y = y + len(lines) + 1
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

        # One rule everywhere: [q]/[esc] back out one level, [Q] quits from
        # any depth. `q` used to mean both, so from inside a nested view
        # there was no way to leave without pressing it repeatedly and no
        # way to know whether the next press would exit the program.
        # [Q] quit from anywhere · [q] back one level · [esc] back to the
        # root list regardless of depth. q alone used to mean both "back"
        # and "quit", so from a nested view there was no way to leave
        # without pressing it repeatedly and no way to know which press
        # would exit the program.
        if key == ord("Q"):
            return True
        if key == 27:
            if self.detail or self.act_open:
                self.detail = self.act_open = False
                return False
            return True
        if key == ord("q"):
            if self.detail:
                self.detail = False
                return False
            return True
        # The live view nests: run list -> run detail with an activity
        # list -> one expanded event. Each level consumes its own keys so
        # [q] backs out one step instead of quitting from three levels deep.
        if VIEWS[self.view] == "live" and not self.detail:
            if key in (ord("l"), C.KEY_RIGHT):
                self.live_section = (self.live_section + 1) % 3
                return False
            if key in (ord("h"), C.KEY_LEFT):
                self.live_section = (self.live_section - 1) % 3
                return False
            if key in (ord("j"), C.KEY_DOWN, ord("k"), C.KEY_UP):
                step = 1 if key in (ord("j"), C.KEY_DOWN) else -1
                self._move_live(step)
                return False
            if key in (ord(" "), 10, 13):
                # Each table opens its own kind of detail: a live run, a
                # graded result, or the cell's distribution. Left/right at
                # this level move between sections, so descending is
                # [space]/[enter] only -- binding right to both would make
                # section-switching unreachable.
                if self.live_section == 0 and self.live:
                    self.detail = True
                    self.act_cursor = self.act_scroll = 0
                    self.act_sel, self.act_follow = "", True
                    self.act_open = False
                elif self.live_section == 1 and self.cells or self.live_section == 2 and self.rows:
                    self.detail = True
                return False
        # Only the running section has an activity list under it. Letting
        # this block run for the graded and summary details created a level
        # with nothing in it: [space] appeared to do nothing and then took
        # two backs to leave.
        if (VIEWS[self.view] == "live" and self.detail
                and self.live_section != 0):
            if key == ord("Q"):
                return True
            if key in (ord(" "), 10, 13, ord("q"), 27, C.KEY_LEFT):
                self.detail = False
                return False
            return False
        if VIEWS[self.view] == "live" and self.detail:
            if self.act_open:
                if key == ord("Q"):
                    return True          # Q quits from any depth
                if key == 27:            # esc: all the way out
                    self.act_open = self.detail = False
                    self.act_scroll = 0
                    return False
                if key == C.KEY_LEFT:    # left: up one level
                    self.act_open = False
                    self.act_scroll = 0
                    return False
                if key in (ord(" "), 10, 13, ord("q")):
                    self.act_open = False
                    self.act_scroll = 0
                elif key in (ord("j"), C.KEY_DOWN):
                    self.act_scroll += 1
                elif key in (ord("k"), C.KEY_UP):
                    self.act_scroll = max(0, self.act_scroll - 1)
                elif key == C.KEY_NPAGE:
                    self.act_scroll += 15
                elif key == C.KEY_PPAGE:
                    self.act_scroll = max(0, self.act_scroll - 15)
                return False
            if key in (ord("j"), C.KEY_DOWN, ord("k"), C.KEY_UP,
                       C.KEY_NPAGE, C.KEY_PPAGE):
                step = {ord("j"): 1, C.KEY_DOWN: 1, ord("k"): -1,
                        C.KEY_UP: -1, C.KEY_NPAGE: 15,
                        C.KEY_PPAGE: -15}[key]
                # Any deliberate move pins the selection; until then it
                # follows the newest event.
                self.act_follow = False
                self.act_cursor = max(0, self.act_cursor + step)
                self.act_sel = ""        # resolved from the index next draw
                return False
            if key in (ord(" "), 10, 13, C.KEY_RIGHT):
                # Expanding pins the selection unconditionally. Reading one
                # event while the list follows the newest is how the thing
                # you opened disappears mid-sentence.
                self.act_open, self.act_scroll = True, 0
                self.act_follow = False
                return False
            if key == C.KEY_LEFT:        # left: back to the run list
                self.detail = False
                self.act_cursor = self.act_scroll = 0
                return False
            if key == ord("Q"):
                return True
            if key in (ord("q"), 27):    # one level == root from here
                self.detail = False
                self.act_cursor = self.act_scroll = 0
                return False
        if key == 9:                                  # tab: next view
            self.view = (self.view + 1) % len(VIEWS)
            self.scroll = self.cursor = 0
        elif key in (C.KEY_BTAB, 353):                # shift-tab: previous
            # 353 is the literal code some terminals send when terminfo
            # carries no kcbt entry, so both are accepted rather than
            # leaving the key dead on a terminal that reports it raw.
            self.view = (self.view - 1) % len(VIEWS)
            self.scroll = self.cursor = 0
        elif key == ord("S"):
            if VIEWS[self.view] == "tasks":
                self.task_desc = not self.task_desc
            elif VIEWS[self.view] == "design":
                self.design_desc = not self.design_desc
            elif VIEWS[self.view] == "live" and self.live_section == 2:
                self.graded_desc = not self.graded_desc
            elif VIEWS[self.view] == "live" and self.live_section == 1:
                self.sum_desc = not self.sum_desc
        elif key == ord("s"):
            if VIEWS[self.view] == "tasks":
                self.task_sort = (self.task_sort + 1) % len(TASK_SORTS)
                self.cursor = self.scroll = 0
            elif VIEWS[self.view] == "design":
                self.design_sort = (self.design_sort + 1) % len(DESIGN_SORTS)
                self.cursor = self.scroll = 0
            elif VIEWS[self.view] == "live" and self.live_section == 2:
                self.graded_sort = (self.graded_sort + 1) % len(GRADED_SORTS)
                self.graded_scroll = 0
            elif VIEWS[self.view] == "live" and self.live_section == 1:
                self.sum_sort = (self.sum_sort + 1) % len(SUM_SORTS)
                self.sum_scroll = 0
            else:
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
            self.pane_scroll = 0
            if self.detail:
                # Open on the newest event; that is what "what is it doing"
                # means for a run still in flight.
                self.act_cursor = self.act_scroll = 0
                self.act_sel, self.act_follow = "", True
                self.act_open = False
        elif key in (ord("j"), C.KEY_DOWN):
            if VIEWS[self.view] == "live":
                self.live_cursor += 1
                self._anchor_live()
            elif self.detail and VIEWS[self.view] in ("tasks", "design"):
                self.pane_scroll += 1
            else:
                self._move_rows(1)
        elif key in (ord("k"), C.KEY_UP):
            if VIEWS[self.view] == "live":
                self.live_cursor = max(0, self.live_cursor - 1)
                self._anchor_live()
            elif self.detail and VIEWS[self.view] in ("tasks", "design"):
                self.pane_scroll = max(0, self.pane_scroll - 1)
            else:
                self._move_rows(-1)
        elif key == C.KEY_NPAGE:
            self._move_rows(10)
        elif key == C.KEY_PPAGE:
            self._move_rows(-10)
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
    # Find the run's own proxy log rather than making someone name it.
    if not proxy:
        proxy = sd.paired_proxy_log(files) or None
    if not files and not sd.load_rows():
        print("no results found — run the suite first "
              "(make all), or pass FILES=<results.jsonl>", file=sys.stderr)
        return 1
    curses_main(lambda scr: SuiteTui(scr, pattern, ref, proxy, files, expect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
