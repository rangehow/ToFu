# Channels: external conversations as event sources and reply targets

Responsibility: channel accounts, normalized channel events and their media,
quote replies (C13), threads, the deterministic ingress policy, subscriptions (passthrough), delivery into
conversations, the outbox, the owner's assistant for other people's messages
(review authority, reply flow, approval and audit, C11), and the 大象 (Daxiang)
and GitHub adapters. Router
roles, forwarding and standing instructions are owned by [ROUTER.md](ROUTER.md).

Owner: `lib/channels/` (guide: `lib/channels/AGENTS.md`). Wire, tool and
vocabulary authority: [`contracts/channels_v1.schema.json`](../contracts/channels_v1.schema.json)
(enums, defaults, provider limits, HTTP DTOs, tool schemas); the browser types
are generated from it (`npm run generate:channels` →
`frontend/src/api/channels.generated.ts`). The boundary between this core and
each chat app's adapter (envelopes, intents and receipts, capability
descriptors, typed health, versioning, process model, conformance suite and
the migration of the adapters onto it) is the channel protocol,
[CHANNEL_PROTOCOL.md](CHANNEL_PROTOCOL.md)
([`contracts/channel_protocol_v1.schema.json`](../contracts/channel_protocol_v1.schema.json)).

## Vocabulary

