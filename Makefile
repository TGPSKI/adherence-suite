PYTHON  ?= python3
PY      ?= PYTHONPATH=src $(PYTHON)
ADAPTER ?= adapters/opencode.sh
MODEL   ?= local/qwen36-35b-a3b-nvfp4
TRIALS  ?= 1
OUT     ?= results.jsonl
REF     ?= a1
S       ?=
ARMS    ?=
ARMSDIR ?=
FILTER  ?=
PROXY   ?=

ARMFLAGS = $(if $(ARMS),--arms $(ARMS),) $(if $(ARMSDIR),--arms-dir $(ARMSDIR),)
RUNNER   = $(PYTHON) -m adherence.runner --adapter $(ADAPTER) --model $(MODEL) \
             --trials $(TRIALS) $(ARMFLAGS) --out $(OUT)

.PHONY: help check ci-local compile selftest schema lint table matrix report \
	analyze floors calibrate screen mkpr mkscenarios trees probe run all clean

help:
	@printf '%s\n' \
		'validation — no model, no GPU, no network:' \
		'  make check                 compile + schema + selftest (what CI runs)' \
		'  make selftest              graders both directions, cost metrics, test runner' \
		'  make schema                frozen transcript/result schema vs its goldens' \
		'  make lint                  ruff check (dev-time only; never a runtime dep)' \
		'  make ci-local [JOB=lint]   run CI'"'"'s own steps locally, from the workflow file' \
		'' \
		'viewing — read-only, never spends a GPU-second:' \
		'  make table [FILTER=a3/*]   one-shot snapshot (NOCOLOR=1 to strip ANSI)' \
		'  make matrix [FILTER=a3/*]  interactive results matrix' \
		'  make report [FILES=...]    publishable markdown scoreboard' \
		'  make calibrate             proxy vs adapter agreement — the H4 gate' \
		'  make analyze [FILES=...]   pre-specified falsifier verdicts (docs/EVAL.md)' \
		'  make floors [FILES= ARMS_DIR=]  per-arm instruction floor for' \
		'                             tok_in_marginal, cross-checked vs bytes' \
		'' \
		'runs — these spend GPU time:' \
		'  make run S=s05             one scenario' \
		'  make all                   the full suite' \
		'  make screen                score candidate fixture repos (needs gh)' \
		'  make mkpr REPO= MIRROR= SINCE=  merged PRs -> tasks, gradeability proven' \
		'  make mkscenarios TASKS= MIRROR= FIXTURE=  tasks -> runnable scenarios' \
		'  make trees MIRROR= BASE= ARMSDIR=  each arm as a folder, to read and diff' \
		'  make probe                 difficulty probe: A1 only, no generation' \
		'                             [JOBS=4 TIMEOUT=900 ONLY=a,b PROBE_TRIALS=5]' \
		'' \
		'variables:' \
		'  MODEL    <provider>/<model>; provider is a key in bench/opencode-bench.json' \
		'  TRIALS   trials per scenario (default: 1)' \
		'  ARMS     comma-separated arms, e.g. a1,a2,a3 (needs ARMSDIR)' \
		'  ARMSDIR  directory from tools/mkarms.py' \
		'  PROXY    proxy log to read for the calibration gate'

# --- validation ---

check: compile schema selftest

# Runs CI's own steps, extracted from the workflow file, under the shell
# GitHub uses. Hand-copying a step into a terminal loses `set -euo pipefail`,
# which is how a broken step reached main twice.
ci-local:
	bench/ci-local.sh $(or $(JOB),validate)

compile:
	$(PY) -m compileall -q src scenarios

selftest:
	$(PY) -m adherence.selftest

schema:
	$(PY) -m adherence.schema

lint:
	@command -v ruff >/dev/null || { echo 'ruff not installed: pip install ruff'; exit 1; }
	ruff check .

# --- viewing ---

table:
	@FILTER="$(FILTER)" REF="$(REF)" PROXY="$(PROXY)" $(PY) -m adherence.table $(FILTER)

matrix:
	@REF="$(REF)" $(PY) -m adherence.matrix_tui $(FILTER) --ref $(REF) \
		$(if $(PROXY),--proxy $(PROXY),)

report:
	$(PY) -m adherence.report --ref $(REF) $(or $(FILES),$(OUT))

analyze:
	$(PY) -m adherence.analyze $(or $(FILES),$(OUT))

