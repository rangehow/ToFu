# Deadline-only harness candidate

## Current implementation and rejected controls

**Decision:** public `AgentRuntime.start` stamps the run's original expiry from
`timeout_s` before routing/setup and intersects it with existing execution limits
via `lib/agent_core/execution_limits.py::with_run_deadline`. The root loop already
checks `execution_limit_reason` before rounds; model/tool admission and
`invocation_deadline_requested` retain their existing cancellation/expiry owners.
No new tuning parameter, pool, hardware-derived admission or benchmark change.

**Located evidence:** at source base `643bac01bb01021e7f27ac04a8129bbbc19c48d1`,
the previous proposal's `run_phase.py`, `run_phase_control.py`, test suite and
`RUN_PHASES.md` were not delivered. There is no delivered sufficiency/reissue gate
to remove. Do not reintroduce it: `true`, `printf PASS`, a successful read, or
any other exit-0 command is not task verification. The deterministic real-root
fixture runs a fake check followed by a required read without a reissue loop.

**Rejected assumption:** the longest observed round bounds the next round or
finalization. It does not. A round may include an arbitrarily slow model, write,
provider wait or verifier; the next round may exceed every previous observation.
The silence threshold in `lib/tasks_pkg/liveness.py` diagnoses a stalled task;
it is not a completion-time bound and cannot be reused as a reserve.

**Missing prerequisite:** a caller-owned, trusted delivery contract must identify
an exact candidate artifact and applicable verifier, authorize its publication,
and bound the complete write/verification/settlement operation. Only then can a
work cutoff be derived as declared expiry minus that contractual bound. Existing
`executionLimits` declares expiry, tokens and tools, not that operation or bound.
`tool_delivery.py` already supports an application-owned verified delivery, but
neither selects an arbitrary coding-task verifier nor promises a finish duration.
No general explore→attempt→verify→finalize policy or mandatory reserve ships.
No promise is made that a nearly expired run creates an artifact from nothing.
Existing partial files are neither deleted nor rolled back when the deadline is
observed. Cancellation continues to forbid further model/tool dispatch.

## Evidence audit (original snapshot, not a new experiment)

Read-only recount of the supplied report's frozen data under the parallel project:
`results/analysis/tofu_improvements_2026-11-18/readout_snapshot/trials_s1.json`.
Tofu: 30 timeout rows, all with reward, mean 0.1066283333, 8 positive. OpenCode:
20, mean 0.05, 1 positive. Codex: 20, mean 0, none positive. Measured Tofu cells
are 111; infra/setup/unmeasured rows are not included in that denominator.

Independent paired discordance calculation on measured tasks: Tofu−Codex n=99,
11 positive/4 negative timeout indicators, difference 0.070707, exact two-sided
binomial/sign-flip p=0.118469; Tofu−OpenCode n=104, 16/8, difference 0.076923,
p=0.151590. These agree qualitatively with the report's Monte Carlo p-values
0.117/0.152: **not significant**. Neither S0 nor S1 establishes a whole-system
ranking; keep them separate and retain the report's multiple-comparison caveat.

Paper A sources independently opened: `results/analysis/loop/per_model.csv`
(MuSiQue/base/MiniMax: 24.242 rounds, cap-hit 0.924, 15.556 post-answer rounds)
and `results/analysis/stopping/rerun_contrasts.csv` (Qwen3.5-9B seed-matched rerun,
n=180: calls −1.52 [−2.61, −0.46], EM p=0.208, cap-hit p=0.253). Instruction-only
stopping did not establish the required outcome, though post-sufficiency rounds
fell −1.79 [−3.20, −0.23], p=0.0055. It is not evidence that an exit-0 command
is sufficient or that any particular reserve is valid.

## Disjoint artifact and evaluation protocol

Build **deadline-only**, not `tofu-finalize`: separately pinned wheels from base
and candidate source commits in distinct directories; record each source SHA,
wheel SHA-256, package version, build command and validation outcome. A wheel is
not a self-contained Paper B VM archive; Python and declared dependencies are
required. No production installation, restart or model experiment follows.
The durable build manifest is under `.eval-runs/deadline-rework-4b094529/`.
Identical package versions are not interchangeable: use the exact digest.

Built wheels (both `tofu_agent-0.19.2-py3-none-any.whl`, in disjoint directories):

| Arm | Source commit | SHA-256 | Artifact validator |
|---|---|---|---|
| control | `643bac01bb01021e7f27ac04a8129bbbc19c48d1` | `095e6cca594bbf8d21acab54dd3e0034ffd710b2cfb6619d31aef4cdd1520bfa` | pass |
| deadline-only | `f81c3ca87321566cf67a25fe7d5112df3836cbec` | `6aaec50b2f3d3dd0e6d73e0f581d0ab938333e6d52693339d0bab405a432fb8a` | pass |

