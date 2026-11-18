# Native Terminal-Bench runner (no Harbor, no agentix)

Runs Terminal-Bench 2.x tasks on CodeLab sandbox VMs (sandboxGateway, e.g.
cluster 857) with our own code end to end: one VM per trial, a stdlib-only
trial executor inside the VM, and a host orchestrator that resumes safely.

## Components

| Module | Role |
| --- | --- |
| `harness.py` | Task loading, protocols, Tofu/OpenCode/Pi/Claude-Code launch specs, trial spec, task bundle (never contains `solution/`) |
| `../claude_code_contract.py` | Claude Code 2.1.207 pins, native-binary runtime tree, guest settings and launch program |
| `anthropic_bridge.py` | Anthropic Messages ↔ OpenAI chat-completions bridge for the claude-code arm, in front of the shared relay |
| `source_guard.py` | Benchmark-source blocklist, one rule projected into each arm's own mechanism, plus the in-container canary |
| `vm_trial.py` | Uploaded into each VM: image pull → container → runtime install → agent under deadline → `tests/test.sh` → `result.json` + bundle; egress filter proxy |
| `gateway.py` | pssdk session start/stop and the ingress native exec channel |
| `runner.py` | Concurrent trials, resumable attempts, `summary.json` |
| `relay.py` | Model relay shared by every arm: same FRIDAY deployment, same keys, same sampling; chat, Anthropic Messages and Responses faces |
| `../pi_contract.py` | Pi version pins, prebuilt runtime tree, guest `models.json` and launch program |
| `third_party.py` | Kimi Code, Qwen Code, Goose, mini-swe-agent, Hermes Agent and oh-my-pi: version pins, pinned runtime artifacts, per-trial setup and launch programs |
| `audit.py` | Benchmark-answer contamination flags for review |
| `eval_matrix/` | EVAL-MATRIX pre-registration (`PREREG.md`) and `status_tables.py`, which regenerates per-cell score, CI, cost and tokens from `per_trial.csv` plus raw run dirs and relay logs |

The VM-side entry point is the ingress native exec channel
(`/native/exec|status|read|kill`) on the nlp/harbor branch named by
`TOFU_TB_INGRESS_BRANCH` or `run --branch` (b35af6f). Nothing in the trial
path runs Harbor. The per-VM-session ingress token (below) is not on this
branch yet, so runs use the tokenless channel.

## Workflow

```bash
PY=<interpreter with pssdk, aiohttp, dirhash>   # e.g. the Tofu app interpreter (.tofu_env.json)
RUN=.eval-runs/<run-dir>
# 0. the run's relay key (mode 0600; kept when it exists; never on a command line)
env -u PYTHONPATH $PY -m evaluations.terminal_bench relay-key $RUN/relay.key
# 1. relay (keys stay in this process; sandboxes see a key-less URL and present the run key)
env -u PYTHONPATH $PY -m evaluations.terminal_bench.relay --port 18451 --model glm-5.3-flash-m17 \
  --temperature 1.0 --top-p 1.0 --max-tokens 65536 --reasoning-effort max \
  --log $RUN/relay-requests.jsonl --files-dir $RUN/files --ingress-key-file $RUN/relay.key
# 1b. only for the claude-code arm: Anthropic bridge in front of the relay
env -u PYTHONPATH $PY -m evaluations.terminal_bench.anthropic_bridge --port 18452 \
  --relay-url http://127.0.0.1:18451 --log $RUN/bridge-requests.jsonl
# 2. run (SANDBOX_857_TOKEN injected by the sandbox-direct-access skill env overlay)
env -u PYTHONPATH $PY -m evaluations.terminal_bench run --work-dir $RUN/full --concurrency 178 \
  --tasks-dir <tb2.1 tasks> --arms tofu,opencode[,pi,claude-code] --protocol vendor-6h \
  --relay-url http://<host-ip>:18451 --artifacts-manifest $RUN/artifacts.json \
  --endpoint https://model.internal.example.com/sandboxGateway/system/857 \
  --image rootfs://harbor-pipeline-dind-v5 --proxy http://10.70.33.35:8080 --relay-key-file $RUN/relay.key
  # add --anthropic-url http://<host-ip>:18452 when --arms includes claude-code
# 3. score: merges fix-up dirs, attaches the audit, applies human contamination verdicts
env -u PYTHONPATH $PY -m evaluations.terminal_bench.report $RUN/full [$RUN/<fixup> ...] \
  --tasks-dir <tb2.1 tasks> [--review $RUN/review.json]
# 3b. compare arms run separately: list every run's dirs and reviews, write elsewhere
env -u PYTHONPATH $PY -m evaluations.terminal_bench.report $RUN/full [$RUN/<fixup> ...] $OTHER/full \
  --tasks-dir <tb2.1 tasks> --review $RUN/review.json --review $OTHER/review.json \
  --output .eval-runs/<comparison>/report.json
```

