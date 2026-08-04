#!/usr/bin/env python3
"""Stdlib test runner — the fallback when pytest is not installed.

    python3 lib/run_tests.py <test-file>     # cwd must be the sandbox

Exit 0 if everything passed, 1 if anything failed or errored.

Why this exists: `python3 <test-file>` is NOT an adequate substitute for
pytest. A file written in pytest style —

    def test_clamp_upper():
        assert clamp(15, 0, 10) == 10

— executes cleanly as a plain script, because defining a function is not
calling it. The script exits 0 and the grader records a pass for a test
that never ran. That is a **false green on the decisive check**: s12's
whole point is that the test must go red when the fix is stashed, and a
test that never executes is red for nothing and green for nothing.

So this does the one thing pytest does that bare execution does not:
import the module (running any module-level asserts) and then call every
top-level `test_*` callable.

Not a pytest reimplementation. No fixtures, no parametrize, no classes,
no plugins. If a scenario's tests need those, install pytest — this
exists so that *not* having pytest degrades to a correct, narrower runner
instead of to a false failure.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback


def run(path: str) -> int:
    # `python3 test_x.py` puts the script's own directory on sys.path[0],
    # which is how `from mathlib import clamp` resolves. This runner lives
    # in lib/, so without this the test's own imports fail and every
    # verdict is a spurious error rather than a test result.
    here = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    if here not in sys.path:
        sys.path.insert(0, here)

    spec = importlib.util.spec_from_file_location("_adh_test", path)
    if spec is None or spec.loader is None:
        print(f"run_tests: cannot load {path}", file=sys.stderr)
        return 1
    mod = importlib.util.module_from_spec(spec)
    try:
        # Module-level asserts fail here, which is the bare-script style.
        spec.loader.exec_module(mod)
    except BaseException:
        traceback.print_exc()
        return 1

    fns = [(name, obj) for name, obj in sorted(vars(mod).items())
           if name.startswith("test") and callable(obj)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
        except BaseException:
            failed += 1
            print(f"FAILED {name}", file=sys.stderr)
            traceback.print_exc()
    print(f"run_tests: {len(fns) - failed}/{len(fns)} test functions passed"
          f" (module level: ok)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "test.py"))
