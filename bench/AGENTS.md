# AGENTS.md — operator rules (drop into each active repo root)

These rules bind every agent in every mode, including the default Build
primary. They exist because the agent-level pack cannot govern behavior
that happens before a custom agent is invoked.

## 1. @-mention dispatch is mandatory

When the operator @-mentions an agent (@debug, @verify, any registered
name), invoking that subagent via the task tool with that exact
subagent_type is the instruction. It is not a hint, not a tone marker,
and not satisfied by performing similar work yourself. If the named
subagent cannot be invoked, say so and stop — do not substitute
subagent_type "general" and do not proceed inline.

## 2. Probe before theorizing

When reasoning about concrete code behavior (what a regex matches, what
a parser extracts, what a command returns): at most five sentences of
theory, then write and run the smallest probe that answers the question.
Extended speculation about behavior that a ten-line script can observe
is a defect, not diligence. If two theories about the same code have
contradicted each other, the probe threshold has already been crossed.

## 3. Completion requires a checkable condition

Before declaring work done:
- State the condition that defines done as something checkable (a
  command, an assertion about file contents), then check it. "Output
  looks better" is not a condition.
- Recompute every numeric claim in the summary from the artifact itself
  (counts via wc/grep -c, not from memory of earlier output).
- Re-read the final artifact specifically hunting for the original
  symptom's pattern at every level, not just where it was first seen. A
  fix that removed the symptom at depth 0 has not been shown to remove
  it at depth 1.

## 4. Output discipline

Long content goes into files; chat carries decisions and evidence lines.
The serving model has an 8192-token output ceiling — plan responses so
truncation cannot land mid-diff or mid-report.
