# FrontierHarness Eval v1 on the native runner

This package runs [FrontierHarness Eval](https://github.com/frontier-harness-eval/eval)
v1 on our own sandbox infrastructure. Each arm is a harness: Tofu as the
candidate, Codex CLI as the reference control. Both run on the same Kimi K3
deployment. Trials run on the shared native Terminal-Bench runner
(`evaluations/terminal_bench`). Scoring uses the FHE repository's own
unmodified `normalize-results.mjs`.

The parallel project identified the benchmark (its `HANDOFF_TO_TOFU.md`). This
package does not repeat that identification.

## Identity

| Input | Pin |
| --- | --- |
| FHE repository | `e837a70bd6beb4e72eeeda62dd06e3bd34f6cb63` (v1 frozen; `benchmark.json`, published `tasks/`, scorer, `pricing.json` `kimi-k3-2026-08-20`) |
| Terminal-Bench files | `laude-institute/terminal-bench-2` @ `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`, the legacy Harbor registry pin of `terminal-bench@2.0` that FHE's `run-trials.sh` runs |
| DeepSWE corpus | `datacurve-ai/deep-swe` @ `435ee89ec2f2e2289f33b0da4f992f0b7b7266b9`, the corpus commit FHE's reproduction stack installs (`SKILL.md`, "Benchmark stack") |
| Task directory | Terminal-Bench: tests, environment and solution from the Terminal-Bench pin, `task.toml` and `instruction.md` from the FHE pin. DeepSWE: the whole corpus task directory |
| Pins file | `prepare-tasks` writes `task_checksums.json` (Harbor dirhash per task); the runner refuses any drift |

**Problem.** FHE's published `task.toml`/`instruction.md` match no upstream
revision (69671fba, TB2 main or TB2.1). The differences:

- `allow_internet = false` on all 21 tasks;
- a 1200 s agent budget, not 900 s, on regex-log and sqlite-db-truncate;
- extra PATH/workdir env;
- an instruction for build-cython-ext that names an offline `/app/pyknotid.bundle` which no upstream image provides.

**Decision.** The published files are the frozen v1 definition the
leaderboard cites, so they win for agent-visible inputs and budgets. The
manifest records both digests.

**Alternatives rejected:**

- the upstream files alone, which are a different task variant;
- the TB2.1 checkout, which is a different dataset.

We cannot tell which variant FHE's baselines executed: their runner fetches
registry files, not the published copies. Reports must disclose this.

build-cython-ext stays a pending repair (`fhe-task-manifest.json`). Its
bundle must be provided before a full run.

### DeepSWE (9 tasks, suite `datacurve`)

**Decision.** The corpus task directory wins whole. FHE documents that
"DeepSWE uses the pinned corpus metadata" (`reference.md`, "Task metadata").
Six published copies equal the corpus. Three do not: expr-try-catch-errors,
httpx-multipart-response-parsing and python-statemachine-state-data-scoping.
Those three published copies are an older schema 1.1 definition with no
artifact collection, images without the `-v1.1` tag, and no instruction to
commit, so the corpus grader cannot score them. The manifest records
`published_definition_matches` per task.

The schema 1.3 contract runs as the corpus defines it (`[verifier]
environment_mode = "separate"`):

1. **Agent.** The agent works in the task image with `[agent] network_mode =
   "no-network"`. Its trial proxy allowlist is empty: like Pier's per-agent
   allowlist, it reaches only its model, through the relay.
2. **Collect.** The trial removes `/logs/artifacts/model.patch`, so nothing the
   agent planted is graded. It then runs `[[verifier.collect]]` (`git diff
   --binary <base> HEAD > /logs/artifacts/model.patch`, so only committed work
   counts) as root in the agent's container, copies the patch out (regular
   files only), records its size and sha256, and removes that container.
3. **Verify.** `tests/Dockerfile` (the task image plus the hidden tests) is
   built with `--network none` and run as a fresh container with no network,
   the `[verifier.environment]` resources and `[verifier.env]`. The patch is
   mounted read-only at `/logs/artifacts`. `tests/test.sh` writes
   `reward.json`.
4. **Reward.** A patch that does not apply is a scored 0 (`apply_failed: 1`).
   The corpus's crash sentinel, `reward.txt` = `-1` from test.sh's EXIT trap
   when the grader itself failed, is no reward: the trial is infra-invalid.

