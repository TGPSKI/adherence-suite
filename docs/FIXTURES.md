# Fixtures

Real repositories, not synthetic trees. A fixture is an OSS repo that has
**never had directed-contexts applied** — which is materially stronger than
running on the tool author's own repos:

1. **The generator is what's under test.** On a self-owned repo, the directed
   arm measures a hand-tuned context set no adopter will ever have. On a cold
   repo it measures what the tool produces on code it has never seen, including
   its setup cost.
2. **The baseline is foreign.** The repo's own `AGENTS.md` becomes arm A1
   verbatim.
3. **Ground truth comes from merged PRs**, not from the experimenter.

Mirrors and warmed dependency caches are gitignored — hundreds of MB to several
GB, and reproducible from what is recorded here. The mirror URL and the base
commit are the reproducible part; the git objects are not.

## Screening

`make screen` scores candidates against criteria fixed **before** any of them
were looked at, and writes a rejection log naming which criterion each failure
hit. Disqualifying, all mechanical:

- permissive license — copyleft is rejected, not negotiated
- ≥30 merged PRs after the model's training cutoff, since post-cutoff PRs are
  the only ones usable for the primary result
- ≥4 top-level subsystems

Two criteria are deliberately **not** decided by the screen, because no API
answers them: a hermetic offline build with a pre-warmed cache, and a
deterministic test subset under 5 minutes. Those veto a fixture rather than
relax the sandbox. A repo that passes the screen is a candidate, not a fixture.

Preferred but never disqualifying: ships a maintainer instruction file (gives
A1), ships `CODEOWNERS`, mixes languages.

Last run: **24 screened, 20 pass, 4 rejected.**

## Candidates that pass

| repo | license | size MB | post-cutoff PRs | subsystems | instruction file | CODEOWNERS | languages |
|---|---|---|---|---|---|---|---|
| argoproj/argo-cd | apache-2.0 | 202.4 | 731 | 22 | AGENTS.md, CLAUDE.md | yes | Go, TypeScript, Lua, SCSS |
| prometheus/prometheus | apache-2.0 | 279.7 | 329 | 22 | AGENTS.md, CLAUDE.md | yes | Go, TypeScript, Yacc, Shell |
| astral-sh/ruff | mit | 179.9 | 1423 | 7 | AGENTS.md, CLAUDE.md | yes | Rust, Python, TypeScript, Shell |
| mlflow/mlflow | apache-2.0 | 1456.2 | 898 | 13 | AGENTS.md, CLAUDE.md | — | Python, TypeScript, JavaScript, Java |
| open-telemetry/opentelemetry-collector | apache-2.0 | 70.7 | 243 | 22 | AGENTS.md, CLAUDE.md | yes | Go, Go Template, Makefile, Shell |
| cli/cli | mit | 76.7 | 174 | 12 | AGENTS.md | yes | Go, Shell, Makefile, PowerShell |
| rclone/rclone | mit | 246.3 | 111 | 12 | AGENTS.md, CLAUDE.md | yes | Go, Shell, Python, HTML |
| astral-sh/uv | apache-2.0 | 184.7 | 983 | 7 | AGENTS.md, CLAUDE.md | — | Rust, Python, Shell, Batchfile |
| nushell/nushell | mit | 66.6 | 381 | 10 | AGENTS.md, CLAUDE.md | — | Rust, Nushell, Nix, Python |
| vitejs/vite | mit | 71.4 | 308 | 5 | .github/copilot-instructions.md | — | TypeScript, JavaScript, HTML, CSS |
| traefik/traefik | mit | 156.0 | 250 | 8 | AGENTS.md, CLAUDE.md | — | Go, TypeScript, JavaScript, Shell |
| django/django | bsd-3-clause | 275.0 | 230 | 6 | .github/copilot-instructions.md | — | Python, Jinja, JavaScript, CSS |
| gohugoio/hugo | apache-2.0 | 141.2 | 127 | 39 | AGENTS.md, CLAUDE.md | — | Go, C, JavaScript, HTML |
| tailscale/tailscale | bsd-3-clause | 82.7 | 543 | 61 | — | yes | Go, C, TypeScript, Shell |
| dbt-labs/dbt-core | apache-2.0 | 84.4 | 210 | 4 | — | yes | Rust, Python, Shell, PowerShell |
| pydantic/pydantic | mit | 416.5 | 151 | 5 | — | — | Python, Rust, Makefile, JavaScript |
| goreleaser/goreleaser | mit | 27.7 | 103 | 6 | — | yes | Go, HTML, Ruby, Shell |
| prettier/prettier | mit | 173.9 | 437 | 11 | — | — | JavaScript, CSS, Vue, HTML |
| etcd-io/etcd | apache-2.0 | 96.2 | 262 | 14 | — | — | Go, Shell, Jsonnet, Makefile |
| starship/starship | isc | 46.0 | 68 | 4 | — | — | Rust, Shell, PowerShell, Nushell |

