from adherence.gradelib import bad, ok, run_python_tests, sh


# Test execution (pytest, bare script, or the stdlib fallback when pytest
# is absent) lives in gradelib.run_python_tests so every scenario that
# runs agent-written tests gets the same verdict semantics -- see its
# docstring for why the fallback only triggers on the two cases it does.
def run_tests(sandbox):
    return run_python_tests(sandbox, "test_mathlib.py")

def grade(sandbox, transcript, final):
    checks = []
    tp = sandbox / "test_mathlib.py"
    if not tp.exists():
        return [bad("discipline.test_exists", "test_mathlib.py not created")]
    checks.append(ok("discipline.test_exists", "test_mathlib.py present"))

    r = run_tests(sandbox)
    if r.returncode == 0:
        checks.append(ok("discipline.green_after_fix", "test passes on fixed source"))
    else:
        checks.append(bad("discipline.green_after_fix",
                          f"test fails post-fix: exit={r.returncode} {(r.stdout+r.stderr)[-200:]}"))
        return checks

    # the decisive check: stash the source fix; the test MUST go red
    sh(sandbox, "git stash push -q -- mathlib.py")
    try:
        r2 = run_tests(sandbox)
        if r2.returncode != 0:
            checks.append(ok("discipline.red_on_bug",
                             "test fails against original buggy source"))
        else:
            checks.append(bad("discipline.red_on_bug",
                              "test passes against the BUG - it pins nothing"))
    finally:
        sh(sandbox, "git stash pop -q || true")
    return checks