The budgets are 5400 s agent, 300 s collect, 1800 s verifier build and
1800 s verifier. With the runner's fixed overhead, a DeepSWE trial needs
`--max-alive-min 240`; the longest Terminal-Bench trial (dna-insert, Tofu)
needs 123 min, so Phase A needs `--max-alive-min 150`. The runner refuses
to start a run whose session lifetime is shorter than any trial's budget.

Not done here: Pier itself (`datacurve-pier==0.3.1`). Its isolation details
beyond the task contract are unverified, so reports disclose "Pier not used".

## Protocol (`--protocol fhe-v1`)

- **Budgets and resources.** Agent and verifier budgets come from `task.toml`, at multiplier 1. The container gets the task's `cpus`/`memory_mb`; the sandbox VM keeps 4 CPU / 16 GiB.
- **Verifier.** One verifier run. An unfinished verifier is infra-invalid, never retried into a score.
- **Harness defaults.**
  - Tofu runs `tofu-agent run` with no `--thinking`: Kimi K3's route default is `max`.
  - Codex runs exactly as Harbor's adapter runs it: `codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --model kimi-k3 --json --enable unified_exec`, with a fresh per-trial `CODEX_HOME`.
  - Codex's only config change is `web_search = "disabled"`, because the provider would run hosted search outside the egress policy both arms share.
- **Model.** `kimi-k3` on FRIDAY, behind one relay with one key and an owner-assigned `--max-rpm`. Each harness uses its own wire face:
  - Tofu uses the Anthropic face, the production pin for the K3 line.
  - Codex uses the Responses face, the only one it speaks.
  - Codex omits `max_output_tokens`. The relay's `--responses-max-output-tokens` fills it only when set, and records that on every request.
- **Network.** The trial egress proxy is an allowlist (`--egress-allowlist`, FHE `providers.sh` hosts minus the provider host). Agent and verifier share it, and the run records it. Model calls reach the relay directly. The Tofu benchmark-source guard is off: `--no-source-guard`, since the allowlist is the policy. A `no-network` (DeepSWE) agent gets an empty allowlist and its verifier no network at all. The proxy binds clients that honor the proxy environment; a process that dials the VM's upstream proxy directly is not stopped, so this is not a packet-level guarantee.
- **Context audit.** `--first-body-dir` keeps the first request of every trial. Every relay record also lists `tofu-context` block ids and date markers. Until headless context slimming lands, this is the record of exactly what each harness injected.

`--protocol fhe-v11` (FrontierHarness Eval v1.1, whose study runs every model and harness at effort `max`) keeps everything above except the effort: Tofu gets `--thinking max` and Codex `model_reasoning_effort = "max"`, which FRIDAY's kimi-k3 Responses face accepts (probe 2026-11-13: HTTP 200, reasoning output, effort echoed).

`--protocol fhe-v11-hold` (PREREG-2 Amendment 7) is `fhe-v11` without our provider quota in the agent budget, since FHE's reference runs have no rate limits:
- **Held seconds.** The relay times each model request's whole hold in its gates before going upstream: key cool-down, request window, token window, and the queue on their locks. It logs the hold per call as `held_s` and serves a per-trial running total, holds in progress included, at `GET /arm/<arm>/task/<task>/held`. It rebuilds the totals from its log when it restarts.
- **Agent deadline.** The deadline is the task budget plus the trial's held total since the agent started. `vm_trial` re-reads the total each time the deadline is reached, and once a second while a hold is in progress.
- **One enforcer.** The runner stops both arms alike: SIGTERM to the agent's process tree, then SIGKILL. Tofu's own `--timeout-s` is set to the VM lifetime, with no finalize grace.
- **Host backstop.** The host-side trial deadline moves by the same total.
- **Recorded per attempt:** `extension_s`, `stopped_at_s`, `relay_held_base_s`, `relay_held_reads` and `relay_held_errors`.
- **Earlier protocols.** The relay's `rpm_wait_s`/`tpm_wait_s` count only a request's own pauses, not its queue on the gate locks, so there they are lower bounds of the time held.

## Scoring (`export`)

`export` writes one FHE run directory per arm, then runs FHE's normalizer.
Statuses follow FHE's accounting rules:

- **success**: reward >= 1 with model usage;
- **failure**: any other reward with usage, or an agent timeout after start;
- **infra_invalid**: everything else, including the DeepSWE crash sentinel.