Relay ingress (`relay_ingress.py`, 2026-11-13). A relay listening beyond
loopback needs `--ingress-key-file`; every route (model faces, `/models`,
`/probe`, `/stats`, `/files`, `/held`, unknown paths) then answers HTTP 401
before its handler and before any upstream call unless the request carries
the key as `Authorization: Bearer` or `x-api-key`. Refusals are logged as
`ingress_refused` (peer, method, path, which header was present; never a
value). The runner (`--relay-key-file`, the same file) uploads each trial
spec with the key in place of the agents' API-key placeholder `relay-held`
and sends it on artifact downloads and `/held` reads; `spec.json` and
`run.json` keep only the placeholder and the path. Operator tools that call
a keyed relay send the header from the file, e.g.
`(umask 077; printf 'Authorization: Bearer %s\n' "$(cat $RUN/relay.key)" > $RUN/relay.key.header)`
once, then `curl -H @$RUN/relay.key.header ...`. A key FRIDAY reports
disabled (HTTP 401 `App ID 已被禁用`) is retired like any 401/402/403
key (`key_retired` with `disabled: true`, `/stats` `disabled`); once no key
is left every request gets HTTP 403 `relay_key_disabled` (or
`relay_key_retired`) with `x-should-retry: false` and no upstream call,
instead of the retryable 502 `no live key` that Tofu's dispatch waits out
without end.

`report` refuses a dir whose protocol, model, context, image, endpoint, proxy
or dataset commit differs from the first dir's, or that plans a task the first
dir does not; reviews that disagree on a trial are refused too. A trial counts
as a budget exhaustion (`agent_budget_exhausted`, `agent_timeouts`) when the
runner timed it out or its agent ran for its whole budget: Tofu stops itself at
the deadline (exit 3) without the runner's timeout flag.

Rerunning `run` with the same work dir resumes from `run.json`; measured
trials are never recomputed. `summarize` rewrites `summary.json`. A new run
refuses task directories whose Harbor checksum (`dirhash` sha256) differs
from `evaluations/terminal_bench_21_task_checksums.json`.

When a runner or runtime defect exhausts a trial's attempts, rerun just those
trials in a separate work dir with the fixed code/artifact (never a second
runner on a live work dir) and pass it to `report` after the main dir; a
measured trial there replaces the unmeasured one.

Runtime artifacts (`artifacts.json`, served from `--files-dir`):

- Tofu: standalone CPython 3.13 with the `tofu-agent[full]` wheel built per
  `docs/DEVELOPER_RUNTIME.md`. The released wheel cannot run a task headless
  (`lib.storage` excluded but imported by `lib/tasks_pkg`; `languages.json`
  not shipped), so the artifact adds both from the same commit. It also
  installs that commit's `packages/tofu-search` wheel: tofu-search is a
  separate distribution, and the base runtime's release would keep its own
  fetch gate and URL blocklist (the guard canary then fails closed). Verify
  any rebuild with `env -i ... python3 -I -c 'import lib.tasks_pkg.manager'`
  (the host exports `PYTHONPATH=<repo>`, which otherwise hides missing
  modules) and check that the runtime's freeze lists both wheels.
- OpenCode: `opencode_contract.OPENCODE_VERSION` linux-x64-baseline binary plus
  the pinned `@ai-sdk/openai-compatible` seed from `prepare_cache_seed`.
- Claude Code: `python -m evaluations.claude_code_contract build-runtime --work-dir <scratch> --out-dir <files>`
  verifies both npm tarballs against the registry integrity pins, confirms the wrapper pins the
  platform package at the same version, and packs the native `claude` binary (glibc >= 2.17),
  exactly what the wrapper's postinstall would install on linux-x64 glibc.
- Pi: `python -m evaluations.pi_contract build-runtime --work-dir <scratch> --out-dir <files>`
  packs Node `PI_NODE_VERSION` (sha256-checked against nodejs.org) and a
  global `npm --ignore-scripts` install of `PI_PACKAGE@PI_VERSION`, plus
  `pi-runtime.json` listing every installed package. Node is invoked by
  absolute path, so the task's own tools and PATH are unchanged.
- Native Pi contract verification: set `TB_NATIVE_PI_RUNTIME` to an extracted
  `build-runtime` output tree to run the real pinned runtime against the local
  relay stub. With it unset, the offline contract tests still run and only that
  live-runtime test skips.
- glibc floor: TB2.1 includes Debian bullseye images (glibc 2.31). On a newer
  host uv picks `manylinux_2_34` wheels (`cryptography`,
  `tree-sitter-language-pack`), which fail to import there. Build with
  `uv pip install --python-platform x86_64-manylinux_2_28 --no-build` and scan
  every `.so` for `GLIBC_2.x` above 2.28 before packaging. Build interpreters
  from `readlink -f` of the uv-managed path: copying the version symlink makes
  uv install into the shared interpreter.

## Fairness and protocol controls

