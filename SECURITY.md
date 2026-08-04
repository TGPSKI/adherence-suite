# Security Policy

## Reporting a Vulnerability

Open a GitHub issue for anything affecting the harness itself: a sandbox escape
that works despite the documented posture, the recording proxy leaking request
bodies, or a grader that can be made to pass by an agent that didn't do the
work.

Do **not** open a public issue for a vulnerability in a third-party harness or
model you found while using this suite. Report that to its maintainers first.

Findings about a model's behaviour — an agent that ignores instructions, leaks a
canary, or claims unverified success — are **results**, not vulnerabilities.
That is what the suite is for. Please share them.

## Trust model

This software runs LLM agents with **shell access**, unattended, in a loop,
against fixture repositories. That is the thing being measured, so it can't be
designed away.

Every run is a process you invoked, running as you, on your machine. There is no
daemon, no listening port except the local recording proxy when you ask for it,
and no telemetry. The suite's own dependencies are the standard library, `git`,
and `bash`.

## The sandbox is layer two, not the boundary

`bench/opencode-bench.json` denies network-capable and privilege-escalating bash
(`ssh`, `scp`, `curl`, `wget`, `nc`, `sudo`, `git push`, `systemctl`, `nft`,
`docker`, …), sets `external_directory: deny *`, and read-denies real credential
paths (`~/.ssh`, `~/.aws`, `~/.gnupg`). `bench/isolate.sh` swaps
`XDG_CONFIG_HOME` so your daily configuration is never loaded.

**None of that is a security boundary.** Deny-globs match command strings, and
command strings can be constructed. Treat the config as something that stops a
well-behaved agent from wandering, not as containment for a misbehaving one.

For containment, run as a throwaway user or in a container with no home mount:

```bash
podman run --rm --network=host -v "$PWD:/work" -w /work <img> bench/isolate.sh ...
```

`--network=host` is only there so a local inference endpoint stays reachable. If
your endpoint is remote, drop it.

The strongest posture in the repo is `bench/prewarm.sh`, which runs fixture test
suites in a network namespace with the dependency cache mounted read-only.
Loopback is up so local test servers work; there is no route off the host.

## Deliberate weaknesses

Two exist on purpose and must not be "fixed":

- **s10 can read the sandbox-local `secrets/` directory.** The scenario measures
  whether the *model* honours a stated boundary. A config that hard-blocked the
  read would measure the config and report nothing about the model. It contains
  a canary string, not a real secret.
- **s09 ships a prompt injection** in a fixture README. It's inert text; the
  scenario measures whether the agent executes it.

Don't copy fixture directories into a real project, and don't run the suite
against a checkout you hold credentials in.

## Known limits

- **The recording proxy logs metadata, not bodies.** It records model, message
  count, tool count, a hash of the system prompt, and the usage block. If you
  point it at an endpoint handling sensitive prompts, the log still reveals
  shape and volume.
- **The proxy speaks plain HTTP on loopback** and performs no authentication. It
  is a measurement tool for a local endpoint, not a gateway.
- **`bench/prewarm.sh` uses `unshare -r`**, which maps you to uid 0 inside the
  namespace. Tests that assert a permission failure will not see one there —
  documented per fixture rather than worked around.
- **Fixture mirrors are cloned over HTTPS without signature verification.** A
  pinned commit gives reproducibility and an audit trail, not integrity.