Trial ids come from the prepare-tasks manifest (`terminal-bench/<task>` or
`datacurve/<task>`); DeepSWE trials also carry their `model_patch` evidence.
Each native retry counts as FHE attempt 1, so the normalizer keeps the
earliest valid one. Costs price the relay's per-call usage at the frozen FHE
table (`calls.jsonl` holds one priced row per call, with its key tail). This
is a benchmark basis, not FRIDAY billing. Smoke runs use a `smoke*` label,
which FHE's canonical selection excludes.

Every run is `methodology_comparable: false`: it is a non-official
environment. A rank is defensible only from a matched control. That means the
Codex arm must run in the same native run, on the same tasks, budgets,
allowlist, relay and key, and its per-task outcomes must be compared with the
published Codex 0.148.0 row.

## Replicate blocks (`multi_attempt`)

`python -m evaluations.frontier_harness.multi_attempt --block k1=<work dir> --block k2=<work dir> ...`
scores each replicate block per arm with FHE's unmodified normalizer (through `export`), then aggregates the
canonical cells across blocks: task-weighted pass rates with task-clustered bootstrap CIs, the paired
treatment - reference difference with a sign-flip permutation test, the reference arm's calibration against an
FHE published row (Poisson-binomial test; PREREG.md's block-1 McNemar rule beside it), an FHE-anchored estimate
withheld unless calibration holds, and spend / requests / 429s / RPM per key. Variants: `primary` (FHE's cells),
`harness_fault_as_failure` (a no-model-call cell whose trial still got a verifier reward is a failure) and
`unthrottled`. The decision rules are preregistered in `preregistrations/PREREG-2-20261113.md`.

## Commands

```bash
PY=<interpreter with aiohttp, httpx, dirhash, pssdk>   # e.g. .tofu_env.json "python"
python -m evaluations.frontier_harness prepare-tasks --fhe-repo <fhe@e837a70> \
  --tb2-repo <terminal-bench-2@69671fba> --deep-swe-repo <deep-swe@435ee89e> --out-dir $RUN/tasks-fhe-v1-full
env -u PYTHONPATH $PY -m evaluations.terminal_bench.relay --port 18731 --key-suffixes <tail> \
  --max-rpm <budget> --model kimi-k3 --anthropic-model kimi-k3 --responses-model kimi-k3 \
  --log $RUN/relay-requests.jsonl --files-dir $RUN/files --first-body-dir $RUN/first-bodies
env -u PYTHONPATH $PY -m evaluations.terminal_bench run --work-dir $RUN/<run> --concurrency <n> \
  --tasks-dir $RUN/tasks-fhe-v1 --task-checksums $RUN/tasks-fhe-v1/task_checksums.json \
  --egress-allowlist $RUN/tasks-fhe-v1/allowlist.json --no-source-guard --arms tofu,codex \
  --protocol fhe-v1 --model kimi-k3 --relay-url http://<host-ip>:18731 \
  --artifacts-manifest $RUN/artifacts.json --endpoint https://model.internal.example.com/sandboxGateway/system/857 \
  --image rootfs://harbor-pipeline-dind-v5 --proxy http://10.70.33.35:8080 --max-alive-min <150 TB | 240 DeepSWE>
python -m evaluations.frontier_harness export --native-dir $RUN/<run> --relay-log $RUN/relay-requests.jsonl \
  --fhe-repo <fhe@e837a70> --out-root $RUN/fhe-<run> --label <label> --run-id <run> --provider "FRIDAY kimi-k3"
```

Verification: `tests/evaluations/test_frontier_harness_native.py` covers the
Codex arm, the FHE protocol, allowlist semantics, the relay's Responses face,
key reveal, request window and context audit, task pinning, the export, and
the real FHE normalizer when `FHE_EVAL_CHECKOUT` names the pinned checkout.
`tests/evaluations/test_frontier_harness_deepswe.py` covers the DeepSWE task
contract, collect, the separate verifier, the crash sentinel, the trial budget
and session-lifetime refusal, corpus task pinning and the export. When
`DEEPSWE_CORPUS_CHECKOUT` names the corpus pin, it also checks every v1 corpus
task's contract and runs the corpus's own `grader.py` offline on a collected
patch (solved, failing, unapplied, absent).
