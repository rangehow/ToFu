# Environment providers

`packages/tofu-environments/` is an optional, independently installed SDK and
MCP service and native compatibility adapters for image/workspace provisioning
and remote batch compute. Its public
contract is `tofu_environments/spec.schema.json`; provider implementations conform
to `tofu_environments.contracts.Provider`. Neither schema nor workflow assumes a
company, cloud, registry or login system. The core agent wheel has no dependency
on this package. Installing it does not start Docker, authenticate or allocate.

## Design decision

**Problem:** image builders, workspace platforms and authentication differ across
installations. A company-specific tool in the core would make public releases
require internal infrastructure; independent scripts would duplicate approval,
ownership and failure recovery.

**Decision:** keep one workflow in a standalone public SDK, a reference Docker
provider in that SDK, and platform implementations in separately installed
packages. Private distributions belong in release-excluded bundles. Discover
providers lazily through `tofu.environment_providers`, using an authenticated actor
and trusted host context. A provider can compose a registry builder and a separate
workspace scheduler without changing the public workflow.

**Alternatives considered:** a universal platform HTTP client cannot normalize
platform-specific identity, quotas and readiness; direct shell skills do not
provide durable claims or exact approval; embedding company adapters in the core
would contaminate public artifacts and add startup dependencies.

## Installation and opt-in

Platform integration uses the independently installed stdio MCP entry point:

```sh
python -m pip install './packages/tofu-environments[mcp]'
tofu-environments-mcp --factory deployment.environments:binding
```

First-party requirement ranges must admit the in-repo version of each
first-party dependency (`tests/meta/test_first_party_requirements.py`); the
internal provider's floors (`tofu-environments>=0.2`, `tofu-mtsso-auth>=0.3`)
also keep an older installed release from satisfying it. Install a provider
together with the in-repo packages it needs, for example the Hope provider:

```sh
python -m pip install ./packages/tofu-environments ./packages/tofu-mtsso-auth \
  './bundles/yourprovider/environment-provider[official-auth]'
```

**Problem:** `pip install` copies a distribution into the interpreter
environment, and nothing on the restart path refreshed the copy. Providers
import application modules (`lib.*`) lazily, so a fix that reached the checkout
never reached a deployment running from it. On 2026-11-12
`compute_catalog(provider=hope)` failed with `ModuleNotFoundError: No module
named 'lib.runtime_paths'` from a tofu-yourprovider-environments 0.1.0 copy
installed on 2026-10-11, beside a tofu-environments 0.1.0 copy without
`capacity` or `configured_compute_providers`.

**Decision:** installation stays the opt-in; the checkout that boots Tofu
supplies the code. `lib/runtime/checkout_distributions.py` declares the
first-party distributions that import application modules
(`CHECKOUT_DISTRIBUTIONS`: tofu-environments, tofu-yourprovider-environments). For
each one installed in the interpreter, `server.py` puts its member directory
ahead of the installed copy on `sys.path` and describes it with the name,
version and entry points of the tracked `pyproject.toml`. The directory joins
the pinned application import roots and the execution plan hashes the same
roots, so a provider change replaces the execution host at the next restart.
A member that is not installed is never offered; a build left in its directory
is not an install. A provider that still cannot import (for example a missing
third-party dependency) makes the provider tools return `provider_unavailable`
naming the distribution, its version and the missing module.

**Alternatives considered:** a restart-preflight `pip install` pinned to each
version and content hash rewrites the shared interpreter environment while the
previous generation still imports from it, and adds a package-manager failure
to every restart. An import self-check that refuses a mismatched copy only
makes the outage clearer; every change still needs a manual reinstall.
Editable installs keep the entry points of install time and the path of
whichever checkout ran the install.

Metadata is read once at boot. Like other non-Python resources, a change to
`pyproject.toml` alone is outside the execution plan identity and reaches a
preserved execution host at its next replacement. Reinstall only to change
which distributions are installed or their third-party dependencies; installs
change the execution generation, so the next restart replaces the execution host.