floors:
	$(PY) -m adherence.floors $(or $(FILES),$(OUT)) \
		$(if $(ARMS_DIR),--arms-dir $(ARMS_DIR),) --ref $(or $(REF),a1)

calibrate:
	$(PY) -m adherence.calibrate $(or $(FILES),runs/cal-results.jsonl) \
		$(or $(PROXY),runs/proxy.jsonl)

screen:
	$(PY) -m adherence.screen_repos

## Materialize each arm as a real folder you can read and diff
trees:
	@test -n "$(MIRROR)" -a -n "$(BASE)" -a -n "$(ARMSDIR)" || { \
		echo 'usage: make trees MIRROR=fixtures/x.git BASE=<commit> ARMSDIR=fixtures/x.arms [OUT=fixtures/x.trees]'; exit 1; }
	$(PY) -m adherence.trees --mirror $(MIRROR) --base $(BASE) \
		--arms-dir $(ARMSDIR) --out $(or $(OUT),fixtures/trees) \
		$(if $(ARMS),--arms $(ARMS),)

## Turn a verified task set into runnable scenarios
mkscenarios:
	@test -n "$(TASKS)" -a -n "$(MIRROR)" -a -n "$(FIXTURE)" || { \
		echo 'usage: make mkscenarios TASKS=scenarios-pr/x MIRROR=fixtures/x.git FIXTURE=x'; exit 1; }
	$(PY) -m adherence.mkscenarios --tasks $(TASKS) --mirror $(MIRROR) \
		--fixture $(FIXTURE) --suite $(or $(SUITE),suite-pr.yaml)

## Difficulty probe: A1 only, no context generation. Answers "can these
## tasks discriminate?" before any of the expensive work.
# Its own file: a difficulty probe is calibration, not results, and the
# two must not pool. PROBE_TRIALS is separate from TRIALS because TRIALS
# is `?= 1` at the top, so `$(or $(TRIALS),3)` resolved to 1 and the
# documented default of 3 never once applied.
PROBE_OUT    ?= runs/probe.jsonl
PROBE_TRIALS ?= 5

probe: SUITE := $(or $(SUITE),suite-pr.yaml)
probe:
	@test -f "$(SUITE)" || { \
		echo "no $(SUITE) yet. The probe runs last; build its inputs first:"; \
		echo "  1. make mkpr REPO=<owner/name> MIRROR=<fixtures/x.git> SINCE=<YYYY-MM-DD> OUT=scenarios-pr/x"; \
		echo "     -> extracts merged PRs and proves each is gradeable (no GPU)"; \
		echo "  2. make mkscenarios TASKS=scenarios-pr/x MIRROR=<fixtures/x.git> FIXTURE=x"; \
		echo "     -> writes the scenario dirs and $(SUITE)"; \
		echo "  3. make probe SUITE=$(SUITE)"; exit 1; }
	mkdir -p $(dir $(PROBE_OUT))
	bench/isolate.sh $(PYTHON) -m adherence.runner --suite $(SUITE) \
		--adapter $(ADAPTER) --model $(MODEL) --trials $(PROBE_TRIALS) \
		--arm a1 --out $(PROBE_OUT) \
		$(if $(JOBS),--jobs $(JOBS),) $(if $(TIMEOUT),--timeout $(TIMEOUT),) \
		$(if $(ONLY),--only $(ONLY),)
	@$(PY) -m adherence.probe $(PROBE_OUT)

## Extract merged PRs into scenarios and prove each is gradeable (no model)
mkpr:
	@test -n "$(REPO)" -a -n "$(MIRROR)" -a -n "$(SINCE)" || { \
		echo 'usage: make mkpr REPO=owner/name MIRROR=fixtures/x.git SINCE=YYYY-MM-DD OUT=scenarios/x'; exit 1; }
	$(PY) -m adherence.mkpr --repo $(REPO) --mirror $(MIRROR) --since $(SINCE) \
		$(if $(UNTIL),--until $(UNTIL),) --out $(or $(OUT),scenarios/$(notdir $(REPO))) $(if $(LIMIT),--limit $(LIMIT),)

# --- runs ---

run:
	@test -n "$(S)" || { echo 'set S=<scenario> (e.g. make run S=s05)'; exit 1; }
	bench/isolate.sh $(RUNNER) --only $(S)

all:
	bench/isolate.sh $(RUNNER)

clean:
	command rm -f results*.jsonl
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
