import re, sys

TOKEN_RE = re.compile(r"^\s{0,8}([A-Za-z][A-Za-z0-9_.:-]*)(?:\s|,|$)")


def extract_targets(lines):
    results = []
    for line in lines:
        seen = set()
        m = TOKEN_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            results.append(name)
    return results


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "raw_targets.txt"
    with open(path) as f:
        lines = f.readlines()
    for name in extract_targets(lines):
        print(name)


if __name__ == "__main__":
    main()