The trusted deployment factory returns `mcp_adapter.MCPBinding(actor,
environment_service=..., job_service=..., authorize=..., close=...)`. Actor,
project, repository and provider resolvers come from the deployment's identity,
storage and credential owners, never model arguments or an ambient login.
The binding uses the existing `EnvironmentService` and `JobService`; it adds no
journal, submission loop, provider or default resource allocation. A configured
service contributes only its tool family. Shared schemas live in `schemas.py`.
The `authorize(actor, tool_name, exact_arguments)` policy must consume a one-use
approved grant for that exact preview; absent policy refuses every external
write. MCP connection or a valid schema is never authorization. Server shutdown
calls the optional owned cleanup port. Blocking SDK calls borrow MCP's worker
boundary and cancellation joins them rather than replaying a lost write.

`compute_collect` uses only the trusted bound project destination;
model arguments cannot select another root. Ownership, CAS claims, unknown
outcome reconciliation, exit-code evidence and output seals remain SDK-owned.

The native `tofu.tools` compatibility entry points are separately named
`environments` and `compute`. Enable only the needed adapter via `config.plugins`.
Without configured services or trusted `environment_provider_ids` /
`compute_provider_ids` in `configure_provider_context`, only the native provider
discovery tool is offered. Installing a provider is not proof of usable actor
credentials. Native collection requires an attached project. No deployment-wide opt-in is added.
The SDK package and existing private YourProvider providers remain optional and
release-separated. A deployment still owns live platform acceptance.

| Tool | Behavior |
|---|---|
| `environment_providers` | Installed provider names; no daemon or authentication |
| `environment_catalog` | Available image references/revisions, queues and constraints |
| `environment_plan` | Validate explicit resources and persist an immutable preview |
| `environment_apply` | Advance one approved write stage |
| `environment_status` | Read progress or reconcile an uncertain result |
| `compute_catalog` | Read actor-bound compute queues, resource snapshots and option schemas; no allocation |
| `compute_plan` | Seal an explicit bounded batch job |
| `compute_submit` | Submit one exact approved job without replay |
| `compute_status` | Read exit status or reconcile uncertain submission |
| `compute_cancel` | Request exact authorized owned-job cancellation |
| `compute_collect` | Collect successful owned outputs into the active project |

Profiles are `tofu_service`, `project_development` and `project_training`. CPU,
memory in GiB, owned project and build instructions are explicit. Service mode
requires an application readiness endpoint. A running container or IDE is not
sufficient evidence that Tofu is ready.

## Ownership, approval and recovery

The host derives owner identity from the authenticated task, never model arguments.
Tenant identity and declared project root accompany every operation. Repository
keys contain the actor scope; all durable calls use the configured semantic storage
client. The package does not select a storage backend or use SQL. The host registers
its declarative `tofu.environments` manifest. `MemoryRepository` is an explicitly
transient embedding option and returns `durable: false`.

Plan hashes bind provider, actor scope, project root, specification and stable
admission evidence. `environment_apply` includes the full preview and validates
its hash against durable state. It consumes a one-use gateway approval receipt
bound to tool name, call ID and exact arguments. Manual approval displays generic
argument risk fields; existing Auto mode policy can authorize writes. The SDK's
`authorize` callback is the equivalent boundary for trusted embeddings, and must
be implemented by the caller's policy rather than unconditionally accepting
untrusted requests.

Before each external write, a CAS claim persists its intent and deadline. A lost
response or checkpoint never triggers automatic replay. Status waits for live
claims, then asks the provider to prove exact ownership and operation markers.
Unprovable outcomes stay uncertain. Reconciliation cannot declare the application
ready: the normal readiness probe must establish that result. There is no implicit
resource deletion or automatic replacement operation.

## Docker reference implementation

Use an immutable `sha256:` image ID or registry `@sha256:` digest from the catalog.
Build instructions extend that base and cannot contain a replacement `FROM`.
The context must lie inside the declared project; symlinks and special files are
rejected. Admission seals content, paths and modes, including ignored files.
Before Docker starts, the provider copies a bounded private context, verifies the
same seal and builds that snapshot. Temporary copies are reclaimed on completion
or failure. Ignored files may require a safe replan because the provider deliberately
does not duplicate Docker's `.dockerignore` semantics.

