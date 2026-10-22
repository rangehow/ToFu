# Internal MLP environment provider

Private optional distribution. This directory is below the existing whole-bundle
public-export exclusion. The public SDK has no import or dependency on it.

Install the public SDK first, then this directory. Install the optional
`official-auth` extra only when using the existing official Agent-auth package.
Expose MLP/Hope through the public SDK MCP binding when platform tools are
needed. Native compatibility adapters opt in separately as `environments` and
`compute`; neither is enabled merely by installation. Providers resolve lazily.
No authentication or allocation happens at import/install.

## Trusted identity composition

The host must supply an actor-aware credential source. It is deliberately not
accepted from model tool arguments or request configuration. For an explicitly
bound official Agent identity, trusted application bootstrap can compose:

```python
from tofu_environments import Actor
from tofu_environments.host import configure_provider_context
from tofu_meituan_environments import OfficialAgentCredentialSource

# Resolve these IDs from authenticated deployment identity authority.
actor = Actor(owner_id=authenticated_owner_id, tenant_id=authenticated_tenant_id)
configure_provider_context(credential_source=OfficialAgentCredentialSource(actor))
```

Restore the approved binding during host bootstrap; another owner/tenant fails
before the auth CLI is invoked. Multi-account hosts can provide a source that
resolves each actor's identity through their credential repository. Do not bind
a guessed numeric owner or reuse an ambient CLI login for unrelated users.
Headless Agent authorization can use the official non-browser flow; this adapter
does not need the user to share passwords or browser cookies. Tickets stay in
headers/transient memory; errors and connection results omit them.

## Image and CodeLab workflow

`environment_catalog(provider="mlp", project=...)` reads base records and queues.
Plans require an explicit image base ID and version-record `base_revision`, queue,
CPU and memory in GiB. Set `provider_options.hadoop_account` to the reviewed
platform account; project-name stripping is not an identity mapping.
`provider_options.use_sankuai` is optional and boolean. Workspace scope is personal;
shared/production deployment, arbitrary port maps and local binds are rejected.

The native image builder accepts Dockerfile extensions in config components.
Local COPY/ADD contexts are not supported. For deploying Tofu, first prepare
reviewed release artifacts in an accessible artifact location or a compatible
base image, then use approved RUN instructions to install them. Preserve the
platform's CodeLab bootstrap components. This adapter does not implement the
separate Harbor registry-distribution pipeline or upload local working trees.

`project_development` and `project_training` create image → build/push → image
admission → CodeLab → IDE. `tofu_service` additionally requires an exact argv
`startup_command` and `service_port`; it starts the approved command through the
owned Jupyter terminal and checks the application readiness endpoint through
CodeLab's service proxy. It never counts shell submission or IDE-ready as service
ready. Startup commands must launch the approved application in the prepared image.

Platform IDs, memory units, build components and endpoints follow the reviewed
MLP CLI 0.3.60. Owner/operation markers guard all subsequent reads. Existing
running-workspace limits and image admission are checked before allocation.
Unknown outcomes require marker-based read-only reconciliation; failed lookup
or incomplete pagination leaves uncertainty instead of another creation request.

## Acceptance boundary

Offline fixtures cover create/build/admission, CodeLab payload, exact ownership,
unknown replies, terminal acknowledgement versus echoed input, cleanup, verified
TLS and credential isolation. They do not certify a live build or allocated
CodeLab. Each installation must test its actual base image, Hadoop account,
queue permissions, platform response shape and proxy readiness route. No live
resource has been created merely by installing the package or running these tests.

Run from the checkout:

```sh
PYTHONPATH=packages/tofu-environments:bundles/meituan/environment-provider:. python -m pytest bundles/meituan/environment-provider/tests
```


## Hope batch provider

Installing this distribution declares `hope` in `tofu.compute_providers`.
`hope.hope_provider_factory` requires trusted host ports `hope_mcp_call`,
`hope_prepare_job`, `hope_job_evidence`, and `hope_job_outputs`. Missing ports
fail before allocation. The MCP port receives Actor and must use its owned
bridge; mutations pass `retry_on_disconnect=False`. No ambient Hope CLI login
is selected and credential values never enter job plans.

