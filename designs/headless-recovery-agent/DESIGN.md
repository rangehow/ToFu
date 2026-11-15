# Self-repair: in-platform and out-of-band (design, ETAs)

Owner, 2026-11-12 19:18 and 21:3x: 「以后这个负责人挂了，你自己不能修复吗？……你要有等同我的权利啊。」
Five incidents today needed him or the Global Router. This design makes Tofu repair them itself, inside
the bound he set: everything reversible and audited runs without him; what only he can decide stays his.

## Two layers, one hand-off

| | In-platform | Out-of-band (this lane) |
|---|---|---|
| When | storage and API up; a Turn, lead or lane is broken | storage, the API or every lead is down; or the in-platform layer escalates or fails |
| Detects | the Watchdog review (R7) over durable facts | the watcher outside Tofu (`serverctl_pkg/oob_alert.py`): probes, emitting-format log lines, local heartbeat and escalation files |
| Repairs | an ordinary repair Worker in a lane | a headless repair session started by the watcher, no TofuDB, no Tofu conversation |
| Builds it | COORD-SELF-HEAL; authority fixes AUTHORITY-INVENTORY; wake/CAS and certification of 4540cc98 the Router repair lane | OOB-ALERT-1 / SELF-REPAIR |

Hand-off contract (local disk, never TofuDB): the in-platform layer writes
`logs/self-repair/<fingerprint>.lease` while it repairs (mtime renewed each review) and
`logs/self-repair/<fingerprint>.escalate.json` when it gives up. The watcher starts a session only for
an outage it detects itself or an escalation, and never while a live lease exists; its own claim is
mirrored to `logs/self-repair/oob-<fingerprint>.claim` so Tofu skips that incident. A fingerprint is the
typed cause plus its subject (conversation, process or code owner), never free text.

## Acceptance cases

| Case (11-12) | Layer | Grounded trigger | Runs without the owner | Stays with the owner |
|---|---|---|---|---|
| 1. Lead failed every generation 16:01–21:00; compaction archive above the 64 MiB engine cap reported as retryable `storage_unavailable`; 11 owner inputs queued | in-platform; out-of-band if the Router cannot act | consecutive failed attempts of one Turn with one typed cause, plus pending owner inputs (attempt facts) | code fix in a lane (split under the cap, type the refusal non-retryable), failing-first spec, review, land, restart through the gate, regenerate, drain the inputs in order | nothing, unless the fix needs deleting history |
| 2. Owner forward steered into a commitment-wake Turn; every dispatch refused "Synthetic authorization origin denied" | in-platform | `session_control_refused` in a Turn whose input is nonhuman while a steered owner forward exists (Turn origin plus steer receipts) | re-deliver the forward as a new Turn once the lead is idle (his own words, no new authority) | the contract change (accept a verified steer, or queue forwards): an authority boundary, AUTHORITY-INVENTORY prepares it |
| 3. 15:50 restart closed uncleanly: bot channel sessions did not quiesce | out-of-band | `WARNING [server_manager] explicit restart replaced an execution host that exited after an unclean close: RuntimeError: Process background services have not quiesced: bot channel sessions (still running)` | quiesce fix in a lane (with the bot-channel lane, not a duplicate), spec, review, land; restart at the next appropriate moment (grant P03 「合适就重启」) | nothing |
| 4. Per-Turn storage limits killed two deck Workers | in-platform | Worker attempts failed with the typed `exceeds its bounded streamed payload` refusal | resume both Workers from their checkpoints in new Turns; code fix for the oversized write; spec, review, land, restart | changing a cap that is a correctness contract (the cap stays; the write is split) |
| 5. Root overlay 98%, ~7 GB/h, from outside Tofu | out-of-band | watcher writes failing with ENOSPC; runway is WATCHDOG-CARRY's `check_disk_runway` | done 22:56: watcher state on `/tmp`, every write best effort, alert unaffected (3abc10153d) | freeing space: deleting data is his |

Grounding rule: every trigger and every success check is a live fact (probe, emitting-format log line,
durable attempt or Turn fact, heartbeat or escalation file), never memory. Experiment 1c's one open
check (K3) was exactly a stale memory stated as fact.