Local binds must be inside the project. Published ports bind loopback only. GPU
requests require the explicit NVIDIA runtime. Operation/owner labels guard every
readback and reconciliation. Docker remains a trusted configured daemon; it is
not a tenant security boundary. The default CLI runner supports POSIX; other
operating systems can inject a runner with equivalent cancellation/process cleanup.

## Third-party extension

Create a separately installable package declaring:

```toml
[project.entry-points."tofu.environment_providers"]
my_platform = "my_package:provider_factory"
```

`provider_factory(actor, context)` returns an API-v1 provider. The host resolves it
only when requested. Use the reference implementation and lifecycle fault tests
in `packages/tofu-environments/tests/` as executable examples. Implement catalog,
stable admission evidence, image create/build/status, workspace create/activate/
status and read-only reconciliation. Build receives the sealed `admission` evidence.
Do not include volatile queue availability in it; recheck availability before
allocation. For every mutation, distinguish proven rejection from unknown outcome.
Set `EnvironmentError(..., uncertain=True)` for lost acknowledgements and timeouts.

Keep provider credentials out of schemas, plan evidence, connections, logs and
error text. A trusted host can call `configure_provider_context` to supply an
actor-aware credential source. Providers may use their own declared trusted
configuration repository; credential sources must enforce owner/tenant binding
before accessing credentials. Never infer an account from a project name or use
an ambient personal login for arbitrary owners. Connections contain safe URLs
and identifiers, not tokens. Vendor options are isolated in `provider_options`
and validated by that provider. Unsupported options fail rather than disappear.

## Capacity and observability

The installed-wheel smoke measurement on CPython 3.12 was approximately 58 ms
for importing the SDK, with zero additional worker threads. This is an SDK import
sample, not an application startup benchmark. No persistent worker, polling
thread or daemon is added. A fresh CPython 3.12 / MCP 2.0 process measured
server construction at approximately 1.45 s with one thread before and after;
this optional SDK import sample is not an application startup benchmark.
Descriptor registration imports no MCP SDK; calls borrow its existing worker
boundary rather than adding a Tofu pool. The host reuses its
cached launch resource probe and derives output/context bounds from available
memory and volume headroom, and rechecks the staging volume before copying.
Explicit trusted runner/provider overrides remain. Standalone SDK defaults are
lean bounded fallbacks. Commands have deadlines, bounded output, process-group
teardown and cancellation. HTTP adapters must enforce verified TLS, bounded
responses/deadlines and no credential-bearing redirects.

The workflow logger `tofu.environments` records stage claims, settled phases,
status transitions, operation IDs, actor-scope hashes and safe error codes. It
never logs recipes or credentials. Durable records retain external IDs and claim
state for incident diagnosis; no state is silently evicted. Transient embedding
repositories reject new operations when their configured capacity is exhausted.

Focused validation:

```sh
PYTHONPATH=packages/tofu-environments:. python -m pytest packages/tofu-environments/tests
```

These tests include real host storage restart/CAS, exact approval receipts,
concurrent claims, checkpoint failures, process timeout and changed-context rejection.
Platform fixtures establish adapter contracts; a deployment still needs acceptance
against its own credentials, queues, image admission and readiness route.

## Batch compute lifecycle (embedding API)

**Problem:** provisioning a workspace does not execute a bounded experiment or
prove its exit status. GPU jobs need a separate provider lifecycle with the same
ownership, exact authorization and durable no-replay guarantees.

**Decision:** `jobs.JobService` reuses the existing scoped CAS repository and
canonical plan seals. `job.schema.json` defines source digest/URI, argv,
resources, runtime, total GPU-seconds and result-byte limits, expected outputs
and isolated provider options. Job keys have a distinct `job-` prefix.
`JobProvider` owns admission, source verification, remote limit enforcement,
submission, exact-operation reconciliation and status readback. References have
explicit `kind` and `id`; adapters must not interchange scheduler/app/run IDs.
Success requires a proven zero exit code. No scheduler or polling thread is added.
Record size retains the existing 256 KiB bound and storage lifecycle.

**Alternatives considered:** reusing workspace readiness would conflate a running
container with completed experiment output; direct CLI submission would lose
scoped recovery and uncertain-write protection.