The preparer returns a sealed workdir/include list, source/configuration digests,
binding revision and `limits_enforced=True`. That assertion must follow actual
source, command, CPU/GPU/memory and runtime/output-limit validation; it is not
a bypass flag for untrusted callers. Prepared content is rechecked before
packaging. Explicit usergroup, job type, queue and priority are required; P0
also requires a resource code. Quota retry is disabled.

The evidence port must prove actor, project and the `tofu:` operation marker
for status and reconciliation, and obtain a genuine process exit code. The
output port must read declared files from that exact job with bounded chunks
and deadlines. Submitter logs alone do not prove experiment completion.
This adapter is offline-contract tested; the host ports, platform file transfer
and live GPU acceptance are not yet configured or verified.

`cancel_job` first validates the exact owned job through the evidence port, then
uses Hope stop_job dry-run/one-use confirmation before the actual stop. The
stop response is only acknowledgement; terminal state must be read back.
No token is persisted and mutation transport replay stays disabled.

### Known-reference execution evidence

`hope_evidence.HopeEvidencePort(actor, repository, mcp_call,
expected_creator=...)` implements the evidence callback for references already
persisted by the scoped compute repository. The external creator comes from a
trusted actor/account binding, not model arguments. Owner, tenant, project,
plan seal, exact Hope reference, raw application identity and Worker lookup
must agree. An assigned Worker stays queued even if its AM reports Running.
An owned terminal failed/stopped Worker makes the operation failed even if the
platform application remains Running; its process exit code stays unknown until
independently observed. A successful Worker still waits for platform success.
Problem: a real checkpoint publication failure left the AM Running and the
observer waiting for a Worker that had already Failed. Decision: terminal Worker
failure takes precedence over nonterminal platform state. Alternative: trusting
only AM status delays failure analysis and hides stalled applications. This does
not automatically stop the application or authorize replay.

Success requires terminal platform and Worker success, one complete structured
runner receipt, zero exit, exact declared output names/hashes/byte bounds,
finite elapsed time within the workload limit, and actual CPU/memory/GPU/worker
allocation matching the plan. Truncated/missing evidence fails closed. Multiple
Worker attempts are rejected for the single-attempt protocol. Artifact bytes
still use the separate owned output port; receipts alone do not collect them.

Problem: real submit tools can return a known reference while acceptance polling
times out, and text/structured blocks can be duplicated or prefixed `MCP Error:`.
Decision: decode only agreeing JSON objects and preserve a reference explicitly
in SUBMITTING state for observation. A proven UNSUBMITTED rejection remains a
failure. Alternatives: generic JSON parsing lost known references; replaying
uncertain writes risks duplicate jobs. Unknown-reference reconciliation remains
an explicit read-only callback and is never replaced by numeric-ID guessing.

Observed deployment limits: the current ml-easy-job submitter rejected a
4,966,224,868-byte code package with `File oversize, should be less than 1GB!`.
Heavy model/data inputs need a separate declared artifact transport. This is a
recorded platform restriction, not a fabricated universal host default. Current
GPU workers also failed public Hugging Face DNS resolution; external HTTPS
weight download has not been established as an available input route.

## Hope source preparation port

Problem: the Hope job adapter previously required every host to reimplement
owned ZIP extraction, resource rendering and worker-limit enforcement. Decision:
`hope_source.HopeSourcePreparer` implements that port using the public SDK's
verified ZIP boundary and bounded POSIX worker runner. A trusted, actor-bound
profile supplies account, queue, image reference and scheduler GPU field. Model
arguments cannot supply that profile. CPU, memory and GPU count remain explicit
in the sealed job specification; the rendered configuration is `tofu-job.hope`.

The host supplies capacity-derived archive/file/aggregate staging bounds and owns
one preparer's request/run lifetime. Repeated preparation verifies both the source
archive and staged files before returning the same admission. Call `close()` only
after all dependent operations settle; it removes private staging, never source
archives. No background worker or persistent cache is created. Aggregate staged
bytes cannot exceed the declared host bound. Durable restart recovery needs a
host-owned staging repository; this scoped implementation does not claim it.

Alternatives: per-experiment shell generators duplicate identity and limit checks;
a module-global cache obscures ownership and leaks staged data. Neither is used.
This component does not install providers, wire host bootstrap or implement MCP,
evidence and output ports; those remain required for a complete live deployment.

## Hope MCP transport port

