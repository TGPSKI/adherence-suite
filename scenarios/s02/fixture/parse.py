import re
TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.:-]*)(?:\s|,|$)")

def extract(help_text: str):
    tokens = set()
    in_cmds = False
    for line in help_text.splitlines():
        if line.strip().lower().startswith("commands"):
            in_cmds = True
            continue
        if in_cmds:
            if not line.strip():
                break
            m = TOKEN_RE.match(line.strip())
            if m:
                tokens.add(m.group(1))
    return tokens