| Term | Meaning |
|---|---|
| **Channel** | An external conversation medium: `dx` (大象), `github` (GitHub issues and pull requests, C8) and the bot platforms `wecom` (企业微信), `qq` (QQ) and `wechat` (微信) (C12). Per-channel capabilities (command chat, quote replies) are `x-channelTraits` in the contract. |
| **Channel account** | The owner's binding to one channel identity (大象 uid/mis/name; GitHub numeric user ID/login/name; a bot platform's owner, bound by pairing or the 微信 login, C12) plus its configuration, status and catch-up frontier. Owner-scoped. |
| **Thread** | One event stream: `dx:gid:<gid>` (group), `dx:uid:<uid>` (private chat), `dx:pid:<pid>` (public account / bot session), `github:<owner>/<repo>#<number>` (one issue or pull request, stored lowercase because GitHub names are case-insensitive), `<wecom|qq|wechat>:uid|gid:<provider ID as lowercase base32>` (a bot platform's private chat or group, C12). 大象 IDs are digits. |
| **Channel event** | One inbound message, recorded once per `(owner, thread, external ID)`; its event ID is `<thread>/<external ID>` (split at the last `/`). A provider message ID belongs to exactly one thread, so this is the `(owner, channel, externalId)` identity. |
| **Channel media** | An image, file or voice original of an event: provider facts, then the owner-scoped attachment it was stored as, or why it was not (C9). |
| **Quote reply** | An inbound message that quotes another one. The core resolves the quoted message and shows it with its author above the message, or says why it cannot be read (C13). |
| **Command chat** | The owner-selected 大象 thread, or a bot platform's private chat with its bound owner (C12), whose owner-authored messages are human input to the Global Router. GitHub has none (C8). |
| **Notification robot** | An optional 大象 open-platform robot that posts a group command chat's outbox items, so they notify the owner (C6). |
| **Robot binding** | Proof, from the server's own 大象 session, that the robot's chat belongs to the account's identity: the account created the group, or its session received that identity's message there. The robot posts nothing else (C6). |
| **Thread mode** | Deterministic ingress policy: `command`, `watch`, `mentions`, `ignore`. |
| **Reply policy** | What Tofu may send to a non-command thread (C11): `notify` (default), `draft`, `auto`, `never` (shown as Off). A contact's policy is its private chat's, a group's its thread's. |
| **Review** | A Global Router Turn whose input is a batch of other people's messages; it runs under the review ceiling and thread scope (C11). |
| **Approval code** | The short code of a draft (its item ID's first hex digits) that the owner's own words name to approve or discard it from a chat (C11). |
| **Owner note** | Owner-written guidance for replies Tofu drafts: one for the 大象 account (the owner's voice) and one per thread (C11). |
| **Subscription** | A thread → conversation passthrough edge created by a router. Many-to-many. |
| **Outbox item** | One durable outbound message with an idempotency key, a per-thread queue position and a state (`draft`, `queued`, `sending`, `unknown`, `sent`, `failed`, `discarded`). |
| **Outbox attachment** | A file sent with an item's text as its own 大象 message (image or file), or a preview copy of one (C10). |
| **Watermark / frontier** | Per thread: the newest processed provider ID (bounds history replay; never decides duplicates). Per account: the start time of the last completed catch-up. Both drive reconnect catch-up. |

## Decisions

### C1. Record once, then route; gaps become duplicates, never losses

**Problem:** IM transports deliver at least once, reconnect, get kicked off, and
the 大象 engine's `subscribe_messages` only listens during its call window, so
messages between windows are silently lost.

**Decision:** the bridge keeps persistent handlers for its whole process
lifetime and every message becomes a `ChannelEvent`. `ingest` processes events
in order: echo check → `ignore` keeps thread metadata only → classify →
record with CAS (`record.put`, `expected_version=0`) → route → advance the
thread watermark. A routing failure raises **before** the watermark moves; the
adapter then owes a catch-up, and its replay finds the event recorded but
unrouted (`state: pending`) and routes it again with the same deterministic
turn command ID. On every (re)connect the adapter lists sessions updated since
the account frontier and replays each non-ignored thread's history after its
watermark (or, for a thread without one, after `max(frontier, thread.sinceMs)`)
through the same `ingest`, before any live event queued behind the `connected`
status; dedup turns overlaps into duplicates. Only the CAS record decides a duplicate: a push may arrive after a
newer one of the same thread, so a never-recorded event below the watermark is
processed normally (the watermark never moves back). The account frontier moves
only to the start of a completed catch-up, never to the server time of a live
push, because pushes of different threads can arrive out of server-time order.
Enabling an account sets the frontier to "now"; turning a thread
from `ignore` to another mode resets its watermark and sets `sinceMs`, so an
ignored past is never replayed.

**Alternatives rejected:** trusting the live stream (loses messages); periodic
polling of every thread (adds a latency/interval knob and cost — constraint 5);
per-event retry loops in the adapter (a retry count; the supervisor's existing
tick already retries the owed catch-up).

**A failed catch-up never ends a live session.** *Problem:* the adapter ended
the bridge on any catch-up or ingest failure. The 大象 engine aborts each HTTP
request after 15 s, so an unreliable egress path (a proxy leaving some
CONNECTs to `api.neixin.cn` unanswered) fails many catch-ups
(`sessions_failed`: `session_list_failed`; `history_failed`:
`browserless_runtime_error`), as do storage outages (`database_timeout`). Each
failure discarded the queued live pushes and cost a relaunch — a new SSO
exchange and a minute-long login — whose own catch-up met the same odds, so
the account cycled between connect and failure and owner messages waited
minutes. *Decision:* the session stays up. A connection owes a
catch-up from its `connected` status, and any failed catch-up or live ingest
leaves one owed; the supervisor's next tick queues it on the ingest thread
behind the events already received, and a reconnect's `connected` runs it too.
While one is owed, live events are recorded and routed at once but do not move
thread watermarks (`ingest(advance_watermark=False)`), so the replay still
starts below every message the gap may hide; the frontier still moves only at
a completed catch-up. The status stays `connected`, its detail naming the
failure (code and engine message) until a catch-up completes, and status writes
are best effort, so a storage outage cannot end the session either. A gap
message replayed after a newer live push reaches the Router after it, with its
own send time. *Alternatives rejected:* holding live pushes until a catch-up
succeeds (keeps the outage latency); advancing watermarks from live pushes
(the next replay would skip the gap); replaying every thread back to the
frontier on each catch-up (reads all recent history on every reconnect); an
immediate retry, backoff schedule or longer engine timeout (new knobs —
constraint 5; the tick is the existing cadence).

**Except an expired launch ticket, which ends the session for a relaunch.**
*Problem:* the launcher exchanges the 大象 SSO ticket once per bridge launch
(`tofu_mtsso_auth exec`), and the bridge runs the engine on that ticket alone
(`MEITUAN_SSO_AUTH_MODE=env`). Once it expires, 大象 refuses every IM-token
mint the engine needs for an HTTP read (`DX IM token generation failed:
rescode=92002`, or `DX sign refresh failed`, from dx-engine
`credential-provider.js`). The authenticated WebSocket keeps sending, so the
session sat half-working: on 2026-11-09 every history read failed from 06:14,
silently, and an accepted file could never be confirmed (C10). No later tick
could help, because a running bridge cannot receive a new ticket.
Uploads need the same credentials: on 2026-11-17 the first of four files of
one message went out, and the other three came back `invalid_input`, because
the engine's upload refreshes its sign and then re-mints from the expired
ticket, and dx-websdk-runtime reports every upload exception as
`invalid_input`. The outbox took them for refusals of the files; sent one by
one after the relaunch, all three went through.
*Decision:* the adapter checks the refusal of each history, session-list and
download read and of each upload (`send_image`/`send_attachment`). One whose
message matches `DX (IM token generation|sign refresh|image upload|attachment
upload) failed: rescode=92\d{3}` ends the session: `alive()` turns false, and
the supervisor's next tick stops the bridge, stores `disconnected` with the
detail `大象 SSO ticket expired (<code>: <message>); the next tick relaunches
the bridge for a fresh exchange`, and launches a new one. Stopping the old
bridge releases its Web channel lock (C6) before the new one takes it. The
relaunch's launcher exchanges a fresh ticket from the shared SSO session,
without a login push while that session is valid. If it needs the owner,
the launcher exits 42 and the account stays `needs_login`: no tick relaunches
it until the SSO adapter answers, without a login push, that the login works
again, or the owner reconnects (Supervisor, C6), so no tick loops a login
push. `kicked` stays down until the owner reconnects. Every other failed read
keeps the session as above. The refused upload, and any later one of this
session, answers a deferred receipt (`not_connected`, CHANNEL_PROTOCOL.md P4)
without touching the file: nothing reached the IM session, so the part stays
unsent and unattempted, the item stays `queued`, and the relaunched session's
first outbox pass uploads each remaining file once with its fresh
credentials (C10). That relaunch is the only refresh and the only retry: no
second loop, no count.
*Alternatives rejected:*
- the core handing the running bridge a fresh ticket: credentials never
  travel in protocol messages (CHANNEL_PROTOCOL.md P8);
- letting the engine exchange from the shared session itself (`auto` mode):
  that moves SSO-session access, and possibly a login push, into the bridge,
  past the launcher boundary;
- a timer that relaunches before the ticket expires: a new knob
  (constraint 5), when the refusal itself is the signal.
- retrying a refused upload in the same session, or an `invalid_input`
  upload in general: the session cannot mint credentials again, and a real
  refusal of the file (empty, unreadable) would only repeat.

For an adapter on the channel protocol the core runs this catch-up itself
(`lib/channels/protocol/catchup.py`, CHANNEL_PROTOCOL.md P3) with the same
rules, reporting a failed one as typed health `degraded`
(`history_failed`/`ingest_failed`); 大象's still runs in its adapter until it
moves onto the protocol.

### C2. The fast path is deterministic; the router is the slow path

| Mode | Owner-authored message | Other people's messages |
|---|---|---|
| `command` | Global Router, **human input** | as `watch` |
| `watch` | context for subscribers only | delivered as a candidate |
| `mentions` | context for subscribers only | delivered when the thread is a private chat, or the message @-mentions the owner (or everyone) or quote-replies to the owner |
| `ignore` | dropped | dropped; only thread metadata is kept so the owner can enable it |

In the command chat the notification robot posts into, other people's
messages (and other servers' robot posts) are context instead: one robot
serves many servers (C6). A message whose sender the platform marks as a bot
or a platform notice (`ChannelEvent.sender_kind`, the channel protocol's
`Sender.kind`) is at most context in every mode: recorded where a person's
message would be delivered (subscribers see it with `sender_kind`), never a
command or a candidate, so no review or auto-reply answers one automated
account with another.

Defaults: the command chat is `command`; private chats `mentions`; groups
`ignore`; public-account/bot sessions (`pid`) `ignore` — they are broadcast-like
(approval and notification bots) and the owner enables the ones that matter;
GitHub issues and pull requests (`issue`) `mentions`. Reply policy default:
`notify` (C11). Owner-authored messages outside the command chat are never commands (C11).
On a channel without a command chat (GitHub) the owner's own messages are
delivered as nonhuman candidates in `watch` and are context otherwise (C8). Messages sent by the outbox are never ingested as input:
an owner-authored event is an echo when it carries the outbox marker
(`extension.tofuOutbox`) of an outbox item, when its provider ID or client UUID
was recorded for a sent item, when its marker field holds any other value (only
an API sets it, so another Tofu send path stamped it), when the bridge flags it
`hostSent` (another engine process of this server sent it as the account, C6),
which ingress also records so a later replay stays an echo, or when the bridge
flags it as its own connection's send (the order of the channel protocol's
`x-selfEcho`); an echo of an in-flight item confirms that item as sent. Mentions and quote replies
are protocol facts (`extension.at`, mention links, decoded quote senders),
compared with the identity bound at enable time. Delivered events, owner
command messages and owner context are stored (`channel_read`); dropped
messages and ignored threads are not. The Global Router decides on delivered
candidates and may stay silent.

**Alternatives rejected:** keyword or regex prefilters (heuristic knobs); running
the model on every message of every group (cost and privacy).

### C3. Subscriptions are passthrough, observed by the Global Router

**Decision:** `channel_subscribe(thread, conversation_id)` (Global Router only,
on a genuine human request like every subscription change, C7) creates an
owner-scoped edge to an active conversation of the same owner.
Delivered candidates and owner context of a subscribed thread go directly to
every subscriber, skipping a router decision. The Global Router receives a
non-waking observation in its **project mailbox** (the router is the bound
Coordinator of its home project): a fixed-shape pointer naming the thread, the
event ID range and the subscriber count (the subscriber list is one recall
away in `channel_read(thread=…)`), injected as nonhuman context at the router's
next round without starting a turn. A mailbox refusal never blocks ingress;
the event then records no `observation` delivery. `channel_unsubscribe` hands the thread back. A
subscriber whose conversation is gone is unsubscribed at delivery and its
candidates return to the router. Subscribers never send to channels; they may
read their own threads with `channel_read`.

**Alternatives rejected:** re-deciding every follow-up in the router (the cost
passthrough removes); a channel-private observation queue or a new per-round
injection step (a second inbox — constraint 4; the mailbox already owns
durable, non-waking, receipt-confirmed context); waking the router per
passthrough batch (the cost passthrough exists to avoid).

### C4. Delivery reuses the turn command

**Decision:** delivering events into a conversation is one
`dispatch_scheduled_turn(..., inject_mode='steer', turn_kind='channel_reply')`
with a command ID derived from the target and the event IDs, in two namespaces:
`channel-event-…` for nonhuman batches and `channel-owner-…` for owner command
messages. A batch steered into a running Turn is recorded there under that
command ID (`_userSteerInjects[]`); only human-initiated input also records
its committed `author`, which is what lets C7 see a nonhuman batch inside a
human Turn. A running target
receives the batch at its next round boundary, merged with other pending input
(the router's natural batching under load); an idle target starts one turn; a
closed steer window queues the batch. A catch-up replay delivers one batch per
target. Third-party events and owner context use the nonhuman initiator
`channel` (`lib/turn_initiation.py`; persisted as `origin.initiator`, refused by
`require_human_turn`). Command-chat owner messages are human input whose text
is byte-identical to the 大象 message (so ROUTER.md R2 forwarding stays
verbatim), with `origin.channel = {channel, thread, eventIds, title}`. The
nonhuman block lists each thread (`<channel-thread key title mode
reply_policy subscribers>`) and each event (`<channel-event id thread sender
sender_id time disposition private_chat mentions_owner replies_to_owner
reply_to>`) with escaped text, so the model can walk thread → events →
subscribers (chain of recall). Text above the ordinary tool-result inline
limit (`lib.tools.result_value.INLINE_LIMIT`) is spilled to an owner-scoped
tool-result artifact and referenced by `artifact_ref` and event ID.

**Alternatives rejected:** a second inbox, queue or wake loop (constraint 4);
batching windows or debounce intervals (constraint 5); wrapping command text
in metadata (breaks verbatim forwarding).

### C5. Outbox: durable, ordered, exactly once or reconciled

**Decision:** `channel_send(thread, text, reply_to?, idempotency_key, attachments?)` records an
outbox item (ID derived from owner, thread and key) before any network effect;
attachments follow C10.
Policy is decided once at enqueue from the thread's reply policy and the
request's authority (C11): command chat → `queued`; `auto` → `queued` for the
owner's own request or a review's reply to a message it reviews; `draft`, and
`notify` for an owner request, → waits for owner approval (Settings, or the
Global Router's `channel_outbox_approve` on a genuine human turn naming the
draft's approval code); `never` refuses and records nothing. A command-chat owner message that the
Turn command accepts immediately enqueues a receipt: started, steered into the
live Turn, or queued behind it. A queued command adds its started receipt when
its Attempt actually begins (`lib/channels/autoreply.py`). While that genuine
human command-chat Turn runs, the passive chat-Push observer mirrors each
canonical non-deliverable assistant text block once (tool-round
`assistantContent` and released continuation prose, never reasoning or raw
tool payloads), plus each changed `update_task_progress` board summary. The
observer does no model or network work. A Global Router Turn whose human input
came from a channel thread, including a genuine human command steered into a
nonhuman notice Turn, enqueues its final assistant answer to that thread with
idempotency key = the Turn ID, followed by an explicit completed, failed,
interrupted or aborted receipt from the existing task terminal-listener seam.
A successfully reviewed nonhuman notice may instead declare deliberate
silence (ROUTER.md R3): its audit note stays in Tofu, with no empty reply or
misleading completion receipt. A failed or interrupted review sends a
command-chat warning even when that notice had no reply origin. "Notice" is
the same test as the silence gate (`turn_initiation.is_nonhuman_notice_turn`):
channel events, peer reports and every scheduled wake (a `proactive` project
wake such as the keepalive, timers, autopilot) alike; a human or unknown
initiator gets no such warning. Problem: this warning kept a channel/peer-only
gate after the silence gate widened, so failed keepalive wakes settled
`abnormal_stop` with no owner warning at all. Alternative rejected: a second
wake-initiator list here (it drifts from the silence gate). When the
review exhausted the terminal-response budget (empty_stop), the existing
resume owner continues the same Turn in place as a bounded follow-up of the
terminal observer, preserving its evidence and completed tool results; the
warning then says the notice is being continued rather than asking the owner
to resend. A notice the resume gate refuses (owner-authored Turn, a current
owner steer, or only Regenerate offered) and every genuine provider error
keep the existing manual-resend warning, and nothing is marked reviewed. Receipts and
progress use the command chat; other channel events and nonhuman Turns never
gain these automatic messages. With a notification robot (C6), the command
chat's items go out under the robot instead of the owner's account. Agent-authored messages in threads
other than the command chat carry the visible label `[Tofu 代发]`. Each queued
item takes an exact per-thread queue position (CAS on the thread record);
approval appends a draft at the end. The executor sends each thread in order,
threads independently: drafts and definite provider refusals (`failed`) never
block, an `unknown` item blocks only its thread. A send the provider defers
(`not_connected`, `rate_limited`: definitely not sent) keeps the item `queued`
with the time the platform named (`notBeforeMs`, else the next pass) and
holds only its thread until then; it is neither reconciled nor a Watchdog
fault (channel protocol P4). Before any resend an unknown
or interrupted (`sending`) item is reconciled against the thread history since
its attempt, by marker or client UUID. A history read that fails is no
evidence: the item stays as it is, nothing is sent, and it holds only its own
thread until a read succeeds (channel protocol P4). Sent items keep the
provider message ID
and client UUID for echo suppression and recall. Messages are sent as markdown
(大象 quote replies through `reply_message`; GitHub has no quote replies, so
`reply_to` is refused there). Each running adapter registers one executor per
(owner, channel) and executes only its own channel's items. A send that
settles `failed` or `unknown` also becomes one Router Watchdog fault signal per
item and state ([ROUTER.md](ROUTER.md) R7); the outbox's own handling is
unchanged. Router-generated receipts, content, progress, replies and terminal
status are translated to the conversation's configured display language
immediately before the provider send. The first translated bytes are written
back to the durable item before the network call, so an unknown result or
restart retries exactly those bytes. Only the authored text is translated:
the item's `[Tofu 代发]` label (outside the command chat) is kept exactly
once, with or without `reply_to`. A translator failure preserves the
original text and never blocks later items. Manually authored `channel_send`
text is not rewritten.

**Alternatives rejected:** sending inside the tool call (lost on crash,
duplicated on retry); impersonating the owner without disclosure; ordering by
timestamps (equal milliseconds reorder a thread); forwarding token deltas
(unbounded message spam and provisional retry text); translating in an
independent async queue (reorders replies and can retry different bytes).

### C6. 大象 adapter

- **Engine and auth:** the owner's installed `dx-pura-api` skill
  (`owner_global_skills_dir(owner)/dx-pura-api`, engine
  `vendor/node_modules/dx-engine`), always run through its Tofu launcher
  `scripts/tofu/dx-tofu.sh` with `DX_TOFU_OWNER=tofu:<owner>`. SSO tickets
  exist only in the launcher child's environment; the parent passes no ambient
  secrets and nothing enters model context, Tofu storage or argv.
- **Launcher Node** (`bundles/yourprovider/tofu_yourprovider/dx/runtime.py`): the bridge and its SSO
  step run exactly one Node, named by one durable setting: `TOFU_NODE` in the
  project `.env`, which every server start loads into its environment (else an
  explicit server-environment `MTSSO_NODE_EXECUTABLE`, else the SSO adapter's
  own `PATH` choice). It is verified once per file identity (realpath, device,
  inode, size, mtime) by the SSO adapter's `node_runtime` in a child process
  (official engine floor `engines.node >=20`, typed `SSO_NODE_NOT_FOUND`,
  `SSO_NODE_TOO_OLD`, `SSO_NODE_RUNTIME_INCOMPATIBLE`,
  `SSO_NODE_PROBE_TIMED_OUT`) and against the bridge's floor, contract
  `x-dx.bridgeNode` (`DX_NODE_TOO_OLD`): dx-tofu.sh starts the bridge with
  `NODE_USE_ENV_PROXY=1` because this host reaches 大象 only through the
  egress proxy, which Node honours for `fetch()` from 22.21.0 on the 22 line
  and from 24.0.0; Node 20 ignores it. Every launcher child receives that
  binary as `DX_NODE` (the bridge; dx-tofu.sh prefers it),
  `MTSSO_NODE_EXECUTABLE` (the SSO step) and `MTSSO_NODE_VERIFIED` (the
  verification, so the SSO step never probes it again while the file is
  unchanged). An MCP server's own `MTSSO_NODE_EXECUTABLE` field configures that
  server only, never the bridge. The supervisor checks the Node before every
  launch, boot included: a missing, too-old or unrunnable Node is the
  account's `runtime_unavailable` state at once, naming the resolved binary,
  its version and its setting (one Router Watchdog signal), and nothing is
  launched; a check the host was too busy to finish (`runtime_unverified`) is
  logged, shows `starting` with its detail and runs again at the next tick.
  *Problem:* the SSO step probed `node --version` within five seconds on every
  launch; during a host stall (2026-11-10 06:45) the probe of a working Node
  timed out and was reported as `SSO_NODE_UNAVAILABLE` ("install a Node"), and
  an explicit `MTSSO_NODE_EXECUTABLE` could run the SSO step on another binary
  than the bridge. *Alternatives rejected:* a longer probe budget (a tuning
  knob that still fails under a longer stall); a persisted cache file (new
  state with its own ownership rules, while the verifying parent already
  launches every child); editing the owner's skill launcher (overwritten by
  skill updates).
- **Login MIS** (`bundles/yourprovider/tofu_yourprovider/dx/login.py`): the account logs in as the MIS
  the owner entered in Settings, else its bound identity's, else the host's
  shared SSO session account. That MIS selects the launcher child's SSO
  session: the host's shared DataFE session when it belongs to that MIS,
  otherwise the owner's own `<data>/channels/dx/sso/u<owner>/<mis>.json`
  (directories `0700`). The SSO adapter's `exec` reuses a usable session
  (renewing it with its own refresh token first, also when the provider ended
  the access token early) and requests one 大象 login approval only when there
  is none; only the DataFE SDK writes session files and Tofu never reads them.
  The shared session's MIS and whether a login needs an approval are the
  adapter's own answers (`discover_login_mis`, the non-interactive
  `session_status`), asked in a child process.
  **A launch never asks the owner twice by itself.** The adapter keeps one
  durable, secret-free approval record per session (`<session>.mtsso.approval`
  beside the session file, see `packages/tofu-mtsso-auth/README.md`): every
  process using that session (MCP servers, launcher, the same processes after
  a restart) sees an outstanding request and joins it, and after a request
  ended unapproved no launch or tool call sends another. The launcher may ask
  again only for an owner request newer than that one: the owner command that
  starts a session (enable, 重新连接 = re-enable from `needs_login`, another
  login MIS; `lib/channels/service.py`) stores the account's
  `loginRequestedAtMs`, which every session start and identity confirmation of
  the account carries to the launcher (`with_login_request`,
  `MTSSO_LOGIN_REQUESTED_AT_MS`); each such request buys at most one approval
  request. A boot, tick or ticket-expiry relaunch carries no newer request, so
  it reports `needs_login` (`SSO_LOGIN_NOT_APPROVED`, nothing sent) instead.
  *Problem (2026-11-13):* the shared session was ended by the provider about
  18 h after its approval; from 03:57 a Worker's failing `create_doc` calls
  and Hope tools asked the owner for a new approval about every two minutes,
  and the relaunch after the 04:30 restart asked again at 04:32, three minutes
  after the previous request went unapproved, because that outcome lived only
  in the failed process's memory. *Alternatives rejected:* re-asking after a
  backoff or once per approval window (still a loop of pushes while nobody
  answers, at a tuned rate); keeping the outcome in the supervisor (MCP
  processes and restarts would not see it); a flag consumed by the next launch
  (lost with the process that holds it; the stored request time needs no
  consumption and survives a restart).
- **Bridge** (`bundles/yourprovider/tofu_yourprovider/dx/bridge.mjs`, spawned from `PinnedHelperSource`):
  one Node process per account. It logs in with the engine's own
  `DxWebSdkRuntime`, attaches persistent handlers for `IM_message`,
  `PUB_MESSAGE`, `KickOff`, `KickOffWithSameDeviceId` and `ConnStatusChange`,
  writes JSON-lines (`status`,
  `message`, `response`) to stdout and executes JSON-lines commands from stdin
  (`whoami`, `send_text`, `send_markdown`, `reply_message`, `history`,
  `sessions`, `create_group`, `download` (C9)), each answered with its request ID. It decodes
  quote parts so reply facts survive and flags its own sends. Disconnect is
  the SDK's own `CONSTANT.LOGIN_STATUS.DISCONNECTED`. On a transport loss it
  reports `disconnected`, reconnects the existing SDK socket and calls the
  engine's `login()` with its in-memory credentials. The engine owns rejection
  recovery and its existing login deadline. Because `autoConnect` is off,
  nothing else opens a socket: whenever the recovery's own socket closes
  (`DISCONNECTED`) or its authentication fails (`AuthFail`) before `login()`
  settles, the bridge opens the next one for the same pending login, which the
  SDK authenticates again once it connects. It opens at most one socket per the
  SDK's own reconnect time, `RECONNECT_TIME` (2 s, the pause the SDK's
  autoConnect keeps after each `DISCONNECTED`; msg-codec `dxSDK.src.js`,
  `src/core/implmulti/login/impljs/connection.ts`), so a far end that closes
  every socket at once costs one socket per 2 s, not one per close. The SDK's
  socket reconnect does nothing while a socket is connecting or authenticating
  and closes the old socket first, so this is still one session; `login()`'s deadline bounds the
  whole recovery. Concurrent loss notifications and commands join that login. Only successful authentication for the same uid
  reports `connected`, triggering the adapter's ordinary catch-up again.
  Commands already dispatched are never replayed. Failed recovery exits (4)
  for the supervisor/launcher to refresh the session; kick-off exits (43) and
  stdin close exits (0), including during recovery. A kick-off reports
  `DISCONNECTED` before its kick event in the same synchronous dispatch, so
  recovery defers one event-loop turn to let the kick decide. An SDK without
  the required status or socket-reconnect interface fails closed (4).
  It holds no policy and no durable state. One-shot mode
  (`--command whoami|create_group`) serves identity confirmation and
  command-chat creation.

  **Problem:** exiting on every socket close discarded valid IM credentials;
  repeated network closes therefore called `/login/im_token/generate` on each
  restart and left ingress offline until the scheduler tick. **Decision:**
  reconnect in the existing process, retaining the engine's credential cache
  and using its authentication outcome as the validity check. **Alternatives
  rejected:** ignoring disconnect leaves ingress deaf; the engine's
  `reconnect()` unconditionally invalidates credentials; a disk token cache or
  guessed TTL duplicates authentication policy; a new refresh/backoff timer
  adds a tuning knob. No retry budget or credential store is added, and the
  only timer keeps the SDK's own reconnect time.
  *Problem (2026-11-10 06:43):* the recovery's single socket closed about a
  second after it opened, nothing opened another, and the pending login timed
  out (exit 4), buying a relaunch whose SSO step took minutes under load.
  *Decision:* re-arm the socket on each close during a pending recovery, as
  for `AuthFail`; only a recovery `login()` itself fails needs a relaunch.
  *Problem:* re-arming on every close let a far end that closes each socket at
  once (the shared egress proxy refusing the upstream) open a new socket per
  close, through that proxy, until `login()`'s 12 s deadline. *Decision:* the
  bridge's sockets keep the SDK's `RECONNECT_TIME` apart (one per 2 s).
  *Alternatives rejected:* the SDK's 4 s single-connect timeout
  (`ONLINE_WEBSOCKET_TIMEOUT`) bounds a socket that never opens, not one that
  closes; an interval of the bridge's own is a tuning knob.
- **One live session per account (per host).** A second 大象 Web channel login
  of the account displaces the first (`KickOff`, `KickOffWithSameDeviceId`).
  The engine serializes the Web channel actions of all its processes on a host
  (send, reply, recall, subscribe, mark-read) through one lock,
  `web-channel-mutex.js`: the directory `DX_BROWSERLESS_WEB_LOCK_DIR` (default
  `~/.yourprovider_local/dx-browserless-web.lock.d`), whose `owner.json` names the
  holder's pid and action and is stale once that pid is gone. The bridge takes
  that lock before it logs in and holds it until it exits, as
  `connector_owned: Tofu's 大象 channel session owns this account. Only the
  Global Router sends, with channel_send; a project conversation reports the
  file's path and its project to the Global Router and asks it to send` (the
  supported path of CHANNEL_PROTOCOL.md P13, since a Worker has no
  `channel_send`); its own commands run inside the hold. A lock that another live
  process still holds at the engine's deadline fails the start (4, the detail
  names the holder) and the next tick retries; an engine without the lock fails
  closed (4). Every `run_command` child gets `DX_BROWSERLESS_WEB_LOCK_TIMEOUT_MS=0`,
  so a Tofu-run engine write (dx-pura-api `send`, `send-md`, `send-file`,
  `send-image`, `reply`, `recall`, `mark-read`, or its watch daemon) is refused
  at once with the engine's `web_channel_mutex_timeout`, whose message names
  that holder. HTTP reads (`history`, `ginfo`, search) take no lock. Files go
  out only through `channel_send` (C10). A row or push whose client UUID the
  engine's sent-message record holds (`payload-cache.js`, newest 100 sends for
  24 h) and that the session did not send is flagged `hostSent`: Tofu sent it as
  the account through another engine process, so ingress treats it as an echo
  (C2), never as owner input or approval.

  **Problem:** on 2026-11-08 a Worker sent a deck with the skill CLI. Its engine
  logged in as the owner on the bridge's own device, 大象 kicked the bridge
  (terminal `kicked` until the owner reconnected), and the reconnect catch-up
  routed the CLI's 18 sends into the Global Router as owner commands.
  **Decision:** the session joins the engine's own lock for its whole lifetime,
  Tofu-run commands never wait for that lock, and the engine's sent-message
  record marks Tofu's sends made outside the session. **Alternatives
  rejected:** matching command text in `run_command` (scripts, variables and
  other skills bypass it); withholding the 大象 SSO ticket from commands (reads
  need it); editing the owner's skill or engine (overwritten by skill updates);
  waiting out the engine's deadline (the skill CLI kills its engine at 120 s and
  reports that as a success); a Tofu lock of its own (engine processes honour
  only the engine's). The lock is per host: a login elsewhere (大象网页版 in a
  browser, another server) still displaces the session, which then stays
  `kicked`, also across a restart, until the owner reconnects once (Settings →
  重新连接, `PUT /api/v4/channels/dx {"enabled": true}`).