`hope_mcp.HopeMCPPort` accepts trusted bridge and configuration factories that
receive the full authenticated actor, including tenant and declared project.
The bridge factory must supply an exclusive connection/lease owned by this port;
it must not hand out a shared global connection that another task can disconnect.
The configuration factory enforces credential ownership. There is no ambient
account fallback and no login/process startup at import or construction.

Problem: experiment-specific MCP connection code duplicated scope, reconnect and
teardown decisions. Decision: one lazy, bounded, actor-scoped transport permits
only the declared job/readback tools, rejects write replay before connecting,
and preserves uncertainty when an acknowledged response is lost. `close()` waits
for active calls before disposing the connection. Alternative: sharing an ambient
bridge across owners or disconnecting an in-flight write risks cross-task effects
and ambiguous allocation. No resident thread or cache is added. The host owns the
port lifetime; durable job claims/approval remain in the public JobService.

This callable can supply `hope_mcp_call`. Source, evidence and output ports still
need trusted composition and a live host acceptance run; this module alone does
not activate a compute provider or restart a running application.

The lazy MCP read allowlist includes `get_job_attempts`, which the known-reference
evidence port requires. A composed provider/transport/evidence test verifies
Worker receipts and actual allocation without bypassing the transport boundary.
This composition is offline verified; deployment and live acceptance remain separate.

## Hope output integrity port

Problem: generic collection hashes received files, but its first collection must
also match the actual successful Worker's result receipt. Decision:
`hope_outputs.HopeOutputPort` checks the scoped operation seal, exact persisted
run, declared filename, fresh terminal proof and Worker size/hash before admitting
an output stream. Hash or size disagreement fails collection and the generic
collector removes its partial staging. No result cache or background worker is
added; chunks are bounded to 1 MiB and read callbacks receive one absolute
monotonic deadline and the expected byte count. Transport callbacks must enforce
that deadline inside blocking I/O; checks between chunks cannot interrupt a
blocked transport. The host owns transport closure and remote namespace binding.

The trusted callbacks are `prove_success(actor, reference, operation_id,
deadline=...)`, `read_receipt` with the same arguments (complete Worker stdout),
and `read_bytes(actor, reference, operation_id, name, deadline=...,
maximum_bytes=...)`. The proof callback must validate actual resources as well
as owned terminal execution. The bytes callback resolves only the exact owned
published namespace; it accepts no model-supplied URI. This port can supply
`hope_job_outputs` but does not itself publish files or implement Hadoop I/O.

Alternative: trusting locally computed first-collection hashes can admit changed
remote files even when Worker evidence listed different results. Installing this
port alone does not complete artifact publication, runtime bootstrap or live
`compute_collect` acceptance; those remain explicit deployment gates.

## Native Hadoop artifact reader

Problem: the observed Hope MCP catalog supplies directory and log reads, but no
arbitrary artifact byte download. Decision: `hope_artifacts.HopeArtifactReader`
supplies the byte callback for `HopeOutputPort` using an explicitly configured
Hadoop CLI and actor-bound subprocess environment. `owned_artifact_uri` derives a
namespace from owner/tenant scope, project and canonical operation ID, underneath
a trusted host root. Both worker publication and host collection must use that
same function/binding. Only declared files in sealed successful operations are
read; model arguments cannot select the root, account or arbitrary remote URI.

Each read launches one foreground CLI and drains stdout/stderr without a resident
pool, cache or temporary artifact. Output chunks use at most 64 KiB, expected size
cannot exceed the approved result budget, silent processes have a monotonic
deadline, and closing or failing the stream kills and reaps its process group.
CLI stderr is drained without retaining or exposing credential-bearing errors.
The caller supplies the CLI argv and scoped environment; the implementation never
selects an ambient account. Tests exercise actual subprocess pipes, early close,
timeout, wrong bytes and composition with generic collection. These are transport
contract checks, not a measurement of remote Hadoop throughput or GPU acceptance.

Alternatives: returning presigned URLs exposes access material; buffering whole
adapter files consumes memory proportional to model size; reading arbitrary
caller paths bypasses ownership. Neither is admitted. The reader needs files
published into its derived namespace. Publication, namespace permissions, account
identity and actual host bootstrap still require live acceptance; this module
alone does not activate the provider or publish outputs.

## Worker result publication