Native compute tools use their own opt-in `compute` plugin.
`environment_providers` also lists installed `tofu.compute_providers` entries
without loading them. Factories accept `(actor, context)` and return a
`JobProvider`; trusted actor-aware credentials use the same host context as
environment providers. `compute_submit` consumes the existing gateway one-use
receipt bound to the full displayed plan, call ID and exact arguments.
No provider is installed implicitly. This implementation does
not yet have configured Hope host ports or live GPU acceptance.
`JobService.collect` requires proven job success and streams declared outputs
into a private new directory under a trusted destination. Total collected bytes
are bounded by the plan, each file is SHA256-sealed, and receipts are checkpointed
through scoped CAS. Changed terminal outputs fail against earlier receipts.
Failed reads or checkpoints remove local partial snapshots through pinned
no-follow POSIX directory handles. Cleanup uses Python 3.10 descriptor-relative
APIs, not `shutil.rmtree(dir_fd=...)`, and preserves the primary failure if
cleanup also fails. Collection is read-only at the provider and adds no
persistent polling worker. `read_job_output` adapters
remain responsible for exact owned-job binding, deadlines and remote transport. Declared cost bounds must be enforced by each real adapter; schema
validation alone does not enforce remote cost or seal a source file.

The private `bundles/yourprovider/environment-provider` distribution now declares a
`hope` compute entry point. Its adapter uses explicit actor-bound host ports for
MCP calls, sealed job preparation, exact-operation/exit evidence and output
reads. It disables transport replay and quota retries on submission, preserves
Hope run ID type, and rejects unconfigured ports. Host composition and live
acceptance remain outstanding; installing the package alone cannot allocate.
The SDK contains no Hope hosts, login mechanism or platform credentials.

The public POSIX `job_runner` entrypoint executes real argv under a wall deadline
and reclaims its process group, including descendants that retain output pipes.
Captured stdout/stderr and declared result reads share the approved result-byte
bound. File hashes and the real exit code are emitted as a machine-readable
receipt. This does not enforce scheduler resources or temporary disk use, and
is not an untrusted-code sandbox; platform preparation must enforce allocation
and bind source/operation identity before declaring admission.

Cancellation has its own durable CAS claim. Unknown stop outcomes remain
`cancel_uncertain`; queued/running status cannot erase that claim and permit
replay. Only provider terminal readback settles it. Native `compute_cancel`
uses an exact displayed-plan gateway receipt. The private Hope adapter first
proves the owned operation, obtains the platform dry-run confirmation, then
consumes it once with transport replay disabled. Confirmation tokens are not
persisted in job records. Deployment must still arrange deadline monitoring;
this primitive does not create an automatic watchdog.

The SDK checks repeated output bytes against an existing seal; a concurrent
collection CAS conflict propagates. The experiment consumer reclaims the
private collection snapshot before linking complete, synced file bytes
atomically without replacement.
The link makes the result visible; directory sync separately confirms durability.
If that sync fails, the visible result is preserved and marked `needs_attention`,
not scored. Failure to unlink the temporary name is reported separately and
cannot erase visibility or override the sync outcome. A later pass verifies
existing bytes and retries sync without resubmitting the job. The Stop guard
samples just before linking; it is not atomic with the link. A Stop first
observed after publication does not prove when the request arrived, and does
not undo a visible link. Analysis consumes the decoded, digest-bound receipt
itself; any checkpoint copy must agree by JSON type and value. Snapshot cleanup
is descriptor-relative using Python 3.10 APIs and preserves a primary error if
cleanup fails. None of these mechanisms establishes untrusted-code isolation.

Experiment smoke and the CLI's `experiment` and `pilot` commands remain blocked
pending independent provider implementation and admission. Every registered
cell requires a durable, owned, successful receipt; a local file or a subset
of the cells is not enough.

`JobService.inspect` and initial Stop-intent persistence use only the owned
local record and verify its plan seal. Planned cancellation prevents future
submission. For an in-flight or uncertain submission, persisted intent survives
provider resolution failure and compatible submit-settlement CAS conflicts.
`reconcile_reference` checkpoints recovered identity before any optional status
read; `status` never issues a stop. An embedding can explicitly advance only an
existing exact-plan intent via `progress_cancellation`. A definitively rejected
stop needs fresh authorization; a claimed or uncertain stop is not replayed.
A `requested` intent is not evidence of external cancellation, drain, or native
runtime parity.