- **Adapter** (`bundles/yourprovider/tofu_yourprovider/dx/adapter.py`): normalization of live pushes and
  history rows (server time from the snowflake ID), reconnect catch-up,
  sequential ingest, and the outbox sender port. Provider limits are named in
  the contract (`dxHistoryPageLimit` 100, `dxSessionListLimit` 500; a full
  session page bisects its time window). The session list names the session
  types it replays (`chat`, `group`, `pub`), so the engine fails the listing
  when one of their chatlist sources failed. *Problem:* an untyped listing
  answers a timed-out chats-and-groups chatlist with the public-account rows
  alone, as a success, and that catch-up moved the frontier past chat and
  group messages it never replayed; they surfaced only after their thread's
  next message. *Alternative rejected:* parsing the engine's free-text
  warnings (an aborted request names no source). History paging (`history_pages`)
  follows the provider's observed contract, not the engine's `nextCursor`:
  group and private-chat pages arrive oldest-first, public-account pages
  newest-first, `cursorMsg` is an exclusive upper bound, and the engine's
  `nextCursor` is the page's *last* row (the newest one for groups, so
  following it re-reads the same page). The next cursor is the page's smallest
  message ID, as in the skill's own exporter; `-1` or a page without unseen IDs
  ends the walk.
- **Supervisor** (`bundles/yourprovider/tofu_yourprovider/dx/supervisor.py`): runs where the
  scheduled-jobs capability runs, from the scheduler's existing 30 s tick
  (`lib/scheduler/manager.py`) and immediately after a configuration change in
  a hosting process. It starts a session for an enabled account (after
  checking identity, skill and Global Router), stops it when disabled or
  switched to another login MIS, and restarts it after any exit at the next
  tick — no timer, backoff schedule or retry count of its own. An enabled
  account whose login MIS has no bound identity is first confirmed on its own
  thread (see Identity), so neither the tick nor an HTTP request waits for it. Launcher exit `42` (`SSO_SESSION_REQUIRED`) and
  kick-off stop restarts and surface `needs_login` / `kicked`: `kicked` until
  the owner re-enables, `needs_login` until its login works again (below) or
  the owner re-enables; an absent router or skill surfaces `error` without
  starting.
  A launcher whose SSO step refused because of its own runtime or
  configuration (the SSO adapter's envelope `recovery_action:
  repair_agent_configuration`, e.g. `SSO_NODE_NOT_FOUND`, `SSO_NODE_TOO_OLD`,
  `SSO_NODE_RUNTIME_INCOMPATIBLE`; exit 1) surfaces `runtime_unavailable` and
  restarts at the next tick like `disconnected`; a probe that timed out under
  load (`SSO_NODE_PROBE_TIMED_OUT`) is not a configuration fault and reads
  `disconnected`. The launcher Node check (see Launcher Node) refuses most of
  these before a launch. *Problem:* such a refusal read as `disconnected` with
  an SSO message about the login state, and was taken for a login problem; a
  login push cannot repair a missing or incompatible Node. *Alternatives
  rejected:* making it terminal (a repaired runtime should not wait for the
  owner to re-enable); classifying error codes in Tofu (the adapter's envelope
  already says which recovery applies).
  A session reports a state transition once, so an account command that does
  not start a session must not overwrite that status: enabling an already
  enabled account, or saving its unchanged configuration, keeps the reported
  state and `connectedAtMs` (`session_owns_status`, `lib/channels/model.py`).
  Only a first session start, one for a switched account or one for a login
  that works again sets `starting`, and disabling sets `disabled`. A restart
  after an exit keeps reporting that exit's outcome until the new session
  reports `connected`; its detail names
  the launcher's public refusal (`error_code: error`) when the launcher refused
  to start the bridge, and the exit is logged once per distinct outcome.
  Each tick of this and of the GitHub supervisor (C8) also reports an
  unhealthy enabled account to the Router Watchdog: one fault signal per state
  since the last connection ([ROUTER.md](ROUTER.md) R7).

  **Problem:** each restart overwrote the exit's outcome with `starting` in
  the same tick, so a launcher that refused every 30 s for hours read as a
  perpetual start in Settings, left no log line, and the Watchdog, which reads
  the state after the tick, never saw an unhealthy one. **Decision:** the exit
  stays the reported state through the restart. **Alternatives rejected:**
  making every launcher refusal terminal (a transient one, such as a network
  failure, would then wait for the owner to re-enable); a separate restarting
  state or failure counter (a second state machine; the exit already says what
  is wrong).

  **A login that works again reconnects by itself.** *Problem:* on
  2026-11-12 a relaunch at 00:43 needed a 大象 approval nobody answered at
  night, so the account stayed `needs_login`. The shared SSO session worked
  again from 08:54 (other tools' calls through it succeeded), yet 大象 stayed
  down until the owner reconnected in Settings at 11:16: for ten and a half
  hours command-chat messages went unread and the Global Router's sends waited
  in the outbox, and nothing in the state said how to leave it. *Decision:*
  each tick, an enabled account in `needs_login` asks the SSO adapter's
  `session_status` for its login MIS (`login_ready`,
  `bundles/yourprovider/tofu_yourprovider/dx/login.py`) on its own thread, one recheck per owner at a
  time. Only `ready` counts: it comes from the acquisition path the launcher
  runs, without its approval step, so the relaunch reuses that session and
  requests nothing. The account then moves to `starting` under an atomic
  condition (still enabled, still `needs_login`, same login MIS; a change the
  owner made meanwhile decides instead) and its session starts; any other
  answer keeps `needs_login` without a launch. A `needs_login` that a login
  approval resolves names both ways out in its detail
  (`NEEDS_LOGIN_RECOVERY`: any approved 大象 login for the account, or
  重新连接 in Settings), so Settings, the Watchdog and the Global Router tell
  the owner the same thing. `kicked` is never rechecked: reconnecting takes the
  session from the other login (C6). *Alternatives rejected:* relaunching
  `needs_login` at every tick (each launch requests an approval: the push loop
  the state exists to prevent); a timer or backoff (a knob, constraint 5);
  watching the SDK's session files (Tofu never opens them); leaving the
  reconnect to the Global Router's `owner_settings` (it acts only when asked,
  after the owner has already approved a login).
- **Shutdown:** the process lifecycle opens the supervisor before starting the
  scheduler and stops it after the scheduler joins. Stop closes admission for
  ticks, configuration notifications and identity completions before joining
  adapters and all supervisor-owned threads. A timed-out or failed stop keeps
  ownership for the next stop attempt; a replacement session and a new hosting
  lifecycle cannot start before that ownership is released. Production shutdown
  keeps storage live while this join is incomplete. Pending identity probes keep
  their existing provider deadline; their late result cannot write account state
  or start a session after Stop. Ingress wakes immediately on Stop and leaves
  queued events for catch-up from the committed watermarks. An in-flight ingest
  or outbox operation must finish before the final frontier checkpoint and a
  successful stop receipt. Shutdown asks for the stop when its drain begins
  (`begin_stopping_channel_bridges`): Stop's fence goes up and every session's
  ingest and outbox are signalled, without a join, so in-flight work ends while
  the drain runs. The background-service stop then closes the bridges, joins
  and checkpoints as above. The GitHub and bot supervisors follow the same rule
  (C8, C12; [modules/infra_runtime.md](modules/infra_runtime.md), "Channel
  owners stop on drain").

  **Problem:** clearing the adapter registry before joining lost cleanup debt,
  and untracked identity/configuration callbacks could start a new bridge after
  shutdown had passed the channel owner. **Decision:** retain physical worker
  ownership and fence new work under the supervisor lock. A failed ingest no
  longer ends the bridge (C1): it is logged, its event waits for the owed
  catch-up and no failed event advances its watermark; status writes are best
  effort, so failing diagnostics cannot stop ingress. **Alternatives rejected:**
  draining the queue after Stop prolongs shutdown unnecessarily; forgetting a
  timed-out join falsely permits storage release. No retry timer or capacity
  limit is added.
- **Identity:** the supervisor confirms it with `whoami` logged in as the login
  MIS before a session starts; an event whose sender equals that uid is
  owner-authored. The account reports `authorizing` while the adapter says the
  login waits for an approval (`login_required` or `pending`), otherwise
  `starting`. A `whoami` answering another MIS is `identity_mismatch`
  (`error`); a refused or unapproved login is `needs_login`; a launcher whose
  SSO runtime refused is `runtime_unavailable` and is confirmed again at the
  next tick. Choosing another
  MIS clears the identity and the command chat and restarts ingress from now.
  A newer choice starts its own confirmation at once instead of waiting behind
  an older pending approval; the older one's late answer is discarded by an
  atomic condition on the account write (`ChannelStore.set_status(when=)`).
  Problem: enabling ran `whoami`, including a possible 180 s approval wait,
  inside the HTTP request, and the account could not be switched. Decision:
  account commands return at once and the supervisor confirms asynchronously.
  Alternatives rejected: a shorter request timeout (fails slow approvals and
  adds a knob); a browser-polled login job (a second job resource for one
  state the account already reports); copying tokens or running CIBA in Tofu
  (a second credential store or authentication implementation).
- **Command chat:** chosen in Settings, or created on explicit owner request
  (`POST …/command-chat`) as a group named `Tofu Router`; a creation claim on
  the account prevents a second group. With a notification robot (below) the
  robot creates it (大象群管理 §1 机器人创建群, `createGroup` with `owner` the
  owner's uid): the owner is 群主 exactly as when his session creates it, and
  the robot is a member from the start. A robot that provably created nothing
  (a refusal, the rate limit, an unreachable token endpoint) leaves the
  creation to the owner's session as before; the refusal is recorded on the
  robot (`robot.error`, shown in Settings) and noticed once in the new group.
  A creation of unknown outcome (a server failure or lost answer) may have
  made a group, so it is recorded and refused (`group_create_unknown`) rather
  than making a second one. Nothing retries. **Problem:** the owner had to
  find and add the robot by hand in every new command chat. **Decision:** the
  robot creates the group with the owner as 群主: one documented call with the
  token the robot already holds and a permission its application already has
  (获取与更新机器人创建群组信息). **Alternatives rejected:** adding the robot after
  the owner's session creates the group (`addBotByUser`, §15, needs a
  separate 企平 user-token exchange signed with the application secret, a
  second credential flow; the engine has no add-robot route); retrying a
  failed robot creation (reported once, never repeated).

  **Handover from another server.** *Problem* (owner, 2026-11-15): the owner
  ran Tofu on another server ("zw"), created the Tofu Router group there,
  then logged in on this server. Its account showed the command chat as
  未选择 and offered only 创建"Tofu Router"群: the command chat, its creation
  claim and the robot binding are this server's own account record, which no
  server shares, and enabling here set the frontier to that moment, so the
  existing group stayed unknown until someone wrote in it (the logs: catch-up
  `sessions=1` at 16:07:38, the group first seen at 16:08:34 with mode
  `ignore`). Creating would have made a second group, since the other
  server's claim does not reach this one. *Decision:* while an account has
  neither a command chat nor a candidate, the first catch-up of each session
  also lists the account's *group* sessions before the frontier, for their
  names only (nothing is replayed, no watermark moves; a failed listing is
  logged and never fails the catch-up). The newest group named exactly
  `Tofu Router`, there or among the sessions the catch-up listed anyway,
  becomes the account's `commandChatCandidate` (account view field, contract
  `ChannelAccountView`), and Settings offers 使用已有的"Tofu Router"群 as the
  primary action above creating one: one owner action (`commandChat`) adopts
  it. Choosing another 大象 account clears the candidate. The robot then
  posts there once the binding is proven anew (below: the owner's own message
  in the chat). *Alternatives rejected:* adopting the group by itself (a name
  is no proof of ownership, and a command chat decides what counts as owner
  input); replaying the whole history before the frontier (re-routes the
  other server's past); copying state between servers (no shared store; a
  second authority); a server registry (the servers never talk to each
  other).
- **Notification robot** (`bundles/yourprovider/tofu_yourprovider/dx/robot.py`, facts in the overlay's `tofu_yourprovider/dx/facts.json`
  `x-dxRobot` and the contract's `x-providerLimits`). **Problem:** the outbox posted as the
  owner, and 大象 never notifies anyone of their own messages, so receipts
  and answers in the command chat arrived silently. **Decision:** an optional
  open-platform robot posts the outbox items of a *group* command chat; inbound
  stays on the owner's bridge, other threads keep the owner's account, and no
  robot means today's behaviour. The robot follows the official documents:
  a client_secret_jwt `client_credentials` token with scope `client_id:xm-xai`
  (应用认证接入指引 §3.2.1/§3.2.4), reused until its `expires_in`, one renewal
  when the API answers 20004/20005; `sendGroupMsgByRobot` with a markdown
  `text` message (大象消息 §2.2, 消息extension); text split into parts of at
  most 6000 UTF-8 bytes, ending at a line break where possible; posts paced at
  the documented 5 per second. The Authorization scheme is the token's
  `token_type` ("Bearer"). Status codes (错误码) decide the outcome: a refusal
  (application, permission, membership, a missing or rejected secret, an
  unreachable token endpoint) sends what the robot has not posted as the
  owner instead (the whole item, or the parts after the ones already posted)
  and records the refusal on the account (`robot.error`, shown in Settings,
  cleared by the robot's next success); the rate limit (10003) sends the
  unposted rest as the owner at once without recording a refusal (the quota
  is shared, below); server
  failures or lost responses leave the item `unknown`, so it is reconciled
  and continued under the robot after the parts the item recorded as posted
  (`robotPosted`) or the thread history shows, never posting a part twice. A group send has no documented
  quote type, so robot posts never quote. Echoes: robot posts reach the
  owner's connection from another sender without a marker, so the item
  records its exact part texts (`robotTexts`) before posting and each
  returned message ID is remembered at once, under the item (a quote of a
  robot post shows that part's text, C13); in the robot thread an event
  whose ID was remembered, or whose text is a part of an active robot item,
  is an echo that confirms nothing (the API answer settles the item), and
  reconciliation finds a robot item when all its parts are in the history.
  The robot is a separate sender, so its posts notify the owner like any
  member's message (unless the owner mutes the group). It does not reduce
  the owner's IM token requests (`/login/im_token/generate` per bridge or
  launcher process): inbound still needs the bridge, and robot inbound would
  need an open-platform event callback, a Thrift service with an APPKEY.
  Attachments do not go through the robot (C10): its documented `image`/`file`
  bodies carry URLs on the caller's own storage (IM stores no files for
  open-platform sends), so files go as native messages of the owner's session
  and the robot posts the text after them. **Alternatives rejected:** @-mentioning the owner
  (the documented @ prompt targets other members; nothing documents a self-@
  notification); a private chat with the owner (still the owner's own
  message); routing robot posts to the robot's own private chat (splits the
  conversation from the command chat); an undocumented marker in the robot's
  extension (no documented preservation).

  **Falling back and back.** Each send tries the robot first; there is no
  robot timer, probe or retry loop, so the next send after a repair is the
  recovery. The first refusal after working sends one command-chat notice
  with its code and detail, and the first robot success after a recorded
  refusal sends one notice that the robot posts again (origin `robot_notice`,
  untranslated). A notice is an ordinary command-chat item, so it too tries
  the robot first, and it is never repeated. **Problem:** a robot revoked
  part-way left its item `unknown` and the retry reposted every part, and the
  owner learned of a dead robot only in Settings. **Alternatives rejected:** a
  health probe or recovery timer (a cadence knob; the next send already
  probes); a notice per failed send (one per transition says it all).

  **Built-in robot (Internal edition).** Owner decision 2026-11-08: the
  team's robot application travels with the Internal edition, so an Internal
  install posts through it with nothing configured. An Internal export writes
  `{clientId, secret}` to the overlay facts' `x-dxRobot.builtinCredentialFile`
  (`bundles/yourprovider/tofu_yourprovider/dx/robot_builtin.json`) from the export host's owner-only
  (0600) `data/config/dx_robot_builtin.json` and commits it to every Internal
  target alike (the company Dev platform and the private GitHub copy, which
  is admitted only while private). This repository ignores that path and
  never holds the values, so the Open export, which derives from its history,
  cannot carry them, and the Open publication gate reads them from the local
  file and refuses any tree, history blob, archive member or commit text
  holding either, and the package file itself by its name whatever it holds
  (`export_pkg/AGENTS.md`). A robot configured in Settings
  overrides the built-in one; the view marks a built-in robot `builtIn`, with
  no vault entry, and keeps its refusals on the account like a configured
  one's. **Problem:** a robot configured only in the leaving owner's Settings
  would stop with his install. **Alternatives rejected:** values in tracked
  source (the shared history feeds the Open export and several mirrors); the
  vault in the handoff tarball only (an install from the Internal repository
  would lack it).

  **One robot, many servers.** *Problem* (owner, 2026-11-08): colleagues run
  their own servers from the Internal export with the same robot; each must
  stay bound to its own user's MIS. *Delivery model* (办公开放平台 documents,
  read 2026-11-08): the open platform pushes robot events to one 服务APPKEY
  per application, an OCTO thrift service implementing
  `XmOpenCallbackServiceI.eventCallback` (01 回调事件,
  https://km.internal.example.com/collabpage/1348518781; no HTTP callback, no long
  connection), so all servers would share one event destination: one
  receiver, or OCTO spreading events over whichever servers registered.
  `sendGroupMsgByRobot` and the group APIs allow 5 calls per second per
  application and path (大象消息 §2.2, 大象群管理), one quota for all servers;
  a robot granted the read permissions pulls the history of any group it is
  in (大象消息 §8 `getHistoryGroupMsgs`). *Decision:*
  - Inbound never goes through the robot. Each server reads 大象 only through
    its own user's session (the bridge above, logged in with that user's SSO
    session and confirmed by `whoami`), which is per-user by construction;
    the robot only posts and subscribes to no event. Robot inbound needs a
    central relay (an OCTO service routing events by uid and group to the
    right server), which the owner deferred.
  - The robot posts only for the 大象 account this server's own live session
    is logged in as (the bridge's `connected` uid), into the command chat
    bound to it (`robot.binding_refusal`). Only that session proves a
    binding, never a typed value: the account created the group for its
    identity (`commandChatCreation`, by the robot with the owner as 群主 or by
    the owner's session), or its session received that identity's own
    message in the chat (`robotBinding`, written by ingress before that
    message's receipt is sent); both prove the user is a member. Choosing
    another chat or another 大象 account clears it. Unbound, or with a live
    session of another uid, the robot refuses (`robot_unbound`,
    `robot_binding_stale`) and the item goes as the owner with the one notice
    of any refusal. A fresh install, an Internal export's included, has no
    account and so no binding until its own user enables 大象 and creates or
    speaks in a command chat.
  - In the robot's chat only the owner counts: other members and robot posts
    that are not this account's own echoes are `context` (recorded and
    readable with `channel_read`, never reviewed or answered). Another member
    may be another server's owner, answered by that server, and every
    server's robot posts arrive from the same sender, so a review would
    answer a second time or bounce robot posts between servers.
  - The shared quota never parks an item: a rate-limit answer (10003) sends
    what the robot has not posted as the owner at once, records no refusal
    and sends no notice (the robot is not broken), and this server asks the
    robot nothing until the documented one-second window has passed
    (`RATE_WINDOW_S`, from `dxRobotSendsPerSecond`); posts keep the documented
    pacing.
  - An Internal export carries no owner binding: runtime data (accounts,
    bindings, threads, events, outbox, SSO session files) never travels,
    dx-pura-api's identity file and session cache stay behind (its
    `.skillignore`), and the carried credential vault leaves the reserved
    `channel.*` entries behind (`export_pkg/AGENTS.md`).

  *Least privilege* (owner action in 办公开发者后台, 权限管理 and 事件订阅): the
  built-in application needs 「发送单聊、群组消息」 and, to create command
  chats, 「获取与更新机器人创建群组信息」. Every message-reading permission
  (「获取单聊、群组消息」, 「获取群组所有消息」, 「获取群组@所有人消息」,
  「获取用户发送至机器人单聊与群组中at机器人的消息」), the other group and user
  permissions and every event subscription are revoked, so the key reads no
  one's chats. The visible range covers every colleague using the Internal
  edition; otherwise the robot cannot create or post into their groups and
  they fall back to their own account. *Residual risk:* isolation between
  servers is enforced by code, not by the key. Whoever holds an Internal copy
  holds the secret and can post as the robot into every group it is in,
  create groups, and use what the console still grants; `addGroupMembersByBot`
  (大象群管理 §2) documents only that the robot be in the group, so if the
  granted group permission covers it, a key holder could add himself to a
  colleague's Tofu Router group (which permission it needs is unverified).
  Only a send relay holding the key, or a robot of one's own in Settings,
  keeps the key out of colleagues' copies. A robot item whose answer was lost
  is reconciled by its texts, so in a group shared with another server an
  identical text of that server's robot could settle it. Two servers logged
  in as the same 大象 account both act on its commands: one account, one
  server. *Alternatives rejected:* inbound through the robot (one
  destination per application: the deferred relay); binding to a MIS typed
  in Settings (anyone can type any MIS; the session's own login is the
  proof); waiting out the quota under the robot (the item blocked its chat
  until a later tick while other servers kept the quota busy); a per-server
  share of the quota (needs the server count, a tuning knob, constraint 5);
  checking membership with an API call per send (a read permission or a probe
  per item, where the binding's proofs are facts already received).

  **Owner setup** (only the owner can do these):
  1. In 企平应用开放平台 (https://open.internal.example.com/developer/app/apply) create an
     application, keeping the default client authentication
     (client_secret_jwt); its 应用凭证 give the client_id and client secret.
     Request the 大象 办公 resource there, which creates the 办公应用.
  2. In 办公开发者后台 (https://xopen.internal.example.com/home): 应用功能 → 机器人, enable;
     权限管理, open 「发送单聊、群组消息」; 应用发布, add yourself to the visible
     range and submit for approval (it takes effect about 10 minutes after).
  3. In the 大象 desktop client, open the command chat group → 群信息 → 群机器人
     → 管理, search the robot's exact name and add it.
  4. In Tofu, Settings → 大象 → 通知机器人: enter the client_id and paste the
     secret, or choose an existing vault entry, and save (the card writes
     `PUT /api/v4/channels/dx` `robot: {clientId, secret}` or
     `robot: {clientId, credentialName}`). The secret is checked with a token
     request, then kept only in the owner's reserved credential vault entry
     `channel.u<owner>.dx.robot`; Remove (`robot: null`) deletes it. A
     replaced or removed entry is deleted only after the account committed
     the new record (`retire_replaced_secret`), so a save that fails partway
     (a storage outage after the token check) leaves the account naming an
     entry that still exists, and the same save can simply be repeated. A
     robot whose vault entry is gone reads `robot_credential_missing` in its
     view (`robot.error`) instead of 已配置, which is what its next post would
     meet.

  A group Tofu creates already has the robot (Command chat above), so step 3
  is for a group chosen in Settings. The robot card shows these four steps
  (`settings.dx.robot.step1`–`step4`); change both together. **Problem:** the owner needs the steps where the
  robot is configured, but Tofu serves no repository documents and the
  public repository has no copy of this file. **Decision:** the card lists
  the steps with their two consoles and names this section as their source.
  **Alternatives rejected:** a link to the GitHub copy (missing until it is
  published, then trailing this checkout); a route serving `docs/` (a
  document server for one card).

### C7. Authority

| Source | Authority |
|---|---|
| Owner in the command chat | Human input (sender = bound owner account, thread = command chat) |
| Owner in any other thread | Nonhuman context |
| Owner's own GitHub comments | Nonhuman evidence: a candidate in `watch` threads, context otherwise; never human input (C8) |
| Other people | Nonhuman evidence (initiator `channel`); a Router Turn reviewing it runs under the review ceiling and thread scope (C11) |
| Other people and robot posts in the robot's command chat | Context only: recorded, never reviewed or answered (C6, one robot many servers) |
| Bots and platform notices (sender kind `bot`/`system`) | Context only: recorded, never reviewed or answered (C2) |
| Outbox echoes | Never input |
| Router or worker text | Nonhuman |

Tool authority is decided once in `lib/channels/tools.py`: every channel tool
requires the owner's Global Router conversation (resolved through
`lib.router.binding.global_router`), except `channel_read`, which a subscriber
of the requested thread may also use (one thread: `thread` and `event_id`,
when both are given, must name the same thread), and `channel_send` to the
owner's own command chat, which a Project Router may also use, in its owner
requests and its continuation wakes alike, never a Worker (ROUTER.md R9
"Leads reach the owner directly": any other thread is `owner_chat_only`, and each
call, refusals included, is a `lead_channel_send` audit record); `channel_outbox_approve`,
`channel_configure_thread`, `channel_subscribe` and `channel_unsubscribe`
(the owner's decisions: what Tofu sends and who receives a thread)
additionally require the executing user attempt
(user principal, attended, the task's own assistant Turn) whose human input
passes `require_human_turn`, and so must every input steered into that Turn
(persisted or already consumed) that was not queued as the next request,
through its committed author (`steered_input_turn`). Channel events, Router
reports, relayed questions and peer messages record no author, and neither do
legacy steer records, so any of them merged into the owner's request revokes
the authority; owner command-chat messages and UI steers keep it. The
`channel-event-`/`channel-owner-` command namespaces only name a steered
batch's review scope (C11), never authority. A chat decision
on a draft also needs its approval code in the owner's own words, and a
review is confined to its threads (C11). The catalog projection (`ToolSpec 'channels'`)
mirrors this and the Global Router's coordinator ceiling admits the names via
`ROUTER_TOOLS` (a review's ceiling via `x-assistant.reviewTools`, C11). HTTP account changes need the `admin` scope; reads and owner
decisions need `conversations`.

TODO(enterprise): one channel account and bridge per Tofu user; the account
binding and store keys are owner-keyed, but the supervisor hosts only the
scheduler principal's owner and ingest is serialized per owner inside one
process. Several scheduler replicas need an enabled-account index and a
per-account lease first. The GitHub supervisor has the same limit.

### C8. GitHub adapter

**Problem:** task context lives on work surfaces as well as in IM: the owner
chooses plan A in the 大象 command chat, then switches to plan B in a pull
request comment, and the Global Router must relate the two. Tofu runs on a
private server that GitHub webhooks cannot reach, and a GitHub comment is
written, and editable, by many people.

**Decision:**

- **Threads.** One thread per issue or pull request,
  `github:<owner>/<repo>#<number>`, kind `issue` (GitHub's REST API treats
  every pull request as an issue: one number space, one comments endpoint),
  stored lowercase. Default mode `mentions`, default reply policy `notify` (C11).
  Subscriptions, standing instructions (ROUTER.md R4), `channel_read` and the
  outbox work on these keys like on 大象 threads.
