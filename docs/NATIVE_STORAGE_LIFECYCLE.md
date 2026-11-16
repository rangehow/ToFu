# Native storage across application restarts

## Problem and decision

Stopping the native database with every Web worker discards its caches and
repeats authority open, WAL recovery and projection open on each application
restart. A production observation before this change spent 111.267 seconds
opening the authority and 135.326 seconds in application storage recovery.
Those are separate phases, not a controlled performance baseline for this change.

For a Linux lifecycle-managed source checkout whose configured native transport
resolves to `daemon`, `server_manager.LifecycleManager` now owns a persistent
`serverctl_pkg.native_storage_host` process. That host reuses `StorageRuntime`
and `StorageSupervisor`; it owns the native child, its close-on-exec stdin lease,
crash budget, readiness handshake and graceful shutdown. Restarting the Web
worker closes only that worker's authenticated clients. Storage caches and
background engine ownership survive. The same host now retains an explicitly
selected SQL Sidecar across Web restarts, publishing its existing authenticated
v1 handoff; native transport keeps the v2 identity contract. Explicit embedded
transport and explicitly supplied external connection files retain their owners.

This supersedes the worker-only lifetime decision in
`STORAGE_TOFUDB.md` (“Worker restarts re-open the authority in-process”).
The backend, data-layout resolver and durability selection are unchanged.
No migration, fastpath activation or weaker durability is implicit.

## Identity and readiness

`contracts/native_storage_connection_v2.json` owns the private v2 connection
document. The host publishes it atomically with mode 0600 only after a native
health handshake. Attachment validates protocol, backend, owner/tenant, project/data root,
engine content identity and PID/start identity before authenticated health.
The active projection/fastpath descriptors accompany that same generation.
An application cannot rebuild an external owner's executable during attachment;
direct launches still prepare before starting their own database.

Problem: reattaching after transient health failures reverified the executable
on disk, although deployment preparation can replace it while the admitted
native process still serves. This rejected that same process and exhausted the
application's recovery budget into permanent fencing. Decision: initial native
attachment retains full source/content admission; subsequent attachments compare
the private connection document's engine digest with that original admission,
alongside scope, project/data root, PID/start identity and authenticated health.
They do not reopen the deployment artifact. Managed child launches still verify
the exact admitted bytes and pin, and a changed service engine still requires a
drained replacement. Alternatives rejected: adopting the latest disk digest
silently upgrades a live application; disabling the crash breaker hides repeated
failures; checking deployment files during reconnect confuses disk preparation
with running-process identity. The existing in-memory admission is reused;
there is no additional persistent state or retention.

