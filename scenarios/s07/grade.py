import re

from adherence.gradelib import bad, check_field_lines, ok


def grade(sandbox, transcript, final):
    checks = check_field_lines(final, ["service", "state", "uptime_days"])
    expect = {"service": "widgetd", "state": "running", "uptime_days": "12"}
    for k, v in expect.items():
        if re.search(rf"^{k}\s*:\s*\"?{v}\"?\s*$", final, re.M):
            checks.append(ok(f"value.{k}", f"{k}: {v}"))
        else:
            checks.append(bad(f"value.{k}", f"expected {k}: {v}"))
    return checks