Problem: hashing files in the worker does not make them available to host
collection, and assigning remote destinations after plan authorization would
leave them outside the admission seal. Decision: JobProvider admission and the
source preparer receive the exact operation ID before planning. An optional
trusted `artifact_profile={root_uri, command}` on `HopeSourcePreparer` renders
`tofu-publication.json` and the standalone `hope_publish` wrapper into the sealed
source package. Its namespace uses the same owner/project/operation derivation
as the artifact reader. Separate operations have separate destinations even for
identical experiment sources. Profile changes require a new admission.

The wrapper runs the generic experiment, snapshots files while rechecking their
Worker hashes, records an exclusive publication intent before any remote write,
creates a new namespace without overwrite, sets mode 0700, then uploads only
declared snapshots. A final successful Worker receipt is emitted only after all
uploads succeed. Publication failure or unknown acceptance leaves a failed
receipt and retained intent, never automatic overwrite or replay. Files from a
failed workload are not published. Execution, hashing and publication share the
same runtime deadline; subprocess groups are reclaimed on timeout. Temporary
snapshot storage is bounded by declared result bytes and reclaimed at exit;
remote successful artifacts are durable user results, not an evictable cache.
Large-file transport can be enabled only by the trusted host's optional
`artifact_profile.parallel_upload={jar_path, sha256, threshold_bytes}`. The
absolute local JAR is bounded to 16 MiB, pinned, copied into the reserved sealed
package, and checked again before publication intent or network writes. Build
its retained `java/CheckpointFileTransport.java` with Java 6 bytecode (`javac
-source 1.6 -target 1.6`) and include all generated classes. The Hadoop launcher's
own runtime/configuration remains the authority; direct Java launch is not an
equivalent authenticated environment. No compiler or network runs at import.

Publication derives concurrent lanes from the sealed requested CPU count. Large
snapshots stream in 128 MiB parts through the existing Hadoop launcher; the
resolved backend concatenates those parts and renames the complete file to its
original declared URI. Smaller files retain CLI put. The shared deadline kills
the owned uploader group; failure retains the exclusive intent and has no
fallback/replay. Collection still streams and verifies every declared byte/hash.
Problem: a real 25.5 GB checkpoint hit the sequential-upload deadline. Decision:
use a host-admitted optional backend transport preserving logical output files.
Alternatives: silently reducing checkpoint contents loses exact optimizer/RNG
recovery; unsealed destinations or retrying partial writes break admission.
One live 512 MiB same-file trial measured CLI 181.08s versus four-lane 48.71s,
with both complete remote hashes checked. This supports the next full-checkpoint
experiment; it is not full 25.5 GB recovery or statistical throughput acceptance.

The configured root must already exist and its account/permissions must be
verified by trusted deployment. This adds no resident worker or persistent host
cache. Offline tests use real subprocesses and a filesystem transport fixture;
remote throughput, cluster admission and restart acceptance remain live gates.

Alternatives: assigning a destination only at submit changes approved intent;
reusing a source-only namespace collides across operations; overwriting a failed
partial publication obscures uncertainty. These are rejected. Without an artifact
profile the preparer retains its non-publishing mode for embeddings; that mode
cannot claim complete output collection. Source staging is still request-scoped
and production bootstrap remains required.

## Composed provider entry point

`hope_provider_factory` can consume one trusted `hope_binding_loader(actor)`
instead of four independently implemented low-level ports. The binding fields
are scope, project_root, profile, artifact_profile, expected_creator, environment,
mcp_configuration, mcp_timeout_seconds and output_timeout_seconds. These values
come from authenticated deployment composition, never task/model arguments.
Scope/project mismatch fails before opening a bridge. The loader must validate
account ownership, artifact-root permissions and configuration provenance.

The public host supplies its repository, capacity bounds and per-call cleanup
registrar. The private composition builds the existing source/publication,
MCP, Worker-evidence and artifact ports and registers owned teardown. Bridges
are exclusive and lazy; by default a fresh MCPBridge is constructed for the
explicit owner, rather than disconnecting a globally shared bridge. A host can
inject an equivalent exclusive hope_bridge_factory. Output evidence calls share
one deadline and use the same creator/resource proof as job status. Secrets in
the binding environment/MCP configuration are not copied into plan admission.