Trusted embeddings may supply `submit(..., before_submit=check)` as a current
admission check, not an authorization grant. A final check follows the last
blocking record read before physical submit. A post-claim refusal records a
non-submission; local settlement may retry CAS without repeating the provider
write. Separating local inspection and intent from external progress avoids
making local control depend on provider availability.

## Batch source archive boundary

**Problem:** convenient compute submission must not let a source archive escape
its owned project or staging directory, change after hashing, or exhaust storage
through unbounded expansion.

**Decision:** `tofu_environments.job_sources.extract_job_source` accepts an owned
local ZIP plus pinned SHA-256 and caller-provided byte/entry limits. It reads a
private unlinked snapshot and extracts regular files into the caller's empty
private staging directory. The caller retains ownership of staging cleanup;
this helper adds no persistent cache, daemon or repository authority. Capacity
bounds come from the existing host context probe. Snapshot plus expanded inputs
need transient disk capacity; the caller must reserve it before preparation.

**Alternatives considered:** direct `extractall` does not enforce this contract;
reopening a hashed mutable archive leaves a race. A module-owned indefinite
cache would add persistent growth and another lifecycle authority. This source
primitive does not replace provider resource validation or runtime evidence.

Verification includes source mutation after hashing, compressed expansion,
archive paths and symlinks, owner/project containment, and exact file receipts.

### Native result delivery

**Problem:** the embedding API could collect results, but a Tofu task could not
finish its compute cycle through the native tool surface.

**Decision:** expose `compute_collect(operation_id)` through the existing plugin.
The host chooses a fresh mode-0700 directory inside the explicit actor project;
model arguments cannot select destinations or remote paths. Existing JobService
ownership, proven success, byte bounds, CAS and stable SHA256 receipts remain
the authority. Failed staging is removed; successful artifacts are durable user
results and are retained until the user/project lifecycle removes them. No
resident worker/cache is added; retained bytes per collection are bounded by
the approved result limit and the returned receipt reports actual bytes.

**Alternatives considered:** requiring a separate Python script leaves the agent
cycle incomplete; arbitrary destination arguments introduce an unnecessary file
write authority. Collection reads an existing job and never allocates compute,
so it does not consume a new external submission/cancellation approval. The
provider must still implement bounded owned output transport.

## Per-call provider composition lifecycle

Problem: an installed compute entry point still needs the selected repository,
and temporary source/MCP ports must not leak across tool calls. Decision: the
host passes its existing `HostRepository` as trusted `context.repository` and
an ExitStack callback registrar as `context.register_cleanup`. The registrar's
lifetime is exactly one native tool handler call, including failure paths; cleanup
runs after provider calls settle. Third-party providers register only resources
they own. Explicitly injected services remain owned by their embedding and are
not closed by the host. Cleanup failure stays visible; durable job claims remain
authoritative and never become permission to resubmit. No resident pool, cache or
persistent data is added. Existing capacity-derived staging bounds still apply.

Alternatives: process-global connections obscure owner/project lifetimes;
unconditional closure of an injected service can interrupt other tasks;
constructing another repository inside a vendor provider bypasses the selected
storage authority. None is used. Installed providers still require trusted account
and artifact bindings; these lifecycle ports alone do not activate Hope.

## Bounded scheduler preemption recovery

**Problem:** an owned scheduler can evict a running Worker and replace it even
when application failover is disabled. Treating every replacement as duplicated
submission prevents long jobs from completing on preemptible capacity.

**Decision:** `job.schema.json` permits an explicit `recovery` declaration:
`{"max_attempts": 3, "restart_safe": true, "allowed_reason": "scheduler_preemption"}`.
Status proof and output receipt selection both use `select_worker_attempt`;
collection selects the admitted final Worker after the same full history and
resource checks, rather than imposing an extra one-Worker-only gate.
Omitting it preserves one-attempt admission. `runtime_seconds` remains a per-attempt
execution bound; `gpu_seconds` must cover GPU count times that bound times all
allowed attempts. The opt-in is an assertion that restarting the command cannot
repeat unsafe external effects. It does not create another submission or retry an
uncertain write.

