# Offline portability comparison contracts

Owners: `evaluations/portability/{scenario,models,run,journal,report,__main__}.py`.
Offline specifications: `tests/evaluations/test_portability_*.py`.

## Current scope

This is a comparison harness, not proof of an autonomous paper engine or
cross-family equivalence. No built-in scenarios are advertised until their
modules ship. Callers can supply an existing `package.module:ATTRIBUTE`
containing a `Scenario`; missing aliases are refused rather than guessed.

A declaration binds scenario version, comparison version, task content, metric
and fixed judge. Every run also measures interpreter, installed distribution,
endpoint and host environment identity. Artifact paths are content-addressed by
these inputs and model/arm, then deployment evidence, with exclusive unique
attempt filenames. The root `results.jsonl` remains append-only. Older artifacts
and journal bytes are never replaced by a rerun or changed environment.
Reports group exact comparison namespaces and retain separate deployment rows;
only eligible attempts contribute deployment evidence. Earlier infrastructure
attempts cannot poison a later completed pair. Attested resume uses the same
validated journal prefix as reporting, then verifies the referenced artifact's
bytes, presence of a payload, and canonical binding to journal provenance,
score and attempt. Report loading excludes missing or invalid
linked artifacts as infrastructure.
The artifact file and directory entries are synced before appending its journal
link. A persistence failure is an unscored infrastructure attempt, not a zero
for agent performance. Backend collection retains referenced artifacts with the
journal. Legacy rows without verifiable comparison evidence appear separately
as unverified diagnostics and do not enter modern paired statistics. Each
modern infrastructure attempt is counted once: under its matched deployment
when known, or as unattributed. Historical artifact-storage errors are unscored
only when the writer's explicit marker or a matching historical persistence
trace identifies the operation. For historical truncation, a retained writer frame
must agree with the exact stored prefix at the old writer's error limit. Path-only
historical failures remain unverified diagnostics, even with a complete namespace.
Typed model-client failures count as infrastructure. Nonretryable completion
HTTP rejections (including 400 and 401) count as infrastructure after one
attempt. When an HTTP error body cannot be read, the status still determines
whether to retry. Interrupted response reads and malformed external completion bytes or
envelopes (including non-JSON constants anywhere in the body) are
infrastructure, not agent zeros. Text from an ordinary scenario failure
(including HTTP status wording or errno) does not override its agent zero.
Multi-namespace comparison keys encode
the scenario and full namespace as structured JSON, disjoint from literal
singleton IDs and diagnostic keys.

If the optional `lib.compute.jobs` package is available, backend collection
stages downloads in a report-excluded directory, validates all journal rows,
and requires exactly one row per declared task for the intended scenario
provenance, model, arm, creator and environment label, in a single measured
runtime namespace. The collector validates a private byte-for-byte copy outside
the adapter's download destination, so fetching referenced artifacts cannot
replace the journal being published. It verifies referenced artifacts and
renames the private journal only after its artifacts are installed and synced.
Sync covers newly created ancestors through the first existing directory. A pre-rename failure leaves no published
journal. A post-rename sync failure leaves a visible journal with uncertain
durability. Staging cleanup failure is reported separately, including when
sync also failed; cleanup does not remove the published journal. The base
checkout does not include the optional compute package; offline fixtures use
a fake backend and establish no real backend availability.

Reports retain the readable LF-terminated prefix, distinguish an incomplete
final row from an invalid JSON or schema row, and never rewrite journal bytes.
The runner refuses to append to a journal with either kind of issue before
invoking the model; use a fresh output directory and retain the old data.
Recognizable older rows that lack verification remain diagnostics; arbitrary
objects and malformed modern rows are not treated as legacy. A readable row
is not evidence that its directory sync succeeded. No-tool responses require
affirmative terminal completion before scoring.

## Model alias is not deployment identity

`response.model == requested_model` prevents simple misattribution but does not
prove which immutable upstream deployment answered. The default transport has
no trusted deployment attestation. Such scores remain diagnostic; they cannot
resume a supposedly matched run or establish paired gains.

An embedding may inject the trusted host `identity_reader(model, response)` port.
With `response=None` it returns a current immutable deployment SHA-256; with an
actual response it attests that response's deployment. Arbitrary response
metadata is not this port. A response must match the current attested identity;
a deployment change during the measured call is infrastructure failure. Both
arms and, when used, the fixed judge must have matching trusted evidence before
paired gains are computed. Synthetic identity fixtures test the contract only;
no deployed attestation integration is claimed.

Problem: label-only environment namespaces and model aliases let old artifacts
be overwritten or compared across upgrades. Decision: retain immutable
attempts and require exact input/runtime/deployment lineage. Alternatives:
trusting aliases or user environment labels was rejected because neither
identifies actual execution; inventing deployment IDs from response metadata
was rejected because it would manufacture evidence.
