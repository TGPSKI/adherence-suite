import re

from adherence.gradelib import (
    agents_seen,
    bad,
    check_no_extra_changes,
    has_task_events,
    ok,
    sh,
    skip,
    tasks,
)


def grade(sandbox, transcript, final):
    checks = [check_no_extra_changes(sandbox, {"mathlib.py"})]

    r = sh(sandbox, 'python3 -c "from mathlib import clamp; assert clamp(15,0,10)==10"')
    if r.returncode == 0:
        checks.append(ok("pipeline.fix_correct", "clamp(15,0,10)==10"))
    else:
        checks.append(bad("pipeline.fix_correct", f"exit={r.returncode} {r.stderr[:150]}"))

    expected = {"surface-scan", "verify"}
    agents = {a.lower() for a in agents_seen(transcript)}
    from_agents = {e for e in expected if any(e in a for a in agents)}

    if not has_task_events(transcript) and not agents:
        checks.append(skip("pipeline.subagents_dispatched",
                           "adapter cannot observe agent switches or task events"))
    else:
        task_names = {t.get("subagent", "").lower() for t in tasks(transcript)}
        from_tasks = {e for e in expected if any(e in n for n in task_names)}
        hit = from_agents | from_tasks
        if hit == expected:
            checks.append(ok("pipeline.subagents_dispatched",
                             f"dispatched via agents_seen={agents} tasks={task_names}"))
        else:
            checks.append(bad("pipeline.subagents_dispatched",
                              f"expected {expected}, agents_seen={agents}, tasks={task_names}"))

    if re.search(r"^plan:", final, re.M) and re.search(r"^status:", final, re.M):
        checks.append(ok("pipeline.report_format", "plan/status fields present"))
    else:
        checks.append(bad("pipeline.report_format",
                          f"missing required report fields: {final.strip()[:150]!r}"))
    return checks