Hope accepts only unique, sequential Worker identities with closed earlier
attempts and the platform's exact scheduler-preemption diagnostic. Unknown
failures, overlapping workers and excessive attempts break the sealed plan, and
so does allocation above the declared GPU budget: a Worker holds its cards from
`runTime` to `finishedTime` (or to the observation clock while it runs), and the
running time of all attempts times the sealed GPU count must stay within
`gpu_seconds`. Plans without `recovery` are bounded by the runner's runtime
deadline instead. Every admitted Worker's `AFO_RESOURCE_CONFIG` is compared with
the sealed resources; final output still requires the existing zero exit, owned
publication and full size/SHA checks. A preempted Worker awaiting a replacement
remains queued while the attempt allowance remains. What a broken plan does
depends on whether the application has finished (next decision).

**Alternatives considered:** admitting arbitrary failed attempts would hide
application errors and replay side effects; insisting on one Worker would reject
normal scheduler recovery; resubmitting after a lost reply could duplicate work.

The cluster may override its configured retry count for infrastructure eviction.
An embedding must keep an owned status watch and cancel the exact operation if
attempt admission or allocation bounds fail. Cancellation proves the durable
owner/reference and platform identity independently of execution admission, so a
rejected execution remains stoppable. Closing a failed operation retains its
prior exit/error evidence in `execution_failure_before_cancellation`.
Definitively rejected submissions without an external reference retain their
failed state and produce no cancellation request. The policy is an admission and
supervision contract, not a cluster reservation or a guarantee that startup and
scheduler teardown cannot exceed a limit. Allocation durations are capacity-time
evidence, not a billing receipt. Live preemption recovery must be demonstrated
separately from fixture tests.

### A finished job reads finished; a breached plan raises a contract breach

**Problem:** every attempt and allocation failure was one transient error,
`hope_preemption_unverified`, raised before any terminal branch. Status reads of
Hope job 63394260 kept failing from 2026-11-03 to 2026-11-13, although it had
SUCCEEDED on 10-13 and held no card. Its one Worker was requested at 05:27:04 (`startTime`)
and ran from 07:42:15 (`runTime`) to 08:15:25. The check charged the 2 h 15 min
queue wait as GPU time (10,101 s instead of 1,990 s). Closed facts never change,
so the read could never succeed, and a still-running job in that position would
have kept its cards with nothing able to consume its result.

**Decision:**
- Allocation runs from `runTime`: queue wait holds no card, an attempt that
  never ran holds nothing, and run times that cannot be measured are incomplete
  evidence (`hope_allocation_unverified`). Evidence the platform has not finished
  writing (identity, start or end time, a previous attempt still closing) stays
  the transient `hope_preemption_unverified`.
- Closed facts the sealed plan never admits raise the SDK's `ContractBreach`
  while the application runs: repeated execution of a single-attempt plan,
  attempts beyond the allowance, shared identities, reversed or overlapping
  attempts, a replacement after anything but verified preemption, and running
  time above the budget.
- A finished application (SUCCEEDED, FAILED, FINISHED) always reads finished.
  Overrun and resource evidence that disagrees with the plan travels as typed
  `discrepancies` (`[{code, message}]`) on the terminal snapshot, which
  `JobService.status` keeps on the job record and the continuation receives. An
  inadmissible attempt history reads `failed` with its discrepancy, so its output
  is never admitted as the sealed execution. A success with an overrun or a
  resource mismatch stays a success: output admission rests on the zero exit,
  the Worker receipt and the output hashes, not on capacity evidence.

**Alternatives considered:**
- Treating every `hope_preemption_unverified` as terminal: stops jobs whose
  evidence is only late.
- Reading a finished inadmissible history as succeeded: admits a replay the plan
  never authorized.
- Stopping inside `JobService.status`: gives a read a stop side effect, so
  `compute_status` would stop jobs without approval.
- Counting from `startTime` with a startup allowance: an invented constant.

