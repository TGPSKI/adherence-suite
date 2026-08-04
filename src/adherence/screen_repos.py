#!/usr/bin/env python3
"""tools/screen_repos.py — E1: mechanical fixture screening (design §8.1).

    tools/screen_repos.py [--candidates FILE] [--out docs/SCREENING.md]

Scores candidate repositories against the criteria that were committed
*before* any of them were looked at (§12 confound 13), and writes a
rejection log naming which criterion each failure hit. Every number comes
from the GitHub API via `gh`, so the screen is reproducible and not a
matter of taste.

Disqualifying, all mechanical:

- **permissive license** — MIT / Apache-2.0 / BSD / ISC / MPL-2.0.
  Copyleft is rejected, not negotiated.
- **>= 30 merged PRs after the assistant's training cutoff (2026-05)** —
  §8.3: post-cutoff PRs are the only ones usable for the primary result,
  and a repo that cannot supply ~40 to sample from cannot supply ~10 that
  survive the calibration gate.
- **>= 4 subsystems with differing conventions** — approximated by
  top-level source directories, then confirmed by eye before vendoring.

Two criteria are deliberately NOT decided here, because no API answers
them: hermetic **offline** build with a pre-warmed cache, and a
deterministic test subset under 5 minutes. Those are E2's gate, and §13
is explicit that they must *veto* a fixture rather than negotiate the
sandbox open. A repo passing this screen is a candidate, not a fixture.

Preferred (scored, never disqualifying): ships a maintainer-written
instruction file — that file becomes arm A1 verbatim (§4) and a repo
without one forfeits the practical baseline; ships CODEOWNERS; mixes
languages or code/docs/infra.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CUTOFF = "2026-05-01"          # assistant training cutoff, §8.3
MIN_POST_CUTOFF_PRS = 30
MIN_SUBSYSTEMS = 4
PERMISSIVE = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc",
              "mpl-2.0", "0bsd", "unlicense", "zlib"}

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules",
                     ".github/copilot-instructions.md")
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")

# Directories that are never a "subsystem with its own conventions".
NOT_SUBSYSTEM = {
    ".git", ".github", ".gitignore", ".idea", ".vscode", "vendor",
    "node_modules", "third_party", "dist", "build", "target", "LICENSE",
    "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    "SECURITY.md", "assets", "images", "logos", "hack",
}

CANDIDATES = [
    # Go — the strongest offline story (GOMODCACHE / `go mod vendor`)
    "cli/cli", "gohugoio/hugo", "rclone/rclone", "goreleaser/goreleaser",
    "grafana/loki", "prometheus/prometheus", "etcd-io/etcd",
    "open-telemetry/opentelemetry-collector", "argoproj/argo-cd",
    "tailscale/tailscale", "traefik/traefik", "minio/minio",
    # Python
    "pydantic/pydantic", "python-poetry/poetry", "encode/httpx",
    "dbt-labs/dbt-core", "mlflow/mlflow", "django/django",
    # Rust
    "astral-sh/uv", "astral-sh/ruff", "nushell/nushell", "starship/starship",
    # TS / mixed
    "vitejs/vite", "prettier/prettier",
]


def gh(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["gh"] + args, capture_output=True, text=True)
    return p.returncode, (p.stdout or p.stderr)


def gh_json(path: str, *extra):
    rc, out = gh(["api", path, *extra])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def post_cutoff_prs(repo: str) -> int:
    d = gh_json(f"search/issues?q=repo:{repo}+is:pr+is:merged+"
                f"merged:>={CUTOFF}&per_page=1")
    return (d or {}).get("total_count", -1)


def top_level(repo: str) -> list[str]:
    d = gh_json(f"repos/{repo}/contents/")
    if not isinstance(d, list):
        return []
    return [e["name"] for e in d
            if e.get("type") == "dir" and e["name"] not in NOT_SUBSYSTEM
            and not e["name"].startswith(".")]


def has_path(repo: str, path: str) -> bool:
    rc, _ = gh(["api", f"repos/{repo}/contents/{path}", "--silent"])
    return rc == 0


# Directories whose presence means a repository can be graded on observed
# BEHAVIOUR rather than on internal API. This is the single most valuable
# thing the screen can look for, and it was not looked for: on cli/cli, 30
# of 34 extracted tasks were unit-test-only and none acceptance-only, which
# left no way to grade a feature PR except by comparing CLI flags -- and
# that ceilings at 100%, so it discriminates between arms not at all.
BEHAVIOURAL_DIRS = ("acceptance", "e2e", "integration", "features",
                    "functional", "system-test", "tests/e2e")

# Title prefixes that mark a bug fix. Fixes are worth far more than
# features here: their tests call API that already EXISTS, so an agent that
# implements the behaviour passes whatever it names its internals. A
# feature PR's tests name what the PR adds, which no agent can be expected
# to guess -- 18 of 34 cli/cli tasks landed in that bucket.
FIX_PREFIXES = ("fix", "bug", "bugfix", "hotfix", "patch")
FEATURE_PREFIXES = ("feat", "feature", "add")

# A PR this large costs hours per trial and, measured, does not
# discriminate: cli-cli-13057 spans 13 files, takes 45 minutes, times out
# on 3 of 5 trials and passes both times it finishes.
BIG_PR_FILES = 8


def pr_sample(repo: str, limit: int = 60) -> list[dict]:
    """Recent merged post-cutoff PRs with the fields the new checks need."""
    code, out = gh([
        "pr", "list", "--repo", repo, "--state", "merged", "--limit",
        str(limit), "--json", "title,files,mergedAt,labels"])
    if code != 0 or not out.strip():
        return []
    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [p for p in prs if (p.get("mergedAt") or "") >= CUTOFF]


def _bucket(files: list[str]) -> str:
    """What a PR actually changes. A raw merge count is not supply:
    measured on cli/cli, 174 post-cutoff merges contained 32 usable tasks,
    because 47.7% were CI and agent-config and 24.1% dependency bumps."""
    if not files:
        return "empty"
    if all(f.startswith(".github/") or "workflow" in f or "/skills/" in f
           for f in files):
        return "agent-config"
    if all(f.endswith((".mod", ".sum", ".lock", ".toml", ".json"))
           or "vendor/" in f for f in files):
        return "deps"
    src = [f for f in files if f.endswith((".go", ".rs", ".py", ".ts", ".js"))]
    if not src:
        return "docs-other"
    tests = [f for f in src if "_test." in f or f.startswith("tests/")
             or "/test" in f]
    return "source+tests" if tests else "source-no-tests"


def task_supply(repo: str) -> dict:
    """The checks this session's failures argued for, all from the API."""
    prs = pr_sample(repo)
    if not prs:
        return {"sampled": 0}
    buckets, fixes, feats, big, behavioural = {}, 0, 0, 0, 0
    sizes = []
    for pr in prs:
        files = [f.get("path", "") for f in (pr.get("files") or [])]
        b = _bucket(files)
        buckets[b] = buckets.get(b, 0) + 1
        title = (pr.get("title") or "").lower()
        head = title.split(":")[0].split("(")[0].strip()
        if head in FIX_PREFIXES or title.startswith("fix "):
            fixes += 1
        elif head in FEATURE_PREFIXES:
            feats += 1
        if b == "source+tests":
            sizes.append(len(files))
            if len(files) > BIG_PR_FILES:
                big += 1
            if any(f.startswith(BEHAVIOURAL_DIRS) or "/e2e/" in f
                   or "/acceptance/" in f for f in files):
                behavioural += 1
    usable = buckets.get("source+tests", 0)
    sizes.sort()
    return {
        "sampled": len(prs),
        "buckets": buckets,
        "usable": usable,
        "usable_rate": round(usable / len(prs), 3),
        "fix_prs": fixes,
        "feature_prs": feats,
        # Titles are the only cheap signal, and most repositories do not
        # use conventional prefixes -- on cli/cli just 3 of 60 were
        # classifiable, so a fix_rate of 1.0 there rests on three PRs.
        # The denominator is reported so the rate is never read as
        # stronger than its sample.
        "fix_classifiable": fixes + feats,
        "fix_rate": (round(fixes / (fixes + feats), 3)
                     if fixes + feats >= 8 else None),
        "behavioural_tests": behavioural,
        "behavioural_rate": round(behavioural / max(1, usable), 3),
        "median_files": sizes[len(sizes) // 2] if sizes else 0,
        "oversized": big,
    }


def screen(repo: str) -> dict:
    meta = gh_json(f"repos/{repo}")
    if not meta:
        return {"repo": repo, "error": "not reachable via gh api"}
    lic = ((meta.get("license") or {}).get("key") or "none").lower()
    dirs = top_level(repo)
    langs = gh_json(f"repos/{repo}/languages") or {}
    instr = [f for f in INSTRUCTION_FILES if has_path(repo, f)]
    owners = any(has_path(repo, p) for p in CODEOWNERS_PATHS)
    prs = post_cutoff_prs(repo)
    supply = task_supply(repo)
    has_behavioural = any(has_path(repo, d) for d in BEHAVIOURAL_DIRS)

    fails = []
    if lic not in PERMISSIVE:
        fails.append(f"license={lic} not permissive")
    if prs < MIN_POST_CUTOFF_PRS:
        fails.append(f"only {prs} merged PRs since {CUTOFF} "
                     f"(need {MIN_POST_CUTOFF_PRS})")
    if len(dirs) < MIN_SUBSYSTEMS:
        fails.append(f"only {len(dirs)} top-level subsystems "
                     f"(need {MIN_SUBSYSTEMS})")
    # Raw merge count is not supply. Applied to the sample rather than the
    # whole history, so it is an estimate and says so.
    if supply.get("sampled") and supply["usable"] < 6:
        fails.append(
            f"only {supply['usable']}/{supply['sampled']} sampled PRs are "
            f"source+tests ({supply['usable_rate']:.0%}); at that rate "
            f"{prs} merges yield about {int(prs * supply['usable_rate'])} "
            f"tasks")

    total_bytes = sum(langs.values()) or 1
    mixed = sum(1 for v in langs.values() if v / total_bytes >= 0.10)

    return {
        "repo": repo,
        "license": lic,
        "size_mb": round((meta.get("size") or 0) / 1024, 1),
        "post_cutoff_prs": prs,
        "subsystems": len(dirs),
        "subsystem_names": sorted(dirs)[:12],
        "instruction_files": instr,
        "codeowners": owners,
        "languages": sorted(langs, key=langs.get, reverse=True)[:4],
        "mixed_languages": mixed,
        "behavioural_suite": has_behavioural,
        **{f"supply_{k}": v for k, v in supply.items()},
        "fails": fails,
        "passes": not fails,
        # Preference score, used only to order the survivors. The three new
        # terms outweigh the old ones because they are what decided whether
        # cli/cli could produce a usable task, and the old ones were all
        # satisfied by a repo that then produced one in-band scenario in
        # twelve.
        "score": (2 * bool(instr)) + bool(owners) + (mixed >= 2)
                 + 3 * bool(has_behavioural)
                 + 2 * ((supply.get("fix_rate") or 0) >= 0.5)
                 + 2 * (supply.get("usable_rate", 0) >= 0.35)
                 + (supply.get("median_files", 99) <= 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", help="file with one owner/repo per line")
    ap.add_argument("--out", default="docs/SCREENING.md")
    ap.add_argument("--json-out", default="docs/screening.json")
    args = ap.parse_args()

    cands = CANDIDATES
    if args.candidates:
        cands = [ln.strip() for ln in Path(args.candidates).read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]

    rows = []
    for repo in cands:
        r = screen(repo)
        rows.append(r)
        mark = "PASS" if r.get("passes") else "reject"
        print(f"{mark:6} {repo}", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(rows, indent=2) + "\n")

    ok = [r for r in rows if r.get("passes")]
    ok.sort(key=lambda r: (-r["score"], -r["post_cutoff_prs"]))
    bad = [r for r in rows if not r.get("passes")]

    L = []
    L.append("# E1 — fixture screening\n")
    L.append(f"Criteria fixed before screening (design §8.1): permissive "
             f"license; >= {MIN_POST_CUTOFF_PRS} merged PRs since {CUTOFF} "
             f"(the assistant's training cutoff, §8.3); >= {MIN_SUBSYSTEMS} "
             f"top-level subsystems. Preferred, never disqualifying: ships a "
             f"maintainer instruction file (becomes arm A1 verbatim), ships "
             f"CODEOWNERS, mixes languages.\n")
    L.append("**Two criteria are not decided here.** Hermetic offline build "
             "with a pre-warmed cache, and a deterministic test subset under "
             "5 minutes, cannot be read from an API. They are E2's gate and "
             "they veto a fixture rather than relax the sandbox (§13). "
             "Everything below is a *candidate*.\n")
    L.append(f"Screened **{len(rows)}** repositories: **{len(ok)}** pass, "
             f"**{len(bad)}** rejected.\n")

    L.append("## Candidates that pass\n")
    L.append("| repo | license | size MB | post-cutoff PRs | subsystems "
             "| instruction file | CODEOWNERS | languages |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in ok:
        L.append(f"| {r['repo']} | {r['license']} | {r['size_mb']} "
                 f"| {r['post_cutoff_prs']} | {r['subsystems']} "
                 f"| {', '.join(r['instruction_files']) or '—'} "
                 f"| {'yes' if r['codeowners'] else '—'} "
                 f"| {', '.join(r['languages'])} |")

    L.append("\n## Rejection log\n")
    L.append("Every rejection names the criterion it hit. Recorded so the "
             "fixture set cannot be re-derived to taste later (§12 "
             "confound 13).\n")
    L.append("| repo | criterion hit |")
    L.append("|---|---|")
    for r in bad:
        why = r.get("error") or "; ".join(r.get("fails", []))
        L.append(f"| {r['repo']} | {why} |")

    out.write_text("\n".join(L) + "\n")
    print(f"\nwrote {out} and {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
