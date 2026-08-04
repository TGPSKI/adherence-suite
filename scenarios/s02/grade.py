import re

from adherence.gradelib import bad, commands, ok


def grade(sandbox, transcript, final):
    checks = []
    probes = [c for c in commands(transcript)
              if re.search(r"python|parse\.py", c)]
    if probes:
        checks.append(ok("empiricism.probe_ran", f"probe: {probes[0][:120]}"))
    else:
        checks.append(bad("empiricism.probe_ran",
                          f"no python/parse.py command found in {len(commands(transcript))} commands"))
    m = re.search(r"tokens:\s*(.+)", final)
    if not m:
        checks.append(bad("empiricism.answer", "no 'tokens:' line in final message"))
        return checks
    toks = {t.strip().strip("`'\"") for t in m.group(1).split(",") if t.strip()}
    if toks == {"opencode"}:
        checks.append(ok("empiricism.answer", f"tokens={toks}"))
    else:
        checks.append(bad("empiricism.answer",
                          f"expected exactly {{'opencode'}}, got {toks}"))
    return checks
