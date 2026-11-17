# Desktop devices and remote execution

This domain connects owner-authorized desktop devices ("nodes") to Tofu
servers for desktop tools, remote project work, subscription egress, and local
subscription adapters, plus desktop packaging and pairing. The bridge is a
capability boundary, not a trusted-network shortcut. "Device" and "node" name
the same thing; the tool field keeps the name `device`.

## Ownership

| Concern | Owner |
|---|---|
| Wire admission/lifetime policy | `lib/desktop_wire/`, `contracts/desktop_bridge_v3.schema.json` |
| Node protocol: pairing links, permission tiers, node listing, attachment migration | `lib/desktop_wire/node_protocol.py`, `contracts/desktop_node_v1.schema.json` |
| Bridge credential resolution | `lib/bridge_auth.py`, `routes/_bridge_caller.py` |
| Poll HTTP adapter | `routes/desktop.py` |
| Device registry, command queue, streams | `lib/desktop/bridge.py` |
| Bridge owner across the API/execution split | `lib/desktop/bridge_relay.py` over `lib/runtime/loopback_relay.py` |
| Node ID per host (shared home directories) | `lib/desktop_agent/host_identity.py`, `lib/desktop_agent/_run.py::_ensure_agent_id` |
| Node status/listing/removal, pairing, distribution API | `routes/api_v1/desktop.py` |
| Pairing authority (6-digit code + link secret, one row) | `lib/desktop/pairing.py` |
| Node runtime (bridge loop, command execution) | `lib/desktop_agent/` |
| Node links: one loop per joined server + local server | `lib/desktop_agent/links.py` |
| Joining: redeem a pairing link, persist the attachment | `lib/desktop_agent/attachments.py`, `lib/desktop_agent/config.py` |
| Agent permission policy | `lib/desktop_agent/_permissions.py` |
| Remote project binding | `lib/desktop/remote.py`, `lib/conv_config/` |
| Waits for an offline device (`await_device`) | `lib/desktop/device_waits.py`, `contracts/device_waits_v1.schema.json` |
| Remote project execution | `lib/tasks_pkg/handlers/project.py`, agent `_project.py`; [paper builds](../PAPER_BUILDS.md#desktop-execution) reuse the bridge and compiler via agent `_paper.py` |
| Subscription egress routing | `lib/desktop/egress.py`, `egress_preferences.py`, Sidecar `desktop` domain, agent `_egress.py` |
| Local subscription adapter / borrowed Codex login | `lib/desktop/adapter.py`, agent `_adapter.py`; [Codex login owner](../IDENTITY.md#borrowing-an-existing-codex-login) |
| Desktop app launcher, join dialog, role window | `desktop/launcher.py`, `desktop/connect_ui.py`, `desktop/role_window.py` |
| Build and artifact store | `lib/desktop_dist/`, `tofu.spec`, release scripts |
| Browser UI | None since 2026-11-17: computer control left the frontend until the multi-device design places it ([frontend record](../FRONTEND_ARCHITECTURE.md)); the Settings desktop download card (`frontend/src/features/settings/client-downloads.ts`) is a download, not a control |

## Trust and identity

Remote nodes authenticate with an owner-scoped credential carrying `agents:bridge`, obtained by pairing. The packaged desktop app may
use the non-persisted process capability from `lib/bridge_auth.py`; it is accepted only by the desktop poll boundary. IP
address, loopback appearance, and a global shared secret grant no authority. Every poll must include a stable `agent_id`
frame; anonymous/ version-one pollers are rejected. Registration records owner, credential ID, platform, version,
declared capabilities, share roots and the optional `system` facts (below); status and device lists filter by
authenticated owner. The in-memory bridge
queue is deliberately ephemeral transport, not durable job state (credentials/owner records stay in the Sidecar); a
restart fails outstanding bridge calls honestly and durable jobs reconcile via their own state machine.

### One node ID per host

Problem: the node ID (`agent_id`) lived only in the agent config
(`~/.tofu/desktop_agent.json`, or `TOFU_DESKTOP_CONFIG`). Hosts sharing one home
directory (containers on a network home) read one file and presented one ID,
so the server saw one node whose registry entry they overwrote poll by poll,
and an addressed command reached whichever twin polled first.

Decision: the config keeps one ID per host in `agent_ids`, keyed by
`host_identity.host_key()`: a digest of the platform machine identifier
(`/etc/machine-id`, macOS `IOPlatformUUID`, Windows `MachineGuid`), plus the
hostname on Linux because containers built from one image share
`/etc/machine-id`. Renaming a Mac or Windows computer keeps its ID; renaming a
Linux host (or recreating a container) yields a new node ID. Only digests are
stored. Migration: the legacy single `agent_id` goes to the first host that
starts with the new build and stays in the file for older builds; a second
host that shared it gets a fresh ID. Pairing credentials are not bound to the
node ID, so every host keeps polling with the shared attachment; removing one
of them through its credential (`disconnect_credential`) still drops every
node on that credential.

Alternatives considered: a host-local file outside the home (`/etc`,
`/var/lib`) is not writable by an unprivileged node and `/tmp` does not survive
a container restart; server-side conflict detection by per-boot nonce needs a
wire change and cannot tell which twin keeps the ID (still the remedy for a
config file copied between machines, together with device-held keys in
[the multi-device design](../MULTI_DEVICE_NETWORK_DESIGN.md#7-identity-trust-and-pairing)).

## Command lifecycle

1. A service resolves the authenticated owner and optional target `agent_id`.
2. The bridge verifies an explicit target is online and owner-owned. An
   unaddressed command is accepted only when that owner has at most one online
   device; ambiguous selection is refused with a listing of the owner's online
   devices (name, agent ID, OS, default shell).
3. The command queues with owner, target, optional explicit TTL, and a unique command ID.
4. A matching device poll claims the command before wire projection.
5. Only the claiming owner/device may append stream frames or settle the result.
6. Sequence numbers deduplicate resent stream frames.
7. Default synchronous waits have no private deadline. Completion or explicit
   cancellation/wait bounds settle the caller once. Unclaimed stopped calls
   withdraw their queue entry; claimed work retains owner/device settlement
   until its late result or an explicit pickup TTL settles it.

No environment switch disables addressing; feature switches may hide
remote-worktree UX but cannot weaken bridge authority.

## Client execution and connection lifecycle

`lib/desktop_agent/links.py` owns link start/stop/restart: the desktop app and the headless CLI (`python -m lib.desktop_agent [--join <link>]`) keep one polling worker per joined server, plus a local link with the process capability to the packaged app's own server. Remote links get status, route repair over the attachment's own candidate routes and a per-link browser relay (its origin gate admits only that server, so a signed-in page finds exactly its own relay); the local link never discovers remote destinations. A worker alive after the stop join stays tracked. A Tofu `401` on a remote link means its credential was revoked: the link stops and the attachment is marked `revoked` until the owner joins again.

The agent executes ordinary commands, paper builds and egress streams outside of the poll thread, on demand without a local worker ceiling. Legacy IDs stay known until shutdown. Negotiated bridge v3 retires expired finite IDs only after completion and output ACK and refuses stale redelivery; server delivery credits derive from CPU/memory headroom. This is process-local deduplication, not durable exactly-once execution. See [client lifecycle](../DESKTOP_CLIENTS.md). `TOFU_DESKTOP_EXECUTION_WORKERS` is retired and ignored. Pending results/stream frames use launch-probed memory reuse (1/128 of the smaller capacity/headroom, 1 MiB lean fallback), split 7/8 for results and 1/8 for streams; overflow spools to private local temporary files instead of blocking producers. One result poll still sends up to 512 frames/16 MiB and one stream poll up to 256 frames/2 MiB. Full response validation precedes prefix acknowledgement; acknowledgement immediately deletes spooled frames. Teardown waits for owned execution to settle before reclaiming the outboxes. Project stream cancellation kills/reaps its process tree and joins pipe readers; `cancelled`, `timed_out`, and exit status remain independent outcomes. Only Popen reaps its direct child so a killed process cannot acquire a false zero exit status. The browser relay owns an uncapped live-job FIFO and removes each job on timeout, completion or close; close wakes pending request/take waiters. Server egress readers release consumed body chunks instead of retaining full replay; diagnostics retain the 256 KiB provider-error prefix budget (`test_egress_stream_memory.py`). Retry waits are jittered, capped 60s, interruptible by stop. Direct probes/polls honor Requests proxy/NO_PROXY and CA settings, suppress ambient netrc, and do not follow redirects; auth errors carry the flat `bridge_auth_required` envelope. Credentials fail over only to explicitly configured attach candidates — LAN health, public boot IDs, and discovered SSH hosts never authorize bearer reuse; unattended service needs a gateway-supported device route.

## Permissions

The agent reconstructs its permission floor on startup. Read, write, execute,
GUI, egress and Codex-login tiers are enforced twice from the one command-tier
map in `contracts/desktop_node_v1.schema.json` (`x-command-tiers`,
`x-command-param-tiers`): the server refuses to enqueue a command whose tier
the target node did not declare in its latest poll frame (a node declaring
nothing is read-only; `lib/desktop/bridge.py::_tier_enqueue_error`), and the
device refuses it again before execution (`lib/desktop_agent/_dispatch.py`).
The frame re-declares the live tiers on every poll, so a tray toggle takes
effect at both ends on the next command. Server-side admission never
substitutes for the device check. The read-only
default lists and reads files and shows a system overview. `desktop_list_files`
and `desktop_read_file` resolve the real path inside a declared share root on
every tier (`_project.shared_path`), so `~`, `..`, absolute paths and symlinks
cannot leave the shared folders, and no share roots means no local file reads.
The clipboard (read and write) is GUI tier, like reading the screen. Write and
move need the write tier, open/launch/run the execute tier; those act on the
machine as the tray describes. Project commands resolve against declared share
roots (below). Remote project writes apply local equivalents of path
containment, freshness, read-before-edit, and atomic-write rules; destructive
targets resolve before execution and must stay inside the selected root.

Problem: the read-only default listed and read any path and exposed the
clipboard, although this section promised share-root scope. Decision: confine
listing/reading on the device itself, on every tier, and move the clipboard
to the GUI tier. Alternatives rejected: a server-side path check (the device
must not trust the server); lifting the confinement under the execute tier
(the execute tier already has `desktop_run_command`; one rule is easier to
verify). Verify `tests/desktop/test_desktop_agent_read_scope.py`.

## Remote projects

A conversation stores `remote:<agent_id>:<root>` in its existing project-path
field; `lib/conv_config` translates this to a `project_remote` binding only when
remote worktrees are enabled. Binding validation requires an online device owned
by the caller, an exact declared share-root name, and both agent and root fields.
Project tools keep their public names; the server route selects the remote
adapter from the validated binding and the agent revalidates the root. Streamed
`run_command` output enters the same `tool_progress`/terminal-tool result
contract as local execution.

## Subscription egress and adapter

`lib/desktop/egress.py` routes only the exact subscription-host allow-list or a
declared adapter loopback port; the agent repeats the target check before opening
a socket. Owner-scoped candidates order by explicit pin and recent health;
failover is bounded and observable. The explicit pin is durable user state in the
Storage Sidecar; the live device registry stays ephemeral transport, filtered by
owner/online/`egress` capability before API display or pin acceptance. A retired
per-installation JSON file is a read-once migration source only — an explicit
Sidecar row (even empty) prevents it becoming a second authority.

The authenticated owner is captured at each HTTP/task boundary and propagated
through dispatch, preflight, OAuth callback/device workers, token refresh, and
provider probes. No transport layer guesses a personal owner: an ownerless
request cannot address a desktop device. Adapter/account/ensure-task caches are
bounded, keyed by `(owner, agent)`, so a cached response cannot bypass ownership
after re-pairing. OAuth relay/device workers carry an opaque flow generation, so
a late callback cannot redeem a replacement login. Secrets stay request headers,
redacted from logs/status; steady healthy selections are DEBUG-only.

The optional local subscription adapter runs on the device loopback interface;
server policy mints adapter credentials and relays management/model calls through
the addressed device. Discovery replaces only its deterministic owner-scoped
model-routing v2 ProviderAccess bundle via bounded CAS — explicit Connection
adapter metadata and encrypted credential references, never a legacy row. Stopping deprovisions that bundle only after the owning device confirms.
Bring-up starts on demand with one active task per owner/device. Active task
state remains owned until settlement; only terminal status uses the TTL cache. Catalog refreshes are
event-driven; fresh quota headers trigger a TTL-throttled (1 h) re-sync only when
the upstream model set changed.

## Nodes and pairing

Problem: the desktop product shipped two artifacts. The full app bundled a
second, independent Tofu server and controlled only that one; the separate
controlled end (Tofu Remote / 受控端) attached to a remote server through a
personalized installer or a pasted `<url> <token>` connect line. Users installed
the full app and the server kept showing "not running". The status route then
guessed the user's network from the browser's peer address, which an ssh `-L`
tunnel or same-host proxy makes look like loopback, and hid the remote flow.

Decision: one node model.

- Every desktop install is a node. One artifact (the full desktop app) runs
  standalone with its own server and can join any number of other servers. A
  node keeps one outbound bridge loop per joined server (node-to-server only,
  so NAT, ssh tunnels and reverse proxies need no inbound route) and shares
  one permission set, read-only by default, across them.
- Joining is pairing. A pairing is minted
  (`POST /api/v1/desktop/pair-code?base=<page origin + prefix>`, owner-bound,
  one-shot, 5 minutes): a `link` of the form
  `<server base>/#tofu-pair=<secret>[&via=<route>]*` and the same pairing as a
  6-digit `code`. The owner pastes the link into the desktop app's "Join a
  server" dialog (tray or control panel) or passes it to
  `python -m lib.desktop_agent --join`. The node tries the `via` routes (a
  reachable direct LAN address) first and the browser-visible base last, and
  redeems the secret through unauthenticated `POST /api/desktop/pair`, which
  returns a normal `agents:bridge` credential once. Nobody copies a credential.
  The link parser tolerates an SPA route or trailing slash around the pairing
  fragment and never folds query or fragment text into the server base.
- The server sees nodes by observing their polls. `setup_state` is
  `connected` (one of the caller's nodes polled within the window) or `pair`;
  there is no peer-address classification. `GET /api/v1/desktop/nodes` lists
  the caller's nodes (`NodeListing`): live registry entries with declared
  tiers, plus paired credentials no live node uses (offline). Tools address a
  node by `device` (node ID or name); with one online node it is the default.
- Removing a node (`DELETE /api/v1/desktop/nodes/<node_id>`) or revoking its
  credential (`DELETE /api/v1/desktop/token/<id>`) revokes the key and calls
  `disconnect_credential`: the registry entry disappears at once and the
  node's pending commands fail with a typed error; the node's next poll gets
  Tofu's `401` and stops that link.

Migration: a node config with the retired single `remote_server` is folded into
`attachments` on first read (`migrate_attachments`), keeping its credential and
candidates, so already paired devices keep working without re-pairing. The join
dialog also accepts an old connect line and stores it as an attachment. The
separate controlled-end build, its personalized installer routes
(`agent-installer`, `agent-bundle`, `agent-attach-bundle`), the
browser-pushed attach channel and the agent-side discovery ladder (loopback/LAN
broadcast/ssh self-tunnel) are removed; a route left by an old installer is
re-pointed only to the attachment's own candidates.

Alternatives considered:
- Keeping two artifacts and fixing the panel copy: the root cause is two
  products with overlapping roles; every fix kept one of them confusing.
- Detecting tunnels server-side: there is no signal; a guess misroutes
  true-local users. Observing the node's own outbound poll needs no guess.
- Inbound connections (server dials node) or a peer mesh: needs reachable
  nodes, breaks behind NAT and corporate proxies, and widens the attack
  surface; outbound polling already works through all of them.
- Personalized installers carrying a credential: platform-specific (Windows
  NSIS trailer only), mints credentials per download whether used or not, and
  cannot join a second server. A one-shot link is cross-platform and costs
  nothing until redeemed.
- Server federation (one server relaying another server's nodes): deferred;
  a node joining several servers covers the multi-server need without a
  trust relationship between servers.

Distribution: the Settings desktop card offers only the full desktop app, for the
visitor's OS and architecture; an empty `downloads` list keeps the release
link. Remote node builds no longer exist. Stable registry/install-directory,
bundle IDs and protocol/config keys preserve upgrades.
`lib/desktop_dist/store.py` serves only exact manifest keys; mirrors/builders
publish atomically and keep a last-known-good on refresh failure. Publication
requires the changelog, complete platform/component assets and size floors;
each native build runs a frozen-runtime smoke before packaging. The full-app
spec preserves the `lib/model_info/data` catalog, the `contracts/` directory
and `frontend/authoring-inputs.json` metadata at their runtime-relative paths,
and checks source freshness before building. Login startup uses per-user HKCU
Run, XDG autostart or macOS LaunchAgents via `desktop/autostart.py` and starts
the desktop app; failed publication preserves the prior entry.

Release discovery reads github.com, never the REST API: the
`/releases/latest` redirect names the tag, and that release's `SHA256SUMS`
lists every asset with its digest (`lib/desktop_dist/platforms.py`). Problem:
the unauthenticated API allows 60 requests/hour per egress IP; a shared egress
exhausted it (HTTP 403) and the mirror kept serving a superseded installer.
Decision: two unmetered github.com reads per probe. Alternatives: an
authenticated API probe (an operator credential for a public read), or the API
with a web fallback (two discovery paths that still spend the shared quota).

Mirrored downloads are checked against that digest, and against an advertised
byte count when a probe supplies one, before atomic publication; invalid or
mismatched metadata keeps the previous installer, and a changed digest
invalidates same-name/same-size reuse. Assets without digest metadata are
legacy downloads, not signature-verified updates (a local checksum is not
publisher authentication); this cache is not an installed-client automatic
updater. It records `integrity_source` (`release-sha256sums`/`computed-only`),
keeps `release_sha256` separate, and acquires sequentially (2 GiB/file, 30 min),
the platform of the visitor whose poll started the refresh first.

Freshness is judged against this server's own version: a store whose release
is older (`store.behind_server`) re-checks on the short `_ERROR_RETRY_S`
cadence instead of waiting out the max age. A check that finds the release the
store already holds stops at the probe (`checked_at`); the verifying sync,
which re-hashes every installer, keeps the max-age cadence. Each `downloads`
row carries `version` and `older_than_server`, the status payload adds
`server_version` and `installers_refreshing`, and the download surface
(the Settings desktop card) labels an older installer as such instead of
presenting it as current.

## Bridge owner across the API/execution split

Problem: the managed composition runs an `api` host that terminates node polls
(`routes/desktop.py`) and a resident `execution` host that runs desktop tools,
the environment device listing, `await_device`, desktop egress and the task
liveness judge. The registry, queue and streams of `lib/desktop/bridge.py` are
process-local, so the execution host saw no nodes: desktop tools were not even
offered (`is_desktop_agent_connected` was false there) while Settings showed
the node online.

Decision: as for the [browser bridge](browser_automation.md#bridge-owner-across-the-apiexecution-split),
the API host stays the single owner. At background-service start it publishes
`<project>/data/desktop-bridge-relay.json` (mode 0600, per-incarnation bearer
token, removed on stop) over the shared loopback transport
(`lib/runtime/loopback_relay.py`); the execution host selects it and the
public verbs of `lib/desktop/bridge.py` forward there: `list_agents`,
`online_agents`, `online_connection`, `is_desktop_agent_connected`,
`command_active` (liveness judge), `send_desktop_command`,
`enqueue_desktop_command`, `consume_desktop_command`,
`release_desktop_command` and `get_frames`. Addressing and tier admission run
in the owner against the node's own frame. Poll-side verbs (register, take,
resolve, revoke) are never relayed. Role `all` keeps the in-process owner.
Caller EOF cancels the owner-side wait like a local Stop; the caller keeps its
task's device-wait publication. An unreachable owner is a command error (and
`resolve_device_target` names it); presence predicates read it as no reachable
device; nothing is replayed.

Alternatives considered: moving the registry into the execution host would
forward every poll the other way; a durable shared registry would store
transient liveness.

Verification: `tests/desktop/test_desktop_bridge_relay.py` drives a real
separate owner process (owner-scoped listing, name resolution, command round
trip through the desktop tool handler, owner-side tier refusal, streamed
frames, liveness and cancellation), missing owner, token refusal and role
selection.

## Failure semantics

- Missing/invalid bridge credential: `401`.
- Authenticated poll without stable device identity: `400`.
- Offline/wrong-owner target or ambiguous unaddressed command: reject before
  enqueue.
- Wrong device/owner result or stream frame: ignore and audit; never settle.
- Pickup/result timeout: typed bridge transport failure.
- Remote path outside declared root or insufficient device capability: local
  denial returned as tool failure.
- Device offline while its conversation could wait: the tool error names
  `await_device` (below).
- Stream state expires before terminal: aborted transport, never an infinite
  wait.
- Distribution refresh failure: serve last known good artifact and expose
  refresh diagnostics.

## Invariants

- Credentials and stable agent identity are mandatory on every poll.
- Owner filtering happens before device selection and delivery.
- Only an online, owner-matching, egress-capable device can be newly pinned.
- Command claim and result/stream settlement bind to the same device.
- No compatibility switch can disable addressing or receipt validation.
- Local permissions and root validation remain authoritative on the device.
- Remote/local project paths share user-visible tool semantics and failure
  honesty.
- Egress uses exact target classes and defense-in-depth checks.
- There is one desktop artifact; node permissions default to read-only and
  are enforced on both the server and the device.
- Artifact serving never maps untrusted path text directly to the filesystem.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Poll/auth/identity | `lib/bridge_auth.py`, `routes/desktop.py` | credential and missing-frame tests |
| Routing/stream settlement | `lib/desktop/bridge.py` | cross-owner/device, TTL, async wake |
| Device command | agent `_dispatch.py` + focused implementation | permission and wire parity |
| Remote project operation | server project handler + agent `_project.py` | root, freshness, streaming |
| Egress host/route | both server and agent egress owners | allow-list, failover, redaction |
| Adapter lifecycle | both adapter owners | loopback policy, account/catalog sync |
| Pairing/node protocol | `lib/desktop_wire/node_protocol.py`, pairing service, attachments | expiry, one-use, owner binding, link parsing, migration (`tests/desktop/test_desktop_node_protocol.py`, `test_desktop_pairing.py`) |
| Device-online wake | `lib/desktop/device_waits.py`, `register_agent` epochs | exactly-once wake, owner isolation, restart |
| Installer/artifact | `lib/desktop_dist/`, workflow/spec | kind, checksum, smoke, traversal |

## Test map
Run focused tests under `tests/desktop/`, desktop distribution and installer
parity under `tests/desktop_dist/`, remote worktree routing under `tests/tasks_pkg/`
and `tests/tools/`, and asynchronous polling under `tests/browser/`. Device waits:
`tests/desktop/test_device_waits.py` and `tests/api/test_api_v4_device_waits.py`.

Hope large-checkpoint publication is owned by the private environment provider's
[Worker result publication contract](../../bundles/yourprovider/environment-provider/README.md#worker-result-publication).
Problem: sequential transport prevented a real 25.5 GB research checkpoint from
being delivered. Decision: admit an optional host-pinned Java6 uploader into the
source seal, preserving actor/operation destinations, one publication intent,
the shared deadline and independent byte/hash collection. Alternatives: dropping
optimizer/RNG state prevents exact recovery; retrying uncertain uploads risks
replay. Private Hadoop configuration stays behind the provider boundary; public
compute specs and logical output names remain provider-neutral. The 512 MiB
live same-file trial supports full-checkpoint validation, not completed recovery.


Demand-owned desktop delivery — Problem: negotiated execution slots and replay counts withheld commands, while default 90-second pickup expiry rejected late accepted work. Decision: dispatch independently of CPU/memory/replay credits. `execution_slots` remains a compatibility poll-window hint; bounded command batches and rotating, byte-bounded activity pages regulate transport only. Owner/device-bound delivery acknowledgements prevent omitted activity IDs from causing old work to fill later batches. Default commands have no pickup deadline; their first-dispatch replay horizon is stable and `deadline_enforced=false` distinguishes it from an explicit TTL. Missing policy fields preserve old-server expiry behavior. Completed replay IDs retire only after the horizon, execution completion and both output ACKs. Cancellation and explicit TTLs remain effective. Alternative rejected: larger slots/TTLs still refuse later work; forgetting unacknowledged IDs permits duplicate execution. The queue remains ephemeral transport, not a durable-job authority; application restart reconciliation stays with the owning job. Guards cover forty old queued commands with zero advertised slots and thirty-two held handlers entering concurrently in legacy/v3 loops, plus lost ACK, addressing, cancellation and metadata page rotation.


## Synchronous desktop wait ownership

Problem: both the bridge and desktop tool handler imposed an implicit 30-second wait, then discarded the command even when a device was still executing it. Decision: default waits continue until an owned result; explicit nonnegative finite wait bounds remain exact (zero is immediate). Tool task Stop is checked without creating a watcher thread or another queue. Stop before publication creates no command. A timeout or Stop withdraws unclaimed work; a claimed command retains its existing entry until the claiming owner/device supplies a result. The caller receives an unconfirmed remote outcome on Stop, never a claim that a GUI action or remote process was killed. Settlement and caller detachment use one queue lock, closing their race. The transport remains ephemeral. Alternatives considered: increasing 30 seconds still loses healthy late work; dropping claimed entries loses result authority; pretending Stop kills device work is unsupported by the generic desktop protocol. Verification: addressing tests advance the wait clock beyond one hour before delivering the owned result, check task cancellation before/after pickup, late settlement after explicit timeout, foreign-owner refusal, duplicate result handling and entry cleanup.


## Desktop command execution time

Problem: healthy desktop commands were killed by implicit 30/300-second worker timeouts, project overrides were clamped to one hour, and server project calls abandoned results under a separate 60/330-second wait policy. Decision: absent execution timeout means actual completion; finite nonnegative explicit seconds are passed unchanged to the executing device. Invalid values fail before spawn. The server waits for the worker's observed outcome with task cancellation, forwarding explicit execution bounds without a private bridge clamp. Its progress reader retires in a joined `finally` path, including bridge failures. Generic desktop shell commands share the existing streamed subprocess path, including exec permission checks, owner-stop/connection-loss cancellation and joined pipe-reader cleanup. Their runtime admission lasts until the asynchronous terminal callback; replay cannot start another copy. Direct synchronous fallback keeps its separate completion owner. Alternatives considered: larger defaults still kill healthy work; just removing the server wait leaves the worker timer; removing cancellation abandons live processes. Verification: real blocked commands survive a simulated 4,000-second elapsed interval before completion, explicit 5,000-second values remain exact, invalid policies never spawn, and default-unlimited cancellation joins readers before terminal settlement. Cooperative stop/connection-loss and abrupt owner death share the supervised command lifetime described below.


## Independent desktop process supervision

Problem: a direct subprocess outlived an abruptly killed agent after private execution deadlines were removed. Decision: synchronous and streamed commands reuse `lib/paper/builds/process_worker.py` through the existing source/frozen worker entry. The worker waits for its private start marker, checks its creator before admission, and owns descendant retirement independently of the agent. Linux subreaper ownership catches descendants that start a separate session; Windows job binding precedes the start marker, with creator birth/liveness checks protecting the pre-binding interval. Separate private stdout/stderr files support windowed frozen executables without stdio handles. The agent tails these files and joins readers before removing its directory and delivering the final result. Explicit timeout/Stop requests ask the worker to retire children and retain the actual signed command exit status. Cleanup failure remains a visible terminal error. Parent death removes the validated desktop IPC files independently on POSIX. Alternatives considered: a thread watchdog dies with its owner; killing only a shell misses escaped descendants; restoring a timer kills healthy work. Verification: real Linux owner SIGKILL with an escaped child and grandchild leaves no live commands, reaps command descendants and removes private output; pre-admission retired ownership causes no side effect. Separate streams, signed exit status, Stop, explicit timeout, startup failure and cleanup failure remain covered. Windows creator identity is contract-tested; native Windows job/crash cleanup is not verified by the Linux tests. Source/frozen dispatch uses the existing worker flag and does not add an agent startup service. A source agent runs the supervisor as its pinned script (`lib/runtime/pinned_helper_source.py`), like paper builds, instead of `-m lib.paper.builds.process_worker`: the package import cost every command about 0.25 s before it started (measured end to end on the development host: median 0.60 s to 0.35 s for `true`). On Windows both the worker and the command start with `CREATE_NO_WINDOW`, so a windowed agent opens no console window per command.


## Windows command shells

Problem: Windows commands always ran as `cmd.exe /c`, passed to `Popen` as a list. `list2cmdline` escapes inner quotes as `\"`, which cmd.exe does not understand, so a command such as `git commit -m "a b"` reached cmd with literal backslashes. Models also write PowerShell or POSIX text far more often than batch syntax, the model-facing tool never said which shell a device used, and output was decoded per 4 KiB read, splitting multibyte UTF-8 characters at read boundaries on every platform.

Decision: model-authored commands on Windows (`run_command` on a Windows server through `lib.compat.get_command_shell_args`, and device commands) run as one PowerShell script: `pwsh` when installed, else Windows PowerShell. `lib.compat.powershell_args` embeds the text as a single-quoted literal inside `-EncodedCommand`, so there is no quoting layer and execution policy does not apply. The bootstrap parses the text first (a syntax error reports the user's own line and exits 1), then dot-sources it with a trailer that exits with the last statement's status: a failing native command's own exit code, 1 for a failed cmdlet, else 0; an explicit `exit N` is kept. Output is UTF-8 with progress records and ANSI styling off. A command whose encoded form exceeds the CreateProcess 32,767-character command line is refused with a typed message asking for a `.ps1` file. `desktop_run_command` takes an optional `shell`: `powershell` (default on Windows; needs `pwsh` elsewhere), `cmd` (Windows only: `cmd.exe /d /s /c` whose tail the supervisor passes verbatim, as Python's `shell=True` does, after `chcp 65001`), or `sh` (default on POSIX; on Windows the `sh.exe` on PATH or beside Git for Windows' `git.exe`). Unknown or unavailable shells fail before spawn. Stream readers use an incremental UTF-8 decoder. Operator-authored scheduler commands keep `get_shell_args` (cmd.exe) because existing schedules are written in batch syntax.

Alternatives considered:
- Keeping cmd.exe and only fixing its quoting leaves models writing PowerShell/POSIX text into batch syntax.
- `-File script.ps1` lifts the length limit but is blocked where Group Policy enforces an execution policy; `-Command -` from stdin runs line by line and does not stop on errors.
- A direct-argv fast path like the POSIX one would skip PowerShell startup for literal commands, but PowerShell aliases (`curl`, `sort`, `where`) resolve differently from the executables, so it needs its own specification first.

Verification: `tests/compat/test_cross_platform.py` (encoding, limit), `tests/desktop/test_desktop_agent.py` (selection on a simulated Windows host), and `tests/desktop/test_desktop_exec_streaming.py`, which runs the real bootstrap through the supervisor under PowerShell 7 when `pwsh` is on PATH (exit statuses, syntax errors, UTF-8, no ANSI), the verbatim cmd command line, and split multibyte output. Windows PowerShell 5.1, cmd.exe and `CREATE_NO_WINDOW` are not executed by the Linux suite; the dangerous-command and delete guards still parse commands with POSIX grammar.


## Desktop shell selection

Problem: the argv builder treated every space, quote and backslash as shell syntax, so each multi-word command, including one simple command whose only metacharacters were quoted data, ran under `/bin/sh -c`. Where `/bin/sh` is dash (Debian/Ubuntu), the shell stays the parent: a command killed by SIGTERM was reported as the shell's status 143 plus a shell-printed `Terminated` line instead of its signed status -15. Bash hosts hid this because bash execs a lone `-c` command. Lone words took the opposite branch and ran without the shell that gives them meaning: builtins (`cd`, `umask`), `NAME=value`, and a trailing carriage return that `shlex` dropped.

Decision: `lib/desktop_agent/_exec.py` tokenizes with POSIX quoting (blanks, backslash, single and double quotes). Exactly one simple command whose words are literal runs as that argv without a shell; quoted metacharacters are data. Unquoted operators, redirections and here-documents, `$`/backtick expansion, patterns, tilde, comments, braces, `!`, newlines and continuations, `NAME=value` prefixes, control characters, unterminated quotes, an empty command name, and reserved words or builtins in command position keep the explicit `/bin/sh -c` path. A direct command reports its own exit status or negative signal. The shell path reports the shell's own status unchanged: POSIX exposes a signalled last command as 128+N, and dash prints the signal name. This POSIX path is the `sh` shell; Windows selects among shells as described in the next section. For parity with the shell, commands receive `PWD` naming their working directory; a relative `cwd` resolves against the agent's directory, not the worker's source root. In desktop mode the supervisor applies the execvp rule that runs a file without a recognized header as a sh script. A direct command that cannot start (not found, permission) reports the supervisor's typed start error and status 125, not the shell's 127/126 message.

Alternatives considered:
- Rewriting 128+N as -N is ambiguous, because a program may exit 143 and ksh93 reports 256+N.
- Prefixing `exec` to user text, or using a `sh -c 'exec "$0" "$@"'` trampoline for every simple command, gives exec-failure parity but puts a shell back in front of every simple command, including lone words that never used one.
- `shlex.split` does not follow POSIX (it keeps the backslash in `"\$"`) and reports no quoting, so it cannot tell data from syntax.
- Checking PATH and file headers in the agent races the real exec.

Verification:
- Host-shell differential specs compare the direct argv with the words `/bin/sh` assigns through `set --`.
- End-to-end specs cover quoted metacharacters reaching the program with its exit status, the program's own -15 with clean stderr through all four desktop/project synchronous and streamed entries, the shell's own -15 and `$?` 143, lone builtins, a script without an interpreter line, and agent-relative `cwd` with `PWD`.
- A Windows contract spec pins the PowerShell default (previous section).
- A random 14,000-case differential (8,359 commands accepted for direct argv) against dash, bash, bash invoked as `sh` and `bash --posix` found no mismatch.
- Native Windows execution is not exercised.


## Addressing one of several devices

Problem: desktop tools carried no target device, and the bridge refuses an
unaddressed command while the owner has more than one online device, so two of
the owner's machines could not both be controlled. The registration frame sent
only `sys.platform`, so the model could not tell which shell a device runs and
wrote POSIX commands for Windows devices.

Decision: every desktop tool schema (`lib/desktop_tools.py`) takes an optional
`device`: an agent ID or a case-insensitive device name, resolved by
`lib.desktop.bridge.resolve_device_target` with the rules `await_device` uses
(`match_devices`: exact agent ID first, else name; several matches are
ambiguous). Matching runs only over the caller's own devices, so another
owner's device is never resolved or named in an error. A match becomes the
command's `target_agent_id`; `device` never reaches the agent. An unknown,
ambiguous or offline selector, or an unaddressed call while several devices are
online, fails before enqueue with a listing of the owner's online devices
(name, agent ID, OS and release, architecture, default and available shells);
an offline match also names `await_device`. The agent frame carries an optional
`system` object (`SystemFacts` in `contracts/desktop_bridge_v3.schema.json`):
OS family, release, version, architecture and `shells` (`available` among
`sh`/`powershell`/`cmd`, plus `default`). Detection mirrors what the executor
can run: Windows always lists PowerShell (default) and cmd, plus sh when
`_windows_posix_shell()` finds one; POSIX lists sh (default), plus PowerShell
when `pwsh` is on PATH. The server stores only the declared fields of a valid
object; a frame without `system` (older agent) keeps the previous facts, a
malformed one clears them. Device lists (`/api/v4/desktop/status`, `/devices`)
return it as `system` (null when unreported). While a desktop tool is
available, the `environment` context block lists each online owner device on
one sorted, timestamp-free line, so the text changes only when the device set
or its facts change, and conversations without desktop tools keep
byte-identical context.

Alternatives considered:
- Selecting the most recently seen device for unaddressed calls is the
  latest-device shortcut the bridge forbids and silently runs on the wrong
  machine.
- Putting device facts into tool descriptions changes the cached tool prefix
  whenever a device connects; a dedicated context block would need its own
  registry entry and localized presentation for the same few lines the
  environment block already carries next to the browser capability.
- Reporting only `sys.platform` and deriving shells on the server cannot see
  whether Git for Windows' sh or pwsh is installed on the device.

Verification: `tests/desktop/test_desktop_device_selection.py` (addressed call
with two online devices, by name and ID; unaddressed listing; foreign-owner
name and ID never resolve; ambiguity; offline refusal; old frame without
`system`; malformed/oversized facts dropped; agent frame detection on simulated
Windows/POSIX hosts; schema; environment lines). Real Windows hosts are not
exercised.

## Waiting for an offline device

Problem: work that must run on one of the owner's devices (desktop tools or a
remote worktree) failed while that device was offline, and nothing continued it
when the device returned; the owner had to notice the reconnect and ask again.

Decision: `await_device(device, reason)` registers one durable wait for the
current conversation and the model ends its turn. `lib/desktop/device_waits.py`
owns the owner-scoped records in portable record CAS
(`contracts/device_waits_v1.schema.json`): one record per wait for its whole
lifetime, plus a pending pointer `<owner>:<deviceKey>:<conversationId>` that
exists only while the wait is pending, so a connection reads only the waits
naming that device and a conversation holds one pending wait per device.
`device` is an agent ID or a device name; a known device binds to its agent ID,
an unknown selector matches the first of the owner's devices that connects with
that ID or name (case-insensitive). If the device is online, nothing is
registered.

`register_agent` reports a device's online transition once per connection
epoch: its first poll in this process, a poll after the connection window
lapsed, or a poll under another owner. The poll route then wakes every pending
wait of that owner naming the device through `dispatch_scheduled_turn`: one
`proactive` nonhuman notice, `steer` into a busy conversation, and a command ID
hashed from (owner, wait ID), so a replay after a crash, reconnect or restart
reuses the command the conversation already received. `started`, `steered` and
`queued` close the wait as delivered (agent, epoch, command); a missing
conversation cancels it; a durable notice that no executor started fails it
visibly; any other outcome or failure leaves it pending for the device's next
connection, without failing the poll. The wake continues with the registering
turn's execution choices: the continuation tool choices and creating-turn
policy shared with `timer_create` (`CONTINUATION_TOOL_CHOICES`, and
`CONTINUATION_POLICY_KEYS` through `capture_continuation_policy`: model route,
reasoning, prompt, interaction mode, tool exposure, workspace roots and owner
preference mappings, replayed as one `continuation_policy` override so the wake
runs as the agent that registered it; a wait recorded without a model identity
keeps the conversation's own selection), the awaited capability
(`desktopEnabled`, a validated `project_remote` binding) and `schedulerEnabled`.
Identity, grant and attended fields and a caller's tool-schema list are never
captured; the notice is not owner text and grants no authority, and device tools
keep their approvals. A conversation in Plan mode defers the wake (`busy`): it
stays pending for the device's next connection instead of running outside the
read-only plan (`docs/modules/scheduling_ops.md`).

Only a conversation whose turn the task owns can wait. The tool is offered where
device tools are, and to Coordinators and Routers through the coordinator
ceiling (it executes nothing on a device). Headless requests, child invocations
and conversation-less tasks are refused like other deferred lanes
([task engine](task_engine.md#background-command-delivery-owner)); project
Workers are denied (`workerDeniedTools`) because the ordinary turn command
discards the assignment grant, so a wake would run outside the grant's tool
ceiling and workspace. Workers report the dependency instead. Offline device
tool errors name `await_device` only when the task can wait.

The owner lists pending waits (`GET /api/v4/desktop/device-waits`, which pages
the pending index; `includeClosed=true` pages every record in registration
order) and cancels one (`POST /api/v4/desktop/device-waits/<waitId>/cancel`,
idempotent; another owner's wait is not found) through the API; the Settings
→ Devices list left the UI with computer control on 2026-11-17. Deleting
a conversation cancels its pending waits; restoring it does not revive them.
Nothing is polled and nothing expires; closed records stay as owner history.
Pointers are written before records, so a delivery keeps a pointer whose record
does not exist yet: the registration still in progress delivers itself if the
device is online by then, and an interrupted one is completed by registering
again.

Alternatives considered:
- A `timer_create` watcher polling device presence: adds a poll interval, a
  timeout and a second presence check beside the bridge registry.
- Waking from every heartbeat or the online list: repeats work on every poll;
  the connection epoch names the transition, and the derived command ID makes
  replays idempotent across processes and restarts.
- Keeping waits in the in-memory bridge registry: lost on restart, while a wait
  must outlive both the turn and the process.
- Letting Workers wait: their wake would lose the grant
  (`test_a_wake_into_a_worker_conversation_could_not_carry_its_grant`).

Limits: delivery runs once per connection epoch, so a storage failure during
delivery leaves the wait pending until the device connects again or the server
restarts; the wait stays listed and cancellable meanwhile. Wait IDs order by
registration millisecond.