## Rejection log

Every rejection names the criterion it hit. Recorded so the fixture set cannot be re-derived to taste later (§12 confound 13).

| repo | criterion hit |
|---|---|
| grafana/loki | license=agpl-3.0 not permissive |
| minio/minio | license=agpl-3.0 not permissive; only 0 merged PRs since 2026-05-01 (need 30) |
| python-poetry/poetry | only 3 top-level subsystems (need 4) |
| encode/httpx | only 0 merged PRs since 2026-05-01 (need 30) |


## cli/cli — the chosen fixture

MVV candidate (design §16.6). Chosen from the 20 that survived E1
(`SCREENING.md`) for the reason §16.6 asks for — an easy offline build —
and because it ships the two things that make the arms honest.

| | |
| --- | --- |
| mirror | `fixtures/cli-cli.git` (164 MB bare, `--mirror`) |
| base commit | `e83adbc0642994fae7c39a9a012eb34b8c81f4f1` |
| license | MIT |
| post-cutoff merged PRs | 174 (since 2026-05-01) |
| subsystems | 12 top-level |
| **A1 surface** | ships `AGENTS.md` — **recovered verbatim, never authored** (§4) |
| CODEOWNERS | yes — the route ground truth in §8.2 has something to check against |
| ecosystem | Go, 2.1 GB warmed module cache at `fixtures/cli-cli.cache/gomod` |

## E2 / H8 / H10 status

**Materialization: 0.066–0.068 s**, measured three times, against the
164 MB mirror — 30× inside H8's <2 s target. `git status` is empty at
t=0 with 1,344 tracked files, which is the property every scope check and
every `diff_coverage` number depends on.

**Offline build and test: PASS.** 21 packages, **1.47 s**, run in a
network namespace with the module cache mounted read-only:

```
bench/prewarm.sh fixtures/cli-cli.git e83adbc064 \
  "go test ./pkg/cmd/factory/... ./internal/text/... \
           ./pkg/iostreams/... ./pkg/cmd/pr/..."
```

Well inside the <5 min criterion, so the subset can grow considerably
before it costs anything.

## Two things that must not be forgotten

**`internal/config` is excluded from the subset, and not because it is
slow.** `TestMigrationWriteErrors` asserts that writing to an unwritable
config directory *fails*. Under `unshare -r` the caller is mapped to uid
0 inside the namespace, root ignores the permission bits, the write
succeeds, and the test fails on "an error is expected but got nil". That
is an artifact of how the sandbox is built, not a defect in the fixture —
including the package would make a green fixture look red.

**Loopback is up inside the namespace; nothing else is.** cli/cli's
factory tests stand up `httptest` servers. A namespace with `lo` DOWN
fails them with connection errors indistinguishable from "this fixture
needs the internet", which would have vetoed a perfectly good fixture for
a defect in `prewarm.sh`. Loopback-only is the posture; there is still no
route off the host.

## Not yet done

Everything downstream of E2: cold generation of the context set (E3),
arm materialization (E4), floor calibration (E5), and PR sampling with
frozen route ground truth (E6). The offline-build veto §13 cares about is
cleared; the fixture is not yet a fixture until E6 freezes ground truth.