**Not runnable-ready:** actual isolated CLI smokes of both exact wheels fail
before the first model request: `task_start_failed`, missing `lib.storage`.
`lib.project_dispatch.repository` eagerly imports the excluded storage package.
This is independently reproduced on control and candidate, not a deadline
regression. Further bounded diagnosis exposed other durable-only import paths
(continuation/delegation, round prefetch, scratch placement and credential
redaction); a one-import lazy fix is insufficient. Exploratory repairs were all
reverted instead of shipping an incomplete composition/security change. The
existing `test_tofu_agent_storage_free_run.py` remains failing for this reason.
`smoke_results.json`, per-arm import reports, and `defects.json` retain evidence.
The offline build first failed on a cold cache; successful builds fetched only
the declared pinned build backends into isolated build environments. A/B model
spend is blocked until a separately scoped storage-free repair passes actual
installed-wheel execution. Static artifact validation alone did not catch this.

Future evaluation uses a new runner/code snapshot and isolated work dirs
`deadline_only/rep1` and `deadline_only/rep2` (separate working directories for
both arms as well). Never point either at S0/S1/A5/A6 or Paper 3 directories.
Existing benchmark runners/pins remain unchanged. Hold task, budget, model,
relay/pacing and credentials equal. Any native or VM packaging requires its own
actual install check and pin; wheel validation alone does not certify that path.

Required paired endpoints, task-cluster bootstrap and declared multiplicity family:

- Timeout indicators under the same original outcome definition (report both
  wall-clock and explicit deadline-cause counts; do not relabel early errors as
  successes to improve timeout rate).
- Reward on each arm's timeout rows **and** candidate reward on the fixed set
  where control timed out. Keep missing rewards distinct; include an all-terminal
  missing-as-zero sensitivity. Baseline mean 0.106628 and 8/30 positive are not
  guarantees for another run.
- Full success (reward exactly 1), overall reward and short-anchor regressions.
- Post-deliverable rounds only after an applicable external verifier passes for
  the exact artifact version. With no such evidence, report unknown, not a value
  inferred from command exit 0. Count repeated reads by exact operation plus
  source version, distinguishing deliberate polling and changed sources.
- Input/cached/output tokens and agent/model/throttle seconds; aggregate tokens
  and seconds per fully solved task, with denominators/coverage. Dollars require
  an explicit tariff. Reward/cost guardrails must be declared before model spend;
  no cost-inflation threshold is invented here.

Power limits: the report gives S1 paired timeout SE about 0.045 at n≈100 and a
worst-pair reward MDE 0.096 at 166 tasks. A 60-task subset or two correlated reps
cannot establish a small whole-system effect. No new power calculation or reward
benefit has been verified here. A lower timeout count alone is not efficacy.

## Scoped acceptance and remaining ownership

This tranche accepts explicit deadline plumbing only, not mandatory finalization
or an efficacy result. Packaging prerequisite owner
`worker-9863f9db8fc061e9bb491560` owns the actual wheel-boundary repair; this
change does not duplicate it. Overall finalize behavior and packaging remain open.
No live restart, benchmark runner, experiment pin or model-spend change is made.

## Regression evidence

`test_timeout_is_stamped_before_setup_without_mutating_config` advances the
setup clock by 45 seconds and still requires the original 1060 expiry, with
unchanged caller configuration and token/tool allowances. The scoped rework's
full deadline file covers 15 cases, including equal/zero existing deadlines and
the exact unbounded token/tool shape when only a timeout is supplied. Initial added-test failures were test
fixture mistakes (missing `monotonic` on the clock stub and asserting a normalized
away empty tools key), not runtime defects; corrected before acceptance.

Unchanged neighboring verification is reused from
`.eval-runs/deadline-rework-4b094529/regressions.xml`: 295 tests, zero
failures/errors/skips, including the original 12 deadline cases.
`defects.json` records failing-first 3 failed / 8 passed and final 12 passed;
the bounded rework audit did not locate a raw pre-fix failure log, so that
failing-first count is recorded-summary evidence, not a newly reproduced run.
Existing wheel digests were rehashed and match the source-pin manifest; the
complete archived source trees were not independently compared against Git.

`tests/tasks_pkg/test_public_agent_run_deadline.py`: original timeout propagation
and retained-partial/admission cases failed before the fix; explicit clock
advancement (no deadline sleeps) covers expiry, earlier caller deadline,
no-deadline behaviour, and cancellation. Fake-check tests intentionally keep
following required work; they do not claim an achievable deliverable can be
recognized generically. The real-root loopback test saves a partial file, executes
`printf PASS`, and still consumes the required evidence in the next read.