Problem: production composition duplicated the acceptance script's port wiring.
Decision: one actor-bound factory owns this wiring and lifecycle. Alternatives:
reimplementing each callback in every host causes drift; ambient shared bridges
obscure ownership. Existing complete four-port embeddings remain supported;
partial legacy configuration fails rather than silently switching paths.
This entry point adds no account defaults, resident cache or import-time network.
It still requires a deployment binding loader and live host activation; installing
it does not itself authorize cluster jobs or prove universal onboarding.

## Owned host configuration

When no legacy callbacks or explicit binding loader are supplied, the installed
Hope factory lazily loads `data/config/compute/hope/<actor.scope>.json`. The scope
is the SDK owner/tenant hash. The machine-readable authority is
`hope_profile.schema.json`. The version1 document requires version=1, owner_id,
tenant_id, projects (explicit absolute allowlist), profile, artifact_profile,
expected_creator, environment, mcp_timeout_seconds and output_timeout_seconds.
Use a regular, non-symlink mode0600 file, at most64KiB. Duplicate/nonfinite JSON
and owner/tenant/project mismatches fail before MCP credentials are read. Missing
profiles report hope_profile_missing before connection or allocation.

The operator verifies account identity, queue/image, artifact-root permissions
and the actor-specific Hadoop environment. No environment is automatically
inherited; the profile environment must provide its explicit runtime/credential
bindings. MCP configuration comes from the existing explicit-owner Hope store
and must be enabled. Profile secrets and MCP configuration are not copied into
plan JSON or task arguments. The profile is trusted configuration, not proof of
valid platform credentials or a login service.

Problem: production needed an injected experiment callback to obtain a binding.
Decision: discoverable lazy owner configuration avoids private host dependencies
in core packages. Alternatives: global account constants or task-supplied profiles
bypass ownership and are rejected. No resident worker/cache is added. Deploy the
updated SDK lifecycle and provider together, configure the owned profile, then
verify actual tools after normal restart. File configuration alone does not prove
live host activation or universal account onboarding.

## Stable admission across tool calls

Problem: a temporary workdir included in plan admission changed when the next
native tool call reconstructed sources, rejecting every cross-call submission.
Decision: Hope admission excludes the ephemeral workdir and seals the complete
prepared file-name/SHA256 map as prepared_sha256, including runtime and publication
wrappers. Submit reconstructs and validates the same source/configuration/content
seal, then packages its current private workdir. Cleanup remains per-call; no
persistent staging cache or global lock is added. Tests close the planning
preparer, submit through a new service/preparer and reject changed runtime bytes
before allocation. Alternatives: retaining random directories leaks cache;
ignoring wrapper content permits an unapproved runtime change. Neither is used.
Older planned admissions containing workdir require explicit replanning under a
new request ID after confirming the original remains unsubmitted. Existing known
queued/running references are observed without replay or admission migration.


## Durable Hope worker receipts

Problem: a successful Worker can publish data while the platform drops its final
stdout line. Platform success alone cannot admit scientific output, and replaying
the job would duplicate work.

Decision: new prepared sources seal `hope_worker_receipt_contract.json`. After
publishing declared outputs, the Worker commits the same frozen result JSON to
`__tofu_worker_receipt_v1.json` in its owner/project/operation namespace, then
prints it. The evidence port reads this reserved control file with bounded I/O.
Both transports use the existing receipt validator; conflicting complete stdout
and native receipts fail closed even when the log is marked truncated. Platform,
creator, Worker, allocated resources, execution limits and collected output
hashes remain mandatory. Legacy sources without the sealed contract continue to
require their original complete stdout receipt.

The control file is at most 1 MiB per successful operation, retained with that
operation's durable artifacts. There is no resident cache or background worker.
`elapsed_seconds` covers execution and data publication until control commit
begins; control commit uses the remaining execution deadline. This is not an
exact cluster billing measurement. Unknown control writes are never replayed;
a failed acknowledgement is reported as failed/unknown and any contradictory
success evidence is rejected.

Alternatives considered: accepting platform success loses process/output proof;
reconstructing a receipt from data invents execution evidence; resubmitting after
lost logs duplicates allocation; a second receipt schema creates competing
acceptance authorities. All are rejected.


Control-receipt reads use at most 300 seconds even when a trusted profile permits
longer transfers for large scientific artifacts. Problem: using the artifact
transfer budget as the receipt budget rejected valid long-transfer profiles
before planning. Decision: retain the overall profile transfer deadline and cap
only the small control read. Raising its bound with large artifacts was rejected
because the receipt remains at most 1 MiB.