- System CA store (protocol amendment 2026-11-12, Harbor parity; every arm,
  every protocol): Harbor's agent installs bring a system CA store into each
  task image (installing curl pulls in `ca-certificates`), so a trial whose
  image has none of the bundle files Go's `crypto/x509` probes
  (`harness.SYSTEM_CA_BUNDLE_FILES`) gets the pinned Mozilla bundle (curl.se
  extract 2026-11-05, 118 roots; manifest entry `ca-bundle`) as
  `/etc/ssl/certs/ca-certificates.crt`, as root in the install phase before
  the arm's runtime is installed, outside the agent budget. Only that file is
  added: no package, no hashed certificate directory. Each trial records
  `system_ca_bundle.state` (`present`: the image's own store, or `installed`)
  in `result.json`; those records are the authoritative list of affected
  images. `run` refuses a manifest without `ca-bundle`. Found by goose, which
  cannot build its HTTP client without a store (false 0.0 on bare
  `ubuntu:24.04` images before any model call).
- Same model, keys, relay, sampling and reasoning effort for every arm; the
  relay overrides `temperature`, `top_p`, `max_tokens` and (when
  `--reasoning-effort` is given) `reasoning_effort` on every request, and logs
  both the harness's original values and what was sent. The claude-code arm reaches that same relay
  through `anthropic_bridge.py`: Claude Code speaks only Anthropic Messages,
  so the bridge translates each request to the relay's OpenAI dialect under
  the same arm label, and the response back. `output_config.effort` maps to
  `reasoning_effort` (low/high/max; medium->high, xhigh->max, the deployment's
  levels), `thinking` enabled/adaptive maps to Z.ai's `thinking: {type:
  enabled, clear_thinking: false}`, and assistant `thinking` blocks go back as
  `reasoning_content`, so thinking is preserved across turns. The bridge logs
  the client's settings, the translated settings and the history's reasoning
  counts per request, so the protocol can be audited on the wire. Anthropic's
  server-side tools (web_search) have no OpenAI counterpart: they are dropped
  with a system note rather than failing the turn. `document` blocks become
  OpenAI `file` parts (the deployment reads PDFs this way); `count_tokens`
  returns 404, which Claude Code handles with its own estimate.
- Claude Code runs as Harbor's upstream adapter does (32164177f009): the
  instruction on stdin, `--verbose --output-format=stream-json
  --permission-mode=bypassPermissions --effort max --print`, every model alias
  pinned to the run model, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = the protocol limit
  (its default for an unknown model is 32000). Claude Code assumes a 200 000
  token context for a model name it does not know; the recorded 262 144 applies
  to the other arms, and there is no supported override short of disabling
  compaction. Its run ends at the final stream-json `result` record, so a
  process lingering on children it started is not killed and its daemons stay
  up for the verifier.
- Reasoning effort parity: Pi always sends `reasoning_effort` for GLM-5.3
  (its catalog maps levels to low/high/max) and Tofu pins its native
  `--thinking max`; OpenCode has no client-side effort field. TB2.1 runs also
  pin `--reasoning-effort max` at the relay for every arm rather than relying
  on the deployment default. The 2026-11-03 relay probe
  (`.eval-runs/native-harness-artifacts/effort-probe`, 16 samples x 2 prompts)
  found the default indistinguishable from `max` (Mann-Whitney z 0.06 and
  -0.47) while `high`, the Harbor preflight's level, used 2.3-2.6x fewer
  completion tokens; that justified the first baseline's default, not an
  implicit default in later runs. Any arm that changes effort must be re-run
  for all arms.
- Pi's `models.json` replaces only the built-in provider's `baseUrl` and sets
  the same context/output limits every arm is told, so Pi keeps its shipped
  thinking format and tool streaming. Pi's stdout is filtered like Harbor's
  (no `message_update` records); its session files under
  `/logs/agent/pi/sessions` are the audit source.
- Every agent runs once, as its upstream adapter runs it (OpenCode and Pi as
  in Harbor, Tofu as `tofu-agent run`), with its own product defaults. The
  runner never resumes, nudges or retries an agent and sets no harness tuning
  knob: a run that ends early, for example at the output-token limit, is
  scored as it ended. See the decision below.
- Same container, resources (4 CPU / 16 GiB), egress policy, budgets, and
  model facts (context window 262 144, output 65 536) for every arm, each
  through its own configuration surface: OpenCode's provider `limit`, Pi's
  `modelOverrides`, Tofu's routing deployment `max_output_tokens`.
- Tofu stops its own run at the agent budget (`--timeout-s`) and then writes
  its result document, the audit's trajectory source; only that write gets
  `TOFU_FINALIZE_GRACE_SEC` before the runner's kill. In the 2026-11-03 run
  no Tofu trial needed the kill and the largest overrun was 2.0 s.
- `vendor-6h`: 21 600 s agent budget for every task (Z.ai GLM-5.3-Flash card
  footnote); `task-default`: `task.toml` budgets.
- Agents run as `bash -lc` in the image `WORKDIR` like Harbor, as the child of
  a parent shell whose argv carries the trial marker (`MARKED_SHELL`); at the
  deadline that shell's process tree gets SIGTERM, then SIGKILL after
  `KILL_GRACE_SEC`, so a program that `exec`s or ignores SIGTERM stops too.
  Detached daemons keep running for the verifier. Tests are uploaded only
  after the agent finished.
- Benchmark-source blocklist (`source_guard.py`, on for every new run and
  frozen in `run.json` as `source_guard`). One rule: `ANSWER_HOSTS`
  (tbench.ai, marginlab.ai, harborframework.com and their subdomains) refused
  everywhere; `SOURCE_REPOS` (harbor-framework, laude-institute and the
  audit-found mirrors Bruce-lys/LLM-benchmark and Eyan-L/Agent_Bench-NSCC)
  refused wherever they appear in a URL or command; `NAME_MARKERS`
  (terminal-bench and variants, which forks keep) refused inside a URL. Each
  arm enforces it through its own mechanism: the trial egress proxy refuses
  the hosts at CONNECT and the markers in plain-HTTP URLs; Claude Code runs a
  PreToolUse hook on Bash/WebFetch/WebSearch (exit 2 shows the reason to the
  model); OpenCode gets `permission.bash` deny patterns; Pi loads a
  `tool_call` extension; Tofu gets its product `search.skip_domains` setting:
  the answer hosts as host rules and every source repository and name marker
  as a path pattern on every host (`*/**/*terminal-bench*`), enforced at the
  fetch gate, in search listings, in browser navigation and on absolute URLs
  in `run_command` (scheme-less references such as `git clone github.com/...`
  stay audit-only for Tofu).
  HTTPS hides paths from the proxy, so paths on shared hosts are refused only
  where a harness exposes the URL before it runs; other internet access stays
  open. Before the agent starts, the trial runs `guard_check` in the container:
  the canary URLs must be refused and the control URL on the same shared host
  must pass, and a failure is an install error, never a scored trial.
- Egress goes through a per-trial proxy that logs every request; agent-phase
  denials are audit evidence (the setup-phase canary is refused on purpose and
  is not). Only network calls (web tools or URL fetches) give strong evidence:
  task images ship visible tests, canary-bearing sources and whole
  repositories, so local reads of hidden lines are a weak flag. A strongly
  flagged pass is excluded after reviewing the cited tool calls.

- Gateway cache affinity: Tofu's evaluation Connection declares
  `cache_affinity_mt_session_id`, so the product sends its stable per-lineage
  `Mt-Session-Id` header. The relay forwards this header for every arm, including
  retries on another API key. It never generates an identity for a client that
  sends none, and never inserts a marker into the prompt. The request log's
  `cache_affinity` field records the carrier name without the identity. This
  measures each harness's configured transport behavior; comparisons intended
  to isolate agent behavior must separately control affinity for every arm.
  Pre-change runs whose relay stripped client headers use a different transport
  configuration and must be identified as such.

## Codex arm, allowlist egress and the `fhe-v1` protocol

- The `codex` arm runs the pinned Codex CLI (`codex_agent`) exactly as
  Harbor's adapter does: `codex exec --dangerously-bypass-approvals-and-sandbox
  --skip-git-repo-check --model M --json --enable unified_exec`. It runs with
  a per-trial `CODEX_HOME` under `/logs/agent`, so no login can exist and the
  session rollout is collected. Its relay provider uses the Responses face
  (`/arm/<arm>/task/<task>/v1/responses`), the only face Codex speaks.
  `web_search = "disabled"` is its one config change: hosted search would run
  at the provider, outside the trial egress policy. The arm has no
  source-guard projection, so a codex run needs `--no-source-guard` and
  `--egress-allowlist`.
- **Relay options.**
  - `--responses-model` enables the Responses face. Only `model` is pinned, and
    usage and status are read from the terminal `response.*` event.
  - `--responses-max-output-tokens` fills `max_output_tokens` only when a
    request omits it, and records the fill.
  - `--key-suffixes` now reveals only the matching credentials.
  - `--max-rpm` bounds upstream request starts per trailing minute, across
    every face and retry. It is an owner-assigned key budget.
  - `--max-tpm` bounds the estimated input tokens of those starts the same way
    (eval-quota rule 12: FRIDAY's second per-AppId+model limit, `…的tpm超过限制`).
    The estimate is the body size over a per-face bytes-per-token ratio taken at
    the 1st percentile of logged kimi-k3 calls (Messages 3.1, Responses 3.6), so
    it errs high for Tofu and Codex (99.1% / 98.9% of logged calls); another
    harness needs its own check. A 429 naming tpm halves the token budget, any
    other 429 the request budget; without `--max-tpm` every 429 halves
    `--max-rpm` as before.
  - `--budget-file` keeps both budgets in a JSON file: read at start when it
    exists, re-read whenever it changes (a guard or operator can lower a
    budget without a restart) and rewritten on every halving, so a restarted
    relay never resurrects a higher budget. `--assume-full-window` starts a
    successor as if its draining predecessor had used both whole budgets in
    the last minute.
  - `--stop-file` is written once, at the first upstream 429 or the first key
    retirement (HTTP 401/402/403: the key serves no further request), so
    runners start no trial that could only measure the outage.
  - `--first-body-dir` keeps the first request of each (arm, task) at mode 0600.
  - Every chat, Anthropic and Responses record lists the `tofu-context` block ids
    and date markers its prompt carries. This is the injected-context audit.
  - `--pin-file` sets or omits request fields per face (dotted paths) for every
    arm, so a model's reasoning and sampling are identical across harnesses;
    each record lists what the pin replaced.
  - FRIDAY also sends its per-minute 429 inside HTTP 200 bodies and SSE
    streams (`response.failed` with `rate_limit_exceeded`, an Anthropic `error`
    event, a chat `data:` line carrying its `{"status": 429}` body). The client
    gets the stream as sent; the relay applies the 429 rule to it (stop file,
    key rest, halved window) and marks the record `stream_429`, which the
    throttle accounting counts. Observed 2026-11-12: Codex retried five such
    streams within 6 s and failed its turn while the relay logged only 200s.
- `--egress-allowlist FILE` freezes a host list in `run.json`. The trial proxy
  then refuses every other host, for agent and verifier alike. Entries match
  exactly, or as `*.suffix` subdomains, and never the apex of a wildcard.
  Each refusal is logged with its `rule`.
- `--task-checksums FILE` pins a task set other than TB2.1, in the same
  `{dataset_commit, task_checksums}` shape.
- Schema 1.3 tasks with `[verifier] environment_mode = "separate"` (DeepSWE)
  run as their contract says. `load_task` refuses any shape vm_trial does not
  run: artifacts outside `/logs/artifacts`, a verifier with network, or a
  missing `tests/Dockerfile`. The trial then works as follows:
  - The agent works in the task container. A `[agent] network_mode =
    "no-network"` agent gets an empty proxy allowlist; it still reaches the
    relay, which no trial proxy carries.
  - vm_trial deletes the artifact paths, runs `[[verifier.collect]]` as root
    in that container, and `docker cp`s the artifacts to `out/artifacts`. Only
    regular files count, and their size and sha256 are recorded. It then
    removes the agent container.
  - It builds `tests/Dockerfile` with `--network none`. `tests/test.sh` runs
    in a fresh `--network none` container with the `[verifier.environment]`
    resources and `[verifier.env]`, and the artifacts mounted read-only.
  - `reward.txt` = `-1`, the corpus's grader-crash sentinel, is recorded as
    `raw_reward` / `reward_sentinel` and gives no reward.
- `trial_budget_sec` is a trial's wall deadline: agent plus grace, verifier
  runs, a separate verifier's collect and build budgets, and the fixed
  overhead. `run` refuses a `--max-alive-min` shorter than any planned trial's
  budget, so a sandbox session never ends mid-trial.
- With `TOFU_857_LEASES` (the sandbox-857 ledger's `leases.py` CLI) and
  `TOFU_857_LANE` set, every sandbox session is under the cluster lease
  ledger (`runner.SandboxLeases`). Each new session acquires its lease before
  its start; a refusal (ceiling or halve-and-hold) waits and asks again every
  30 s, and never starts a VM. A failed or slow start is reported to the
  ledger. A confirmed stop releases the lease; a stop that cannot be confirmed
  keeps it open until the ledger reclaims it.
- `--start-deadline TIME` (ISO 8601) holds every new attempt whose sandbox
  start allowance (the gateway's 660 s start budget) plus trial budget would
  end after TIME, so a run on a key with a planned end (a scheduled
  revocation) starts nothing it cannot finish; held trials run in a later
  invocation. It applies to one invocation and is never frozen in `run.json`;
  like `--finish-by` it is checked again during the lease wait and after the
  grant.
  `--start-allowance-sec N` replaces the start budget and the fixed trial
  overhead with N seconds before the agent starts (for example an observed
  p95 of session start to agent start): the attempt must then fit N plus its
  agent setup, agent, grace and verifier budgets.
- `invalidate --work-dir W --arm A --task T --attempt N --rule R --evidence E
  --decided-by D` records an operator's verdict that a finished, stopped
  attempt measured an outage rather than the harness (the provider withdrew the
  model mid-trial, or the relay was stopped for a production incident). The
  attempt keeps every record (`invalidated.json` beside them); the runner
  treats it as unmeasured, so the next attempt runs, and the FHE export scores
  it `infra_invalid`.
- The `fhe-v1` protocol is FrontierHarness Eval v1, documented in
  `evaluations/frontier_harness/README.md`:
  - `task.toml` agent and verifier budgets and container `cpus`/`memory_mb`;
  - one verifier run;
  - no reasoning level passed to any arm.
- `kimi-k3` routes Tofu over FRIDAY's Anthropic face as `moonshot/kimi-k3`,
  provider `example-corp`. That is production's pin for the K3 line. The
  projection declares vision and video, as the production offering does.

## Third-party arms (`third_party.py`)

- `kimi-code` (`@moonshot-ai/kimi-code` 0.37.2), `hermes` (Hermes Agent
  0.20.4, git `044acf2b`: 0.20.x is not on PyPI and its `setup.py` refuses
  wheel builds, so its pinned source archive is a runtime artifact installed
  as its own installer's hash-verified tier does, `uv sync --extra all
  --locked` on Python 3.11) and `oh-my-pi` (17.4.0, its upstream release
  binary `omp-linux-x64`, preinstalled; see Install)
  at FrontierHarness Eval v1's published versions, plus `qwen-code`
  (`@qwen-code/qwen-code` 0.27.2), `goose` (1.59.0 release binary) and
  `mini-swe-agent` (2.4.6 on CPython 3.12). Each runs once as Harbor's
  upstream adapter runs it (oh-my-pi as Harbor runs Pi), against the relay's
  task-scoped chat route `/arm/<arm>-<label>/task/<task>/v1` with the
  placeholder key `relay-held`, whatever face the run model uses for Tofu, so
  every call is attributed to its trial (the harnesses use the base URL as a
  path prefix; Goose composes it from host plus base path). Their exact
  flags, environment and deviations are in `third_party.py`; each run's
  adapter card records them with the results.
- Install: the runtime (Node from the Pi pin, a static uv, or Goose's
  release archive) is a sha256-pinned artifact like any other. The harness
  package installs during the trial's `setup` phase into `/var/tmp/tofu-<arm>`:
  before and outside the agent budget, as the image's user, through the trial
  egress proxy, at its exact version, with transitive dependencies resolved
  as of `DEPENDENCY_SNAPSHOT` (`npm --before`, `uv --exclude-newer`). The
  resolved tree goes to `/logs/agent/setup` (`npm-ls.json`, `pip-freeze.txt`).
  A failed setup is an install error, never a scored trial. Setup time counts
  toward the trial's wall budget (`trial_budget_sec`).
- oh-my-pi ships preinstalled instead: its single-file release executable
  (the binary upstream's own installer installs), pinned by the release's
  `SHA256SUMS.txt` digest and packed into a runtime archive (`bin/omp`,
  root-owned 0755, zero timestamps), so its setup only writes its provider
  file. Its per-trial `npm install` of the package and Bun (~412 MB) had hung
  after its downloads until the setup budget expired, costing whole attempts.
  The binary links glibc, as the npm Bun build it replaces did.
- Nothing joins the task's `PATH`: each harness runs by absolute path, and
  Python harnesses use uv's managed CPython, never the image's interpreter.
- None of these arms has a source-guard projection, so a run that includes
  them needs `--no-source-guard` and `--egress-allowlist`. Provider-hosted
  web search does not exist for them on this relay: every fetch goes through
  the trial proxy and its allowlist.
- Runtimes: `python -m evaluations.terminal_bench.third_party fetch-runtimes
  --files-dir <relay files dir> --manifest <artifacts.json> [--source <dir>]`
  places the pinned archives (reusing a local copy with the pinned digest),
  packs each single-file release (`RELEASE_FILES`) after checking it against
  its pin (found in a `--source` directory under its asset name, e.g.
  `omp-linux-x64` or `cacert-2026-11-05.pem`, otherwise downloaded), and adds
  their manifest entries (`node`, `uv`, `goose`, `hermes-src`, `omp`,
  `ca-bundle`). A packed archive's manifest digest is computed when it is
  packed; packing is repeatable, so one file gives one digest.

## Claude Opus 5.5 at medium effort (`--protocol claude-6h-medium`)

- Run model `claude-opus-5.5`, wire `kunlunbedrock.claude-opus-5.5-m17`, on
  FRIDAY's Anthropic face. Start ONE relay for both arms:
  `python -m evaluations.terminal_bench.relay --port 18521 --anthropic-model
  kunlunbedrock.claude-opus-5.5-m17 --key-suffixes 6672,0733 --read-timeout
  1800 --retry-budget 900 --log ... --files-dir ...`.
- The protocol keeps vendor-6h's 21 600 s agent budget and resources, sets
  reasoning to `medium` for both arms and uses the model's own facts: no
  temperature/top_p (Opus 5.5's catalog profile omits them), a 1 000 000-token
  context window and a 128 000-token output cap.
- Tofu's routing envelope declares protocol `anthropic`, the FRIDAY wire name
  and FRIDAY's provider id (`example-corp`, which selects its Claude cache
  multipliers and gateway sanitization). Pi uses its built-in
  `anthropic/claude-opus-5-5` definition, whose `models.json` override points
  at the relay. Each trial's base URL is
  `/arm/<arm>-<label>/task/<task>/anthropic`, so the relay attributes every
  request to a trial; Pi sends no affinity header and sandbox peers are NAT'd.
- Reasoning equivalence: Tofu sends `thinking: {type: adaptive, display:
  summarized}` with `output_config.effort = medium`. Pi's built-in Opus 5.5
  entry declares `supportsMidConvoEffort`, so it asks for a constant top-level
  `effort: high` and carries `medium` in empty mid-conversation `system`
  messages. FRIDAY rejects those markers (`system content must contain at
  least one block`), so the relay folds them into the top-level effort their
  last marker asks for and records the fold (`pi_effort_fold`, `sent_effort`).
  Both arms therefore reach the model with `output_config.effort = medium`.
  `--preserve-pi-effort-markers` passes them through instead, to measure that.
- The relay logs, per request: cache breakpoints with their TTL, digests of
  tools/system/each message (excluding `cache_control`), joined tool-name
  bytes, a digest of the Mt-Session-Id value, the request's own settings and
  the provider usage. No prompt text or affinity value is logged.
- `count_tokens` is forwarded too, so Tofu's exact token counter keeps working.

## Decisions

**Problem:** the runner resumed OpenCode and Pi with `--continue` and a
fixed nudge (up to 16 times) when a run ended at the output-token limit,
and gave Tofu evaluation-only upstream-fault budgets
(`TOFU_GATEWAY_OUTAGE_BUDGET_S`, `TOFU_429_SATURATION_SECS`). Both compensate
for, or tune, behavior of the harness under test. In the 2026-11-03 run the
loop resumed OpenCode in 4 trials, 2 of which then passed. **Decision:** the
evaluation layer only provisions, enforces the shared budget, collects
evidence and verifies. Each harness runs once with its own defaults, and how
it handles an output-limit stop is part of what is measured. **Alternatives
rejected:** keeping the loop for every arm (it measures the runner's
recovery policy, not the harness's); extending an equivalent loop to Tofu
(same objection); per-harness retry knobs (tuning the subject of the
measurement).

**Problem:** Harbor + agentix wrapped the agent in layers we could not fix
quickly (Python 3.15 interpreter selection, a 14 400 s wrapper cap below the
protocol budget, CCR translation and a 16 384-token cap for OpenCode, sandbox
apt mirror rewrites). **Decision:** drive the sandbox VM directly with a
generic exec channel and keep all trial semantics in this package.
**Alternatives rejected:** patching Harbor/agentix per finding (each fix
needed a pushed branch and re-sync, and the layers kept diverging from the
product); the rootless local QEMU runner (no KVM capacity for 178 parallel
6 h trials on this host).

**Problem:** installing agents inside task images through package managers
broke on stale mirrors and picked wrong interpreters. **Decision:** prebuilt,
sha256-pinned runtime trees copied in with `docker cp`; Tofu runs with
`python3 -I` so a task's `lib/` package or `PYTHONPATH` cannot shadow it.
**Alternative rejected:** per-trial `uv tool install` (network-dependent,
version drift, Python 3.15 selection).

**Problem:** the third-party arms (2026-11-12) arrived while the host was
at its cgroup memory limit, with local installs and builds suspended, and
each harness would otherwise need its own packaging contract like Pi's.
**Decision:** keep the interpreter a pinned artifact (Node, a static uv,
Goose's binary), so the earlier problems stay solved: no image interpreter
is selected and no task mirror is used. Install only the harness package
per trial, in a setup phase outside the agent budget, at an exact version
with dependencies resolved as of one snapshot instant, and record the
resolved tree. **Alternatives rejected:** Harbor's install scripts as they
are (`curl | bash` from a moving branch, apt packages added to the task
image, unpinned dependency trees); prebuilt trees per harness now (needs
local builds; the recorded setup trees are what such a tree would freeze,
once a matrix needs per-trial installs removed). Revisited the same day for
oh-my-pi only: its per-trial npm install sometimes hung past the setup
budget, and upstream publishes a pinned single-file binary that needs no
build, so that binary is now the arm's runtime artifact.

**Problem:** blocking answer sources at the tool level missed `curl`/`urllib`
fetches. **Decision:** network-level proxy deny list plus an evidence audit
(fingerprints of the task's `solution/`, `tests/` and `README.md` lines —
none of which the agent is given — received before the agent wrote them,
canary GUID in tool results, benchmark-source tool calls). **Alternative rejected:** TLS
interception to see GitHub paths (changes every task's trust store).

**Problem:** the deadline kill found the agent by a marker passed as `$0` of
`bash -lc program`, but bash replaces itself with a lone simple command or the
last command of a list, so the marker vanished and the kill signalled 0
processes (the 2026-11-02 run's `opencode/mailman` agent and
`model-extraction-relu-logits` verifier attempts). **Decision:** run the
program under `MARKED_SHELL`, a parent shell that only waits, and track
victims by pid and start time so processes that outlive their marked parent
still get SIGKILL. **Alternatives rejected:** `docker exec <container> kill`
(trusts tools inside the image); stopping the container (also stops the
daemons the verifier tests).

**Problem:** `start_container` made both log mounts `chmod 0777`. A process the
agent or the verifier's own `test.sh` dropped to a de-privileged uid (e.g.
65534) could then create, rewrite, delete, rename over or symlink-swap
`/logs/verifier/reward.txt` — the reward channel was writable by the very code
being scored, and a `reward.txt` the agent planted before the verifier ran was
read back as a real reward when the test never wrote one. Verified in a real
container on cluster 857 (2026-11-09, one synthetic task, no model): with
0777 a uid-65534 child forged the reward six ways and a delayed grandchild
deleted it, ending in `no_reward`; a uid-1000 agent's planted `1.0` was the
score. **Decision:** `/logs/verifier` is a root:root 0700 directory
(`seal_verifier_logs`), emptied again immediately before the verifier runs so
nothing the agent phase planted survives; every verifier command runs as
uid 0, so the test still writes its reward and logs as root; `/logs/agent`
belongs to the image's user (the agent's uid/gid, read from the container) at
0755, so the agent still writes its own logs and nobody else can; rewards are
read only from regular files, never through a symlink, and a planted FIFO or
directory no longer blocks or crashes the reader. The red-team attacker is
`tests/support/reward_forgery_probe.py` (create, in-place rewrite, truncate,
delete, rename swap, symlink swap, hard-link swap, directory rename/chmod,
read), driven as uid 65534 by
`tests/evaluations/test_tb_reward_forgery.py`; that module is stdlib-only so
the VM smoke uploads and runs it inside the real container.
**Alternatives rejected:** keeping 0777 and trusting the verifier's `test.sh`
not to run untrusted code (the submitted code is the thing under evaluation);
only removing `reward.txt` before verifying, which still allows the swap races
after the legitimate reward is written; running the verifier as the agent's
uid, which shares the reward channel with the agent.

**Problem:** Harbor 0.25.0 does the same thing in its own run mode:
`environments/docker/docker.py` calls
`ensure_dirs(_mount_targets(writable_only=True))` at start, which is
`mkdir -p ... && chmod 777 /logs/verifier /logs/agent` as root
(`environments/base.py::_ensure_dirs_command`), `TrialPaths.chmod_dir` makes
the host-side trial/agent/verifier dirs 0777, and
`trial/trial.py::_run_separate_verifier` empties and re-`chmod 777`s the
verifier mount right before `test.sh` runs. Tofu's rootless QEMU adapter
inherits all of it even though `capabilities.mounted` is False (the trial
passes the log binds as `auto_mounts`). **Decision:**
`rootless_vm/harbor_environment.py` re-seals `/logs/verifier` to root:root
0700 at the end of every directory command it builds, and a verifier exec runs
as root explicitly (a task-level `[verifier] user` is honored by chowning that
directory to the named user, never by re-opening it to every uid). The logs
come back by guest download, so a root-only container directory does not
impede the host reader. **Alternatives rejected:** patching Harbor upstream
(different owner, and its `prepare_logs_for_host` chowns host binds back to
the host user, so a root-only mount would break a non-root Harbor host);
leaving the mount 0777 because the guest is a VM (the uid boundary is inside
the container, exactly where the agent's and the tests' code runs).

**Problem:** the VM ingress serves `/native/exec|status|read|kill` as root,
and the Harbor routes (`/run_sync|/run_async` run an uploaded task bundle,
`/update_proxy` rewrites the environment later children inherit), to anything
that reaches its port. A task container reached its own VM's ingress, and
peer VMs' ingresses at `hostIp:mapped port` (EVAL-MATRIX-2 probes, 2026-11-13).
`vm_trial.isolate_ingress` drops the own-VM and VM-subnet paths, but the
mapped port is on the node network, which no in-VM rule can cover.
**Status:** open on this branch. The decision below is implemented on
`parked/native-exec-auth-20261116`, which stays parked until the owner settles
his uncommitted `gateway.py` draft of the same change; until then
`gateway.py` sends no token and `state.json` records no `ingress_auth`.
**Decision (parked):** a per-VM-session token behind one default-deny boundary. The
token is `gateway.native_session_token`: HMAC-SHA256 of the session id keyed
by the sandbox secret. `SandboxSession.start` puts it only into the ingress's
start environment; every native call sends it as `x-native-token`. The
ingress (nlp/harbor `env_ingress.py`) refuses every route but `/health` and
`/monitor/alive` without it, refuses the native channel outright when started
without a token, and takes the token out of its own environment before
anything it runs (the trial driver, docker) could inherit it; `vm_trial`
passes only explicit `-e` values into containers. At start the session sends
one tokenless call and every attempt's `state.json` records the answer as
`ingress_auth`: `refused`, `served`, or `unknown: ...` when the ingress did
not answer yet. On `INGRESS_BRANCH` a VM that served it (its boot-time branch
sync failed, so the image's own ingress runs) gets no trial: `exec` raises
`IngressUnprotectedError`, the attempt ends as `submit_error` and the VM is
released. The start itself still succeeds, because the runner reports every
start exception to the 857 ledger as a platform failure, which holds every
Tofu lane. A burst seed's read-only token (`start_body(native_read_token=)`)
opens `/native/read` only under `BURST_SEED_READ_ROOT`. Rollout covers new VM
sessions only: a VM syncs its branch once at boot, so running VMs and runs
frozen on the open branch are untouched. **Alternatives rejected:** iptables
alone (cannot cover `hostIp:mapped port`); a random token per
`SandboxSession` object (327ee106cd: a restarted runner, a start retry that
reuses its body, and every separate `vmctl` command lost their VM); storing
the token in `state.json` or a file (copied into evidence and reports); one
static token for every VM (one leak opens every peer); keeping the channel
open when no token is configured (old VMs never run new ingress code, so it
only hides a misconfigured start); an unconfined read-only token (it reads
`/proc/self/environ`, which holds the session token).

## Verification

`pytest -q tests/evaluations/test_third_party_harnesses.py` (each third-party
arm's launch program, setup and install check parse; flags and environment
as Harbor's adapters; the setup phase runs before the install check and a
failed setup is an install error; chat-face trial attribution; runtime pins
refused on a digest mismatch),
`pytest -q tests/evaluations/test_tb_system_ca_bundle.py` (the system CA
store rule: spec contents, the manifest guard, installation as root before the
arm's runtime only when the image has no store, the store probe's file test),
`pytest -q tests/evaluations/test_terminal_bench_native.py` (offline: bundle
excludes solutions, spec/protocol, launch contracts, proxy allow/deny/CONNECT,
reward parsing, safe bundle extraction, summary, audit rules),
`pytest -q tests/evaluations/test_claude_code_native.py` (Claude Code command
assembly, bridge request/response/stream translation and an end-to-end run
against a fake relay), `pytest -q tests/evaluations/test_tb_source_guard.py`
(the blocklist rule and its bash/JavaScript/OpenCode projections, canary
pass/fail, audit evidence), and
`pytest -q tests/evaluations/test_pi_native_contract.py` (Pi launch spec,
guest program and event filtering against a scripted Pi, audit of Pi session
files),
`pytest -q tests/evaluations/test_tb_reward_forgery.py` (the reward channel is
root-only: directory modes and owners before the agent and before the
verifier, verifier execs as uid 0, agent-planted rewards/links/FIFOs removed,
rewards read only from regular files, and — with real root, or in the VM smoke
— a uid-65534 child unable to create, rewrite, delete or swap `reward.txt`
while a legitimate root reward is written and read back and the agent's own
log still works), and
`pytest -q tests/evaluations/test_harbor_verifier_log_permissions.py`
(rootless Harbor adapter: the verifier mount is re-sealed to root:root 0700
after Harbor's own `chmod 777` preparation, `/logs/agent` keeps its
writability, and a task-named verifier user gets ownership instead of a
world-writable directory). Setting `TB_NATIVE_PI_RUNTIME` to an extracted Pi runtime tree also
runs the real pinned Pi against a local OpenAI-compatible stub, asserting the
wire request and that an output-limit stop ends the run unresumed. Live smoke: two short tasks per arm
on cluster 857.
