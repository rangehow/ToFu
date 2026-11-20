# Offline handoff defensive acceptance fixture

Run only the generated, in-memory metadata specification:

```sh
.venv/bin/python3 -I tests/fixtures/offline_handoff/acceptance_fixture.py
```

No arguments, input files, store paths, credentials, imports from the application,
network, services, engine binaries, or user records are used. The executable
creates dictionaries only. It does not open or hash any manifest. It is reusable
via the pure `check_handoff(binding, evidence_refs, authenticated_observations)`
function, but is deliberately not wired to an operator command or release gate.

## Problem, decision, alternatives

The exact restore owner emits source authority UUID, durable sequence, source
CONTROL generation and manifest digest (`packages/tofu-db/src/main.rs`, restore
branch). The separate verify owner emits integrity verdict and counters, but no
target-specific identity or acknowledged-operation frontier. Restored CONTROL
preserves the source authority UUID; it is not a distinct restore-target ID.
`serverctl_pkg/tofudb_restore_selectors.py` derives flags from authenticated
manifest bytes; it does not authenticate that evidence or establish graph custody.
The existing native test is `tests/tofudb/test_exact_backup_restore.py`.

Decision: keep this a standalone, generated-metadata consistency specification.
No complete handoff consumer/schema was found in the bounded inspection of
`lib/`, `serverctl_pkg/`, and `contracts/`. Extending native receipts or presenting
selector parsing as custody verification would create a new public contract or
misstate the proof. Modifying the release gate is explicitly out of scope.

This file is **not a supported receipt schema**. Its normalized binding repeats
identity across observations to expose cross-identity substitution; real receipts
need not duplicate manifest fields. Independently authenticated original manifest
bytes can supply those fields under the existing procedure in
[Storage operations](../../../docs/STORAGE_OPERATIONS.md#native-online-backup).
A future supported adapter would need its own contract, authentication boundary,
and tests; this fixture is not that adapter.

## Minimal content-free operator prerequisites (not requested or accessed here)

Only already authorized, independently authenticated metadata could support a
real assessment. The operator would need:

- Expected frozen candidate and compatible compiled engine/build identity;
  explicit tenant/owner and source authority UUID.
- Independent original-manifest digest, source CONTROL generation and durable
  cut; never inferred from filename, epoch, count, or restored CONTROL.
- Distinct source storage, independently retained immutable custody identity,
  disposable restore-target storage identity, and exact restore-run identity.
  `source_storage`, `custody_storage`, and `target` are canonical storage identities
  in one shared namespace, not paths, display labels, or authority UUIDs. The
  external authenticator must resolve aliases to the same identity before calling
  the fixture, and bind each identity to its actual storage. Exact string equality
  then rejects source/custody equality and target equality with either one. The
  fixture does not resolve aliases itself; differently spelled labels alone do
  **not** prove physical separation, independence, or immutability.
- Exact external-attachment inventory digest (including an explicitly verified
  empty inventory, if applicable); counts and database references are insufficient.
- Independently captured required source acknowledged sequence **and** frontier
  digest, plus the target/run-bound restored frontier comparison. The required
  frontier cannot be chosen from whatever happened to restore successfully.
- Independently authenticated observation references for full reachable graph
  retention, exact attachment custody, completed full exact-selector restore,
  full target verification, and equality of the restored acknowledged frontier.

No payloads, filenames, record IDs, keys, or actual attachment bytes are needed
for these joins. The fixture does not establish any of these observations itself.
`authenticated_observations` is a caller-owned trust boundary: do not populate it
from unchecked submitted JSON, a success flag, or hashes calculated from the same
candidate. A real operator must verify provenance, physical independence,
coverage, and target/run attribution outside this fixture. Parsing and matching
metadata are necessary consistency checks, not proof that the underlying work
occurred. Even its positive output is `fixture-consistent:not-release-proof`.

## Exact frozen scope and known refusal

The scope is `a28b2b7bf4cc28b981720fbf36e535ea7034fc5c` only. Historical retained
witness **8605** identifies generation **313671**, sequence **5278467**, digest
`9d92db4c6084d9110367a4746decb12584e59bb27c35ae2436cc0615d05e9a9a`.
These identifiers were taken from retained conversation evidence, not a fresh
retention lookup or rehash. This establishes **manifest-only** evidence, not an
available backup. No actual authority/owner/tenant values are invented for it.
The fixture overlays these four known fields on generated metadata solely to
prove that even fabricated positive observations cannot promote this cut:
`blocked:8605-manifest-only`. Changing that hard refusal requires a separately
reviewed evidence decision, not merely supplying JSON to this fixture.

The positive case uses generated generation 17 / cut 23 / ACK 22 and invented
identities/digests. It is not a restore or an alternative rollback. Schema 71's
incompatibility with the earlier schema 70 handshake remains unchanged, as do
all frozen candidate, auth, retention and open-source/private policy decisions.

## Executable refusal coverage

- Manifest witness, catalog count and smoke success alone: required evidence
  missing. A smoke restore remains insufficient even with other positive claims.
- Every binding field changed independently at each of five stages: reject
  candidate, tenant, owner, authority, generation, cut, manifest, source/custody
  storage, target, run, attachment inventory, source ACK sequence/frontier, build.
  An explicit regression sets required ACK 22, restored ACK 21, and cut 23;
  the ACK observation refuses despite the sufficiently high cut. Foreign targets
  with identical source lineage also refuse.
- Missing/extra/wrong-typed identity fields, absent authenticated observation,
  missing stage, reused observation reference, partial/unknown coverage,
  nonzero missing blocks and boolean-as-zero: refuse.
- Changed frozen candidate, same source/custody storage, ACK beyond cut,
  zero/boolean ACK and empty owner: refuse.
- Target equal to source or custody storage in both the expected binding and
  every observation: refuse both consistently attributed in-place restore cases.
  The distinct-target positive case remains consistent, not recovery proof.
- Known witness8605 cut: blocked with absent evidence and with invented complete
  evidence. No latest-generation fallback exists in this fixture.

This specification neither modifies nor executes native restore, verify, selector
extraction, backup/retention, vendor build, services, release or publication.
Passing it cannot satisfy or downgrade any existing release prerequisite.
