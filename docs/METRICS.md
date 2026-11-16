# Agent harness metrics

Responsibility: what "the #1 agent harness" means in numbers — seven metrics,
their exact definitions, the first baseline and the targets. A harness is #1
when the owner gives it work and then only steers: it starts Workers at once,
recovers from its own faults, delivers, and never needs the owner to unstick
it. Owner: `scripts/harness_metrics.py` (computation) with
`tests/diagnostics/test_harness_metrics.py` (specification). The evidence
owners are [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md) (assignments,
births), [ROUTER.md](ROUTER.md) (Routers, Watchdog signals, owner delegation)
and the conversation sync protocol (`contracts/conversation_sync_v3.yaml`).

## How the numbers are read

The script reads only the running application's loopback GET API: the
conversation list, each harness conversation's main-lane Turns
(`/sync?segmentPayload=refs&toolPayload=lazy&turnWindow=tail-96`, then
`/turns/history` pages back to the window start), Worker task rows
(`/tasks/by-conv`), project-dispatch assignment pages and lifecycle approvals.
It writes nothing, attaches no storage handle and calls no provider. Harness
conversations are the Global Router, the Watchdog, every Project Router
(`coordinator-*` or a Worker's `_coordinatorSource`) and every Worker
(`_coordinatorSource` set); ordinary chats are out of scope. The window is N
complete local days ending at today's midnight; a Turn belongs to the day it
was created unless a metric says otherwise.

## Definitions

**M1 — Owner interventions per day.** Owner touches are (a) the owner's own
messages to a harness conversation: input Turns with initiator `human` (大象,
WeChat) or none (typed in the browser) that are not Router forwards
(`origin.forwardedFrom`); (b) the owner's browser turn actions, recognised by
their command id `<operation>:<turnId>:<revision>` for `continue`,
`regenerate`, `checkpoint_resume` and `answer_guidance` (Routers use
`session-control:`, restart recovery `restart-resume:`); and (c) lifecycle
approvals decided in the browser (`decided_by: ui`). Each touch gets one class,
first match wins:

| Class | Rule |
|---|---|
| recover | the reply to the owner's previous message here failed; a recovery cue (挂了, 卡住, 停工, 崩溃, 重启, 修复, 继续, retry, …) in the first sentence; or a Continue/Regenerate/checkpoint resume |
| approve | a consent reply (开, 可以, 删除, 行，…, a one- or two-character option such as "A"); a delegation cue anywhere (你决定, 你定, 全权, 无所谓, 我已经…); a reply of at most 20 letters after an agent question (decision or blocked report, `ask_human`, a Global Router question relay); `answer_guidance`; a browser lifecycle approval |
| correct | the owner stopped the previous reply; a correction cue in the first sentence (不对, 不要, 别管, 不需要, 为什么, 其实, 明明, 放弃, 那你…, why, should, …) |
| repeat | a repeat or chase cue in the first sentence (我说过, 到底, 了没有, 太慢, 加速, 全力推进, again, …); or a message at least 0.8 similar (difflib ratio, letters and digits only) to an earlier owner message in the window |
| new | anything else: new work, information, status questions |

Interventions are the touches classed recover, approve, correct or repeat; the
metric is their count divided by the days in the window. "不X" cues skip the
"X不X" yes-no form (对不对, 够不够). The cue lists live in the script; change
them only with new labelled examples in its test.

**M2 — Agent actions that needed the owner.** Actions are executed tool calls
in harness conversations (`rejected` calls never ran). Owner-needed actions
are `ask_human` calls, calls of tools whose contract permission is `approval`
(`attentionKind: interactive`), `report_to_user` calls of kind `decision` or
`blocked`, and lifecycle approval requests. M2 is owner-needed actions ÷
actions; the count per day is reported beside it.

**M3 — Worker-start success and latency.** A start is the first
`delegate_project_work` call per assignment id in the window (replays and
follow-ups of a Worker born earlier are skipped). It succeeded when the
Worker's conversation has a task row; it was refused when the call failed,
returned no Worker or `failed_to_start`, or when a later Worker status read
(`poll`, `await_project_workers`, `get_project_workers`) reported its birth as
`failed_to_start`: a birth runs after the call returned `starting`
([Delegation returns at once](PROJECT_COORDINATION.md#delegation-returns-at-once-its-worker-is-born-on-its-own-thread)).
Otherwise it did not start. Latency is the Worker's first task creation minus
the call's start (deferred dispatch excluded), reported as nearest-rank p50
and p95.

**M4 — MTTR, Watchdog fault signal to resolution.** For signals opened in the
window: open time from the Watchdog's wake notice (`Signal: <id> (source=…,
opened … UTC)`) or a resolved record, resolution time from
`watchdog_resolve_signals` results (`resolvedAt`, else the call's end). MTTR
is the median and mean of resolution minus open time; signals with no visible
resolution are counted, not timed.

**M5 — Throughput.** An assignment completed on the day its result Turn
(`execution.turnId`) settled `completed`; it was delivered when its delivery
is `delivered` (day: the publication request, at the earliest the completion).
Assignments whose result Turn settled otherwise are reported as ended
unsuccessfully.

**M6 — Cost per completed task.** The sum of `projection.cost.costUsd` over
assistant Turns of harness conversations created in the window (Routers and
Watchdog included: supervision is part of the cost), divided by M5's completed
assignments. Costs are the API-equivalent list price (`costBasis:
metered_api_equivalent`), not invoices.

**M7 — Coordinator availability.** Over the settled model Turns (`reply`,
`scheduled_reply`, `channel_reply`) of each Router: turn success is completed
÷ settled. Time availability is 1 − outage time ÷ observed time, where an
outage starts when a Turn that ran tool calls fails or is interrupted (at its
end), or when a Turn that ran none fails (at its creation, never before the
last completed Turn ended), and ends when the next Turn that does work starts.
An idle Router stays down until it next proves it works.

## Baseline: 2026-11-06 00:00 to 2026-11-13 00:00 (+08:00)

615 harness conversations, 3,979 Turns, 936 assignments, no gaps. The
evidence is one seven-day read (1,272 GET requests, 1.46 GB, about 26 minutes,
11-13 00:11–00:37), four Worker conversations re-read after their reads timed
out during two of the API host's frequent readiness stalls, and the eight
Routers re-read at 03:20 (152 MB) after the collector learned the
asynchronous birth refusals of M3; merged as `--from-evidence` merges.

| # | Metric | Baseline | Target | Why this target |
|---|---|---|---|---|
| M1 | Owner interventions / day | **16.1** (113 of 142 touches: recover 51, approve 35, correct 20, repeat 7) | ≤ 3, with recover and repeat at 0 | Recover and repeat (58, 51 %) are the harness failing to heal itself or follow through: 33 were the owner pressing Continue, Regenerate or Resume on stuck Turns, which a Router or Watchdog can do itself (R9 `conversation_control`). 17 were browser restart approvals, all before the owner's standing approval of 11-11 18:43 ("门禁通过就直接重启"); none since. What remains is steering: about 3 corrections a day. |
| M2 | Agent actions that needed the owner | **0.13 %** (156 of 122,399; 22.3 / day: `ask_human` 85, lifecycle approvals 38, decision reports 33) | ≤ 0.03 % and ≤ 5 / day | Only R11 classes (c) external and (d) human-only actions need the owner. Restart requests should reach 0: after the standing approval, 4 of 7 passed the gate without the owner and 3 still expired unanswered (12 of 38 for the week) while their requester waited. Decisions should arrive batched in one report. |
| M3 | Worker-start success | **95.0 %** (568 of 598; 30 refused) | ≥ 99.5 % | Every refusal was a harness defect, not an owner decision: "Synthetic authorization origin denied" 14 (3 of them on 11-12 were reported only by the next `poll` after the call had returned `starting`), concurrent dispatch snapshot/update 6, capabilities not issued 3, other 7. |
| M3 | Worker-start latency p50 / p95 | **26 s / 1,285 s** | ≤ 10 s / ≤ 60 s | Owner decision 2026-11-11: delegating is as immediate as sending a message. The asynchronous birth already gives 13 s / 102 s on 11-12; 11-11 (synchronous births under load) had 255 s / 1,732 s. |
| M4 | MTTR, signal → resolved | **157 min** median, 1,394 min mean, p90 5,921 min (228 of 328 resolved; 33 open at the last review) | median ≤ 15 min, p90 ≤ 120 min, ≤ 5 open | Recovery signals already close in 36 min (p50) and the quota watch closes its own in about a minute; channel signals (p50 519 min) waited for batch clean-ups days later. Reporters that observe recovery should close their signals, as the quota watch does. |
| M5 | Completed / delivered per day | **89.0 / 44.4** (623 completed, 311 delivered; 197 = 24 % of 820 finished ended failed or interrupted) | failed + interrupted ≤ 5 % of finished; ≥ 90 % of file-changing completions delivered; then ≥ 75 delivered / day | Demand is not the limit: 180 completed assignments sit `blocked`, 34 `preserved`, and a quarter of finished work failed. Converting those is the cheapest throughput. |
| M6 | Cost per completed task | **$124.36** ($77,477 total; Workers 89 %) | ≤ $60 | A quarter of finished assignments failed after spending; with the cache and compaction levers of [LLM_COST_OPTIMIZATION.md](LLM_COST_OPTIMIZATION.md) halving is the first step, not the end. |
| M7 | Coordinator turn success | **88.3 %** (926 of 1,049) | ≥ 99 % | The Router is the owner's single point of contact; each failed Router Turn is a request the owner must repeat. |
| M7 | Coordinator availability (all / worst) | **93.7 % / 80.9 %** | ≥ 99.5 % for every Router; no outage > 15 min | The tofu Project Router was down 32 h this week. On 11-12, its outage day, the owner made 22 recovery interventions. A restart is planned work and should resume within minutes. |

| Day | Interventions | Owner-needed | Starts ok / all | Completed | Delivered | Cost |
|---|---|---|---|---|---|---|
| 11-06 | 17 | 23 | 89 / 104 | 100 | 19 | $11,037 |
| 11-07 | 22 | 17 | 73 / 74 | 41 | 20 | $3,397 |
| 11-08 | 19 | 21 | 99 / 102 | 121 | 64 | $9,605 |
| 11-09 | 9 | 17 | 111 / 118 | 128 | 90 | $14,490 |
| 11-10 | 9 | 7 | 48 / 48 | 54 | 37 | $9,910 |
| 11-11 | 10 | 32 | 98 / 99 | 62 | 32 | $13,021 |
| 11-12 | 27 | 39 | 50 / 53 | 117 | 49 | $16,017 |

| Router | Settled turns (ok/failed/interrupted) | Turn success | Down (min) | Longest (min) | Availability |
|---|---|---|---|---|---|
| tofu Project Router `coordinator-1a390889…` | 74 (33/28/13) | 44.6 % | 1,923 | 574 | 80.9 % |
| parallel Project Router `coordinator-edae46bf…` | 110 (80/15/15) | 72.7 % | 979 | 488 | 90.3 % |
| autoharness Project Router `coordinator-d4e467e4…` | 3 (2/0/1) | 66.7 % | 906 | 906 | 91.0 % |
| Global Router `coordinator-98907386…` | 319 (286/28/5) | 89.7 % | 337 | 90 | 96.7 % |
| rsi-exam Project Router `coordinator-ec2e3892…` | 2 (1/0/1) | 50.0 % | 327 | 327 | 96.8 % |
| ndp Project Router `coordinator-8b5047cf…` | 9 (8/0/1) | 88.9 % | 38 | 38 | 97.9 % |
| Watchdog `coordinator-17715d53…` | 527 (511/13/3) | 97.0 % | 0 | 0 | 100 % |
| apk Project Router `coordinator-2666b08e…` | 5 (5/0/0) | 100 % | 0 | 0 | 100 % |

The tofu Project Router's outages longer than 30 minutes: 11-06 14:10–14:54,
11-06 22:38–23:48, 11-07 00:10–09:44, 11-07 13:07–20:40, 11-11 17:46–11-12
00:40 and 11-12 15:51–21:29. The last is the "5-hour outage" its own report
gives as 16:01–21:15. The computed outage starts at 15:51:41, when a restart
interrupted the last Turn that did work. It ends at 21:29:04, when the owner
pressed Regenerate: the service was ready at 21:15, but the Router did not run
again until the owner intervened. That Regenerate is counted in M1.

## Validation and limits

- M1's classifier was checked against hand labels. On this window's 92
  messages, whose errors shaped the cue lists, precision and recall of
  "intervention" are 0.95 and 0.95. On 19 blind-labelled messages from
  11-03 to 11-05 they were 0.88 and 0.58 before, and 0.90 and 0.75 after,
  the delegation cues (你定, 全权, cues anywhere) were added. Read M1 as a
  slight undercount; its trend is comparable because the rules are fixed.
- Only each Turn's current attempt is visible, so a browser Continue
  superseded by a later attempt is missed, and M7 uses the current attempt's
  start.
- M4 sees what the Watchdog transcript shows. Signals closed by their
  reporter (quota watch, compute stall, a notified Router's recovery) appear
  without a resolution; 100 of 328 have none. An exact M4 needs a read-only
  page of `watchdogSignal` records (`FaultSignals`), which no endpoint
  exposes today.
- 90 settled model Turns carry no cost figure. Branch lanes, ordinary chats
  and Turns still running at collection time are out of scope.

## Daily recomputation

There is no setting: the script takes the window and output paths as
arguments. Run it once a day, after midnight, as an owner-visible scheduler
`command` task (for example `30 8 * * *`) from the deployment checkout:

```sh
D=$(date -d yesterday +%F)
.venv/bin/python3 scripts/harness_metrics.py --days 1 \
  --evidence data/harness-metrics/evidence-$D.json --json data/harness-metrics/$D.json
# the week, from the last seven daily evidence files, with no API reads:
.venv/bin/python3 scripts/harness_metrics.py \
  --from-evidence $(ls data/harness-metrics/evidence-*.json | tail -7)
```

A one-day run reads about 0.5 GB in 5 minutes; a seven-day run reads about
1.5 GB in 26 minutes. That is why the week is merged from daily evidence. A
merged week reports any missing day as a gap, and a later read of the same
Turn or assignment replaces an earlier one. Evidence files hold the owner's
messages, which M1 classifies, so they stay under the gitignored `data/` with
owner-only permissions. The report and `--json` carry only identifiers and
counts.

Alternatives rejected: an in-process endpoint (it puts full-history reads on
the loaded API host and is live only after a restart, while the script reads
the same public API from anywhere); attaching a storage client (TofuDB
diagnostics go through the loopback API, [STORAGE.md](STORAGE.md)); logs as
the source (`app.log` keeps a few hours, `audit.log` about a day and a half);
model-based message classification (paid calls, and not reproducible day to
day).