The host's own relaunches run a private copy of the admitted bytes, so a staged
deployment never refuses them; an attachment detaches at once when the
published process exits but keeps a live, starved engine through the
unresponsiveness budget; attachments are never latched by the restart breaker;
and deterministic refusals report `refused` instead of crash pressure. The
decisions and the private-copy retention are in
[STORAGE_OPERATIONS.md](STORAGE_OPERATIONS.md#admitted-engines-start-refusals-and-liveness).

The project-local service record is reconstructible control metadata. A separate
OS lease covers host creation through final native shutdown. The manager passes
that lease descriptor to the child before publishing its PID/start identity.
A missing or corrupt record cannot certify that a held service lease is free.
The record is never a substitute for the actual storage authority lease.

## Restart, upgrade and failures

| Event | Ownership and outcome |
|---|---|
| Ordinary worker restart/crash | Keep the same host and native PID; reconnect and run the existing application recovery gates. |
| Manager executable reload | Same manager PID/start identity retains the host and database. |
| Actual manager death | Host observes lost PID/start identity, joins startup/restart work and drains native storage. A successor waits for the old storage lease, then restarts the same generation for the preserved worker; when the generation changed it drains that worker as a planned restart, whose replacement starts the current host first. |
| Native crash | Existing runtime fences readiness, applies its crash budget, starts the admitted engine from its private copy (whatever a deployment did to the configured path) and publishes a new token/PID. Clients detach on the exited PID and reattach through their existing recovery owner. |
| Starved but live engine | Neither monitor gives it up while it answers at any latency, nor before `TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS` without an answer, nor while the kernel holds one of its threads in a filesystem call; clients keep their attachment and resume when it answers. |
| Repeated native failures | Keep the terminal failure; automatic worker retries cannot reset the database crash budget. Explicit restart may replace the failed host. Attached clients are never latched themselves and reattach once a host publishes a healthy engine. |
| Unexpected host exit under a live manager | Latch a visible failure; do not respawn a new runtime with an empty crash budget on every reconciliation. Explicit restart may recover it. |
| Engine, scope, storage configuration or host lifecycle code changes | Prepare and validate while the old application serves; drain the worker before replacing its service. A live worker cannot adopt a different engine silently. |
| Full stop/offline maintenance | Stop worker, then host and native child. Maintenance still needs the real storage OS lease. |

The manager excludes a resident authority from orphan reaping only after proving
same-host manager/host PID identities and the authority's parent relationship.
A resident authority, native or SQL, may start or recover beside a live Web
lock only when that Web process names the same project and private connection
channel owned by the verified host. Foreign Web owners and maintenance leases
remain fenced, and the storage lease itself stays exclusive.

Alternatives rejected: leaving an unowned native child breaks shutdown and
maintenance; reusing the SQL v1 wire for native storage mismatches protocols;
skipping recovery/readiness gates can report unusable data as ready; silently
adopting a new engine under a live worker breaks configuration ownership.

Problem (2026-11-06): only the native launcher passed that verifier. The manager
spawns the Web worker right after the storage host, and `server.py` takes
`data/.server.lock` before application assembly, so the SQL Sidecar refused its
first start ("A Web process still owns project storage") and every crash or
successor relaunch under the live worker: a fresh managed Linux install on the
default SQLite backend never became ready. Unmanaged workers
(`TOFU_RUN_SERVER=1`) own their Sidecar child and were unaffected. Decision: the
SQL Sidecar entry point applies the same `serves_web_owner` verification when a
resident host launched it (`TOFU_STORAGE_SERVICE_ID`/`_STATE` plus its parent
PID). Alternatives rejected: awaiting storage readiness before spawning the
worker fixes only the first start and still refuses crash and successor
relaunches; skipping the Web-lock check for resident launches admits an
authority beside a foreign Web owner; taking the Web lock only after storage
attachment reopens the duplicate-worker startup race that lock closes. Verify
`test_resident_sql_authority_starts_and_recovers_under_its_web_owner`.

Problem (2026-11-05): a signalled Supervisor's host drained on manager EOF.
Its successor found the preserved worker alive, but the storage generation had
changed since that host started (host lifecycle code and generated storage
semantics). It refused to start another generation under the worker and asked
for an explicit restart that nothing performed; the worker retried attachment
for 1 h 40 min. Decision: when the departed host is gone, the authority lease is
free, its record names this host and a manager that is provably gone, and the
live worker attaches through that host's private connection file, the successor
drains the worker with the planned-restart path
(`stop(keep_desired_running=True)`). The next reconcile's planned recovery
replaces the record with the current generation, then spawns the worker. An
unchanged generation still restarts the host for the same worker. Failed
hosts, foreign hosts, another live manager and unbound workers keep the
explicit-restart boundary; the planned exit neither consumes nor resets a crash
budget. Alternatives rejected: starting the changed host under the live worker
adopts another generation silently (the worker also refuses a different engine
digest and stays fenced); keeping a host alive across manager death for
adoption needs a new handoff channel and an adoption timeout; waiting for an
operator leaves the outage unbounded. Verify
`test_successor_manager_restores_storage_for_the_preserved_worker` (a real
manager death with both generations).

The replacement identity is `native_storage_host.prepare_plan`: the admitted
engine path and digest, the search projection directory, the storage,
data-root and placement environment (`TOFU_STORAGE_*`, `TOFUDB_*`, `TOFU_DB_*`,
the data-root keys, `TOFU_LOCAL_DIR`, `XDG_STATE_HOME`), and the bytes of every
module the host runs while launching the native daemon: its lifecycle owners and
the automatic fastpath planner (`native_fastpath_auto.py`, `local_placement.py`,
`startup_budget.py`, `generated_v2_semantics.py`) and the search projection
launch preparation (`projection_paths.py`), whose capacity decision chooses the
directory each daemon launch receives. Problem (2026-10-27): the
planner runs in the resident host at every daemon launch, but its modules and
placement roots were outside the identity. A restart after changing only them
kept the old host, and a later daemon relaunch could import new planner code
under the old supervisor. Decision: any module or setting the host acts on at
daemon launch belongs in the identity; `tests/server/test_native_storage_host_plan.py`
varies each one. Alternatives rejected: hashing the whole repository replaces
the host, and pays a full authority reopen, on every application-only change;
importing the planner eagerly at host start removes skew within one host
lifetime but still leaves a stale host serving after the files change.

The manager's process lifecycle is shared through
`serverctl_pkg/resident_service.py`: PID/start identity, inherited exclusive
lease, private record publication and verified shutdown have one owner.
`NativeStorageService` supplies storage preparation, connection-file identity
and authority-lease recognition. The common lifecycle's source bytes also
participate in host replacement. This preserves the existing record format,
paths, crash budget and manager-exec behavior while making the same physical
ownership rules available to the execution host. A second independent PID/stop
implementation was rejected because it could disagree about physical release.

The historical native host/module and private-record names remain compatible
while the policy follows the selected backend. SQL generation preparation hashes
its storage implementation, authority configuration and configured secret-file
contents; only digests enter the private plan. It neither selects another store
nor enables the distributed preview. SQLite and PostgreSQL process tests attach,
write, detach, kill the owned SQL child and reconnect while the same storage host
retains the authority and rotates its transport token.

Headless execution uses the same resident owner and the common application
BootRunner/shutdown. A failed boot carries the common runner's phase and error
into the existing execution-service failure record, while shutdown still joins
application cleanup. Problem: reporting only `failed` hid the cause of an
executor outage behind a live API. Decision: reuse the runner's diagnostic
snapshot; a second boot-error state or log-parsing recovery path is unnecessary.
Without a recorded phase error the original status remains the diagnostic.
Verify `tests/server/test_execution_lifespan.py`.
Its generation covers Python application sources, the
planned interpreter's installed packages and launch policy; it excludes frontend
artifacts and reveals no secret values. Preparation
and application construction reject source changes. Lazy imports reuse
`PinnedImportSource`, retaining its documented limits for late package imports
and non-Python assets rather than copying a second application tree. Generation
discovery follows every importable package in those roots, including standalone
clients and worker packages; a separate package allowlist omitted valid owners.
The private execution record replaces one fixed file;
temporary preparation files are removed, and no source archive or history
journal is retained. A preparation measured 7,614 importable Python files in
7.842 seconds and a 336-byte compact plan. This is an inventory measurement,
not a throughput claim.

Problem: installed packages are executable source too, but the generation hashed
only checkout sources. Reinstalling a stale provider package (or upgrading any
dependency without a source change) therefore left the old host, with the old
modules already imported, serving across restarts. Decision: the plan also
records the runtime replica's environment fingerprint of the planned interpreter
(`describe_environment` plus `fingerprint` in `serverctl_pkg/runtime_replica.py`),
so installs, upgrades and removals replace the host at the next restart while
bytecode writes do not; a layout that cannot be described records none.
Alternatives rejected: hashing every installed file reads the whole environment
(10 GB here) per preparation; a second distribution-metadata scan would duplicate
the replica's install detector; requiring a full stop after installs keeps the
stale host as the default outcome. Measured on this FUSE host: 0.15 s to describe
the interpreter plus 0.46 s to fingerprint it per preparation.
Verify `tests/server/test_execution_plan.py`.

Problem: every launch composition (`managed_worker_environment`) runs
`install_process_resource_defaults`, which re-probes free memory and project-disk
free space and materializes about ten retention defaults sized from them
(`TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB`, `TOFU_RAW_ARCHIVE_BUDGET_MIB`, ...).
Both generation identities hashed those values, so they changed whenever free
memory or disk moved: the manager refused to recover a crashed execution host
("execution configuration or code changed; explicit restart required") and an
API restart replaced unchanged execution and storage hosts. The restart drill hit
it on every execution-host crash on a 32 GiB VM (2026-11-13); a 50 MiB change of
free disk on the production host changes four of the values. Decision: both
identities (`execution_plan.prepare_plan`, `native_storage_host.prepare_plan`)
hash the environment through `runtime_guards.without_materialized_resource_defaults`,
which drops only the names the provenance marker
(`TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS`) attributes to the probe. Explicit
settings, the marker and the policy version still count, so an operator change
or a resource-policy change still replaces the host. Alternatives rejected:
reusing one recorded observation per generation needs persisted manager state at
four composition sites and still re-probes at API restarts; quantizing the probe
only moves the boundary at which the same refusal happens. The first deploy
with this identity definition replaces storage once.
Verify `tests/server/test_execution_plan.py` and
`tests/server/test_native_storage_host_plan.py`.

Problem: a manager started in an older environment prepared and launched the
executor with its own interpreter, while importing the application activated
`.tofu_env.json` later. This changed executable identity inside an already
published resident process; the former file-style re-exec also lost the module
import root. Decision: execution preparation resolves the installer's executable
marker first (an absent or unusable optional marker retains the supplied
interpreter), and the resident launch uses the interpreter recorded in that
prepared plan. The module invocation and inherited manager/lease descriptors
remain unchanged. A marker edit after preparation cannot silently select another
launch interpreter. Recovery re-prepares against the current marker and retains
the existing generation fence. Refusals identify changed plan fields without
printing their values. Alternatives rejected: disabling environment activation
runs with stale dependencies; ignoring interpreter mismatches admits another
executable policy; removing the recovery fence can replay tasks under new code.
Verify `tests/server/test_execution_preparation.py`,
`tests/server/test_execution_service.py`, and
`tests/bootstrap/test_module_env_reexec.py`.

The personal Linux manager now composes the selected storage, a headless execution
host and an API host. An automatic native transport resolves to the shared daemon;
an explicitly embedded transport or separately assigned role retains its declared
topology. Both generations prepare before any old process drains. An unchanged
execution host survives API restart; storage replacement joins execution first,
and full stop joins execution before storage. No configured backend is substituted.

Browser and admission truth: the shared application boot projection also
consults the existing command-control owner's verified resident incarnation.
A ready API boot cannot report the whole application ready while that executor
is absent, departed, still starting, or has a mismatched control endpoint.
The full boot endpoint and health's compact boot block share this projection.
A verified live resident record in `starting` projects a running
`execution_service` phase, with no failure or restart advice; the existing
boot poll follows it to ready. An absent/departed/failed owner or mismatched
control endpoint projects a failed phase and points to `serverctl status`,
preserving the manager's recovery/restart authority. Problem: treating every
not-ready executor as failed misreported a successful restart during its
38-second recovery on 2026-11-16. Decision: the command-control owner derives
starting, ready and unavailable from one verified resident snapshot; probes
and commands share that verdict. Alternatives rejected: treating any starting
record as live hides a dead host; waiting a fixed grace interval guesses at
progress instead of checking process identity. The
existing boot gate refuses new mutating API requests before their handlers
while this dependency is unavailable. Read-only APIs, probes, lifecycle approval,
restart and shutdown remain available, and no accepted Turn is rewritten or
replayed by this gate. Readiness returns automatically when verified execution
returns. No second poller, recovery budget or persistent task state is added.
Alternatives rejected: storage readiness alone let every conversation wait
forever; blocking reads hides saved conversations during recovery; restarting
inside the request gate bypasses the lifecycle owner. Specifications:
`tests/app_factory/test_boot_gate.py` and
`tests/tasks_pkg/test_command_control_runtime.py`.

Problem: a healthy API can hide a dead executor. Decision: execution host exits
consume the manager's existing windowed failure budget once per service identity.
`ExecutionService` owns both that host's reconciliation and its readiness
projection. The manager supplies its existing clock, failure-accounting and
observation ports and retains the single `executionRecovery` record. Problem:
keeping these policies in the API manager split the execution lifetime across
owners. Moving them to the existing execution service keeps dependency-order
replacement in the manager without introducing another lifecycle state machine.
Recovery replaces only that host. After application startup has entered, it
requires the same prepared source/configuration generation. API liveness remains
visible while readiness reports the execution outage. An exhausted budget or a
changed launch policy still requires the existing explicit restart.
A killed host's command line disappears before its descriptors close, so its
lease can still be held at the first retry. `ServiceOwnerDraining` keeps that same
departure pending for `NATIVE_SERVICE_STOP_SECONDS`, without another failure
charge; only a lease still held after that budget requires the explicit restart.
Alternative rejected: giving each replacement a fresh budget permits an endless
crash loop; retiring the API on execution failure needlessly interrupts clients.
The bounded recovery record replaces one current value, not a history of tasks.

Problem: exclusive-storage validation first ran during boot, after the serving
generation had drained; a concurrent source change during construction then left
an executor that had never entered startup waiting for another manual restart.
Decision: execution preparation runs the existing static storage boundary before
any service drains. Boot retains that guard against later edits. Plan comparison
distinguishes checkout source from interpreter, installed dependencies and launch
policy without exposing values. Only construction can record
`source_changed_before_boot`; the catch ends before the common application
startup can admit tasks. A departed host with that witness may refresh source
under the existing failure budget after dependency preparation confirms the
resident storage generation still matches. Physical exit and the released lease
remain mandatory; the API and storage are preserved. Unknown/incomplete plans,
policy changes, changed storage or failures after startup cannot use this path.
Alternatives rejected: ignoring generation checks can replay work with another
policy; automatically restarting the entire service interrupts unrelated tasks;
a new retry loop would duplicate the manager's existing budget. This adds one
optional failure classification to the replaced private record, no retained
history, timer, setting or task registry. Verify plan, composition and lifespan
specifications plus the real-process recovery in `test_execution_service.py`.

An explicit restart retires an execution host whose close failed after it
exited. Problem (2026-11-06): an approved Settings restart stopped the API,
then replaced the execution host; its drain recorded `TimeoutError` and
exited, `ResidentService.stop` raised `execution shutdown failed`, and the
manager latched `crashloop` with nothing serving until a CLI restart found the
host gone and only retired its record. Decision: the explicit replacement
(`reset_failed`, which already resets failure latches) passes
`retire_failed_close`, so physical exit plus the released lease retire the
record and the recorded failure is logged. It still refuses while the host
lives or after the record changed identity; automatic recovery and full stop
keep raising. Alternatives rejected: retrying the start stage adds a retry
budget and still needs this retirement; recording `stopped` for a failed
drain hides the failure. Verify
`test_failed_close_is_preserved_unless_an_explicit_replacement_retires_it`
(real hosts) and `test_only_an_explicit_replacement_may_retire_a_failed_executor_close`.

An explicit replacement retires an unconfirmed native storage close the same
way (owner request 2026-11-06, "a refused restart must never leave Tofu
down"). Problem: the 22:34 restart stopped the API, then the storage host's
drain outlived its daemon budget, the host died without a terminal record,
and `native storage shutdown failed: clean close was not confirmed` latched
`crashloop` for 69 minutes. The strict latch protected nothing: the operator's
later restart only retired the same record, and its engine open replayed the
WAL exactly as after any crash. Decision: the explicit replacement passes
`retire_failed_close` to storage too; its successor opens the authority
through the engine's crash recovery, and the authority lease (with the
storage-blocker deferral above) still fences a daemon that outlived its host.
A host still alive after `NATIVE_SERVICE_STOP_SECONDS` during an explicit
replacement is killed and retired like any unconfirmed close (one more budget
for the kill and the lease release), as a worker past its shutdown budget is.
Full stop, automatic recovery and a host failure under a live manager keep
the strict latch (`test_stop_preserves_native_shutdown_failure`).
Alternatives rejected: keeping the latch for storage (an unconfirmed close is
a crash the engine already recovers; waiting for an operator only adds the
outage); raising the drain budget (uninterruptible network-volume I/O
exceeds any finite deadline); recording `stopped` for a failed close (hides
the failure the restart outcome now reports). Verify
`test_unconfirmed_storage_close_still_ends_with_a_serving_server`,
`test_storage_host_past_its_stop_budget_is_killed_and_replaced`
(`tests/server/test_restart_always_serves.py`, real hosts) and
`test_only_an_explicit_replacement_may_retire_a_failed_storage_close`. The
manager side of the contract is in
[infra runtime](modules/infra_runtime.md#assembly-and-lifecycle).
Verify `test_execution_composition.py` and `test_execution_application.py`;
the latter boots the common application against a real SQL authority while the
HTTP port is occupied, joins execution, and proves storage remains alive.

Problem: at the 2026-11-06 14:10 restart the execution host's background-service
join outlived its 10 s budget. The bare `TimeoutError` had an empty message, so
the host recorded `failed` with "TimeoutError: ". The manager then refused the
whole restart ("execution shutdown failed: TimeoutError: ", 14:11:13), and no
API host started until a manual restart at 14:52:37. Decision: the shutdown stack
names the owner still stopping in the timeout it raises
(`server_shutdown._stop_process_background_services`), so the recorded failure
says which drain was slow. Replacing a host whose close failed is the explicit
replacement's `retire_failed_close` above. Alternative rejected:
a longer drain budget still ends in the same refusal on a slower volume.
Verify `tests/server/test_execution_shutdown_failure.py`.
Restart activity protection now reads durable root jobs as well as local
carriers. Storage errors do not certify idle or consume an ordinary restart
approval; explicit force retains the existing approval boundary.

The resident protocol also passes an inherited manager-control pipe. Only the
manager owns its writer; its descriptor remains inheritable across manager exec
and is excluded from child `Popen` descriptors. The private record retains its
descriptor, device and inode so an exec'd manager can verify the same endpoint
and close only its own writer after physical host exit. Hosts verify the read
endpoint and treat EOF as withdrawal of manager ownership. This needs no new
polling schedule and works on the running kernel where `pidfd_open` returns
`ENOSYS`. It changes neither task authority nor the storage connection format.
The existing post-Popen PID publication handshake remains shared and bounded.

Draining the native daemon at host replacement or full stop gets
`restart_budget.NATIVE_DAEMON_STOP_SECONDS` (90 s); the manager's host stop
budget derives from it. Problem (2026-10-27): the host gave the daemon a fixed
25 s. On the FUSE authority a clean drain measured up to 13.4 s of maintenance
stop plus 17.6 s of closing checkpoint, so three of that day's seven stops were
SIGKILLed inside the checkpoint and the next open replayed the WAL for
51-102 s. Decision: the budget covers a measured slow drain with headroom; the
kill remains the backstop for a wedged daemon. Alternatives rejected: skipping
the closing checkpoint moves the same replay onto every open; keeping 25 s and
accepting replay costs more restart time than it saves and kills the authority
mid-checkpoint.

Problem (2026-11-12, GATE-PUBLISH-CARRY drill on a copy of the 4,096-segment
catalog): the drain's cost followed the number of staged blocks, not the
window's bytes. The adoption driver's stop waited 146 s for its in-flight
full-catalog publication (a segment rewrite of up to 256 MiB, or one loose
file and fsync per packed block), and the closing checkpoint
(`Engine::checkpoint`) then materialized every staged block of a 681 MB WAL
window under the authority mutex: at a full catalog a segment merge per chunk
or a loose file and fsync per block. It had not finished after 13 minutes.
Decision: the closing retirement runs the online rotation pipeline
synchronously (`Engine::retire_wal_window_for_stop`: a parked or prepared
rotation installs first, then the rest of the window seals, encodes into
indexed history segments and installs, and the retired WAL is reclaimed).
Staged carried payloads stay owned by indexed history exactly as after every
online rotation, so the stop writes the window's bytes once, in 4 MiB history
segments, and the next open replays nothing. The adoption driver's stop now
abandons its publication at the next block, before any CONTROL write
(`PreparedPackAdoption::cancel_on`): a `WouldBlock` with the stop raised
proves nothing landed, so the authority stays usable and the packed payloads
stay staged and WAL-carried for the closing retirement. Durability is
unchanged: both paths are the ones every online rotation and every failed
adoption already take. Alternatives rejected: a stop deadline that abandons
the checkpoint leaves the window to the next open (the drill's 681 MB window
opened in over 40 minutes); raising `NATIVE_DAEMON_STOP_SECONDS` only moves
the kill. The window size itself was a regression of the overlay drain (one
pass per WAL window, `docs/TOFUDB_WRITE_PATH_OVERLAY.md`). Verify
`daemon::tests::a_graceful_stop_retires_the_wal_window_into_indexed_history_without_materializing_staged_blocks`
and `stopping_the_adoption_driver_abandons_a_full_catalog_merge_before_publication`.

Before a planned restart the operator runs the read-only pre-stop check
`python3 scripts/tofudb_prestop_check.py` once ahead and again immediately
before restarting, and restarts only when both runs pass (exit 0; 1 fails, 2
cannot decide; `--json` for tooling, `--data-dir` for another authority).
It passes when the newest active WAL window, and its predecessor generation
while that file is still present (a sealed window the stop may still have to
install), are each within the rotation soft threshold
(`contracts/tofudb_ir_v1.json` `activeWalRotation.softThresholdBytes`). Older
generations are reported, never judged. Problem (2026-11-12): the stop's
work follows the WAL the engine still holds, not a constant. Windows of
550-681 MB took the drain past the 90 s budget into SIGKILL, and the next
open then replayed the whole window: 153 s of `engine_open` on production
after the 08:35 restart. Decision: gate the planned restart on the one
contract-defined bound of a healthy window, read from the files the engine
holds, so a lagging rotation is waited out rather than killed. Alternatives
rejected: a larger budget only moves the kill; a fixed wait before stopping
is a tuned interval with no contract behind it; decoding CONTROL in Python
would duplicate the engine's slot format. Verify
`tests/tofudb/test_tofudb_prestop_check.py`.

Problem (2026-11-06): on a saturated FUSE authority the daemon's SIGKILL
backstop was not reaped within its confirmation window. The
`subprocess.TimeoutExpired` escaped the resident host's finalizer before it
wrote its terminal record, so the record kept the previous `ready` state, the
manager reported an unconfirmed clean close, and the explicit restart latched
as a crash loop with the application down. Decision: an unreapable SIGKILL is
a reported terminal outcome ("leaving the unreapable child to the OS"), never a
new exception; the host always publishes a terminal record, and a failed drain
is recorded as `failed`; after replacing a host the manager waits for the
authority lease with the existing orphan-reap policy and defers the spawn
through the storage blocker until it clears. Alternatives rejected: raising the
stop budget (uninterruptible network-volume I/O can exceed any finite
deadline); starting the replacement while the lease is held (the engine is
refused and the worker boot fails); treating the stale record as a permanent
latch (the physical lease is usually released moments later).

Problem (2026-11-11): the same unreapable SIGKILL inside a running host fed
its crash path. The restart loop spawned five successors in 8.5 s, each died
on "Another storage sidecar holds the project lease", the breaker counted them
as a crash storm, and storage stayed `degraded` for 92 minutes. The kernel had
freed the killed engine's memory about five minutes after the kill. Decision:
the supervisor remembers the unreaped child, and `start()` raises
`StorageStartDeferred` until `poll()` reports the reap. The runtime retries
on its ordinary restart backoff without a breaker strike, and logs the reap
before the successor spawns. Every managed start also probes the lease's OS
lock first (`server_manager.read_storage_lease_status`, outside the supervisor
lock) and defers while any other process holds it. The same chain recurred at
23:53 the same night; both engines freed the lease about 50 s after the
breaker had latched. A third occurrence, 2026-11-12 06:13 CST, came before
this change was merged: the killed engine's memory was freed 8-19 s after the
breaker latched, and storage stayed fenced for 2 h 22 min until an explicit
restart at 08:35. The unreapable line records `/proc/<pid>` state, wchan
and kernel stack (or why each was unreadable) to tell a network-filesystem
stall from memory-cgroup reclaim, plus the held thread and file named by the
liveness verdict's own reader (`_engine_kernel_wait`). The fencing exit line says the child did not
exit rather than claiming a native-signal crash for the synthetic -1.
Alternatives rejected: a fixed wait (it cannot be derived from any contract,
and uninterruptible I/O exceeds any finite one); counting the lease-held
starts (a few seconds of kernel exit become a latch that only a manual restart
clears); a blocking `wait()` in the restart worker (it would outlive the
runtime's bounded stop).

## History growth and maintenance admission

Problem: paged history manifests can retain more sources than fit in one leaf,
but compaction previously rejected retained counts above the leaf capacity.
Automatic maintenance passes the total history count minus its current source
slice, so normal history growth could terminate the engine repeatedly and
exhaust its restart budget.

Decision: both authority and engine compaction accept every positive retention
count. The physical page encoder still enforces its page size, and automatic
pruning still preserves the overlay replay suffix and bounded source slice.
No durable data is discarded to satisfy a count limit; this changes no storage
format, retention policy, or resident worker count. Alternatives rejected:
raising the leaf constant postpones failure and confuses physical layout with
logical retention; clamping the replay-protected suffix can lose recovery data;
increasing restart attempts repeats the same deterministic failure.
Regression tests exercise counts beyond one leaf and `usize::MAX` through both
entry points, zero rejection, continued writes and reopen. Existing paged-history
and compaction fault tests cover physical publication and recovery.

## Retrying after a breaker trip

Problem: a tripped restart breaker latched storage until a human restarted
the process.

- 2026-11-11: two lease-held storms latched storage for 92 minutes.
- 2026-11-12 06:13: storage stayed fenced for 2 h 22 min, although the
  killed engine's memory was freed 8-19 s after the trip.
- Every alert path ran on the fenced storage, so nobody was told.

The latch turned a transient cause into a multi-hour outage.

Decision: the trip stays loud, and recovery becomes automatic.

- `StorageRuntime` enters `degraded`. It revokes readiness, logs CRITICAL
  once plus the periodic reminder, and calls its `on_degraded` hook once.
  The native storage host's hook starts the out-of-band alert's detached
  `notify` (docs/STORAGE_OPERATIONS.md, "Out-of-band outage alert").
- The restart worker keeps the start on the same `_restart_loop` path, so
  the lease probe, the unreaped-child fence and refusal rechecks apply
  unchanged. A deferral neither re-trips nor re-alerts.
- The first post-trip attempt waits `_RESTART_MAX_DELAY_SECONDS` (60 s).
  Each later one doubles, up to one attempt per breaker window (the existing
  `TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS`, 600 s by default). The cap
  comes from the breaker's own budget, not a new knob: post-trip spawn churn
  stays below the limit the breaker already accepts.
- A successful start clears `degraded`. The host publishes `ready` again
  and the application re-attaches as it does after any engine restart.
- Explicit `start()` stays refused while degraded, and an explicit process
  restart still retries at once.
- While degraded, the host record keeps `failed` (an explicit restart
  replaces a failed host) with `retrying: true`. The manager's verdict then
  names the automatic retry instead of "explicit restart required".

Alternatives rejected:

- Latch until a human restart: the outages above.
- Retry at the ordinary 60 s ceiling forever: ten starts per window, twice
  the breaker's own limit, so the breaker would no longer bound churn.
- A larger crash limit or window: a tuned value that still latches.
- Re-alerting on every failed post-trip attempt: one outage, one alert.

A child that passes the pre-spawn lease probe and then loses the flock race
still counts as a crash. LIVENESS proposed a fix: the launcher reports a typed
lease-held startup error on the readiness channel, and the supervisor maps it
to `StorageStartDeferred`. It is not implemented here.

Verification: `tests/storage/test_storage_restart_breaker.py`, which covers
the post-trip recovery, the backoff, the crash-storm worker and the hook
failure. Also `tests/server/test_native_storage_host_state.py` and the
verdict cases in `tests/server/test_server_manager.py`.

## Verification and measurement

`tests/server/test_native_storage_service.py` exercises real native clients and
manager transitions on disposable data. It verifies exact conversation recovery,
three detach/attach cycles with unchanged PID, native SIGKILL/token rotation,
manager exec retention, manager-only SIGKILL cleanup, warm worker restarts,
configuration replacement and full-stop lease release. Use a source-certified
`TOFU_TEST_TOFUDB_BIN`; tests never build an engine or open production storage.
`tests/storage/test_native_storage_connection.py` covers rejected identities,
failed-handshake client cleanup, reconnection after deployment artifact changes,
and cancellation winning a late handshake. The resident-service tests also
replace the executable by rename on disposable storage, then verify runtime
recovery, unchanged native PID and preserved conversation data, and that an
engine SIGKILL after such a deployment relaunches the admitted bytes and the
application reattaches to them.

One local disposable run measured native attachment at 0.656, 0.584 and 0.535
seconds, and complete minimal-worker restarts at 3.32 and 4.59 seconds. These
are component/minimal-application measurements, not production outage claims.
The full application retains task recovery, import, worker and readiness gates;
its separately measured timing must not be inferred from attachment latency.
After removing the synchronous optional login probe, two complete application
restarts measured 6.549 and 7.073 seconds from process spawn to ready, and
14.410 and 15.990 seconds from request to ready (including preparation while
the previous application still serves). A separate observation during concurrent
native tests measured an 11.870-second readiness outage and 28.884 seconds from
request to ready. These observations establish no production SLO or zero-downtime
claim; load and shutdown work affect the full chain.
The first activation, real manager death and engine upgrades still open storage.

Persistence and retention are specified in `STORAGE_OPERATIONS.md`.

## Application restarts never depend on the storage engine build

Problem (2026-11-11): every restart prepared the storage engine (Cargo build,
pin and source admission) in the same preflight as the frontend, and a failure
refused the whole restart. A broken or slow engine build therefore blocked
replacing application code, the very change needed to fix the fault, although
the resident host was still serving an admitted engine.

Decision: the manager prepares the storage engine and the frontend in separate
children (`serverctl.py prepare-frontend --parts storage-engine|frontend`).
When engine preparation fails and this manager owns a ready storage host
whose engine shares this source's storage contract (`native_engine.retention_refusal`,
see "Engine certification gate"), `LifecycleManager.restart` retains that host
(`NativeStorageService.retained_plan`) and replaces only the API and execution
processes. A refused uncertified engine, a failed operator override or an
engine bound to another (or no recorded) contract refuses before shutdown
instead: retention would run this source against an engine it may not share a
contract with. There is deliberately no
restart option for this (owner decision 2026-11-11: one restart, no modes);
the restart decides from the preparation result alone. Retention is recorded in the manager state (`storageRetention`,
also in `status`) and survives automatic relaunches until a restart prepares an
engine successfully, the host stops, it is no longer ready, or its engine no
longer shares the source's storage contract; then the next launch prepares the
current storage generation as before. Application processes
receive `TOFU_STORAGE_RETAINED_ENGINE_SHA256`, the retained host's admitted
digest; their attachment admits that digest instead of the checkout artifact,
and the published connection must still carry exactly it, with every other
identity check unchanged. In-app restarts no longer prepare the engine in the
API process; only the in-place re-exec fallback (no manager) still does,
because it reopens storage itself. Without a ready host an engine failure still
refuses before shutdown.

Alternatives rejected: skipping preparation silently on every restart would
leave engine changes unadmitted with no record; letting the worker adopt any
published digest drops the drained-replacement fence for engine upgrades;
moving engine builds into the background resolves nothing when the build is
broken. Verify `tests/server/test_execution_composition.py` (retention specs)
and `tests/storage/test_native_storage_connection.py` (retained attachment).

## Engine certification gate

Problem (2026-11-12): restart preparation treated "compiles from the checkout and
passes three disposable probes" as deployable, and every path that spawned a
worker ran it, including crash recovery. Engine-input commits land several times
a night from different sessions; each silently became the engine of the next
restart. The 00:28 restart compiled HEAD a017af788b and deployed it into
`target/certified/` although the manual gate (build, certify with and without
online background work, cargo specs, the native pytest subset, load and the
rollback drill) had certified other sources; its `build.json` even carried an
earlier deployment's evidence.

Decision: restarts deploy only certified engines.

- **Record.** `scripts/engine_certify.py` (owner `scripts/engine_certification/gate.py`)
  is the only writer. For one commit, in a private detached checkout, it runs
  every check in `serverctl_pkg.engine_certification.REQUIRED_CHECKS` and, only
  when all passed, publishes `packages/tofu-db/target/engine-certifications/
  <source_sha256>/<storage_contract_sha256>/` (`tofu-db`, its library, the
  isolated `build.json` and `certification.json` with every check's command,
  exit, duration, log digest and summary, the previous engines drilled, the
  fixture and the operator) by one atomic rename. A record is never replaced.
  Every run, passing or not, keeps its logs and `result.json` in
  `engine-certifications/runs/<run>/`. `serverctl_pkg/engine_certification.py`
  reads records and re-verifies the bytes, required check verdicts and the
  engine's reported source; it has no write path.
- **Storage contract identity.** `native_engine.storage_contract_sha256` hashes
  `contracts/storage_v2.json`, `contracts/native_storage_connection_v2.json`
  and every `contracts/*` entry of the engine's Cargo `external-inputs`.
  Records are keyed by engine source *and* contract because the contract covers
  files the engine fingerprint does not. Deployments record the contract of the
  tree they prepared in `build.json`; manual `deploy_tofudb_engine.py` runs
  record none, and new bytes never inherit the previous deployment's evidence,
  certification note or contract.
- **One decision for every door** (`native_engine._prepare_generation`, shared by
  `serverctl restart/start`, the manager's explicit start, in-place re-exec,
  direct launch, GUI update and `install.sh`): the deployed engine is HEAD's
  -> no change (reported as certified or not); HEAD's source has a passing record
  for HEAD's contract -> exactly those bytes deploy, with the record as evidence;
  otherwise nothing is built, downloaded or swapped. The deployed engine is kept
  when its deployment receipt or a record binds it to HEAD's contract, and
  preparation prints `engine at HEAD is uncertified: <fingerprint>, <commits>;
  kept <fingerprint>`; anything else raises `UncertifiedEngineRefusedError` with
  the exact next commands. Unknown contract identity is incompatibility (owner
  decision 2026-11-12: no Git-derived guess). Each decision is also one
  `TOFU_ENGINE_DECISION {json}` line, which the manager returns as
  `engineDecision` and `serverctl restart` prints.
- **Admission** (`native_engine.verify_admissible`) applies the same keep rule,
  so the kept engine starts; exact identity remains required for isolated
  certification (`--prepare-only`).
- **Operator override.** `serverctl.py restart --deploy-uncertified-engine REASON`
  is accepted only from an interactive terminal, never from the environment, a
  dotenv file or an approval token. serverctl audits the decision
  (`storage_engine.uncertified_override`) and passes it as an argument through
  the manager into one preparation; it is never stored for respawns. Only this
  path reuses a matching local build, downloads a release or compiles. The
  deployment is audited again (`storage_engine.uncertified_deployed`) and
  `build.json` says `UNCERTIFIED operator override <id> by <operator>: <reason>`.
  A failed override refuses the restart. `install.sh` passes the decision only
  for a first deployment, when nothing is deployed yet.
- **Automatic spawns** (crash, planned and lease recovery) prepare only the
  frontend; the new storage host's admission keeps or refuses the deployed
  engine. Starting the manager is not an engine decision either.

The gate (GATE-PUBLISH-CARRY's, confirmed 2026-11-12) passes only when: the
build reports the checkout's source; both isolated certifications pass; both
cargo runs pass; the native pytest subset passes, where only files with failures
or errors are rerun once serially and both results are kept (a serial pass is a
load/timing flake, a serial failure fails the gate); the load run ends in a
clean stop (no `child_exit state=timeout`, the engine's
`step=authority_checkpoint state=done`, every WAL 0 bytes); and in the rollback
drill every probe killed where intended (crash: three commits and the
packed-filter build unfinished; kill during shutdown: 30 commits and
`step=authority_checkpoint state=start`), every leg of every source served by
the candidate and each previous engine (the deployed one, the last certified
one, any `--previous-engine`) exits 0 with no refused read, saves its write, sees
every acknowledged row and verifies offline with `verdict=ok` and
`missing_blocks=0`, and all legs share one conversation digest. The load harness
stops storage with the production host's `NATIVE_DAEMON_STOP_SECONDS`;
`load_run.py` gives older harnesses the same budget.

Operating it: `.venv/bin/python3 scripts/engine_certify.py --fixture <authority>
[--commit REV] [--work-root DIR]`. The fixture is required (no hidden default):
a settled TofuDB authority directory (`CONTROL`, `payload-segments/`,
`blocks/`), for example a stopped copy of a production-shaped store such as the
4,096-segment bench ENGINE-CATALOG-HOLD produced. It is only read: snapshots
hard-link its immutable segment and block files and copy what the engine
rewrites, so the work root must be on the fixture's filesystem; the command
refuses otherwise rather than copying it. A run takes about as long as the
manual gate (offline verifies dominate). With a legacy deployment (no recorded
contract) a restart at an uncertified HEAD is refused until HEAD is certified,
the deployed engine's own commit is certified (`--commit`), or the operator
overrides once.

Alternatives rejected: running the gate inside restart preparation (1-2 h against
a 1,200 s budget); records in Git or backfilled from a worker's `/tmp`; automatic
rollback to the newest certified engine (downgrades cross on-disk format
boundaries); `storage_v2.json` alone as the contract (the compiled operation
catalog changes independently); a runtime-handshake digest (itself an engine
change); the override as an environment or dotenv setting (a delegable knob);
retention on an incompatible contract; making `deploy_tofudb_engine.py` refuse
uncertified bytes (it is also the pin-repair tool). Verify
`tests/server/test_engine_certification_gate.py`,
`tests/deploy/test_engine_certify.py`, the retention specs in
`tests/server/test_execution_composition.py` and
`tests/server/test_native_engine_update.py` (the override's build pipeline).
