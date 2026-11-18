# Compute liveness patrol (Hope)

Responsibility: a standing, read-only liveness patrol of the owner's Hope
account. It flags jobs that hold accelerator cards without using them, and
queued jobs that nobody claims. Each flag is delivered once, as a Watchdog
fault signal; the Watchdog notifies the Global Router, which routes the flag
to the owning lead. **The patrol only notifies.** It never stops, resubmits or
reprioritizes a job, of any project, and no code path in it can.

**Code:**
- `scripts/hope_patrol/`: the off-host `patrol`, the shared `rules`, the host
  `probe`, `status` and `submit.sh`.
- `lib/router/watchdog/compute_patrol.py`: the intake.
- `contracts/compute_patrol_v1.json`: cadence, rule constants and the file
  formats both sides read.

**Signals:** source `patrol`
([ROUTER.md R7](ROUTER.md#r7-the-router-watchdog-is-an-independent-monitor-session)).

## Problem

On 2026-11-13 three knowledge-injection jobs held 16 H910C cards each for 1.9
to 10.7 hours, with flat NPU memory and near-zero worker CPU. Stale queued jobs
from 11-07 also sat in the queues. Only a one-off manual audit found them.

The owner's rule (11-13) is to launch as many jobs at once as is useful, and
never to leave a job hung with nobody managing it. The patrol must find such
jobs every half hour, without loading the production host and without ever
sending the owner an SSO approval.

Four facts constrain where each part can run (verified 2026-11-13):

1. **Hope inventory needs no SSO.**
   - The Hope CLI token is `$HOPE_HOME_DIR/.hope/.token` on DolphinFS. Every
     Hope job mounts that path at the same place.
   - `hopetemplate/jobs/get_yarn_apps` lists queue apps (id, name, state,
     creator, creation time, cards) and job states.
   - An expired token is an error answer, never a prompt.
   - `get_yarn_apps` accepts any token, even one every submit rejects
     (2026-11-18). A working listing therefore says nothing about the token.
     The CLI's `hope ls` GET, `hopetemplate/jobs/get_supported_job_type`, does
     check it: `return_code -2` ("Token expired") for a token Hope no longer
     accepts.
   - Renewal is `hope login`, which needs an owner approval, so the patrol never
     renews the token.
2. **Activity signals need the owner's MLP session, which exists only on the
   production host.**
   - Per-role CPU, NPU memory and NPU utilization series come from MLP
     (`/mlapi/kub/job/{id}/metric/{metric}/allRoles`).
   - The only session is `~/.mlp-cli-token.json` on the host's local disk. Its
     refresh token rotates, and the shared adapter (`packages/tofu-mtsso-auth`)
     forbids copies.
   - Its business call self-heals with a 大象 approval. Only `session_status`
     and `user_token(..., interactive=False)` never prompt.
   - Container stdout is also MLP-only:
     - with the Hope token, `get_task_attempts` returns no attempts for the
       AFO-k8s jobs, so the in-cluster log downloader has no locators;
     - `get_run_log` returns only the submission log.
3. **No live path accepts an external observation into the Watchdog or the
   Global Router.**
   - Every signal reporter runs inside a Tofu process.
   - Storage has one authority and no out-of-process writer.
   - There is no HTTP intake.
   - The Router, Coordinator and Watchdog roles have no file tools.
   - A scheduled agent turn carries only its static instruction.
4. **A Hope job cannot reach Tofu's API** without a Tofu credential on a
   group-readable volume.

## Decision

**Where it runs.** The patrol is one long-running Hope CPU job on
`root.hlsc_training_cluster.your-user.omni_production`: one worker, 2
vcores, 8 GB, in the image's stdlib Python 3.10. All polling, history and
judgment run there. The production host does only the two parts facts 2 and 3
leave to it, each bounded:

- **Activity probe** (`scripts/hope_patrol/probe.py`). An owner-visible
  scheduler `command` task.
  - Each run reads the patrol's candidates: Running jobs that hold cards.
  - For each candidate it makes one MLP read per signal over the last window,
    and writes `observations.json` for the patrol's next cycle.
  - That is at most three HTTPS reads per candidate per half hour, and one short
    process: no loop, thread or resident process.
- **Intake** (`lib/router/watchdog/compute_patrol.py`). It runs inside the
  existing Watchdog periodic review (`7,37 * * * *`), before the snapshot. It
  reads one small file per patrol and records or resolves signals.

**Credentials. No call can trigger an SSO approval.**

*The patrol:*
- reads the Hope token in place;
- never copies, prints or logs it;
- sends it only to the two endpoints the patrol knows, in the fields Hope
  requires: the `get_yarn_apps` POST body, and the query of one
  `get_supported_job_type` GET per cycle that verifies the token.

*The probe:*
- gets its MLP ticket in its own process through the adapter, only after
  `session_status` answered `ready`;
- asks with `interactive=False`, which is the adapter's non-interactive path;
- stops at the first rejected ticket rather than exchanging again;
- with any other status, records `authState` and reads nothing.

`tests/compute_patrol/test_probe.py` is the executable form of this rule. It
replaces the adapter module with one that fails on any prompting path, then
runs the probe end to end.

*Renewals:*
- every publication carries the Hope token's expiry
  (`health.hopeTokenExpiresAt`);
- one day ahead it raises `hope_token_expiring`;
- it raises `hope_token` when the token's local expiry has passed, or when
  the per-cycle check finds that Hope rejects the token. It does so even while
  the queue listing still answers, and keeps reading the listing. If the check
  itself cannot complete, `hope_token` reports the token as unverified, not as
  rejected;
- an MLP session that is not ready raises `activity_probe`;
- renewal stays the owner's decision.

**Cadence.** One cycle every 30 minutes, as the Router's spec asks
(`x-cadenceSeconds`).

**Hang rule (910C-aware).** NPU utilization on 910C reads low even on active
training, so low utilization alone never flags a job; it only vetoes. A Running
job that holds cards is **likely hung** only when its observation covers a full
window (two cycles, 60 min) and all of these hold:

- mean NPU utilization is at most 1.0 %;
- NPU memory range (max − min) is at most 1.0 percentage point. Any change in
  NPU memory is NPU work, and a short burst is progress;
- worker CPU decile range (p90 − p10) is at most 0.5. A heartbeat thread makes
  isolated blips, and those are not progress.

Lifetime aggregates carry only mean and max, so their range is max − mean. A
window shorter than 60 min, an observation older than two cycles, or a missing
signal is `unknown`, which never flags a job. An open episode stays open while
its verdict is `unknown`, because nothing proved recovery.

*Calibration.*

The 2026-11-13 audit's lifetime aggregates (`get_job_report_metrics`, mean/max):

| job | audit verdict | NPU util | worker CPU | NPU memory % | rule |
|---|---|---|---|---|---|
| psxn3o6xru396rfe [ki-A2/mixdoc] | idle | 0.0 / 0.0 | 0.0021 / 0.0057 | 4.6565 / 4.6568 | hung |
| psxlirpxru4vuomj fastacq_c_v2big_sweep2 | idle | 0.78 / 6.1 | 1.426 / 1.478 | 9.7600 / 9.7603 | hung |
| psxll8uxru4vuomj fastacq_c_frontier_grid | idle | 0.32 / 2.75 | 1.474 / 1.539 | 8.251 / 8.627 | hung |
| psxls68xru4vuomj [ki-E 7b training] | active (calibration) | 2.14 / 5.13 | 4.53 / 5.87 | 49.56 / 52.16 | active |
| psxmwqkxru396rfe [ki-D/scale3b_gc] | inconclusive | 1.61 / 4.63 | 2.81 / 4.24 | 28.27 / 28.40 | active |

The 15:28 CST live pass read 60-minute series of the account's Running card
jobs, with the probe's own reads:

- Idle jobs sit at the 4.6 % NPU-memory baseline, with memory range ≤ 0.024 pp
  and CPU decile range ≤ 0.033.
- Working jobs had NPU-memory ranges of at least 11.8 pp, although their mean
  utilization read only 0.18–0.96 %.

Two live cases forced the choice of statistics; both are spec fixtures in
`tests/compute_patrol/fixtures/live_series_20261113.json`:

- **[ki-E/smoke8]** (CPU mean 0.01, one 0.50 blip in 241 samples): with a plain
  max − min, the CPU check would have missed this idle job.
- **psxn96pxru396rfe** (idle except one burst of NPU util to 23 % and NPU
  memory +69.6 pp): a decile statistic everywhere would have flagged it falsely.

*Margins.* The CPU bound sits 7.6× above the audit's largest idle spread and
2.7× below its smallest active one. The memory bound sits 2.6× from both, and
the live working jobs sit an order of magnitude above it.

*Window.* One flat cycle can be a checkpoint save or an evaluation pause, so the
window is two cycles.

*What it does not cover.*
- A job doing CPU-only work on a card queue is not hung by this rule (its CPU
  moves), although it wastes cards.
- Log recency is not used, because the Hope token cannot read container logs
  (fact 2).

**Stale queued jobs.**
- A job in a pre-running state (`x-states.preRunning`) created more than 24 h
  ago is flagged (the Router's spec), with `since` set to its creation time.
- A lead claims a job in `claims.json` (`{"<job id>": {"by", "reason",
  "until"?}}`), which suppresses its flags while the claim lasts.
- Resolving the signal also acts as a claim: the episode keeps its identity, so
  it never reopens.

**Delivery and dedupe.**
- The patrol owns every verdict and episode. It keeps per-job history in
  `state.json` and publishes the open flags in `publication.json`. Each flag
  carries kind, job id, queue, name, creator, owning project when
  `owners.json` infers it, evidence and `since`.
- Each review records each flag as one `patrol` signal with identity
  `(patrol, kind, job id, since)`. Later cycles and replays of the same episode
  are the same signal, and a signal the Watchdog resolved never reopens.
- The review resolves the open patrol signals whose episode the patrol no
  longer publishes (recovered, started, ended or claimed).
- An episode whose queue could not be read stays published unchanged: a failed
  read proves nothing.
- A new signal wakes the Watchdog. The hint tells it to notify the Global
  Router once (`watchdog_notify_router`) and never to stop the job. The Global
  Router routes the flag to the owning lead.
- A later hang of the same job is a new episode, with a new `since`.

**Survival across restarts.**
- A Tofu restart does not touch the patrol job.
- The patrol resumes from `state.json`. If that file is lost, an ongoing hang
  can be reported once more.
- Signals and the review task are durable.

**Stale patrol.**
- Every publication carries `staleAfter`: the next cycle's due time plus one
  cycle.
- A review past it records one `compute_patrol_stale` signal for that
  publication. While the patrol is stale, its job signals stay open. The next
  fresh publication resolves the stale signal.
- A deliberate stop (`control/STOP`) publishes `stopped: true` with no flags.
  The review then closes every open patrol signal, and the patrol is never
  reported stale.
- If the review itself stops, its own heartbeat
  (`logs/watchdog-heartbeat-<owner>.json`) alerts through the existing
  out-of-band watcher.

**Trust.** A publication is untrusted input from a shared volume. The intake
checks it against `contracts/compute_patrol_v1.json` and rejects one that names
another owner. Its text reaches the Watchdog only inside the nonhuman signal
notice.

**Production writes.**
- Hope: only the patrol's own CPU job.
- Files under `data/compute_patrol/u<owner>/hope/`.
- One scheduler `command` task (the probe).
- Watchdog store: `patrol` signals, written by the intake once it is live after
  a restart. Until then the intake runs dry (`status.py`) and real flags are
  relayed by hand.

## Alternatives rejected

- **The whole patrol in a Tofu scheduler task.** It would be simpler, but the
  Router required the polling off the production host.
- **MLP reads in the Hope job, using a copied or dedicated SSO session.**
  - A copy's rotating refresh token would revoke the host's session and cause
    exactly the prompts the patrol must avoid.
  - A dedicated session would need owner approvals at each expiry, and a
    session file on a group-readable volume.
- **No activity signals.** A Hope-only rule cannot tell a busy job from an idle
  one.
- **Log recency as the primary signal.** It would need container logs, which
  only MLP serves (fact 2).
- **Delivery through a scheduled agent turn or a project message.** These carry
  only static text or need a model turn per cycle, and the Router-family roles
  cannot read files.
- **Reusing the browser-crash intake or another unrelated one.** It would be
  the wrong authority for compute faults.
- **A new scheduler task type for the intake.** The Watchdog review already
  runs at this cadence, has a deadline and is watched by the out-of-band alert.
- **One statistic for every signal.**
  - max − min everywhere misses idle jobs with heartbeat blips.
  - Deciles everywhere hide real NPU bursts.
- **Utilization-only thresholds.** They flag active 910C training.

## Runbook

All commands run on the Tofu host from the shared checkout.

```sh
P=/path/to/your/project
```

| Task | How |
|---|---|
| **Start** (or restart after a stale alert) | `sh scripts/hope_patrol/submit.sh $P 1 your-username`, with the Hope CLI on `PATH` and `HOPE_HOME_DIR` set. The steps are below. |
| **Stop** | `touch $P/control/STOP`. At its next cycle the patrol publishes `stopped: true` and exits, and the review closes every open patrol signal. To resume, rerun Start (it deletes `STOP`). |
| **Status** | `.venv/bin/python3 -m scripts.hope_patrol.status --dir $P` (read-only). It prints the last publication's age and its stale verdict, inventory, token expiry, the probe's last read and the open flags. It also prints each signal the intake holds open, with its exact detail. `--now <ms>` evaluates at another moment; `--json` prints the plan. |
| **Activity probe** | One owner-visible scheduler `command` task, cron `13,43 * * * *`, running `cd /tmp && PYTHONPATH=<checkout>/packages/tofu-mtsso-auth/src:$P/bin <checkout>/.venv/bin/python3 -m hope_patrol.probe --dir $P --owner 1 --mis your-username --project your-user`. Its result line shows `authState`; `observations.json` holds the read. |
| **Leads' controls** | `claims.json` claims a job; `owners.json` maps job-name patterns to projects; `queues.json` seeds the queues scanned. The patrol also scans every queue where it saw one of the owner's jobs in the last 7 days. |

**What Start does:**
- Refuses while another patrol still publishes (`--takeover` replaces it).
- Deploys the same code and contract to `$P/bin` for the probe.
- Seeds `queues.json` and `owners.json` once.
- Stages the job package in `/tmp` and submits it.
- Logs the run id in `$P/SUBMISSIONS.log`.
- The first publication follows the Hope start delay (about 15 min on 11-13).

**Where results live:**
- `$P/publication.json`: the latest cycle.
- `$P/state.json`: history and episodes.
- `$P/observations.json`: the probe.
- `$P/cycles/<date>.jsonl`: one line per cycle, kept 7 days.
- The job's stdout: one line per cycle.
- The Watchdog's `patrol` signals.

**Who is alerted:**
- **Hung or stale-queued job:** the Watchdog, which notifies the Global Router,
  which routes to the owning lead.
- **Stale, stopped-by-death or degraded patrol:** the Watchdog, which notifies
  the Global Router; the runbook names the fix.
- **A stopped review:** the out-of-band watcher alerts the owner.