- **No command chat; every comment is nonhuman.** A command chat requires that
  only the owner can author and alter its text. On GitHub, repository
  maintainers, organization owners and apps with write access can edit any
  comment, and an edit made before the poll is indistinguishable in REST from
  the owner's text, so no GitHub thread qualifies (`x-channelTraits.github.
  commandChat: false`; the 大象 account refuses a GitHub command chat). The
  owner's own comments are therefore evidence, rendered with
  `owner_authored="true"`: a candidate in `watch` threads, so the Global Router
  sees the owner's pull request comment next to the IM history that chose
  plan A, and context (subscribers only) elsewhere. To act on it the router
  either asks the owner in the command chat or applies a standing instruction
  the owner created in a human turn and scoped to that thread.
- **Ingress by polling** (`lib/channels/github/adapter.py`, one serial worker
  per owner, as GitHub asks integrations not to make concurrent requests).
  `GET /notifications?all=true&since=<frontier>&per_page=50` (the documented
  maximum) sends the stored `Last-Modified` as `If-Modified-Since`; `304`
  means nothing changed and costs no rate limit. GitHub's `X-Poll-Interval`
  is the only cadence: it is persisted (`poll.nextAtMs`) right after each
  notifications response, before any processing, so a restarted session
  never polls early; until GitHub has sent one, the scheduler's existing tick
  triggers passes. Only `Issue` and `PullRequest` subjects are threads. For
  every notified thread and every thread in `watch` the pass reads the opening
  post once (when it was created after the start), issue comments and, for
  pull requests, review comments (`?since=`, `per_page=100`, `Link` pages).
  Each list keeps its `since` until it returns rows and sends its ETag
  (`If-None-Match`), so an unchanged thread answers `304`. A review reply
  carries its parent's author (`in_reply_to_id`, read by ID when the parent is
  outside the window). All events are ingested in one `ingest_batch`; only
  after it succeeds do the thread cursors and ETags, the account frontier
  (newest notification `updated_at`, GitHub's clock) and `Last-Modified`
  move. A route failure ends the session; the supervisor restarts it at the
  next tick and the same window replays as duplicates. The ended session's
  status is stored best effort, as in C6: a pass usually fails because
  storage did, so an unstored report still ends the worker normally instead
  of killing its thread. A `404`/`410` thread
  is marked unavailable and is read again only when GitHub notifies it.
  Stopping the session interrupts the worker's request in flight through the
  shared client's owner interruption ([NETWORK.md](NETWORK.md)), so a stop
  never waits out a request's timeout; the interrupted pass moved no cursor,
  and an interrupted comment post is an unknown send that reconciliation
  resolves by its marker. The supervisor follows C6's shutdown rule: stopping
  fences ticks, configuration changes and new sessions until the next hosting
  lifecycle starts, a session leaves the registry only once it joined, and
  shutdown asks it to stop when its drain begins.
- **Watch reads the thread directly.** GitHub documents notifications as
  updates for subscribed activity and offers one's own updates only as an
  email option, so the owner's own comments are seen by reading watched
  threads on every pass. A thread that enters `watch` starts at that moment
  (`modeChangedAtMs`); what `mentions` left unread is not replayed. Such owner
  decision times are this host's clock; each pass moves them onto GitHub's
  clock by the skew measured from the notifications response's `Date`.
- **Event IDs.** `<created_at epoch seconds, 10 digits><kind, 1 digit><GitHub
  id, 20 digits>` with kinds 1 opening post, 2 issue comment, 3 review comment
  (`x-github.contentKinds`). GitHub keeps separate ID sequences per kind, so
  the kind keeps a thread's IDs unique; the time prefix orders them like a
  snowflake (watermark, newest-first reads). Mentions are `@login` outside
  code (team mentions do not count); events carry their GitHub `url`.
- **Outbox.** `POST /repos/{owner}/{repo}/issues/{n}/comments` with the
  disclosure label and an invisible marker `<!-- tofuOutbox:<item> -->`
  (echo suppression and reconciliation); at least one second between posts
  (GitHub's guidance for mutative requests). `400`/`422`, `401`, a
  non-rate-limit `403` and `404`/`410` are definite failures
  (`invalid_input`, `auth_failed`, `forbidden`, `unsupported_target`); a rate
  limit, `5xx` or network error leaves the item `unknown`, and it is
  reconciled by reading the owner's comments in the thread for the exact
  marker before any resend (no clock comparison between GitHub and this
  host). Redirects repeat the same request on the same origin; the token
  never leaves the configured origin.
- **Rate limits.** `retry-after`, else `x-ratelimit-remaining: 0` until
  `x-ratelimit-reset` (measured against the response's `Date`, so local clock
  skew cannot shorten the wait), else (`429` or a secondary-limit message) at
  least one minute: the worker pauses polling and sending until then (status
  `disconnected` with the resume time).
- **Token.** The notifications endpoints accept only a personal access token
  (classic) with `notifications` or `repo`. A token pasted in Settings is
  verified with `GET /user` and `GET /notifications` before it is stored, in
  the owner-scoped vault entry `channel.u<owner>.github.token`. The vault
  reserves `channel.` entries from agents (vault index, `run_command`) and
  from the generic credential routes, so no agent can post outside the
  outbox and its reply policy; the owner may instead choose an existing
  general vault entry by name. The value is read per request and never
  logged, returned or written to channel storage. A `401` ends the session as
  `needs_login` with no restarts until the owner saves a token that verifies.
  A changed identity, credential or API base restarts the session. Status
  ownership follows C6: saving a token whose probe leaves the session running
  does not reset a reported state.
- **GitHub Enterprise Server.** `apiBaseUrl` (https only, e.g.
  `https://<host>/api/v3`) is an account field changeable while disabled.