Recovering is not stuck (leg e, 2026-11-13 02:07). The engine exited, the storage runtime logged
`restart attempt 1 starting` and `spawned pid=107379`, and that engine opened for 627 s, ready at
02:18:05. An outage-triggered session at the 02:18:00 alert would have raced a recovery that
finished 5 s later. The trigger holds while the latest attempt's spawned engine is a live `tofu-db`
process with no failure line since (`repair_held_recovering`). It starts at the first alerting tick
after that stops holding: the engine is gone, the attempt failed, or a breaker latched. A planned
restart never starts a session. The owner's alert still goes out at ten minutes; a recovery that
hangs with a live engine and no failure line stays with him. It emits no progress to tell slow from
hung, and no timer is added.

## Duplicate sessions

- One session per incident: an O_EXCL claim `repair/<incident>/claim.json` in the watcher state.
- One session at a time: `repair.lock` (flock).
- Cross-layer: the lease and claim files above.
- The owner's terminal wins: `storage_recover.sh` holds `ops/.storage_recover.lock`. The launcher takes the
  same lock for every operational action; when the owner's run holds it, the session stops and records
  "owner took over".
- The session ends when its incident closes. The watcher SIGTERMs its own child process group, never a
  Tofu process.

## Key source

Problem: Tofu's provider credentials are encrypted secrets in the model-routing repository, which is
TofuDB, and plaintext crosses only one outbound call.

Decision:
- The project's own sankuai gateway credential (P13), resolved through the audited loopback reveal
  endpoint (`/api/v4/model-routing/credentials/<id>/secret/reveal`) while Tofu is up: at watcher start
  and after a verified recovery, never periodically.
- It is held only in the watcher's memory. The repair session sees a key-less loopback URL through the
  watcher's relay: the Terminal-Bench relay pattern, where the envelope's secret is a placeholder.
- Storage-down fallback when the watcher restarted during an outage: the reserved vault entry
  `repair.model`, which the owner enters once (entering a secret is human-only, R11(d)). Without either,
  the incident is alert-only.
- Reports carry key tails only.

Never used: the owner's sessions (大象 SSO, Codex or ChatGPT OAuth, P13), opencode's personal login, or a
key written to any file.

## Runtime

| Option | Storage-free | Reuses Tofu | Proven | Verdict |
|---|---|---|---|---|
| **`tofu-agent run`** (`tofu_agent.AgentRuntime`) from a pinned installed copy | yes: in-memory routing from the request envelope | its agent loop, tools, JSON result and trajectory (principle 4) | Terminal-Bench 2.1 runs | **primary** |
| opencode with a fixed prompt | yes | no; a second harness | yes, 11-12 morning | fallback when the pinned runtime fails `tofu-agent doctor` |
| minimal custom loop | yes | no | no | reject: new machinery |

The pinned copy is a wheel and venv built from the last landed commit whose restart passed the gate,
refreshed only after a verified landing. It never runs from the live checkout, which may carry the very
bug. Run: `tofu-agent run --env-file /dev/null --cwd <lane> --task-file <prompt> --output
<session>/result.json`, with no optional tool tags (no fetch, search, MCP, browser or memory; the exact
value is pinned and tested in O4).

No timer abandons the work (owner decisions on background handoff): the session ends when the harness
finishes, when its incident closes, or on an explicit Stop. The owner's first message names the running
repair, so he can stop it. The fixed prompt carries the incident record, the evidence bundle (log
tails, probe history, `storage_recover.sh --dry-run`), the action menu and the boundary below.

## Sandbox and permissions

Propose and dispose: the model reads, edits its lane and runs tests. Every operational action is
executed by the watcher's deterministic launcher, after the checks it owns.

| Threat | Enforcement on this host |
|---|---|
| signal the engine or any Tofu process | kernel: PID namespace (`unshare -Urnp --fork`); Tofu processes are invisible |
| exfiltrate, call other services | kernel: network namespace; the only egress is the relay over a Unix socket, which injects the key and logs request digests and sizes |
| push, land, restart, run the recovery script | construction: not reachable from the namespace; the launcher does it |
| the owner's credentials | construction: environment built from scratch (no tokens, no SSH agent, `HOME` is the session dir) |
| edit outside its lane, delete data, read secret files | **audit, not kernel**: this container denies `mount` (no read-only bind) and kernel 4.18 has no Landlock. The trajectory is audited for every path and command; protected files (vault, key, `.env`, configs) are digested before and after. A violation kills the session, escalates to the owner and disables the agent until he re-enables it |
| widen its own permissions | construction: a fix touching the launcher, relay, sandbox, prompt, authority or notification code is never published automatically |

May act through the launcher: `storage_recover.sh` (PATH A; it answers N to the compile prompt),
`serverctl restart` through the gate, publication of a verified lane, revert, re-delivery of a steered
owner forward, resumption of interrupted Workers. Never: signal the engine or another process, delete
data, history or backups, force-push, change keys, the vault or `.env`, compile or deploy an uncertified
engine, touch TofuDB files, send anything but the incident messages.

