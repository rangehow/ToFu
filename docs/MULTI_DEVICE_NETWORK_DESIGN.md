# Multi-device network design: one Home, equal nodes

Status: **proposal, awaiting owner decisions** (section 11), written
2026-11-17. Phase 0 (section 10) is done; the later phases change no code yet. The running node model
is described in [remote execution](modules/remote_execution.md#nodes-and-pairing);
when a phase below lands, its behavior moves into that authority and this
proposal shrinks accordingly.

Wireframes: `output/multi_device_design/devices_mockups.html` (rendered as
`frame1.png` … `frame4.png` in the same directory, outside the repository).

## 中文摘要（给 owner 的一页版）

- **结论：每个对话只有一个“家”（Home 服务器），其它所有机器都是平等的“节点”。**
  家是唯一保存对话、记忆、凭证、审批的地方，也是唯一做决策（跑 agent 循环、选设备、判权限）的地方。
  节点（Mac/Windows/Linux 桌面、CodeLab 容器、GPU 服务器、浏览器扩展）只负责执行，不保存对话状态，彼此平等、互不连接。
  手机和浏览器标签页是“遥控器”（客户端），只看状态、发消息、批准操作。
- **为什么不是“主从复制”或“多主”或“全对等”**：Tofu 的状态是一条条有因果顺序的对话和工具副作用，
  复制或多写都需要分布式一致性，而我们以前把数据库放在共享 DolphinFS 上让两台机器同时跑，就出过
  一台机器删掉另一台 PostgreSQL pid 文件导致数据库停机的事故。现在的存储层本来就是“单一权威 + 独占租约”。
  “家 + 节点”与 Kubernetes（控制面/节点）、GitHub 自托管 runner、VS Code Tunnels、Claude Code Remote Control
  的结构相同：节点只向外拨号，穿透 NAT/代理/SSO 都不需要入站端口。
- **允许多个家，但互不同步**：笔记本上的 Tofu 和 CodeLab 上的 Tofu 可以各管各的对话；同一台电脑可以同时是两个家的节点。
  这已经是现有代码的模型（一个节点可加入多个服务器），也和 Android 的“kernel”模型一致（一次只在前台显示一个）。
- **家挂了怎么办**：节点不受伤害，正在跑的命令跑完后结果等家回来再交；对话暂停，不会有“第二个家”抢着接管。
  换家（比如搬到 203 GiB 的新 CodeLab）走现有迁移流程，节点按配对的候选地址重连。
- **界面**：做一个顶层“设备”页面，侧边栏底部有一个状态胶囊（“5/7 在线”）点进去；
  设置里的“设备”页签变成指向它的链接。页面里：家的说明卡、按类型分组的设备列表、统一“添加设备”向导
  （电脑/服务器容器/浏览器/手机/另一个 Tofu），每台设备的健康状态与权限（家这边的上限 ∩ 设备自己的开关）。
- **迁移不推倒重来**：先修两个现有缺陷（API/执行双进程下执行进程看不到桌面节点；共享家目录可能让两台主机用同一个节点 ID；第 0 阶段已完成），
  再加一个只读的统一设备列表，再上页面和向导，最后再考虑按标签路由（“在任意 gpu 节点上跑”）和中继。
- **需要你拍板的 8 件事见第 11 节**，每条都有推荐默认值。

## 1. Problem

The owner asked (2026-11-17) for "more seamless multi-machine control …
cross-platform, a higher-level protocol structure … no separate redundant
controlled end … possibly a multi-server design", and then: *"Because this
involves managing many servers, consider where to place this feature so that
it's easier to use and configure. Right now this would create a rather unusual
network structure: which device is primary? Which ones are secondary? Or are
they all peers?"*

Tofu must connect, seamlessly and consistently, to:

| Device kind | Examples | Reachability today |
|---|---|---|
| Tofu server hosts | the CodeLab container running Tofu (`…/proxy/15000/` behind the CodeLab SSO edge), a laptop running the desktop app's own server | server is reachable only through its edge; it binds `0.0.0.0` in open-auth mode behind that edge ([infra runtime](modules/infra_runtime.md)) |
| Desktop computers | Mac, Windows, Linux desktop with the Tofu desktop app | behind home/office NAT and corporate proxies; no inbound route |
| Headless Linux hosts | CodeLab containers, GPU servers, VMs, often on DolphinFS | internal network; may reach a Tofu host directly by internal IP, may not pass the SSO edge |
| Browsers | Chromium extension in the owner's signed-in browser | MV3 service worker that Chrome may terminate when idle |
| Phones | Android app (WebView shell with server "kernels"), mobile web | mobile networks; client only |

The topology question is really three questions that the current code answers
implicitly and the UI never states: who **owns durable state**, who **makes
control decisions**, and who **executes**. Mixing them is what makes
"primary/secondary/peer" feel unanswerable.

## 2. What exists today (grounding)

Read at `HEAD 66147d70ab` of `eval/opencode-glm53-tb21`.

**Node model (merged 2026-11-15).** One desktop artifact; every install is a
node ([nodes and pairing](modules/remote_execution.md#nodes-and-pairing)).
A node keeps one outbound bridge loop per joined server plus a local link to
its own packaged server (`lib/desktop_agent/links.py::NodeLinks`). Joining is
pairing: Local Control mints a one-shot 5-minute link
`<base>/#tofu-pair=<secret>[&via=<route>]` and a 6-digit code
(`POST /api/v1/desktop/pair-code`, `lib/desktop/pairing.py`, `_CODE_TTL_S =
300`); the node redeems it through `POST /api/desktop/pair` for an
owner-scoped `agents:bridge` credential. The server sees nodes only through
their polls (`setup_state` is `connected` or `pair`; no peer-address
guessing). `GET /api/v1/desktop/nodes` lists `NodeListing`
(`contracts/desktop_node_v1.schema.json`). Removal or revocation drops the node
at once (`disconnect_credential`). Headless: `python -m lib.desktop_agent
--join '<link>'`. Server federation was explicitly **deferred** in that
design: "a node joining several servers covers the multi-server need without a
trust relationship between servers".

**Addressing and permissions.** Every desktop tool takes optional `device`
(agent ID or name), resolved only over the caller's own devices
(`lib.desktop.bridge.resolve_device_target`); an unaddressed call with several
online devices is refused with a listing, never sent to the "latest" device.
Tiers (read, write, exec, gui, egress, codex_login) come from one map
(`x-command-tiers`) and are enforced twice: the server refuses to enqueue a
command whose tier the node did not declare in its latest poll
(`bridge._tier_enqueue_error`), and the device refuses it again
(`lib/desktop_agent/_dispatch.py`). The device is the only place tiers are
*set* (tray toggle). Liveness is a 15 s window (`_CONNECTED_WINDOW_S`).
`await_device` registers a durable, owner-scoped wait that wakes the
conversation exactly once per connection epoch
(`lib/desktop/device_waits.py`).

**Browser extension.** A separate bridge (`routes/browser.py`,
`lib/browser/queue/`) with the same owner + device addressing rule and the
same `agents:bridge` credential family, but its own pairing/mint, its own
server-URL authority (`browser_extension/worker/server-url.js`) and its own
settings surface ([browser automation](modules/browser_automation.md)). Owner
browser use is restricted to the owner's own attended turn
(`owner_browser_refusal`).

**Two-process runtime.** The API host terminates every poll; the resident
execution host runs tools. Process-local state does not cross them. The
browser bridge solved this with a private loopback relay from the execution
host to the API host's owner (`lib/browser/bridge_relay.py`). The browser
document states that the desktop bridge "has the same process-local registry
and is not yet relayed", and at this HEAD `lib/desktop/` contains no relay
while the execution host imports `lib.desktop.bridge` directly
(`lib/tasks_pkg/context_composer/_providers.py`,
`lib/tasks_pkg/handlers/misc/_agents.py`). **Adjacent defect:** in a split
deployment the execution host's registry is empty, so desktop tools are not
even offered there (`is_desktop_agent_connected` gates them) and the
environment device list sees no nodes while Settings shows them online.
*Fixed in phase 0:* the desktop bridge now has the same relay
([remote execution](modules/remote_execution.md#bridge-owner-across-the-apiexecution-split)).

**Storage authority.** One selected authority per server with an exclusive
process lease; no runtime store switching ([storage](STORAGE.md#one-authority)).
Distributed mode scales one *logical* server (api/worker/scheduler roles over
one external PostgreSQL), not several independent ones
([architecture](ARCHITECTURE.md#runtime-topology)). Project memory records the
failure of the earlier attempt to let two machines share one FUSE-mounted
PostgreSQL data directory: one machine deleted the other's `postmaster.pid`
and PostgreSQL shut down (lesson `pg-pidfile-deletion-cross-machine-bug`).

**Network authority.** All Tofu-owned outbound requests use
`lib/http_transport/` with route health per origin and no blind replays
([network](NETWORK.md)); "desktop delegation keeps its independent
owner/host authorization boundary". Node polls honor proxy/NO_PROXY and CA
settings, ignore netrc, never follow redirects, and fail a credential over
only to the attachment's explicitly configured candidates.

**SSO edges.** A desktop process cannot pass the CodeLab login wall, so the
node opens a loopback broker on 15180–15189 and the signed-in Tofu page
long-polls it and forwards the node's poll with the gateway cookie
(`lib/desktop_agent/_browser_relay.py`). This works only while a signed-in
page is open on that machine.

**Multiple servers on the client side.** The Android app already models
several servers as *kernels*: many configured, exactly one in front,
"conversations belong to their kernel"; a phone can even be its own LOCAL
kernel ([Android kernels](ANDROID_KERNELS.md#kernel-kinds)).

**Where the UI lives.** Three places: the Local Control modal (opened from a
composer submenu and the mobile sheet), Settings → Devices
(`frontend/src/features/settings/devices.ts`), and the browser-access
settings. The computer-control entry is being removed from the frontend now
by a sibling assignment; this design defines where it returns.

**Node identity.** `agent_id` is generated once and stored in
`~/.tofu/desktop_agent.json` (`lib/desktop_agent/config.py`). Where several
hosts share one home directory (possible on shared DolphinFS setups; not
verified for the owner's containers) they would present the same node ID and
overwrite each other's registry entry. *Fixed in phase 0:* the config keeps
one node ID per host
([remote execution](modules/remote_execution.md#one-node-id-per-host)).

### Gaps this design must close

1. No single answer, in code or UI, to "which machine is primary".
2. Devices are split across three surfaces and two registries (desktop nodes,
   browser extensions); the server's own host is not shown as a device.
3. Headless onboarding (containers, GPU servers) is a copied CLI line with no
   guided flow and no way to set capabilities from the server side.
4. Many servers: nothing names a server's role; the web UI has no server
   switcher (Android does).
5. The two defects above (unrelayed desktop registry; shared-home identity
   collision). Closed by phase 0.

## 3. Evaluation criteria

From the owner's question and the acceptance brief, every topology is judged
on:

1. **State authority**: who owns durable state and conversation authority.
2. **Consistency under offline/partition**: what a user sees when nodes are
   offline or cut off.
3. **Identity, trust, pairing**: how a device proves who it is and gets in.
4. **NAT/firewall traversal** on the real network: DolphinFS hosts behind
   proxies, CodeLab SSO edges, the browser-extension case.
5. **Failure of the "primary"**.
6. **Control latency**.
7. **Tool routing**: how a command reaches a target device.

## 4. Options

### A. Single primary + secondaries (replicated state)

One server holds the writable store; secondary servers hold replicas and take
over when the primary fails (the PostgreSQL streaming-replica or etcd-quorum
shape; Kubernetes recommends at least three stacked control-plane nodes for
HA, [kubeadm HA topology](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/ha-topology/)).

1. State: primary only; secondaries are read replicas. Clean.
2. Partition: needs fencing and a quorum to avoid split brain; with two
   machines there is no quorum, so automatic failover is unsafe.
3. Identity: servers need a trust relationship with each other in addition to
   device pairing.
4. NAT: secondaries must reach the primary continuously; CodeLab containers
   behind SSO edges cannot reach each other's public URL without a browser.
5. Primary failure: the only option whose answer is "a secondary takes over" —
   but in-flight model calls, tool side effects and execution-host
   process-local state (steers, background commands, swarms) do not replicate,
   so takeover is partial at best.
6. Latency: unchanged for control; write latency rises if replication is
   synchronous.
7. Routing: as today.

Cost: a replication protocol for TofuDB (or forcing PostgreSQL everywhere), a
fencing story, and a quorum the owner does not have. Benefit: survives a dead
host — a rare event for a personal deployment that already restarts in place.

### B. Multi-primary (any server accepts writes, state merges)

Every server writes its own copy and changes merge (CRDTs or last-writer-wins
plus conflict copies, the way Syncthing turns concurrent edits into
`.sync-conflict` files, [Syncthing syncing](https://docs.syncthing.net/users/syncing.html)).

1. State: everyone and no one. A conversation is a causal sequence (turns,
   tool calls with side effects, approvals); two servers continuing the same
   conversation while partitioned produce two histories that cannot be merged
   meaningfully, only shown as conflicts.
2. Partition: available but inconsistent by construction.
3. Identity: every server must trust every other server's writes.
4. NAT: servers must sync with each other; same reachability problem as A.
5. Primary failure: none to fail; but credentials, approvals and owner memory
   can diverge.
6. Latency: local writes fast.
7. Routing: a tool call could be decided on two servers at once; device
   commands could run twice.

Rejected for conversation state. (File sync between machines remains a fine
*tool* — e.g. running Syncthing or rsync on nodes — not a topology.)

### C. Full peers (mesh; every device is equal and talks to every other)

Every device runs the agent, holds state and connects directly to peers
(Tailscale's data plane is a WireGuard mesh, but it still has a central
coordination server for keys and policy,
[how Tailscale works](https://tailscale.com/blog/how-tailscale-works)).

1. State: every peer, so it collapses into B.
2. Partition: as B.
3. Identity: per-device keys are good (Syncthing device ID = certificate
   fingerprint, [Syncthing security](https://docs.syncthing.net/users/security.html));
   but each peer must also authorize each other peer.
4. NAT: requires inbound reachability or hole punching plus relays (STUN +
   DERP in Tailscale, [NAT traversal](https://tailscale.com/blog/how-nat-traversal-works),
   [DERP](https://tailscale.com/kb/1232/derp-servers)). CodeLab SSO edges and
   corporate proxies block UDP hole punching and inbound routes; phones and
   MV3 extensions cannot be reliable peers (Chrome terminates an idle worker
   after 30 s, [service worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)).
5. Primary failure: none, but also no place to run unattended work.
6. Latency: best for node-to-node bulk data.
7. Routing: every peer needs the full device map and policy.

The merged node design already rejected inbound connections and a peer mesh
for the same reasons ([alternatives](modules/remote_execution.md#nodes-and-pairing)).

### D. Hub with equal nodes ("Home + nodes") — recommended

A server is the hub (control plane + durable state) for the conversations it
owns. Every other machine is an equal **node** that dials out to the hub,
declares capabilities, takes commands addressed to it and returns results.
Nodes never talk to each other and never hold conversation state. A machine
may run a hub *and* be a node of other hubs; there may be several hubs, but a
conversation belongs to exactly one.

This is the shape of every comparable system that runs work on many machines
for one owner:

| System | Hub (state + decisions) | Nodes (execution) | Connection direction |
|---|---|---|---|
| Kubernetes | API server + etcd; "all API usage from nodes terminates at the API server" | kubelets self-register and heartbeat via `.status` and a Lease; the node controller marks `Unknown`, taints, then evicts ([nodes](https://kubernetes.io/docs/concepts/architecture/nodes/), [leases](https://kubernetes.io/docs/concepts/architecture/leases/)) | nodes initiate; even API-server→node uses node-initiated Konnectivity tunnels ([control-plane–node communication](https://kubernetes.io/docs/concepts/architecture/control-plane-node-communication/)) |
| GitHub Actions self-hosted runners | GitHub | runners matched by `runs-on` labels and groups to an online idle runner; re-queued if not picked up ([self-hosted runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)) | runner connects out on HTTPS 443 |
| VS Code Remote / Tunnels | the local VS Code UI; the VS Code Server on the remote runs workspace extensions ([remote overview](https://code.visualstudio.com/docs/remote/remote-overview), [remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions)) | the remote | both ends only outbound to a relay service; no listeners ([tunnels](https://code.visualstudio.com/docs/remote/tunnels)) |
| Claude Code Remote Control | Anthropic's servers keep the transcript so devices stay in sync; phones/web drive the session | the local machine keeps execution and files | "outbound HTTPS requests only and never opens inbound ports"; registers and polls for work ([remote control](https://code.claude.com/docs/en/remote-control)) |
| Tailscale / headscale | coordination server holds the device registry, keys and policy; devices keep existing connections and cached policy when it is unreachable ([control and data planes](https://tailscale.com/docs/concepts/control-data-planes), [headscale](https://github.com/juanfont/headscale)) | every device | devices dial the coordinator |
| Codex | app-server owns threads → turns → items; clients attach (`codex --remote wss://…`) and cloud tasks run in per-task containers ([app-server](https://developers.openai.com/codex/app-server), [cloud](https://developers.openai.com/codex/cloud)) | containers / local host | client to server |

1. State: the Home only. One authority, matching the storage contract.
2. Partition: the Home stays consistent; a cut-off node's work simply is not
   delivered; `await_device` resumes the conversation when it returns.
3. Identity: per-node credential from pairing (exists); hub-to-hub trust is not
   needed.
4. NAT: outbound-only from every node — the property that already works
   through NAT, ssh tunnels, corporate proxies and (with the browser relay)
   SSO edges.
5. Hub failure: the conversations owned by that Home pause; nodes are
   unharmed; nothing else needs fencing. Recovery = restart or move the Home
   (section 6).
6. Latency: one hop Home→node over a held long poll (commands wake the held
   poll; no polling interval), plus one more hop through the browser relay at
   an SSO edge. Control latency is dominated by model calls, not this hop.
   Bulk node-to-node data is the weak point and is handled by telling a node
   to copy directly (rsync/shared DolphinFS), not by routing bytes through the
   Home.
7. Routing: the Home resolves `device` (or, later, a tag) to one node and
   enqueues there; refusal on ambiguity stays.

### Comparison

| Criterion | A primary + replicas | B multi-primary | C full peers | **D Home + equal nodes** |
|---|---|---|---|---|
| Durable state / authority | primary | everyone (merge) | everyone | **one Home per conversation** |
| Offline / partition | needs quorum + fencing | divergent histories | divergent | **Home consistent; node work waits** |
| Identity / pairing | node pairing + server trust | + write trust | per-peer keys, n² authorization | **node pairing only (exists)** |
| NAT / SSO / proxies | servers must reach each other | same | inbound or hole punch + relays | **outbound-only (works today)** |
| "Primary" failure | failover (partial: process state lost) | n/a, divergence | n/a | **its conversations pause; restart/move** |
| Control latency | same | lowest local | lowest | **one held-poll hop (+ relay)** |
| Tool routing | as D | double execution risk | full map on every peer | **Home resolves `device` → one node** |
| New machinery | replication, fencing, quorum | CRDT/merge + conflict UI | mesh, NAT traversal, relays | **projection + UI + two fixes** |

## 5. Decision

**Adopt D: each conversation has one Home; every other machine is an equal
node.** Several Homes may exist, never synchronized; a machine can be a Home
and a node of other Homes at once (today's attachments already allow this).

| Plane | Primary | Secondary | Peers |
|---|---|---|---|
| **(a) Durable state** — conversations, turns, memory, owner context, credentials, approvals, device waits, Home capability ceilings | the conversation's **Home** (its selected storage authority) | **none live.** Protection against host loss is backup/export and `migrate_host`, not a replica. | — |
| **(b) Control decisions** — agent loop, model calls, device resolution, enqueue-time permission check, approvals, scheduling | the **Home** that owns the conversation (API host terminates polls; execution host runs the loop) | — | — |
| **(c) Execution** | — | — | **all nodes are equal**: the Home's own host (the "self node"), desktop nodes, headless nodes, browser extensions. Each keeps a local veto (device-side tier and share-root checks). |
| Clients (phone, browser tab, desktop window) | — | — | UI only: they read the Home's projection and send input/approvals; no authority beyond the signed-in owner |

What happens when any node disappears:

| Who disappears | Effect | Recovery |
|---|---|---|
| **The Home** (crash, restart, CodeLab migrated) | Its conversations stop progressing; nodes keep polling with retry/backoff (jittered, ≤60 s) against their attachment's candidate routes. Commands a node already claimed finish locally; their results wait in the node's outbox (memory, spilled to private temp files) until the Home is reachable again. A Home that restarted has no record of those commands (the bridge queue is ephemeral by design): their callers already failed honestly at restart and the owning job reconciles through its own state. Other Homes are unaffected. | Restart in place (existing restart recovery), or move it with `migrate_host`; nodes reconnect without re-pairing as long as one of their candidate URLs reaches the new host (else re-pair: one link). |
| **A node, before a command is sent** | Home refuses at enqueue with the online-device listing; a conversation that can wait gets `await_device`. | Node reconnects → its connection epoch wakes the waiting conversation exactly once. |
| **A node, mid-command** | The Home's wait has no private deadline; Stop withdraws the caller and reports an unconfirmed remote outcome; if the node agent itself dies, the command's supervisor retires its process tree. | Late result settles if the node returns within the same Home process; otherwise the tool result is a typed transport failure, never a silent replay. |
| **The node partitioned, Home alive** | Same as offline after 15 s; the UI shows "last seen". | Same. |
| **The browser extension** (worker terminated, browser closed) | Browser tools fail with typed `browser_extension_disconnected`; a pinned device never falls back to another one. | Extension poll resumes; tools wait ≤15 s for a route before enqueue (existing). |
| **The SSO page used as relay** closed | Nodes behind that edge go offline (their only route was the page). | Shown explicitly as route "browser relay — needs an open Tofu tab"; phase 4 removes the dependency where an internal route exists. |
| **A phone / client** | Nothing changes on the Home. | Reconnects to the projection. |
| **Two hosts presenting one node ID** | Today: they overwrite each other's registry entry (silent). Proposed: the Home flags "identity conflict" and refuses the second until one is re-paired (phase 0). | Re-pair one host. |

There is no "secondary takes over" anywhere. That is the point: by keeping
state in exactly one place per conversation and making execution stateless
across machines, the only failure that matters is the Home's, and its answer
is the one the owner already uses (restart, or migrate the Home).

## 6. Reachability on the real network

Every connection is opened by the node (or by a page acting for it). For each
node the Home stores an ordered **route list**, already present as the
attachment's `via` candidates and browser base:

| Device kind | Route ladder (first that answers wins) | Notes |
|---|---|---|
| Desktop on the same LAN as the Home | `via` LAN address → page origin | as today |
| Desktop elsewhere, Home behind CodeLab SSO | page origin fails at the edge → **browser relay** (signed-in Tofu tab on that desktop forwards polls) | works today while a tab is open; state the dependency in the UI |
| CodeLab container / GPU server on the internal network | `via` internal IP:port of the Home host (direct, no SSO) → page origin | needs the Home to advertise its internal address in the pairing link (it already can via `via`) and the internal port to be reachable; verify per cluster |
| Host that can reach nothing but ssh | ssh reverse/forward tunnel the owner starts (`ssh -L`) → node uses `127.0.0.1` candidate | supported today as a candidate; no auto-discovery (removed on purpose) |
| Browser extension | extension poll to its configured server URL, with the gateway cookie the browser already has | separate bridge, same rules |
| Phone | the Home's URL (Android kernel URL/SSH kinds) | client only |

Relaying through *another Home* (a reachable Tofu server forwarding polls for
an unreachable one) is the Tailscale DERP idea and the "server federation"
the node design deferred. It is not needed for the device kinds above and adds
server-to-server trust; it stays deferred (owner decision 8).

The node's own HTTP client is outside the application's network authority
(`NETWORK.md`: "desktop delegation keeps its independent owner/host
authorization boundary") and already follows its rules that matter for
reachability (proxy env, CA, no redirects, candidate-only failover). The
proposal adds no new route selector: the per-node route list *is* the
attachment candidate list, and its observed health is only displayed.

## 7. Identity, trust and pairing

Kept from today: owner-scoped `agents:bridge` credentials, minted only by
redeeming a one-shot pairing; no IP, LAN or loopback trust; owner filtering
before device selection; revocation is immediate.

Proposed additions, in order of value:

1. **Host-scoped node ID** (phase 0, done). The node config keeps one ID per
   host, keyed by a digest of the machine ID (plus the hostname on Linux,
   where containers of one image share `/etc/machine-id`), so hosts sharing
   one home get distinct IDs and an existing node keeps its own
   ([remote execution](modules/remote_execution.md#one-node-id-per-host)).
   **Collision detection** (deferred to phase 3): the Home would record a
   per-process boot nonce sent as a new optional poll-frame field and mark an
   ID with two live nonces "identity conflict". It remains the remedy for a
   config file copied between machines; it needs a wire change and cannot pick
   which twin keeps the ID, so phase 0 removed the shared-home cause instead.
2. **Confirm on join** (owner decision 5). Redeeming a link creates a *pending*
   node that the owner confirms on the Devices page (name, OS, route,
   a short fingerprint shown on both sides). A leaked link then cannot
   silently add a machine. VS Code Tunnels and Claude Code Trusted Devices
   both bind devices to the owner's account sign-in for the same reason.
3. **Device-held key** (deferred). Bind the credential to a key generated on
   the node (Tailscale node keys, Syncthing certificate IDs) so a copied config
   file is useless elsewhere. Worth doing only after 1 and 2; it changes the
   wire contract.

Capability authority stays two-ended, and gains a server-side ceiling:

- **Device switch** (exists): set on the device (tray; `--allow <tiers>` for a
  headless node at join, proposed). The device enforces it; the Home can never
  raise it.
- **Home ceiling** (new, durable on the Home, owner-scoped): set on the
  Devices page. The Home enqueues only tiers in *ceiling ∩ declared*. This
  gives the owner one place to lower many servers at once and makes headless
  nodes configurable without logging into them. Default ceiling for a new node
  is the preset chosen at confirmation (default read-only).

## 8. Routing tools to a device

Unchanged core: the model names `device` (ID or name); the Home resolves over
the owner's devices; ambiguity and offline targets fail before enqueue with a
listing; the environment block lists online devices with their system facts.

Proposed extensions:

1. **One registry view for all executors.** Desktop nodes, headless nodes,
   the self node and browser extensions appear in one owner-scoped projection
   (`GET /api/v1/devices`), built from the existing registries and the
   credential store. No new authority, no stored liveness.
2. **Tags** as a selector (`device="tag:gpu"`), resolved by the Home with the
   same rule as names: exactly one online match runs; several matches are
   ambiguous unless the tool says "any" explicitly, in which case the Home
   picks the node with the fewest commands in flight (a rule, not a tuned
   threshold). GitHub runner labels are the precedent.
3. **Owner-marked default** per kind (owner decision 7): "my computer" resolves
   to the marked desktop when several are online, instead of refusing. Still
   no "most recently seen" rule.
4. **Self node.** The Home's own host is listed and addressed like any node,
   but its tools keep running in-process (`run_command` on the server); this
   is a presentation unification, not a new transport.

## 9. Where the feature lives in the UI

| Placement | For | Against |
|---|---|---|
| Settings → Devices tab (today) | already exists | buried; settings are for preferences, not a live fleet; no health at a glance |
| Local Control modal (today, being removed) | close to the composer | modal hides fleet state; it framed control as "local", which the owner rejected |
| Sidebar list of devices | always visible | sidebar is for conversations; grows with every device |
| **Top-level Devices page + sidebar status pill** (recommended) | one place for many servers; health at a glance; room for onboarding and per-device settings; deep-linkable (`page:devices` beside `page:project-brain`) | one more page |

Recommended (wireframes `output/multi_device_design/devices_mockups.html`):

- **Entry.** A sidebar footer pill "Devices · 5/7 online · 1 needs attention"
  opens the page; the composer's device chip (when a conversation addresses a
  device) links to the same page. Settings → Devices keeps only defaults
  (default preset for new nodes, pending device waits) and links to the page.
- **Home card** at the top: which server this is, that conversations, memory,
  credentials and approvals live here, storage authority and uptime, and a
  link to switch or move the Home.
- **Fleet list** grouped by kind (This server · Computers · Servers &
  containers · Browsers · Phones & viewers), each row with status from the
  node's own poll (online/last seen, round trip of the latest poll), route
  (direct / internal / browser relay / ssh), version (outdated flag), and the
  effective capabilities as chips (struck = off; amber = allowed here, off on
  the device).
- **Add device wizard** (one for every kind): choose kind → instructions with
  the one-shot link, 6-digit code and a one-line command for headless hosts →
  confirm the detected device → name, preset, shared folders, tags.
- **Device drawer**: health, capability table with *Home ceiling*, *Device*
  (read-only here) and *Effective* columns, shared folders, tags/default,
  recent activity, pause, remove & revoke.
- **Topology explainer** (frame 4) rendered on first visit and in the help
  link, so the "who is primary" question is answered on screen.

## 10. Migration path from today's code

Each phase is independently shippable and keeps all current contracts.

| Phase | Change | Owners | Done when |
|---|---|---|---|
| **0. Fix the substrate — done 2026-11-17** | (a) Relay the desktop bridge from the execution host to the API host's owner over the transport shared with the browser relay (`lib/runtime/loopback_relay.py`; one owner, loopback relay, per-incarnation token). (b) Host-scoped node ID (one ID per host in the shared config; the legacy ID stays with the first host). Boot-nonce collision detection moved to phase 3. | `lib/desktop/bridge.py`, `lib/desktop/bridge_relay.py`; `lib/desktop_agent/host_identity.py`, `config.py`, `_run.py` | Met: `tests/desktop/test_desktop_bridge_relay.py` lists a node from a separate owner process and round-trips a command through the desktop tool handler; `tests/desktop/test_desktop_node_identity.py` gives two hosts sharing one `desktop_agent.json` distinct stable IDs and keeps a paired node's ID |
| **1. One device projection** | `GET /api/v1/devices`: self node, desktop/headless nodes, browser extensions, signed-in clients, each with kind, status, route, version, effective tiers. Read-only projection over existing registries. | `routes/api_v1/`, `contracts/` (new `devices_v1` schema) | contract test; owner isolation; no stored liveness |
| **2. Devices page + wizard** | Top-level page, sidebar pill, add-device wizard over the existing pair-code API, device drawer, `page:devices` jump target; Settings → Devices links to it. Computer control returns to the frontend here. | `frontend/src/features/devices/`, `ui-nav.ts` | browser test of pairing a headless node end to end; i18n |
| **3. Capability ceiling + confirm on join** | Durable owner-scoped Home ceiling per node; enqueue checks ceiling ∩ declared; `--allow` for headless join; pending state + confirmation; boot-nonce identity-conflict flag (section 7). | `lib/desktop/pairing.py`, `bridge._tier_enqueue_error`, storage domain, `node_protocol.py` | both ends refuse; ceiling never raises a device tier |
| **4. Routing by tag / default; route health shown** | `tag:` selector and owner default; per-node route kind and latest round trip in the projection; `via` internal address advertised for container joins. | `resolve_device_target`, desktop tool schemas, pairing link builder | selection tests for ambiguity/any/default |
| **5. Multi-Home switcher** | Web UI lists the owner's Homes (like Android kernels) and switches origin; no sync. "Move this Home" links to `migrate_host`. | frontend shell; `docs/ANDROID_KERNELS.md` vocabulary | — |
| Deferred | Device-held keys; Home-to-Home relay (federation); phones as executors of another Home. | — | only on owner request |

## 11. Decisions only the owner can make

Each has a recommended default that phases 0–2 assume.

1. **Topology.** Adopt "one Home per conversation + equal nodes" (D), with no
   live replica and no multi-primary. *Default: yes.*
2. **Which server is your default Home.** *Default: the new 203 GiB CodeLab
   instance; laptops and other containers join it as nodes.*
3. **Several Homes, unsynchronized.** Allow other Tofu servers (e.g. the
   desktop app's own server) to keep their own conversations, switchable but
   never merged. *Default: allowed; the web UI gets a Home switcher in phase 5.*
4. **UI placement.** Top-level Devices page with a sidebar status pill;
   Settings → Devices becomes defaults + link; computer control returns there.
   *Default: yes.*
5. **Confirm on join.** A redeemed pairing link waits for your confirmation on
   the Devices page. *Default: on* (one extra click per device).
6. **Who sets capabilities.** Home ceiling ∩ device switch (two-ended), new
   nodes read-only until you pick a preset. *Default: two-ended, read-only.*
   Alternative: Home-only for headless nodes (simpler, but then a compromised
   Home can raise any node's powers).
7. **Unaddressed tool calls with several devices online.** Keep refusing, but
   let you mark one default per kind ("my computer", "a GPU box"). *Default:
   allow owner-marked defaults; never "most recent".*
8. **Relay for unreachable nodes.** Rely on internal routes, the browser relay
   and ssh tunnels; defer a Home-to-Home relay (federation). *Default:
   deferred.*

## 12. Alternatives considered

- **A. Primary + replicas.** Rejected: needs a replication protocol for the
  native engine, fencing and a quorum; process-local execution state would
  still be lost on failover; a personal deployment restarts or migrates
  faster than it can safely fail over.
- **B. Multi-primary.** Rejected: conversation history and tool side effects
  are causal and non-mergeable; the FUSE-shared PostgreSQL incident is the
  local precedent for two writers on one store.
- **C. Full peers.** Rejected: needs inbound reachability or hole punching
  that SSO edges and corporate proxies block; phones and MV3 workers are not
  reliable peers; n² device authorization.
- **Keep the Local Control modal or Settings tab as the home of the feature.**
  Rejected: neither shows a fleet's health, and "local" mislabels a
  multi-server feature.
- **Federation now (servers relay each other's nodes).** Deferred: no device
  kind in section 6 needs it, and it adds server-to-server trust.
- **A separate controlled-end product again.** Rejected by the owner and by
  the merged node model.

## 13. Verification and limits of this document

- Code facts were read at `66147d70ab`; file and symbol names are cited
  inline.
- Not verified at runtime: whether the owner's CodeLab containers share a home
  directory; whether CodeLab containers can reach each other's internal
  Tofu port (phase 4 starts by checking it). Phase 0 is verified by its tests
  against a real separate owner process; it is live only after the API and
  execution hosts restart.
- External facts were read from the vendors' own pages listed below; GitHub's
  long-poll mechanism and Kubernetes' exact grace-period default were not
  confirmed and are not relied on.

## Sources

- Tailscale: [how Tailscale works](https://tailscale.com/blog/how-tailscale-works), [control and data planes](https://tailscale.com/docs/concepts/control-data-planes), [DERP servers](https://tailscale.com/kb/1232/derp-servers), [how NAT traversal works](https://tailscale.com/blog/how-nat-traversal-works); [headscale](https://github.com/juanfont/headscale)
- VS Code: [remote development overview](https://code.visualstudio.com/docs/remote/remote-overview), [remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions), [remote tunnels](https://code.visualstudio.com/docs/remote/tunnels)
- Syncthing: [syncing and conflicts](https://docs.syncthing.net/users/syncing.html), [security / device IDs](https://docs.syncthing.net/users/security.html), [relaying](https://docs.syncthing.net/users/relaying.html)
- Kubernetes: [nodes](https://kubernetes.io/docs/concepts/architecture/nodes/), [leases](https://kubernetes.io/docs/concepts/architecture/leases/), [control-plane–node communication](https://kubernetes.io/docs/concepts/architecture/control-plane-node-communication/), [HA topology](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/ha-topology/)
- GitHub Actions: [self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners), [about self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)
- Claude Code: [Remote Control](https://code.claude.com/docs/en/remote-control), [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)
- OpenAI Codex: [app-server](https://developers.openai.com/codex/app-server), [cloud](https://developers.openai.com/codex/cloud), [cloud environments](https://developers.openai.com/codex/cloud/environments)
- Chrome: [extension service worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)