**Alternatives rejected:** webhooks with `X-Hub-Signature-256` (GitHub cannot
reach the private server, and they need an unauthenticated route through the
default-deny boundary plus per-owner secrets, while conditional polls cost no
rate limit when nothing changed); a GitHub command thread (anyone with write
access could edit text into owner commands); raw comment IDs as external IDs
(separate ID sequences collide and misorder mixed threads); an own poll
interval or `since` overlap margin (tuning knobs, constraint 5: GitHub's
header and second-granular `since` with dedup suffice); repository-wide
comment listings or the user events API for the owner's comments (more
requests, and GitHub documents event latency of 30 s to 6 h); fine-grained
tokens or GitHub Apps (the notifications API rejects them).

**Limits:** a comment that becomes visible later than the window that should
have held it is missed (GitHub documents no visibility bound, so no margin is
invented); the owner's comments on threads outside `watch` arrive only with a
later notified pass of that thread; a renamed repository is a new thread
key; pull request review summaries and edits of recorded comments are not
events.

### C9. Media: images, files and voice

**Problem:** 大象 is a chat app with files: the owner sends screenshots, decks
and voice notes from the phone, and colleagues share them in groups. The
adapter flattened every non-text message to `[type:N]`, so the receiving
conversation saw neither the content nor what kind of message arrived.

**Decision:**

- **Types.** The adapter reads each message by its wire type
  (`x-dx.messageTypes`: the official enum of
  [消息类型与消息体字段](https://km.internal.example.com/collabpage/2648372848); numbers
  that page omits follow the Web SDK's `MESSAGE_TYPE` enum). Text and quote
  replies keep their own text. Rich text (general subtype 100, a JSON string on
  the IM wire and Base64 JSON on the send API) renders its nodes: text, links
  with their URL, mentions, emoji, lists and `[image]` for inline images. Every
  other type becomes a readable description of its label and metadata:
  `[file: deck.pptx]`, `[voice message: 3 s]`, `[video: 4 s, 288x352, 286.5 KB]`,
  `[location: …]`, `[card: <summary>]`, `[message type 99]`.
- **Media facts.** Images (type 4 and rich-text images), files (8) and voice
  (2, AMR only) also become `ChannelMedia`: kind, a sanitized name (one path
  component: no directories, control characters or leading dots), MIME type,
  size, duration, and a transient provider locator (URL and file token) that is
  never recorded, rendered or logged.
- **Resolution, once.** Ingress resolves the media of an event that is about
  to be routed (never of dropped messages or ignored threads) after its CAS
  record and before routing. Each original is downloaded through the adapter's
  media port and stored as an owner-scoped retained attachment by
  `lib.media_attachments.ingest_received_file`, the chat-upload pipeline:
  indexed when the knowledge parser reads it, otherwise kept verbatim; never
  executed or opened. A voice message is transcribed by
  `lib.video_analysis.transcribe_track` (ffmpeg, then the owner's
  speech-recognition route, Settings → 语音识别) beside its audio; without a
  route the audio is attached and the turn says transcription is unavailable.
  The outcome is recorded on the event (`media`), so the replay of an unrouted
  event reuses it. A failed download or store is an outcome shown in the turn
  (`failed` with its reason), never a silent drop; a lost bridge raises and
  replays (C1).
- **Download.** Only through the session's own bridge (`download`: no new
  process, login or token mint), only for URLs under the Web SDK's own file
  hosts (`x-dx.fileUrlPrefixes`, `http.common.ts` `DX_FILE_URL.online`), with
  the SDK's file headers (`HTTPCommon.getFileBlob`: the message's file token
  as `X-Xmftk` plus the IM session identity). Redirects are followed by hand
  (the WHATWG Fetch limit of 20) so the headers never reach the signed storage
  location; the stream goes to a private scratch file the adapter chose. A web
  page returned instead of the file (`text/html`) is a failure: the session may
  need a new login. No size cap is added: 大象's own limits bound what arrives
  and the attachment pipeline has none.
- **Delivery.** Stored originals ride the Turn as `attachments`
  (`TurnMediaAttachment`), so model projection shows images and document text
  and the UI shows them, and the text names each by `att_media_<id>`, because a
  steered input reaches the running model as text only (`read_files` and
  `inspect_image` open it). A nonhuman batch lists `<channel-media kind name
  mime_type size_bytes duration_s status attachment_ref detail
  transcript_status transcript_detail>` (the transcript as its content) inside
  the sender's `<channel-event>`: third-party media stay nonhuman evidence like
  their text (C7). An owner command keeps its verbatim text and adds one plain
  line per item (`[file attached: deck.pptx — att_media_…]`, `[transcript] …`).

**Alternatives rejected:** downloading in the adapter before ingress (fetches
the media of dropped messages and ignored groups into the owner's store); the
open platform's temporary-URL exchange (`changeDownloadTempUrl`: a separate
permission and identity, while the session already has the SDK's file access);
the skill's attachment CLI per file (a new process and IM-token mint per
download); the legacy `/api/images` directory (not owner-scoped, a second
store); a channel media store, size caps or type allow-lists (constraints 3–5).

**Limits:** catch-up history rows carry no file token and no voice duration,
and an image only as its preview URL; videos and stickers are described, not
downloaded; 文枢-controlled files outside the SDK's file hosts are reported as
not downloaded. A large download or a transcription delays the owner's later
events, because ingest is sequential per owner (C1).

### C10. Outbound attachments (大象 and personal 微信)