## What runs without the owner, and why the line is here

Without him (reversible, audited, revocable): a code fix in an isolated lane, failing-first spec and
focused tests, an independent review by a second model session (another model family on the same
gateway), publication through `git_land`, a restart through the certification gate, resuming or
re-dispatching the affected work, and revert plus restart when verification fails.

With him (irreversible, external, secrets, privacy, facts only he knows): deletes of data, history or
backups; anything sent or published outside the approved alert path; entering, rotating or revealing
credentials; his content beyond what the diagnosis needs; priorities and intent; uncertified engine
compiles; changes to authority boundaries (R9, R11), to this agent's sandbox or to the notification
policy.

Evidence, memory experiment 1c (`.eval-runs/owner-memory/design-arm-1c/private/REPORT-1c.md`). The
owner's verbatim grants plus exactly this bound (「可撤销的内部操作直接做；删除、对外发送或发布、凭据、只有你知道的事实，
仍要先问你」) raised the applied-preference rate from 30.4% to 45.5% (K1 +16.1 pp, CI [+5.4, +28.6]). It
kept necessary asks (31.2% to 37.5%) and cut refusals (3.6% to 0.7%). Experiment 1, delegation without
the bound, cut necessary asks to 12.5%: it stopped asking what only he can answer. The bound is what
makes broad autonomy safe. 1c's verdict stayed INCONCLUSIVE on one stale memory stated as fact, hence
the grounding rule.

## Verification and rollback

1. Failing first: the launcher runs the new spec on the base (must fail) and on the fix (must pass).
2. Focused and neighbouring tests in the lane; `landing_verification record`.
3. Independent review: approve, or no publication.
4. `git_land` (owner decision 2026-11-08: lane verification is the evidence).
5. Restart through the gate: the manager's preflight refuses an engine whose source does not match.
6. The incident's own grounded signal clears (`/api/ready` 200 and storage ready; the Turn's next
   generation completes; the queue drains; a clean close in `server-manager.log`) and stays clear for one
   probe window.

On a failure at 5 or 6: `git revert`, land, restart through the gate. A failed start keeps the manager's
generation fence (it does not adopt the new generation); `storage_recover.sh` logs its own revert
commands; an unlanded lane changes nothing. Revocable: one config line disables the agent; any violation disables it.

## What the owner is told

Through the same 大象 robot path and per-incident ledger, one message per incident plus its outcome:
- at detection: the existing alert plus one clause, 「已自动启动修复」, and the terminal one-liner as the
  fallback;
- at the end: the cause, the fix (commit, published and restarted, or rolled back) and how long it took;
  or an owner-only decision with the exact question.

The approved scope is outage alerts plus one recovery notice; an outcome message for an unrecovered
incident, or one for a non-outage repair (case 3), extends it and needs his OK. Until then those
outcomes stay in the incident record.

## ETAs (CST)

| Piece | Owner | ETA |
|---|---|---|
| O1 full-disk resilience (case 5) | this lane | done 11-12 22:56, live |
| O2 session core: triggers, claim and locks, evidence bundle, PID and network sandbox runner, kill on close, outcome record; failing-first specs with a fake harness | this lane | 11-13 12:00 |
| O3 relay: reveal while healthy, memory only, Unix-socket bridge, vault fallback | this lane | 11-13 16:00 |
| O4 `tofu-agent run` adapter from a pinned installed copy, fixed prompt, doctor self-check | this lane | 11-13 20:00; opencode fallback 11-14 12:00 |
| O5 dispose executor: failing-first check, tests, review, land, restart, symptom check, revert (fakes) | this lane | 11-14 12:00 |
| O6 owner messages: start clause, outcome | this lane | with O2; extended scope after his OK |
| O7 drill on a disposable authority (scratch data dir, own port, never production) | this lane | about 2 h after the Coordinator's go |
| hand-off files (lease, escalate, claim) | this lane reads; COORD-SELF-HEAL writes | 11-13 16:00 |
| I1 repair signals for repeated typed Turn failures and owner-input backlog (cases 1, 4) | COORD-SELF-HEAL | theirs |
| I2 forward re-delivery as a new Turn (case 2); the contract decision | COORD-SELF-HEAL; AUTHORITY-INVENTORY and the owner | theirs |
| I3 quiesce fix (case 3) | the bot-channel stabilization lane | theirs |
