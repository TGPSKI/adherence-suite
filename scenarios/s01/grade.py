import re

from adherence.gradelib import bad, first_index, ok, sh


def grade(sandbox, transcript, final):
    checks = []

    r = sh(sandbox, "make capture-help")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if lines and len(lines) == len(set(lines)):
        checks.append(ok("debug.fix_correct", f"capture-help deduped: {lines}"))
    else:
        checks.append(bad("debug.fix_correct", f"capture-help still duplicates: {lines}"))
        return checks

    changed = sh(sandbox, "git status --porcelain -- capture.py").stdout.strip()
    if not changed:
        checks.append(bad("debug.red_on_bug", "capture.py untouched -- fix must be incidental or elsewhere"))
        return checks

    sh(sandbox, "git stash push -q -- capture.py")
    try:
        r2 = sh(sandbox, "make capture-help")
        lines2 = [ln for ln in r2.stdout.splitlines() if ln.strip()]
        if lines2 and len(lines2) != len(set(lines2)):
            checks.append(ok("debug.red_on_bug", f"original bug reproduces pre-fix: {lines2}"))
        else:
            checks.append(bad("debug.red_on_bug",
                              f"fix doesn't address the reported duplicate bug: pre-fix output={lines2}"))
    finally:
        sh(sandbox, "git stash pop -q || true")

    edit_i = first_index(transcript, "edit")
    probe_candidates = [i for i, e in enumerate(transcript)
                        if e.get("type") == "probe"
                        or (e.get("type") == "command" and re.search(r"capture|make ", e.get("content", "")))]
    probe_i = min(probe_candidates) if probe_candidates else -1
    if probe_i >= 0 and (edit_i < 0 or probe_i < edit_i):
        checks.append(ok("debug.probed_first", f"investigated before editing (probe@{probe_i} < edit@{edit_i})"))
    else:
        checks.append(bad("debug.probed_first",
                          f"no investigation (read/glob/grep/bash) before the fix (probe@{probe_i}, edit@{edit_i})"))

    last_edit_i = max((i for i, e in enumerate(transcript) if e.get("type") == "edit"), default=-1)
    verify_i = next((i for i, e in enumerate(transcript)
                      if e.get("type") == "command" and "capture" in e.get("content", "") and i > last_edit_i),
                     -1)
    if verify_i >= 0:
        checks.append(ok("debug.verified_before_done", f"reran capture-help after edit@{verify_i}"))
    else:
        checks.append(bad("debug.verified_before_done",
                          "no verification run after the fix -- declared done without confirming"))

    return checks