**Problem:** the owner reads Tofu on the phone, and decks, reports and charts
arrived only as text or as server paths a phone cannot open. 大象 is a chat
app with files, but the open-platform robot sends files only by URL ("IM不提供
文件存储能力", 消息类型与消息体字段,
https://km.internal.example.com/collabpage/2648372848), and the owner's own messages
never notify the owner (C6).

**Decision:** files go as native 大象 messages through the owner's session;
the notification robot keeps posting the text.

- **Admission.** `channel_send(..., attachments=[{path}])` admits every file
  before anything is recorded (`lib/channels/attachments.py`): a regular file
  in the sending conversation's verified workspace roots (relative to the
  primary root, `rootname:path`, or absolute inside a root), read without
  following any symlink (`lib.artifacts.file_snapshot`), one of that
  conversation's own published artifacts by its delivery URL
  (`/api/artifacts/<id>/download`, `/api/response-assets/<id>`), or a slide
  deck it produced by the download URL the slide tools return
  (`/api/v4/slides/<task_id>/file`; the job manifest names the owner and
  conversation, and the newest export in its deck directory is sent). With
  `attachments=[{path, project}]` it is a regular file inside one of the
  owner's registered project roots (a root of any of the owner's project
  folders, primary or not, or a project with a coordination binding; named
  exactly as `get_router_projects` lists it; never the router home), read the same way and recorded with that
  project as its provenance (`OutboxAttachmentView.project`): how a project's
  deliverable reaches a chat (C7; CHANNEL_PROTOCOL.md P13). With
  `attachments=[{path, worker}]` it is a file in the registered checkout of a
  Worker the sending lead dispatched (`lead_attachments.lead_worker_checkout`).
  No carried file may hold a credential (`lib/conversations/attachment_secrets.py`: a
  credential file by name, a vault entry's exact value, an issuer-documented
  credential format; `attachment_secret`).
  Traversal, symlinks, absolute paths outside the roots, unregistered
  projects, other conversations' artifacts and decks, undocumented types and
  oversize files are refused with a code and what to do instead, ending with
  the supported path (`{path, project}`, and that a project conversation asks
  its lead to send instead of using a login of its own); nothing is
  dropped silently. The limits are 大象's
  documented chat upload limits (`x-dxAttachments`; 服务端聊天文件上传接口,
  https://km.internal.example.com/collabpage/684760118): images jpg, jpeg, png, gif,
  bmp; files txt, csv, zip, rar, pdf, doc, docx, xls, xlsx, ppt, pptx; each
  under 30 MiB, checked before the file is read. A channel that carries no
  files (GitHub, 企业微信, QQ, 微信: `x-channelTraits.attachments`) gets each
  file named in the text instead, with a published artifact's delivery URL
  (CHANNEL_PROTOCOL.md P5: degraded, never refused or dropped); its source is
  admitted the same way, but no upload limit applies.
- **Records, not bytes.** An attachment record keeps name, MIME type, size,
  sha256, kind (`image` | `file`) and its source. At send time the original
  is read again and a changed file is not sent (`attachment_changed`). The
  idempotency key covers the attachments (names and digests). Reply policy,
  the disclosure label and draft approval apply to the item as a whole.
- **Transport.** The bridge's `send_image` / `send_attachment` hand one file
  to the engine, which uploads it to 大象's own storage and sends an image or
  file message over the owner's IM session with the part marker
  `<item>/<n>` (`x-dxAttachments.partMarker`) in `extension.tofuOutbox`.
  In the robot's thread the files go first and the robot's text last, so the
  notification arrives when everything is in the chat; elsewhere the text
  goes first, so the `[Tofu 代发]` label introduces the files.
- **Confirmation.** The skill documents that 大象 can acknowledge a file
  message it never delivers. An item with attachments is a list of parts with
  their own state (`textPart` and each attachment), so a resumed item never
  sends a confirmed part again. The text is sent once accepted, as before. An
  accepted attachment (`acked`) is `sent` only when the thread shows it: its
  echo on the owner's connection or the thread history (marker, client UUID
  or message ID). The history of accepted parts is read from the oldest one's
  message ID on (`history_from`; 大象 IDs are time-ordered snowflakes), not
  from the local attempt time, so clock skew cannot hide a delivered file. An
  accepted part that is absent while that history already holds a later
  message is `failed` (`not_delivered`) and never sent again; a
  part whose send outcome is unknown is looked up by its marker and sent again
  only when absent, like an unknown text item. While only accepted parts
  await confirmation the item is `unknown` (`awaiting_confirmation`) but does
  not hold back its thread: those parts are never resent, so they cannot
  reorder it. The pass after the one that sent a part is its confirmation
  point: the echo has had its chance, and that pass reads the history
  again. A part that pass cannot confirm stays pending, never silently. It
  gets the reason `history_failed` when the read failed (no evidence,
  CHANNEL_PROTOCOL.md P4), or `needs_owner_confirmation` when the history
  answered without it or any later message. The item carries that reason as
  its error, raises the Watchdog outbox fault once and is never resent.
  Every later pass reads again, so the first read that shows the part or a
  later message settles it `sent` or `failed` (`not_delivered`), which a new
  send may retry. Neither reason needs a timer: the passes run at the
  supervisors' existing tick (2026-11-09: an accepted PDF sat
  `awaiting_confirmation` for good, because every history read failed with
  an expired SSO ticket, C1). The item is `sent` when every part is, `failed`
  (`attachment_failed`, or the text's code) when any part failed; a refused
  text stops the files it should have introduced (`not_sent`). Failed items,
  blocking unknown items and items with unconfirmed accepted parts become
  Router Watchdog signals (R7).
- **Failure notice.** An upload refused for the session's expired ticket is
  deferred to the relaunched session (C1), never failed. When an item still
  settles `failed` with failed attachments, the owner reads it, not only
  Settings: one untranslated notice (origin `attachment_notice`, idempotency
  key `attachment-notice:<item>`) names each undelivered file with its reason
  and code (`outbox.attachment_failure_text`). It goes to the item's own
  thread when that is the command chat, right under what did arrive, and
  otherwise to the command chat (a notice to other people would itself be a
  draft). *Rejected:* only the Watchdog signal (the 2026-11-17 failure
  reached the owner through the Router's own resend, not through Tofu).
- **Previews for the phone.** Images go as image messages and preview
  inline; documents go as file messages, which 大象's clients open in their
  Office/PDF preview. A pptx, docx or xlsx is meant to be followed by a PDF
  copy, and a pptx also by one PNG per slide, each recorded with
  `renditionOf` (its original's index). The only converter in this repository
  and environment is Tofu's slide renderer (`lib/channels/renditions.py`): a
  pptx exported from a Tofu deck, i.e. beside its `deck.pptd` and at least as
  new as it and its page files (every Tofu edit re-exports), is rendered fresh
  to page PNGs and a PDF of them (PyMuPDF). A deck changed after its export
  gets no copy (it would not match the file); docx, xlsx and other pptx files
  have no converter. Then the original goes alone and the text says why no
  preview copy was made. Copies are made at send time in a scratch directory
  removed after the attempt, never uploaded elsewhere; a resumed item makes
  them again for parts still to send.
- **Final answers.** A Global Router answer to a command-chat message (C5)
  attaches the files of its own workspace and the slide decks it produced that
  it links with Markdown links or images (outside code), through the same
  admission. Web URLs, artifact links (their decoding runs worker processes,
  not for the task's terminal seam) and anything else are left alone; a linked
  file 大象 does not accept is named in a note under the answer; attachment
  trouble never holds back the answer.
- **Settings.** Each outbox item lists its attachments (name, type, size,
  state, the original of a preview copy); original images show a thumbnail
  read through `GET /api/v4/channels/outbox/{id}/attachments/{index}`
  (owner-scoped, re-read and verified like a send).

**Alternatives rejected:** the robot plus company storage (it needs a bucket,
credentials and permissions the owner must provision, puts files behind URLs
others can open, and the mobile preview of such files is undocumented); the
owner's identity for everything (previews, but nothing notifies); trusting
the upload acknowledgement (the documented false success); resending a file
that was accepted but not seen (duplicates a slow delivery, repeats a broken
one); copying files into channel storage (a second blob store, when the source
and its digest already refuse changed bytes); previews by re-importing any pptx
into a Tofu deck (that import drops placeholders, groups and SmartArt, a
misleading preview); LibreOffice for docx, xlsx and any pptx (neither in the
repository nor installed here; a new external converter process needs its own
lifetime and teardown contract, an owner decision); conversion services
outside Tofu (owner files never leave for a third party); a cap on files or
slide images per message (a tuning knob).

**Checks on the owner's phone after a restart** (not provable offline): an
image previews inline; a pptx, its PDF copy and its slide images open in
大象's previews;
the robot's notification arrives after the files; the bridge receives echoes of
its own file messages (otherwise confirmation waits for the history read of
the next executor pass).

### C11. The owner's assistant for other people's messages

**Problem:** the owner wants Tofu to help with other people's 大象 messages —
what needs them and why, summaries, replies drafted in their voice — while
nobody else can chat through Tofu or steer it the way the owner does. The
Global Router reviewed other people's batches with its whole ceiling (project
dispatch, conversations, subscriptions, any thread), so a message's text was
one model judgment away from a tool call; `auto` sent whatever the router
wrote into a thread; and nothing listed what had been sent on the owner's
behalf.

**Decision:**

- **Authority comes from durable facts, never from text.** Only the verified
  owner instructs Tofu: a message whose provider sender uid equals the bound
  identity's uid, in the command chat (C2, C7). Display names, signatures,
  quotes and forwards of the owner, @-mentions of the robot and claims in the
  text or in an attached file never change that; such messages stay nonhuman
  evidence inside their sender's `<channel-event>`. A quote reply's quoted
  message keeps its own author's standing (C13). `lib/channels/assistant.py`
  gives every Global Router Turn one authority (`OutboxAuthority`): `owner` (a
  genuine owner request), `merged` (an owner request into which other
  people's batches were steered), `third_party` (a review: the Turn's input is
  a channel batch) or `nonhuman` (report relays, timers, or a Turn that cannot
  be verified).
- **A review runs under a fixed ceiling.** The coordinator ceiling of a review
  is `x-assistant.reviewTools`: `channel_read`, `channel_send`,
  `channel_outbox`, and the owner's standing instructions
  (`router_instruction_list`, `get_router_projects`, and `route_to_project`,
  whose own check admits only an owner message or an active instruction,
  ROUTER.md R2/R4). The one coordinator boundary applies it to the catalog and
  to every call (`lib.router.policy.router_tool_ceiling`), and a refusal says
  what a review may do instead. Channel tools confine a review further: it
  reads and writes only the threads under review — the events of its input
  batch and of batches steered into it, each remembered by its delivery
  command ID before dispatch — plus the owner's command chats (each
  platform's that has one, `x-channelTraits.commandChat`), and its replies
  carry no files. The owner's notes are its only other context. A review
  therefore cannot dispatch or delegate work, create, control or read other
  conversations, change subscriptions, settings, instructions or memory,
  decide a draft, or reach another thread.
- **Reply flow per thread, decided at enqueue with the authority.** A contact
  is their private chat (`dx:uid:<uid>`), a group its thread. `notify` is
  every thread's default on every channel, private chats included:

  | Reply policy | Review (`third_party`) | Owner request | `merged` / `nonhuman` |
  |---|---|---|---|
  | `notify` (default) | refused: tell the owner in the command chat | draft | draft |
  | `draft` | draft | draft | draft |
  | `auto` | queued only as a reply (`reply_to`) to a reviewed message and without `needs_approval`; otherwise a draft | queued | draft |
  | `never` (Off) | refused | refused | refused |

  The command chat always sends. Every item for another thread carries the
  `[Tofu 代发]` label and keeps C5's ordering and reconciliation.
- **Approval** happens in Settings (the outbox card) or from a chat. A new
  draft is announced in its own platform's command chat, else in 大象's, or
  only in Settings while neither is live (origin `draft_notice`, idempotency
  key `draft-notice:<item>`, not translated, so the owner reads the exact
  text that would go out and the files it carries) with its approval code
  (`approvalCode`: the item ID's first `x-assistant.approvalCodeLength` hex
  digits). `channel_outbox_approve` needs a genuine owner request with no
  batch merged into it (C7) whose owner's own words — the human input or an
  owner message steered into the reply — name that code (「批准 7F3A2C」).
  Text the router read or was sent cannot decide a draft.
- **Owner notes.** `assistantNote` on the 大象 account (who the owner is,
  their voice) and a `note` per thread (that contact or group; for `auto`, what
  Tofu may say without approval) are written only from Settings (`PUT
  /api/v4/channels/dx` `assistantNote`, `PUT …/threads/{key}` `note`); no model
  tool writes one. A batch for the Global Router carries them as escaped
  `<owner-note>` elements after a fixed review instruction
  (`delivery.REVIEW_GUIDANCE`); subscribers never see them.
- **Audit.** Every item recorded for a thread other than the command chat is
  listed, whatever becomes of it, in creation order
  (`GET /api/v4/channels/audit?channel=&before=`, newest first, pages of
  `x-limits.readPageEvents`). Each item keeps the `authority` it was recorded
  with and its latest `decision`: `queued` by `owner_request` or `auto_reply`,
  `approved` or `discarded` by `owner_settings` or `owner_message` (with the
  Turn ID).
- **Others cannot chat through Tofu: silence.** A non-owner's message is never
  handled as a request — written to the owner, @-mentioning the robot in a
  group, or posted in the command chat — and Tofu writes to that person only
  when the thread's policy allows a reply, as the owner's labelled assistant.
  The notification robot has no inbound (C6): a private chat with it reaches
  no one. A fixed notice ("this is X's assistant…") was rejected: it would be
  an unapproved message on the owner's behalf in every thread, it tells a
  prober an agent is listening, and in groups it is noise.
- **The owner's own messages in other chats stay context.** There the owner
  talks to other people, and the same account carries Tofu's labelled sends:
  an "OK, do it" said to a colleague, a sentence someone coaxed from the owner,
  or an echo that escaped suppression would otherwise command Tofu.
  Instructions come only from the command chat and Tofu itself.

**Alternatives rejected:** a separate tool-less drafting call in the channel
layer (a second model invocation path beside the router Turn, constraint 4,
and the router still triages); prompt rules alone (the model would be the only
guard); never steering other people's batches into a running owner Turn (the
dispatcher cannot choose atomically by the running Turn's authority, and
queueing every batch multiplies paid Turns; owner decisions already refuse
merged Turns and their sends become drafts); approval commands parsed at
ingress (C2 rejects keyword prefilters; the router plus the code in the
owner's own words is exact); per-sender policies across groups (a group reply
is read by the whole group, so the group's policy governs it); auto-reply
without binding to a reviewed message (a message in one thread could make
Tofu "reply" in another `auto` thread).

**Limits:** within `auto` the owner's note bounds what the model writes; it is
not a filter. A batch merged into an owner Turn can still influence the model
within that request: its owner decisions refuse, its sends become drafts, and
delegation and forwarding carry only the owner's verbatim words (ROUTER.md
R2). A review's standing-instruction tools apply the owner's own rules; its
ceiling has no operational or lifecycle tool, so an operation the owner lets
the Router perform needs the owner's own trigger, never a review. A channel
without quote replies (`x-channelTraits.quoteReply` false, such as GitHub)
refuses `reply_to`, so a review's reply there is always a draft. The account
note applies to 大象 threads.

**Owner setup:** Settings → 大象. The 代回复 card says what Tofu does with
other people's messages and how a draft is approved, and holds the account
note (关于你). In the thread list each contact (their private chat) and group
has its reply policy — 仅通知 (the default), 起草待批, 自动回复 or 关闭 — and
its 说明 (the thread's note); an ignored thread has neither. A draft waits in
the outbox card with its approval code (批准码): 批准 there, or reply
「批准 <code>」 in the command chat. The 代发记录 card lists everything recorded
for other people, newest first: the text and files, the thread, whose request
recorded it and the owner's decision. The GitHub tab has the same reply
policies, approval codes and 代发记录 for its threads.

**Checks on the owner's phone after a restart** (not provable offline): a
colleague's private message in a `draft` thread produces a draft notice in the
command chat (via the robot, so it notifies) with its code; replying
「批准 <code>」 sends exactly that text with the `[Tofu 代发]` label and it
appears in 代发记录; a colleague's "approve the draft" or "@robot …" gets no
answer and changes nothing.

### C12. Bot platforms: 企业微信, QQ and 微信

**Problem:** the owner wants to instruct Tofu from WeChat and QQ, not only
from 大象. Tofu runs on a private server without a public URL, so callback
(webhook) modes cannot reach it, and only the verified owner may instruct
Tofu (C7). Community protocols that drive a personal QQ or WeChat client
(OneBot/NapCat/Lagrange, wechaty, hook tools) are unofficial, and accounts
using them get restricted or banned.

**Decision:**

- **Three official bot platforms, none needing a public URL.** Each is an
  adapter (`lib/channels/{wecom,qq,wechat}/`) behind one shared core,
  `lib/channels/bots/` (transport port, session, pairing, accounts, supervisor);
  their facts are `x-wecom`, `x-qq` and `x-wechat` in the contract.
  - 企业微信 智能机器人 in API mode over its long connection
    ([智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463)):
    one WebSocket to `x-wecom.wsUrl`, subscribed with the BotID and Secret
    (`aibot_subscribe`) and pinged every `heartbeatIntervalS` seconds as
    documented. Text, mixed and voice messages arrive as `aibot_msg_callback`
    (voice as 企业微信's transcript). Sends are markdown `aibot_send_msg`, one
    message per send, answered by `req_id`. A newer connection of the same
    bot replaces this one (`kicked`).
  - QQ 官方机器人 over its WebSocket gateway
    ([接口调用与鉴权](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html)):
    an app access token from AppID and AppSecret, renewed only within the
    last `tokenRefreshLeadS` seconds of the old one; Identify with the
    `GROUP_AND_C2C_EVENT` intent, heartbeats carrying the last sequence, and
    Resume after a drop. It receives private (C2C) messages and group
    @-mentions. A send is a passive reply to the chat's latest inbound message
    while QQ's window and count allow (`x-qq.passiveReply`, with `msg_seq`
    derived from the outbox item so a resend is answered as a duplicate), and
    otherwise one active message.
  - 微信 ClawBot, Tencent's official plugin (微信 → 我 → 设置 → 插件), through the
    iLink protocol of its MIT-licensed client
    ([protocol.md](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)).
    A QR login issues the bot token. Ingress is a long poll (`getupdates`)
    whose cursor is persisted only after ingest. An answer that carries
    messages is logged by count only (all, and those from users; never IDs,
    tokens or text), so an inbound message iLink never delivered can be told
    apart from one Tofu dropped. A reply carries the
    `context_token` of the chat's latest message: ClawBot cannot write
    first.
- **One authority boundary.** Every inbound message other than a consumed
  pairing code becomes a `ChannelEvent` through `ingress.ingest_batch` (C1,
  C2, C7). Only the bound owner in the
  command chat is human input; everything else is third-party data under
  the thread's mode. Defaults are the contract's: private chats `mentions`,
  groups `ignore` until the owner sets another mode.
- **Binding the owner.** Platform user IDs are per-bot pseudonyms the owner
  cannot look up, so Tofu never takes a configured owner ID.
  - 企业微信 and QQ: saving the credentials issues a one-time pairing code,
    shown in Settings: 8 characters (40 random bits) from an alphabet without
    look-alikes. The first private message from its own author that equals
    the code binds that author as the owner (`identity`) and makes that
    private chat the command chat. The message is consumed, never routed, and
    the bot confirms in the chat. A code sent in a group, or a mistyped one,
    binds nobody. Only Settings shows the code, and it works once; the owner
    can issue a new one or cancel it at any time.
  - 微信: the user who scans and confirms the QR login is the owner, and their
    chat with ClawBot is the command chat.
    Settings exposes **刷新二维码** while waiting, after scanning, and after
    expiry or failure; cancelling first is unnecessary. The provider's
    `expired` answer hides the old code and shows a localized expiry notice.
    There is no invented countdown: the protocol provides status, not a QR
    expiration timestamp. The existing Settings poll observes login changes
    as well as connection status, so `scanned` appears without a page reload.
    QR request network failures use the existing `wechat_unavailable` HTTP
    error and an inline, localized retry message; a failed refresh preserves
    the previous login. Request progress disables the login actions until
    completion. A successful refresh changes the session binding to the new
    QR key, and late replies/status from a retired login are discarded.
    Login status and message polling treat only a response-read timeout as
    an empty long poll. Proxy CONNECT/TLS timeouts are connection failures
    at the shared HTTP boundary and reach the session's error/recovery
    path; they cannot silently leave the account waiting for confirmation.
    A failed login-status request ends the session as `wechat_unavailable`
    without the request URL, which carries the QR key that claims the bot
    token, so the key never reaches a log or the status detail.
    The generic configured proxy covers iLink as it does other external
    hosts; the adapter adds no separate proxy setting or retry budget.
    **Alternatives:** requiring cancel then start adds an unnecessary step;
    silent automatic renewal can replace a code while the owner is scanning;
    a local expiry timer would guess a server deadline.
    Channel routes retain their domain error code in the v4 problem envelope
    through `error_code`; a status-only `service_unavailable` code cannot
    tell the UI which recovery message to show.
- **Identifiers** (`lib/channels/bots/ids.py`). Provider IDs are opaque
  strings (企业微信 userid or chatid, QQ openids, 微信 user IDs). Thread keys
  carry them as unpadded lowercase RFC 4648 base32, which is reversible and
  uses only the lowercase ASCII that store keys allow. An event's external ID
  is the provider's epoch seconds (10 digits) followed by 22 digits of a
  digest of its message ID, so order follows time and a redelivery maps to
  the same event. Each message's external ID is recorded under its message
  ID at first sighting (`ChannelStore.provider_message_id`, a CAS create),
  so a quote that names the message by that ID finds it (C13); 企业微信
  callbacks carry no time, so that first sighting mints the ID.
- **Outbox** (C5), executed by the channel's own session.
  - The platforms expose no thread history, so an `unknown` send is sent
    again: at least once. QQ's `msg_seq` turns a resent passive reply into a
    duplicate answer, which counts as sent.
  - A send that no network route carried is the deferred `not_connected`
    receipt (channel protocol P4): every route's proxy CONNECT, TCP or TLS
    setup failed before the request went out (`NetworkRoutesExhausted`,
    [NETWORK.md](NETWORK.md)), so the platform has nothing. The item stays
    queued, raises no Watchdog fault and goes at the next pass or tick; a
    failure after the request went out stays `unknown`. *Problem:* proxy
    connect timeouts turned 微信 sends that went out on the next pass into
    unknown items and Watchdog faults, read as silent delivery failure ("log
    in again"). *Alternatives rejected:* classifying in the outbox core
    (adapters translate transport facts into receipts); deferring every
    connection error (a reset after the request may have delivered it).
  - A text beyond the bot's documented limit (企业微信's `markdownBytes`, the
    `maxTextBytes` of its `x-adapters` descriptor; QQ and 微信 document none)
    goes out as chunks, each its own send with its own state (channel protocol
    P5): a chunk already sent is never sent again, and an unknown chunk is
    sent again like an unknown item before the chunks after it.
  - Personal 微信 means ClawBot iLink, not 企业微信. Files go as original
    generic FILE items in private chats. The outbox requires the same bound
    bot, owner and immutable vault credential generation at delivery, and an
    inbound context for that generation and chat. Both caption and FILE are
    fenced at shared network wire admission, as are upload setup and CDN
    transfer. A replaced login never supplies credentials for old work;
    an already-admitted request cannot be recalled by a new login.
    Legacy filename-only 微信 originals and notices without a binding are
    refused, including legacy drafts approved after a replacement login.
    Existing chats need a new inbound message to bind their stored context
    to the bot. 企业微信 and QQ retain the explicit filename-only fallback.
  - A file uses `getuploadurl`, an encrypted HTTPS CDN POST, then a separate
    `sendmessage` with the CDN acknowledgment reference. CDN requests go
    only to the documented CDN origin, without the bot bearer or redirects.
    The file's `provider_ack` means iLink accepted the file message, not that
    the handset received it. No numeric file limit or suffix list is asserted
    without a provider contract; provider refusals are not success. A transfer
    that never got to `sendmessage` may defer safely; a lost send ACK stays
    `unknown/delivery_unconfirmed`, is not automatically resent, and does not
    block later items. The outbox durably marks each possible submission;
    recovery resumes only unsubmitted parts, keeps acknowledged captions, and
    rebuilds missing quote-index entries from durable ACK IDs. A same-thread
    quote can use a committed ACK before its index is written. A host-scoped
    OS lock excludes concurrent executors for the same owner on the same
    runtime filesystem; process exit releases ownership without a lease timer.
    A durable claim token fences submission and results, and a deferral clears
    the token and requeues the part in one CAS. Text-only items
    retain their existing retry behavior.
  - The shared outbox view separates personal 微信 send evidence from queue
    state. A queued item has no positive acceptance evidence. A `sent` item
    reports `deliveryEvidence: acceptance_unverified`: recorded as sent,
    but acceptance of the complete message is unverified. Text `sent` rows
    do not retain whether the provider explicitly acknowledged them, including
    the caption of an attachment. A FILE's durable `provider_ack` applies only
    to that file, not its caption. The view conservatively labels all sent
    items this way; it does not infer proof from state, IDs or migration history,
    rewrite old rows, or claim handset delivery. An `unknown` item reports
    `deliveryEvidence: delivery_unconfirmed`: check the chat before requesting
    a new send. Settings labels this as unconfirmed, not as an ongoing delivery
    check. The active list omits settled items; read their evidence with
    `channel_outbox(item_id)`. Personal 微信 send results and item lookups give
    the exact `channel_outbox({"item_id": "…"})` invocation, explain the active-only
    default and the separate file/caption/handset evidence limits. Keep that item
    ID to find a settled command-chat send: command-chat items are not part of the
    audit for other people, and no settled-history search is offered. The note is
    tool-result guidance only; it records no new provider evidence and changes
    neither authorization nor retry behavior.
    `tests/visual/test_visual_wechat_recorded_status.py` covers file-only and
    file-plus-caption synthetic settled records in the full app at desktop and
    phone widths. It restores the conversation, opens its tool results, reloads,
    extracts the exact item lookup from the restored guidance, and re-reads it
    through the existing same-owner `channel_outbox` tool; another owner cannot
    read it. The fixture persists synthetic tool results through the existing
    settled-Turn route, not a model-driven chat request. It starts no channel
    session and holds no account token: this verifies history presentation and
    read-only lookup, not iLink acceptance, handset receipt, or deployment.
  - Incoming FILE items with a supported encrypted CDN reference and declared
    length retain the original through C9; incomplete references are visibly
    unavailable. AES-ECB padding and length checks detect malformed transfers
    but do not authenticate the original bytes. A quote of a file shows only
    metadata, not an extra download.
    Quoted text remains separate from the sender's instruction (C13).
  - Native outbound quotes are unsupported on personal 微信. The tool returns
    `reply_to_unsupported`; omit `reply_to` for a separate plain reply. Automatic
    receipts and final answers to bot command chats remain unquoted. Tencent's
    [pinned iLink protocol](https://github.com/Tencent/openclaw-weixin/blob/c6643f6f0ac03574eb126f2d69569ea4267cc3c6/docs/protocol.md)
    documents FILE and inbound references; its
    [collaborator's clarification](https://github.com/Tencent/openclaw-weixin/issues/24#issuecomment-4618839018)
    explicitly rules out native bot quote sends. Do not serialize a speculative
    outbound `ref_msg` or substitute a filename for a successful file send.
- **Secrets.**
  - A pasted secret is stored only in the owner's reserved vault entry
    `channel.u<owner>.<channel>.secret` (微信's bot token in
    `channel.u<owner>.wechat.token.<generation>`; old logins may still use the
    legacy unsuffixed name), never in channel records, logs or
    views. Settings shows the entry name and the vault's redacted hint.
  - A QQ secret is verified with one token request before it is stored.
    企业微信 offers no side-effect-free check (a probe connection would replace
    the running one), so a refused Secret shows as `needs_login`.
  - A 微信 login answer that names a host outside
    `x-wechat.trustedHostSuffix` is refused, so the token never leaves
    微信's servers.
- **Lifecycle.** `bots/supervisor.py` converges sessions on the scheduler's
  existing tick and on account changes, as the 大象 and GitHub supervisors
  do. It starts and stops sessions, and restarts one after it exits or when
  its app ID, credential, API base or pending QR key changes. It has no timer, backoff or
  retry count of its own. Why a session ended is stored best effort: a storage
  outage that ended it cannot also kill its thread with the report. The comparison is with the running session's own
  `binding`: the session that completes a 微信 QR login adopts the issued
  token's binding and keeps polling, so finishing a login does not replace
  it. *Problem:* the supervisor compared with a copy taken at start, so the
  tick after a login replaced the session that was already polling; for one
  long-poll hold two pollers shared the token and the replaced one dropped
  its answer (2026-11-08: confirmed 14:59:42, replaced 15:00:13, connected
  15:00:32). *Alternatives rejected:* ending the login session after
  confirmation (polling would wait for the next tick); keeping the copy and
  refreshing it from the session (two records of one fact). `needs_login` and `kicked` stop restarts until
  the owner changes the account. Shutdown follows C6's rule: stopping fences
  ticks, configuration changes and new sessions until the next hosting
  lifecycle starts (`start_bot_sessions`), and a session leaves the registry
  only once it joined, so neither a replacement nor a new lifecycle polls
  beside it. Shutdown asks every session to stop when its drain begins
  (`begin_stopping_bot_sessions`, which never waits for the supervisor lock a
  tick holds; a session that tick registers afterwards is asked as it
  registers), and the background-service stop joins them. The Router Watchdog
  reads each bot channel's bridge state.
- **Stopping ends every wait of the run thread.** `BotSession.request_stop`
  signals the session and closes the transport on a thread of its own, waiting
  for nothing (a WebSocket `close()` waits for the platform's closing
  handshake); `BotSession.stop` requests, then joins every thread within its
  contract. `close()` interrupts what
  the run thread waits on, through the shared client's owner interruption
  ([NETWORK.md](NETWORK.md)) for HTTP: a 微信 `getupdates` or QR-status
  long poll and a QQ token or `/gateway` lookup end at once, and a close that
  races a QQ gateway or 企业微信 connect is honored before Identify or
  `aibot_subscribe` (a late subscription would take the bot from the session
  that replaced this one). An interrupted poll delivers nothing and moves no
  cursor. A send in flight is never interrupted: its outcome would become
  unknown, and bot platforms keep no history to reconcile it, so it would be
  sent twice. *Problem (2026-11-10 04:35, also 11-09 11:06):* 微信's
  `close()` only set a flag while the run thread sat in iLink's hold of up to
  35 s, so every restart with 微信 enabled outlived the 2 s join and left
  the execution host unclean. The waits close() cannot end (a send in flight,
  a WebSocket handshake) end during shutdown's drain, because the stop is
  asked when the drain begins. *Alternatives rejected:* a join long enough to
  cover the hold is a tuned timeout (constraint 5) that still stalls every
  restart; a shorter poll hold costs a request per hold and still ignores
  close; abandoning the run thread leaves a live poller writing after its
  stop.
- **Settings: one 消息平台 tab** replaces the 飞书, 大象 and GitHub tabs.
  - Every chat platform (飞书, 大象, GitHub, 企业微信, QQ, 微信) is a
    collapsed section with a one-line status: 已连接, 连接中, 需要处理,
    已停用 or 未设置. A section's panel is read only when it is expanded.
  - `setting:feishu|dx|github|wecom|qq|wechat` open the tab with that
    section expanded.

**Alternatives rejected:**
- 企业微信 self-built application callbacks and 公众号 客服消息: both need a
  public, registered HTTPS callback URL.
- 企业微信 group-robot webhooks: send-only.
- QQ webhooks: they need a public HTTPS endpoint.
- Community QQ and WeChat protocols: unofficial, and they get accounts
  restricted or banned.
- DingTalk Stream, Telegram and Slack Socket Mode for now: not requested.
  Each can follow the same `bots/` port.
- Downloading images and files: each platform encrypts media its own way,
  and some URLs expire within minutes. The owner's instructions arrive as
  text or voice transcripts.
- A configured owner ID instead of pairing: IDs are per-bot pseudonyms.
- Own timeouts, retry counts or poll intervals (constraint 5): the
  platforms' documented heartbeat, long-poll hold and reply windows suffice.

**Limits:**
- 企业微信 sends only to a chat that has written to the bot (pairing does
  this). It allows `x-wecom.sendsPerChatPerMinute` and `sendsPerChatPerHour`
  sends per chat; errcode 45009 is its rate refusal. A second client of
  the same bot takes the connection over.
- QQ messages outside the passive window are active messages, which QQ limits
  separately; an unpublished bot answers only its test members.
- 微信 ClawBot is still rolling out, so it may be missing from 插件. It
  replies only in a private chat that has written to it, and Tencent may
  limit its scale or frequency.
- None of the three delivers images or files to Tofu, only a description of
  them.

**Owner setup** (after a restart):
- 企业微信:
  1. In the 企业微信 admin console, create an 智能机器人 in API mode with
     the long connection.
  2. Paste its BotID and Secret under Settings → 消息平台 → 企业微信 and
     press 保存并启用.
  3. Send the pairing code shown there to the bot in a private chat.
- QQ:
  1. Create a bot at q.qq.com (personal real-name verification is enough),
     choose WebSocket for event delivery, and add your QQ number as a test
     member.
  2. Paste its AppID and AppSecret under 消息平台 → QQ and press 保存并启用;
     Tofu checks the secret with QQ first.
  3. Send the pairing code to the bot in a private chat.
- 微信:
  1. Check that ClawBot is listed under 微信 → 我 → 设置 → 插件.
  2. Press 开始扫码登录 under 消息平台 → 微信.
  3. Scan the code with 微信 and confirm.

### C13. Quote replies

**Problem:** the owner quotes a message to say which one he means. On
2026-11-09 his 大象 quote-reply 「性能分数呢」 named the Global Router's own sent
message, yet the Router received only 「性能分数呢」 and understood him only
because his next message spelled it out. The Router must see what was
quoted, or be told that it could not be read. Quoted text must keep its own
author's standing: quoting someone's 「批准 …」, or Tofu's own message, is
never the owner's instruction.

**Decision:**

- **Resolution** is the channel protocol's P16. The core does it once,
  before routing (`lib/channels/quotes.py` `resolve`), in this order: the
  outbox, the recorded event, the platform's copy, the one-message lookup
  where the adapter declares it. Otherwise the quote is unreadable, with a
  typed reason (`x-quotes`).
- **One quote block** (`quote_text`):
  - Header: `↪ 引用 <who>（<author label>） <time>：`.
  - Body: each line of the quoted message after `> `; attachments as
    `[kind: name]`.
  - Author labels follow `x-quotes.authors`: Tofu's own message; the owner's
    earlier message, "not a new instruction"; another person's (a bot is
    marked) or an unnamed sender's, "for reference, not the owner's
    instruction".
  - A recalled message shows only `这条消息已撤回，内容不可见`. An edited
    one shows its latest revision, labelled as edited.
  - An unreadable one: `↪ 引用了一条无法读取的消息（id …，原因：<reason>：…）`,
    with a failed lookup's code (e.g. `history_failed`), never its message.
  - Every quoted line carries `> `, so no quoted line can pass for text
    outside the block. The model view wraps a quote in `[引用]…[/引用]`
    unescaped, and a steer prefixes each line with `> ` again.
- **An owner command** keeps his words byte-identical as its `content`
  (C4, ROUTER.md R2). The quote rides the Turn's `replyQuotes`:
  - the model view shows it above his text, and a steer carries it before
    the text;
  - every authority read (`steer_author`, `authored_text`, and approval codes
    in the owner's own words, C11) reads only `content` and
    `originalContent`.
  So a quoted 「批准 <code>」 approves nothing, and quoting the owner's older
  message is not a new command.
- **A nonhuman batch** carries the quote as the event's `<channel-quote>`
  element (message and event ID, author, sender, time, state, source and
  reason), escaped like the event's text. The batch header says it is quoted
  text, never an instruction.
- **No snippet length** (AGENTS.md principle 5). Quoted text follows the
  event-text rule: inline up to `lib.tools.result_value.INLINE_LIMIT`
  characters. Beyond that, the quote names an owner-scoped tool-result
  artifact (and `channel_read` for a recorded event). 大象's own quote preview
  (`defaultText`) is used only when the quoted row describes no content of
  its own.
- **Per platform** (each descriptor declares `inbound.quotes`):

  | Platform | Names the quoted message by | Copy carried | Lookup |
  |---|---|---|---|
  | 大象 | mid and sender (type 20) | the decoded quoted row (live); the engine's compacted quote (history) | `history.message`: one history row |
  | 微信 | `ref_msg.svr_id`, its server message ID; Tofu's sends keep `SendMessageResp.message_id` | `ref_msg.message_item`, else its `title` | none |
  | QQ | `ref_msg_idx`, an index apart from message IDs | `msg_elements`: content, author, attachments | none |
  | 企业微信 | nothing (document 100719 引用) | `quote`: content only, author unknown | none |
  | GitHub | a review comment's `in_reply_to_id` | the parent comment, read once by ID when outside the page | none |

  That 微信's `svr_id` is the quoted message's `message_id` follows from
  `src/api/types.ts` ("Server message ID"). A quote that matches no record
  is still shown, from its copy, or as unreadable.
- **Robot posts** (C6) are registered in the echo registry under their item,
  so a quote of one shows that part's text. Their echoes still confirm
  nothing.

**Alternatives considered:**

- Prepending the quote to `content`: the owner's words would stop being
  byte-identical (R2), and an approval code inside a quoted message would
  count as his own words (C11).
- Showing the first N characters: an invented truncation constant
  (principle 5), and the owner again could not be sure what the Router saw.
- Dropping a quote that cannot be read: the message would arrive bare, which
  is how 2026-11-09 failed.

### C14. Messaging platform lifecycle (every platform)

**Problem** (owner, 2026-11-15, "这个信息平台无论什么app都要鲁棒"): after
moving from another server, the 大象 robot's save button in Settings stayed at
处理中… for good and the command chat read 未选择. The access log shows the
save's `PUT /api/v4/channels/dx` answered 200 at 16:09:02 and no re-read
followed. Settings had been saved and reopened twice (16:08:26, 16:08:53);
closing it retired the 大象 panel's opening, and on reopening the 大象 section
was still expanded, so no `toggle` fired and the panel kept showing the
retired opening. Its save then ran, found its view retired, skipped the
re-read, and never restored the button. Each platform also had its own idea of
what "connected", "taken over" and "credential missing" look like, and 飞书's
"connected" was the messaging client's existence (built at the first send), not
the connection.

**Decision:** one contract, for 大象, 企业微信, QQ, 微信 (C6, C12) and 飞书:

- **One account, one server.** A platform account or bot is served by one
  server at a time. Where the platform says another connection took over
  (大象 `KickOff`, 企业微信's newer connection), the session ends in `kicked`
  with the platform's words in its detail, and no tick restarts it, so two
  servers never take the account from each other in a loop. **One owner
  action takes it back**: 重新连接 (`PUT …/channels/<channel>
  {"enabled": true}`; a `kicked`, `needs_login` or `error` status is not
  session-owned, so it restarts), which in turn stops the other server's
  session. Settings shows that action and a hint naming the cause for every
  channel. QQ, 微信 and 飞书 do not report a competing connection: QQ and
  微信 report a refusal (`needs_login`), and 飞书 delivers each event to one of
  the app's long connections, so a second server with the same app silently
  takes a share of the messages; for those the rule is documentation, not
  detection.
- **Server-local state is handed over, not copied.** A server's command chat
  and bindings live in its own account record. A server that takes over finds
  what it needs from the platform through its own session (大象: the Tofu
  Router group candidate, C6 "Handover from another server"; bot platforms:
  the pairing, whose command chat is the owner's private chat with the bot).
- **The status says the real reason.** Every account state carries the
  platform's or the store's own detail: a refused credential (`needs_login`,
  the platform's words), a missing vault entry (`needs_login`, naming the
  entry: `The credential vault entry <name> is missing`), a missing runtime
  (`runtime_unavailable`). 飞书 reports its long connection through
  `lib.feishu.startup.connection_status` (`server-config` `feishu.status`,
  shown in its Settings card): `connected` while the SDK's blocking `start()`
  runs without a failure, `disconnected` with the SDK's refusal (or the
  unreachable route) while it waits for the shared route circuit to admit the
  next attempt, `runtime_unavailable` without the Lark SDK, `disabled`
  unconfigured.
- **Credential custody survives partial failure.** Secrets are written to
  the vault after the platform accepted them and before the account names
  them; an entry an update retires is deleted only after the account
  committed (大象 robot and bot accounts alike). A save that fails partway is a
  typed error, leaves the account naming an entry that exists, and can be
  repeated.
- **Every owner command settles.** `runCommand`
  (`frontend/src/features/settings/channel-settings-view.ts`) ends each
  command with the re-read or with its outcome beside the control: a failed
  command shows its error, a failed re-read restores the control, and a
  command whose view a newer opening retired restores it with
  `settings.channels.staleView`. Each Settings opening re-activates every
  platform section left expanded (`populateMessagingTab`), so no section
  shows a retired opening.

**Alternatives rejected:** a per-platform status vocabulary (the account
states already name every outcome); auto-retrying `kicked` (two servers would
take the account from each other forever); a server-to-server registry or
lease (servers do not talk to each other, and a lease needs a timeout knob);
a client-side timeout on Settings commands (a knob that would still not say
what happened; the request settles, and only the presentation had lost it).

**Limits:** 飞书 reconnects through the network authority's route health
and recovery policy (NETWORK.md; [Feishu reconnection](modules/integrations_api.md#feishu-reconnection)):
no backoff of its own, but its SDK connects over the process proxy
environment, not Tofu's proxy pool, so only that one route is admitted and
recorded. A session that ends without delivering an event keeps the shared
backoff escalating, so a quiet bot whose connection drops repeatedly waits the
schedule's longest circuit. A fix is live after a server restart.

**Verification:** `tests/channels/test_messaging_lifecycle.py` (per channel:
takeover and one-action reconnect, refused and missing credentials, partial
saves; the 大象 handover; 飞书's reported reasons),
`tests/feishu/test_feishu_reconnect.py` (飞书's waits are the shared route
circuit's) and the Settings specs
`frontend/src/features/settings/{messaging-platforms,dx-channel,bot-channel}.test.ts`.

## Durable state

`ChannelStore(owner)` keeps everything in the `channels.v1` record namespace
under `u<owner>/` keys (lowercase ASCII only, because the SQLite prefix match
is case-insensitive; GitHub thread keys are lowercased): `account/{dx,github,wecom,qq,wechat}`,
`thread/<key>`,
`event/<thread>/<zero-padded id>`, `subscription/<conversation digest>/<thread>`,
`outbox/<item>`, `outbox-active/<thread>/<queue position>-<item>`,
`echo/<channel>/{mid,uuid}/<digest>` (the item a Tofu send became, a robot
post's included, or `-` for a Tofu send without one), `audit/<channel>/<created ms>-<item>` (C11)
and `batch/<command digest>` (the event IDs of one delivery to the Global
Router, C11). An event record keeps the sender's `senderKind` (C2), its
edits as `revisions` and, once recalled, `recalled` with its text removed
(channel protocol P2). A quote reply's record keeps the quoted message's
reference (`replyToExternalId`, `replyToSenderId`, `replyToMessageId`),
never the platform's copy of it (C13). An event record is `pending` until routing finishes, then
`routed`; its `deliveries` list each accepted target (`conversationId`,
`kind`, `disposition` and, for a dispatched Turn command, `dispatch`) and only
grow, one entry per target as it accepts (C1). Pending records written before
per-target recording carry `deliveries: []` and replay every target. It is
durable user state: no TTL, no pruning.
An event record keeps its resolved `media` (outcome, attachment ref,
transcript; never a provider URL or file token); the originals are ordinary
owner-scoped knowledge attachments (scope `attachment`, content-addressed: one
record per distinct original) with the media-attachment lifecycle.
Growth is one record per stored event (delivered, command or context), one per
outbox item plus two small index rows (three for an item to another thread:
its audit row), one per batch delivered to the Global Router, and one per
thread; ignored threads add only their thread record. Thread records may hold
the owner's `note`, the 大象 account its `assistantNote`, and every outbox
item its `authority` and latest `decision` (C11). The 大象 account may add its notification robot
(`robot`: client ID, credential vault entry name and the last refusal; the
secret only in the vault entry; with the built-in robot only the last
refusal) and the robot's binding to a chosen command chat (`robotBinding`:
thread, uid, MIS, proof `owner_message` and its event ID, C6), and robot outbox items their part texts (`robotTexts`) and the
message IDs of the parts posted so far (`robotPosted`). The GitHub account adds its credential name, poll
schedule (`intervalS`, `lastAtMs`, `nextAtMs`, `lastModified`) and API base;
each GitHub thread record holds `github` state (opening-post time, pull
request flag, per-list cursors and ETags). The token itself lives only in the
credential vault. The 大象 account adds its chosen login MIS (`mis`); its SSO
session files (C6) are SDK-owned credentials outside the record namespace.
An outbox item with attachments (C10) keeps their records in the item
(`attachments`: name, type, size, sha256, kind, `renditionOf`, the source path
and its roots or the artifact ID, part state and provider IDs), its part order
(`partOrder`), the text part (`textPart`) and its notes; the files stay where
they are (workspace or artifact store), and preview copies exist only during a
send attempt.

A bot platform account (C12) holds `appId`, its credential's vault entry name,
the bound `identity`, the command chat, a pending `pairing` code, and for 微信
the pending `login` (QR code), the long-poll `cursor` and an `apiBaseUrl`
only when 微信 named another trusted host. A bot thread record keeps its
latest reply context (`botReply`: QQ message ID, time and replies used;
微信 context token). Each bot platform adds one alias record per received
message, `msgid/<channel>/<digest>`. It holds the message's external ID, for
ignored chats too, in about 93 bytes of key and value. A redelivery maps to
the same event, and a quote that names the message by its platform ID finds
it (C13).

## Interfaces

- `lib/channels/model.py` — `ChannelEvent`, `ThreadKey`, `OutboxItem`, contract vocabularies.
- `lib/channels/store.py` — `ChannelStore(owner)`.
- `lib/channels/policy.py` — `effective_mode`, `effective_reply_policy`, `classify`.
- `lib/channels/ingress.py` — `ingest(owner, event, media=, lookup=)`, `ingest_batch(owner, events, media=, lookup=)`.
- `lib/channels/quotes.py` — `resolve`, `quote_text`, `MessageLookup` (C13).
- `lib/channels/media.py` — `safe_media_name`, `MediaPort`, `resolve_event_media` (C9);
  originals are stored by `lib.media_attachments.ingest_received_file`.
- `lib/channels/delivery.py` — `deliver`, `deliver_command`, `observe_router`, `global_router_binding`.
- `lib/channels/outbox.py` — `enqueue`, `approve`, `discard`, `execute`, `confirm_echo`, `kick`.
- `lib/channels/attachments.py` — attachment admission (`from_tool`, `for_final_answer` for
  the files a router answer links, `admit`), verified bytes at send time (`read_original`);
  `renditions.py` — preview copies (C10).
- `lib/channels/service.py` — owner commands/views shared by HTTP and tools.
- `lib/channels/tools.py` — `channel_threads`, `channel_read`, `channel_send`,
  `channel_configure_thread`, `channel_subscribe`, `channel_unsubscribe`,
  `channel_outbox`, `channel_outbox_approve` (handler `lib/tasks_pkg/handlers/channels.py`).
- `lib/channels/autoreply.py` — router answer → command-chat thread.
- `lib/channels/assistant.py` — Turn authority, the review ceiling and scope, approval codes,
  draft notices and owner notes (C11); `lib.router.policy.router_tool_ceiling` asks it.
- `bundles/yourprovider/tofu_yourprovider/dx/` — `bridge.mjs`, `adapter.py` (normalization, catch-up, sender port,
  media port `download_media`), `login.py`, `supervisor.py`, `probe.py`,
  `robot.py` (notification robot: token, posts, configuration).
- `lib/channels/github/` — `client.py` (REST transport, conditional requests,
  paging, rate limits), `normalize.py`, `adapter.py` (poll worker, sender
  port), `account.py` (Settings commands, token custody), `supervisor.py`.
- `lib/channels/bots/` — the bot platforms' core (C12): `platform.py` (transport port),
  `ids.py` (thread keys, event IDs), `session.py` (`BotSession`: sink, pairing, outbox sender),
  `account.py` (Settings views and commands, credential custody), `supervisor.py`, `http.py`;
  transports `lib/channels/wecom/transport.py`, `lib/channels/qq/transport.py` (and `probe`),
  `lib/channels/wechat/{transport,login,protocol}.py`.
- HTTP (`routes/api_v1/channels.py`, served under `/api/v4`; each body
  admits exactly the properties of its contract DTO):
  `GET|PUT /api/v4/channels/dx` (config and status; `PUT` enables/disables,
  sets the login `mis`, the command chat or the notification `robot` and
  returns without waiting for a login),
  `POST /api/v4/channels/dx/command-chat` (create `Tofu Router`),
  `GET /api/v4/channels/dx/threads`, `PUT /api/v4/channels/dx/threads/{key}`
  (mode / reply policy, `null` restores the default; the owner `note`),
  `GET|PUT /api/v4/channels/github` (status, token or vault entry, API base),
  `GET /api/v4/channels/github/threads`, `PUT /api/v4/channels/github/threads/{key}`
  (URL-encoded key),
  `GET|PUT /api/v4/channels/{wecom,qq,wechat}` (status, owner, pairing code, 微信 login QR;
  `PUT` enables/disables, sets the app ID with a secret or vault entry, issues or cancels the
  pairing code, starts or cancels the 微信 login), `GET /api/v4/channels/{wecom,qq,wechat}/threads`,
  `PUT /api/v4/channels/{wecom,qq,wechat}/threads/{key}`,
  `GET /api/v4/channels/outbox[?channel=<channel>]` (all channels, or one),
  `GET /api/v4/channels/outbox/{id}/attachments/{index}` (an attached original image),
  `POST /api/v4/channels/outbox/{id}/approve|discard`,
  `GET /api/v4/channels/audit?channel=dx|github[&before=]` (C11). `PUT /api/v4/channels/dx`
  also sets the account's `assistantNote`.
- Browser: Settings → 大象 (`frontend/src/features/settings/dx-channel.ts`,
  markup `frontend/src/settings-panels/dx.html`), GitHub (`github-channel.ts`,
  `settings-panels/github.html`) and the bot platforms (`bot-channel.ts`,
  `settings-panels/{wecom,qq,wechat}.html`: credentials, the pairing code or the 微信
  login QR code, and the setup steps), facade `Api.channels`. With 飞书 they are the
  collapsed sections of one 消息平台 tab (`messaging-platforms.ts`, markup
  `settings-panels/messaging.html`, styles `14-messaging-platforms.css`): each shows a
  one-line status from its account read, populates its panel on its first expansion,
  and opens expanded from a `setting:<platform>` link.
  The channel panels share one presentation (`channel-settings-view.ts`, styles
  `frontend/src/styles/settings/13-dx-channel.css`): a status card, the
  channel's own cards, a thread list, and the outbox of that channel only
  (`?channel=`). A tab re-reads its account while it is `starting`,
  `authorizing` or `disconnected`. The 大象 account card (`dx-account.ts`)
  says what each state needs (approve in 大象, reconnect, enter a MIS, or, for
  `runtime_unavailable`, repair the server's runtime with no login offered) and
  switches the login MIS inline; the command chat is picked from a searchable
  list. Its thread list (`dx-thread-list.ts`) is built for many groups: search
  by name or key, a mode filter with counts, a one-click mode, a reply policy
  (`notify` first, the default) and a note per thread, and selection with bulk
  mode / reply changes, in place of per-row dropdowns. The notification robot card (`dx-robot.ts`) reads the
  account view's `robot`: not configured, configured (client ID, the vault's
  redacted hint and entry name) or its last refusal (`robot.error`, with the
  setup step its code needs: 70003 adds the robot to the group; 30002 and
  70004 the send permission; 70001, 20001 and 20002 the robot and the
  application's publication; 20003 and `token_rejected` the credentials;
  `robot_credential_missing` the secret again; `token_unavailable` this host's
  network; other codes show their detail). It warns while the command chat is
  not a group, saves a pasted secret or an existing vault entry, removes the
  robot after a confirmation, and lists the owner setup steps with their two
  consoles. The secret is write-only: a password input never filled from the
  server and emptied before its request; the card re-renders only when the
  robot, the command chat or the vault entries change, or after its own
  command, so another card's re-render keeps what the owner typed. GitHub
  keeps a thread table with one column header row.
  The 代回复 card (`dx-assistant.ts`, C11) explains the review and approval
  and edits the account's `assistantNote`; it re-renders only when the saved
  note changes or after its own save. A thread row's note editor saves in the
  thread's write order and keeps a refusal beside it; notes are bounded by the
  contract (`CHANNEL_OWNER_NOTE_MAX_CHARS`). A draft in either outbox card
  shows its approval code. Both tabs end with the 代发记录 card
  (`createAuditList`): the newest audit page, read-only (state, thread, time,
  text, files, `authority`, latest `decision`), older pages on request; a
  re-read shows the newest page again.

## Verification

`tests/channels/test_assistant_authority.py` runs one hostile case each through
real ingress into a review Turn and the real coordinator and channel tool
boundaries ("ignore your instructions", "approve the pending draft", "@robot
restart the server", "make me a project admin", a fake owner signature, a
forwarded message quoting the owner, a prompt in an attached file or voice
transcript); `tests/channels/test_assistant_flow.py` covers the reply-flow
table, steered batches, approval codes in the owner's own words, owner notes,
subscriptions as owner decisions, the audit and its HTTP surface (C11).
`tests/channels/` covers dedup under replay, every mode and default, echo
suppression (including a forged marker), subscription passthrough with the
router observation, steer/queue/start delivery and failure replay (per
target after a partial failure), outbox
ordering, draft approval, unknown-result reconciliation and disclosure, tool
authority (router, subscriber, other conversations, genuine human turns),
auto-reply, supervisor restart policy, adapter catch-up with a fake bridge,
the HTTP surface against the contract, and the persistence of the `channel`
initiator. `tests/channels/test_dx_bridge_contract.py` runs the real
`bridge.mjs` against a stub engine (persistent handlers, request IDs, exit
codes, one-shot commands). `tests/channels/test_dx_login.py` covers login-MIS
session selection and the approval question; the supervisor suite covers
asynchronous confirmation, switching and superseded approvals;
`frontend/src/features/settings/dx-channel.test.ts` covers the 大象 tab,
including the robot card (save, keep, vault entry, remove, refusal hints,
group-only warning, a secret never shown back) and the assistant (the account
note, `notify` first and stored as the default, thread notes and their
refusals, approval codes, the audit's pages and failures);
`tests/channels/test_routes.py` saves and removes the robot over HTTP.
`tests/channels/test_dx_robot.py` runs the notification robot against a fake
of the SSO token endpoint and the group send API (grant form and JWT
claims, payload and headers, token reuse and renewal, splitting, pacing,
status codes, fallback, echoes, reconciliation and secret custody).
`tests/channels/test_dx_attachments.py` runs attachments through the real
adapter against a fake bridge (uploads, echoes, history) and the fake open
platform: admission and refusals (traversal, symlinks, outside the roots,
types, size, other conversations' artifacts), native image and file
messages, order with and without the robot, confirmation by echo or
history, an accepted file the history proves missing, unknown outcomes,
resume after a crash, changed files, draft approval with thumbnails, previews
with a stub slide renderer (fresh, changed after export, or none), slide
decks of this and other conversations, and final-answer
scope. The
live probe is read-only (`whoami`, `sessions`,
`history`, a persistent listen; counts only):
`TOFU_DATA_DIR=<data> python -m tofu_yourprovider.dx.probe --owner 1 --listen-seconds 45`.
Media (C9): `tests/channels/test_dx_media.py` covers every type's text, media
facts and name sanitizing from SDK packets and compacted history rows, ingress
with a fake media port (image, file, voice with and without speech
recognition, failed download, dropped and ignored messages, replay without a
second download) and the adapter's port; the bridge contract downloads from a
fake file host (file headers, credential-free redirect, refusals);
`tests/channels/test_channel_media_store.py` stores originals in a real
knowledge Sidecar; `tests/router/test_router_channel_integration.py` persists
the attachments in the real turn authority.
`tests/channels/test_github_{normalize,adapter,account}.py` run the real
GitHub client against a fake REST server (no network): normalization and
event IDs, `Last-Modified`/`304`, `X-Poll-Interval` as the cadence, catch-up
after a failed route, watched-thread ETags, rate limits and `needs_login`,
draft approval, echo suppression, reconciliation before resend, owner
isolation, token verification and custody, the supervisor and the HTTP
surface; `frontend/src/features/settings/github-channel.test.ts` covers the
Settings tab.

Quote replies (C13):

- `tests/channels/test_quotes.py`: the core. Resolution order, every author
  and unreadable reason, recalls and edits, attachments, a failing lookup,
  quoted instructions that gain no authority (also inside a steer), the
  `<channel-quote>` element, and the real 2026-11-09 shape.
- `tests/channels/test_dx_quotes.py`: the 大象 copy and the one-row lookup.
- `tests/channels/test_bot_quotes.py`: the bot platforms' quote facts.
- The conformance cases `quote_*`.
- `tests/router/test_router_channel_integration.py`: persists a quote reply
  in the real Turn authority.

Bot platforms (C12): `tests/channels/test_bot_{wecom,qq,wechat}.py` run each
real transport against the hermetic fakes in `tests/channels/bot_fakes.py`
(WebSocket servers for 企业微信 and the QQ gateway, HTTP fakes of QQ's token
endpoint and OpenAPI and of iLink; no network): authentication and token
renewal, heartbeats and resume, normalization, sends and their refusals, the
passive window, a kicked connection, an expired session, the QR login and its
host check, and an end-to-end owner command. `tests/channels/test_bot_channels.py`
covers keys and event IDs, secret custody, pairing (a wrong code, a group),
the authority boundary, redelivery, group modes, the outbox without history,
refused attachments and quote replies, receipts and final answers in a bot
command chat without any 大象 account, the supervisor, bridge states and the
HTTP surface. `frontend/src/features/settings/{messaging-platforms,bot-channel}.test.ts`
cover the 消息平台 sections and the bot panels (a pasted secret is never shown
back), `setting-jump.test.ts` the platform deep links, and
`tests/visual/test_visual_messaging_settings_tab.py` the tab in real Chromium.