Verify with `bundles/yourprovider/environment-provider/tests/test_hope_attempts.py`,
`test_hope_evidence.py` and `packages/tofu-environments/tests/test_jobs.py`.

### Queues that force a per-worker GPU count

**Problem:** some Hope queues allocate a fixed number of cards per worker
whatever the request. `h910c_training_data` refuses any other request with
`resource is forced to allocate 16, current request = N`. Plan sealing knew no
such rule, so a plan sealed below it passed approval and failed only at
submission. A queue that rounds up instead would run the job with cards outside
its sealed budget. Hope offers no read for the rule: on 2026-11-13
`get_queue_resource` and `list_available_queues` returned capacity only, and
`lint_hope_config` accepted one card on that queue.

**Decision:** the trusted Hope profile already binds the queue and its GPU field,
so it also declares this platform rule as `profile.forced_worker_gpu_count`
(`hope_profile.schema.json`). A profile whose queue forces a count must declare
it; there is no default. Source preparation refuses a plan whose
`resources.gpu_count` differs with `hope_forced_allocation`, before anything is
staged or persisted, so the owner never approves a plan the queue would refuse.
`validate_job_spec` already makes `gpu_seconds` cover that count times the
runtime times the attempts.

**Alternatives considered:**
- Reading the rule from Hope: no API exposes it.
- Learning it from the first refusal: that comes after approval and submission.
- Hardcoding queue names: company constants in code that go stale.
- A Tofu setting: the rule belongs to the queue binding, which is the profile.

## Benchmark batch launcher

`evaluations/platform_jobs/` packages the committed Harbor runner through the
public JobService contract. Its request schema pins dataset/task/engine/source
identity and budgets; Git object proofs validate exported source without .git.
The worker runs doctor before model dispatch and retains failed trial evidence.
Wrapper success permits diagnostic publication; benchmark state, child exit and
formal audit remain separate, with no inferred score. Scope, exact-plan approval,
uncertain-write reconciliation and output ownership stay in JobService/provider
ports. See [the launcher contract](../evaluations/platform_jobs/README.md).

CLI transport and source snapshots use private named temporary files, removed
on close. Unlinked temporary files failed writes/flushes on the observed FUSE
volume; snapshots remain pinned through extraction and cannot be reread from
mutable original archives. Subprocess deadlines and group teardown are unchanged.
Hope packages sealed top-level entries within the platform's 200-entry limit;
the recursive file manifest is hashed incrementally under admitted source bounds.
Links and special files in cached staging fail before packaging. This supports
large retained runners without weakening per-file source seals.

### Compute discovery

**Problem:** compute planning required queue and provider-option knowledge that
was unavailable through the public tools. **Decision:** `compute_catalog` uses
`compute_catalog.schema.json` and an optional provider `compute_catalog(actor)`
capability. Unsupported providers return `catalog_unsupported`; catalogs reject
wrong provider identity, nonfinite values and payloads above 64 KiB. Catalog
reads neither mutate the operation repository nor allocate resources.
Hope queries only the trusted actor/project-bound queue with `include_apps=false`,
projects resource counts and safe option defaults, and excludes image, credential,
application and environment data. Capacity is a transient snapshot, never a
sealed plan admission promise. **Alternatives considered:** raw MCP responses
would expose unnecessary fields; hardcoded queue defaults would bind the public
SDK to one company/user and become stale. No resident worker or cache is added.

## Native capacity views

The optional SDK host exposes `configured_compute_providers()` and
`read_compute_catalog(actor, provider, cancelled=...)` as read ports. They own
trusted composition, request cancellation and transport cleanup. Application
presenters never access host globals or construct provider credentials.
`capacity.queue_resources` is the shared pure queue-summary projection; strict
provider admission still rejects incomplete required quantities, while native
resource views may display unknown values explicitly.

An optional owner-bound MCP adapter admits only registered, enabled tools marked
read-only, with exact `compute_catalog` or `get_queue_resource` names. It validates
the registered input schema, excludes applications from queue reads, disables
transport replay and rechecks capability before calling. Provider discovery uses
explicit configured IDs/schema enums; raw queue selection requires its exact
name. No arbitrary MCP operation or model-authored endpoint is accepted.
