# Canonical Attempt events and conversation replay

## Deleted conversation event lifetime

**Problem:** native soft deletion walked all Turn bodies to discover Attempt
events. Conversations exceeding 2000 identities or 64 nonempty event ranges
could fail deletion despite range-based trash snapshots.
**Decision:** `turn/event_retirement.rs` atomically pins the committed identity
sources and stores an owner-scoped, execution-generation cursor while the
existing delete transaction detaches public and executable state. Restore
remains immediate and inert. The existing `turn.events.prune` maintenance path
reclaims event sequences in bounded transactions, round-robins queued generations
and releases each pin only with its completed cursor. Cursor-only work commits
even when no payload rows exist. Identity claims remain reserved while cleanup
is pending; purge reclaims pending generations before releasing those claims.
Random-key legacy Attempts remain inaccessible after restoration.
**Alternatives considered:** raising row/range limits preserves the foreground
scan and eventual failure; omitting reclamation leaks event data; a new worker
duplicates existing maintenance ownership.

Bounds reuse the generated event-prune attempt, row and materialization budgets.
Each generation holds one cursor and four immutable source ranges; it does not
copy Turn payloads or add a worker. Pins retain source pages until maintenance
finishes, so prolonged maintenance failure retains reconstructible bytes.
The two-generation native fixture measured 218 serialized cursor bytes and two
private pins, then verified zero remaining cursors/pins after bounded draining
and reopen. This excludes index/page and allocator overhead and establishes no
latency/RSS improvement. Fault injection covers both partial progress and final
pin release. Soft deletion/restore is covered at 4096 Turns and 97 event-bearing
Attempts; permanent purge and clone retain their existing identity/range bounds.
Verification: `turn::event_retirement::tests`, `conversation_delete_` tests and
maintenance's `idle_round_commits_event_retirement_without_payload_rows`.
The retained measurements
also record foreground blob collection/decoding: 65,953 / 6,398,259 bytes for
old one/97-Turn fixtures, zero in both candidates. Payloads are approximately
64 KiB per Turn; deferred background work is excluded from this dispatch scope.

## Attempt event document bound at write

**Problem:** `turn.events.list` (`turn::events_list`) reads each Attempt event
document within `maximumAttemptEventDocumentBytes` (32 MiB,
`contracts/tofudb_ir_v1.json`), but the writer checked no bound, while the
request admits a projection patch up to the 64 MiB Turn snapshot (native) or
frame (SQL sidecar). An event between the two, typically a terminal frame that
carries a whole tool output in its patch, was committed and acknowledged, then
answered `database_integrity` on every read. The SQL authority applied only its
4 MiB non-terminal cap, which exempts terminal settlements and the
checkpoint-resume bootstrap, so the SQL-to-native import, which admits the same
bound, would refuse such a row.
**Decision:** both authorities refuse an event document larger than that same
constant before writing it, as `storage_payload_too_large`: native
`turn::append_attempt_event`, the one live writer of every Attempt event, and SQL
`_insert_attempt_event`, for every event type and the bootstrap alike. The refused
transaction commits nothing. Producers already degrade on that code:
`turn_lifecycle._is_frame_overflow_error` retries the frame once slim, and a
terminal frame settles with `model_continuation: frame_over_budget`. Reads, the
frame limits, the 4 MiB non-terminal cap and the stored format are unchanged, so
the rollback engine reads every row; an event acknowledged before the bound keeps
its bytes and still answers `database_integrity` on a native read.
**Alternatives considered:** raising the read bound only moves the failure to the
event page budget the same reader enforces; truncating the payload breaks exact
patch replay; bounding one authority alone makes the two answer differently for
the same frame.
Verification: `turn::attempt_event_bound_tests`,
`tests/storage_authority_sql/test_attempt_event_document_bound.py` and, on every
authority, `tests/storage/test_storage_application_contract.py::test_a_terminal_frame_its_authority_could_not_read_back_settles_slim`.

## Canonical payload references

Problem: each Attempt event was persisted twice in the same transaction: once
as the canonical event and once inside its conversation-sync envelope. Growing
projection and terminal payloads therefore doubled serialization, blob and WAL
work even though the existing retention index already protected the source.

Decision: new native `attempt.event` sync rows retain their public metadata and
only `payload.event.seq` under the distinct physical document identity
`conversation_sync_attempt_ref/v1`. The canonical Attempt event remains the
single immutable payload, with physical version 1. Both records, the monotonic
sync head and the existing owner-scoped retention reference commit together.
No new worker, cache, log, acknowledgement rule or storage authority is added.
The machine-readable invariant lives in `contracts/tofudb_ir_v1.json`.

Replay and operator export validate the physical/logical sync key, authenticated
owner, conversation/Turn/Attempt identity, source sequence, source version and
matching retention reference. They validate the full source JSON before
splicing its original bytes into the public envelope. Missing or inconsistent
references fail closed. Public wire formats and SQLite behavior are unchanged.
Legacy full envelopes remain readable and preserve their exact bytes on export.
Live publication and replay impose no private 8 MiB expanded-envelope or page
quota. The native transport chunks complete responses; requested row limits and
deadlines preserve contiguous replay cursors. Legacy `conversation.activity`
snapshot invalidations remain readable. Exclusive operator migration retains
its separately declared transfer protocol and requires its own certification.

Pruning never reads a full replay document that releases nothing: such a row
is identified by its document header (namespace and logical key) and its
retention locator. A row that releases an Attempt reference (an inline compact
reference) or predates Attempt locators is read and its stored identity
compared with its locator before anything is released. Attempt pruning
cannot retire a source while a sync reference remains. Sync pruning releases
the reference in the same transaction; pinned snapshots retain both records.
Ordinary backup, restore and GC follow the existing entity/blob roots. Operator
migration exports full envelopes and imports the legacy self-contained format.

Alternatives considered: dropping transport rows loses acknowledged replay;
adding another canonical log introduces another publication and recovery
boundary; retaining duplicate payloads preserves the measured write amplification.
The existing exact reference index supplies the required lifetime fence.

## Sync retention within one overlay delta

**Problem:** `turn.sync.prune` deleted one scan page (up to 999 expired
entries) per transaction. An Attempt-event entry is three point deletes
(replay row, age marker, Attempt reference), so a full page carried about
3,000 deletes, past the 192 KiB one rebaseable overlay delta holds. The commit
then took the structural tree path, which first drains the whole overlay
under the writer. Separately, the maintenance stage committed a sync
transaction only when it deleted a replay row, so releasing a deleted
conversation's age markers and Attempt references was discarded every round
and left to the application's prune loop.
**Decision:** each transaction deletes only the entries whose point deletes
the remaining overlay room can carry beside the largest receipt the IR may
attach (`entity_overlay_delta_room`, as Attempt-event retention does), and
reports `remaining` so the next transaction resumes. The scan page stays the
IR's entity range bound. Maintenance commits every sync transaction that
changed state. Specs: `turn::sync_prune_tests` and maintenance's
`idle_rounds_commit_sync_marker_release_after_conversation_delete`; the
opt-in `turn::sync_prune_tests::sync_prune_commit_hold` measures the
writer-held commit beside a pending overlay.
**Measured** (2026-11-13; sandbox VM, 16 vCPU, local virtio disk, release
build without LTO, three repetitions each): the commit of one maximal
retention transaction beside 0 / 8 / 32 MiB of pending overlay deltas took
1.5-1.8 / 136-154 / 1,262-1,267 ms before (999 entries; the overlay drained
to zero records) and 1.9-2.3 / 1.9-2.3 / 2.0-2.3 ms after (646 entries; one
delta appended, nothing drained). The former cost grows with the pending
overlay, and production windows are larger (median 56 MB over 40 minutes on
2026-11-13) on a network volume.
**Alternatives considered:** a smaller fixed row count is a tuning number that
still ignores key sizes; merging the overlay before the commit moves the same
drain under the writer; removing markers lazily on read adds a second
retention path.

## Sync retention reads no settled payload

**Problem:** after R3, production clients waited 140.4 s, 182.4 s and 160.6 s
for single `turn.sync.prune` calls, with no authority hold of that length
(OCR-HOLD, 2026-11-13). For every expired row still present, `sync_prune`
materialized the whole stored document and parsed it, only to re-check the
identity its retention locator already carries. Settled `turn.upsert` rows hold
whole TurnRecords (0.5-0.8 MB blobs), up to a 64 MiB ceiling per transaction.
An uncertified command is rehearsed on the published view before the writer
runs it, so those cold blob reads landed on the network volume outside the
authority mutex, and the prune's caller waited minutes.
**Decision:** a full replay document whose locator names no Attempt releases
nothing, so it is verified from its document header alone (namespace is the
replay identity; logical key is `{conversation}\u{1f}{sequence}`) and its
payload is never read. A row that releases a reference keeps the stored-identity
check before the release: compact Attempt references are read without expanding
their source (small inline documents) and compared with their locator, and a
mismatch refuses. Locators written before history root 91b4c7e205
(2026-10-22T09:49:43Z, which reproduces the released 0.18.3 engine) may lack an
Attempt's identity, so a row whose locator is older than that is read as before
(`SYNC_LOCATOR_ATTEMPT_IDENTITY_SINCE_MS`; a backdated row only takes the read).
Retention cost now follows the rows it deletes, not settled TurnRecord bytes,
and the `maximumSyncPruneMaterializedBytes` bound is retired (DB-5 review,
2026-11-13). The SQL authority already pruned by key. Specs in
`turn::sync_prune_tests`: settled rows whose blobs are missing are deleted;
a compact reference whose stored identity disagrees with a consistent locator
and protection reference refuses; a row older than Attempt locators is read,
and an Attempt event behind an identity-less locator refuses.
**Alternatives considered:** a smaller materialization ceiling keeps the
read and adds a knob; caching decoded payloads moves the cost into memory;
prefetching blobs off the rehearsal still reads every expired TurnRecord;
trusting every locator would release references of pre-root rows on a guess.

Older native engines reject the new physical identity. Downgrade requires a
current-engine canonical export into a fresh, fully verified target authority
whose replay rows use the old representation, plus folding active text heads as described in
[STORAGE_TURN_TEXT.md](STORAGE_TURN_TEXT.md). Never rewrite runtime documents by
hand or switch backend to bypass a format failure. Insert-or-match import into
the original authority does not rewrite existing reference rows; downgrading
in place is unsupported. Full target migration needs its own source/target
certification before activation.

Verification: `turn::sync_attempt_reference::tests`, native neighbor/full tests,
application storage parity, native backup/restore and browser lifecycle tests.
Performance and release claims require matched source/artifact identities and
same-volume workloads with exact body/event replay after crash. Functional
correctness alone does not establish terminal latency or capacity SLOs.

Measured scope: 16-way, 100 frames of 8 KiB at 20 ms, full carried task events,
same-FUSE source/authority ABBA. Scheduled completion P99 was 954–1222 ms for
the frozen baseline and 492–564 ms for references. With the default 3 s
structural cadence and 40 ms frames, it was 493–652 versus 410–451 ms;
invoked settlement ranges overlapped in that workload. Single/four-way tails
varied. Full 200-event conversation replay after crash remained about 43–45 ms.
Physical endpoint sizes varied; this does not certify steady-state growth or
half-sized storage. Delta coalescing was explicitly disabled. Exact bodies and
replay survived every leg. Artifact pins, all legs and limits live in
`tofudb_canonical_event_replay_20261001.json`.

## Task-event source guards

Problem: task-event retention can delete a middle sequence and a later append
can reuse it. Equal `event.bounds` count/base/next values then describe different
payloads, so they cannot fence a paginated diagnostic read.

Decision: `event.bounds` accepts the optional boolean `include_read_guard`.
The additive `read_guard` has the exact `EVENT_READ_GUARD_CONTRACT` from
`lib/task_replay/task_event_contract.py` (exported in the generated storage catalog) and an
opaque 64-character hexadecimal `stamp`. Default bounds retain their old shape.
A stamp binds one task to the selected authority's open lease. Repeat reads and
identical event replay retain it; changed task events or retention invalidate
it. Other tasks do not invalidate it. Reopen/restore requires a new read: stamps
are not archive identities or durable executor-resume receipts.

Problem: whole-task stamps also restart snapshot replay for unrelated usage or
streaming appends. Decision: optional `types` selects 1..64 exact nonempty types
of at most 128 characters. Scoped bounds count only that selection; a guarded
response uses `EVENT_READ_GUARD_SCOPED_CONTRACT` and echoes sorted unique types.
Consumers require that exact contract and scope; old engines cannot admit it.
No prefixes or empty selection are accepted. Omitted `types` preserves v1.

The scoped revision is `A-(R-S)`: canonical append count/watermark A, whole
retained count R and selected retained count S. Unselected appends increase A
and R together; selected appends change the revision. Selected retention
changes scoped bounds, and prune/reinsert changes A even with equal bounds.
Unselected retention invalidates conservatively. Native metadata and two index
seeks per selected type avoid event-body scans. SQL reads counts and existing
UUID/append/prune witnesses in one statement, with no new durable state or
backfill. Its signed revision may be negative for legacy rows; the first
mutation publishes a UUID and may invalidate once even if unselected.

Native reads combine the existing physical append watermark, retained bounds,
owner/task identity, authority UUID and a lease epoch shared by read replicas.
SQL stores one compact UUID/append/prune witness per changed task in schema 63;
mutations and witnesses commit or roll back together. A batch coalesces witness
updates by task. Retention returns at most 1,000 deleted task IDs and updates
only those tasks. Empty-stream witnesses remain: deleting them during a live
lease could recycle an earlier empty-source stamp. Their lifetime follows the
authority, as does the native physical stream watermark. Existing SQL rows need
no startup backfill; before their first mutation, a legacy stamp includes exact
bounds, and the first canonical mutation publishes a new UUID witness.
Offline migration admits only the exact key/column layout and bounded, valid
UUID/counter rows beside mapped task events; unknown shapes remain blockers.
Target leases produce new stamps, so these payload-free witnesses are rebuilt.

The SQL bounds/witness projection uses one statement, including on PostgreSQL
READ COMMITTED. Query-pool and writer sessions share their backend lease epoch.
These witnesses detect canonical source changes, not arbitrary storage
corruption. Existing payload and snapshot-dependency integrity checks remain.
Readers must confirm the exact contract and compare stamps before and after a
multi-page read; mismatch means discard its completeness/latestness claim.
The source guard is a primitive; selected omitted-history loading and delta
replay belong to `lib/tasks_pkg/request_inspector_history.py`, under the
bounded continuation contract in [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md).

Alternatives: count/base/next alone admits retention ABA; an authority-wide
commit counter invalidates unrelated-task reads; a full payload digest per page
adds repeated whole-history materialization; new per-type counters need upgrade
and lifecycle machinery. Existing metadata supplies bounded, scoped witnesses.

Verification: `tests/storage/test_event_read_guard.py` and native
`indexed_stream::read_guard_tests` cover repeat/dedup, unrelated-task isolation,
rollback, retention ABA, empty/recreated streams, read replicas and reopen.
Scoped cases also cover noise appends, sparse sequences and exact scope binding.
Backend performance and long-history UI acceptance require separate evidence.

A synthetic SQL measurement with 100 events across 10 tasks retains 10 witness
records, including after all event rows are pruned. Their logical key/UUID and
two 64-bit counter fields total 540 bytes; this excludes table/index/page
allocation and is not RSS or a throughput result. Physical growth and
steady-state append/retention latency require separate capacity measurements.

## Complete task-event selections

Problem: `event.list` already streams complete responses, but its selected
physical positions inherited an unrelated 8 MiB aggregate read quota. A page of
individually valid inline snapshots could therefore fail while an equally large
blob-backed page succeeded.

Decision: both persisted and in-memory stream point reads return every selected
position without a private aggregate byte quota. The physical stream scan's
ordinary page target remains a resumable work slice, not an admission limit on
an event selection. Sequence, owner, segment witness, digest and missing-source
checks remain mandatory. Storage transport chunks retain their format bounds.
The invariant is `physicalBounds.streamPointReadAdmission` in the native IR.

Alternatives rejected: increasing the quota merely moves the failure; making
current callers guess smaller pages preserves the storage defect; truncation
would destroy request and tool evidence. Compatibility readers still handle
typed size refusals from older engines and other selected backends without
turning a failed read into EOF.

Verification: native
`event_list_preserves_selected_inline_events_above_eight_mib_after_reopen`
checks complete filtered and unfiltered pages after reopen. Existing Request
Inspector and Turn Trace tests retain older-engine error compatibility and
same-cursor retry, while owner/corruption specifications stay unchanged.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

## Replay lineage

Problem: a replay cursor named only owner, conversation and sequence. Crash
recovery, a restore or a failover can give a sequence that a reader already saw
to another change. A cursor naming it then replayed past the new change as if
it had been seen. The protocol record is
[Conversation Sync v3, Replay lineage](contracts/CONVERSATION_SYNC_V3.md#replay-lineage).

Decision: every `turn.sync.changes`, `turn.sync.page` and `turn.sync.snapshot`
result reports, from the read transaction of its rows:

- `authorityEpoch`: 64 bits of OS randomness drawn once per authority open
  after recovery and shared by its read replicas, never persisted or reused.
  Natively it is a dedicated value on `AuthorityDatabase`, distinct from the
  overlay-merge `database_instance_id`. The SQL authority derives it from its
  backend open's identity. A published commit is lost only with a reopen,
  which latches `restart_required` natively, so no sequence repeats within
  one epoch.
- `headDigest`: the digest of the change at the head. It is null at head 0 or
  once that change has left retention.
- `turn.sync.changes` adds `changeDigests`, one per returned change. With
  `after > 0` it also adds `anchorDigest`, the digest of the change now at
  `after`, which is null once that change has left retention.
- `turn.sync.page` read with `anchor_sequence > 0` (the reader's applied head)
  adds the same `anchorDigest` for that sequence, so a history page is fenced
  by the reader's lineage from the page's own read, also when it answers at a
  newer head.

A digest is the first 64 bits of SHA-256, as 16 lowercase hex digits, over the
change's committed record exactly as stored. Natively that is the value bytes
stored for its key; a blob-backed row stores its content-addressed manifest.
In SQL it is the row's length-framed stored columns. Overlay merges,
checkpoints, history retirement and reopen move bytes physically without
changing a digest. A different change committed at a reused sequence always
changes it.

A compact AttemptEvent reference row (see "Canonical payload references" above)
stores only its event's identity and commit time. Recovery can lose an event
and its replay row, then commit a different event at the same attempt and
sync sequences within the same millisecond, so the reference's digest also
covers the referenced event exactly as stored: natively the reference's and
the event's stored value bytes, each length-framed after a domain tag; in SQL
the event's `payload_json` after the row's columns. The retained reference
keeps that event immutable, so the digest stays stable for the row's
lifetime. It costs one extra point read of the event per reference a page
returns or an anchor names; inline rows keep their digest.
(`turn_sync_lineage_tests.rs`
`a_different_attempt_event_at_a_reused_sequence_in_the_same_millisecond_has_a_different_digest`;
`tests/conversation/test_conversation_change_storage.py`
`test_an_attempt_change_recovery_replaced_in_the_same_millisecond_resets_its_cursor`.)

Digests are authority-local identities: the two authorities never compare
them, and a differential comparison checks only their presence and type. An
operator import therefore resets each client once. Field names and widths come
from `contracts/storage_v2.json` `applicationSemantics.conversationSyncLineage`,
generated for both authorities, and the declaration moves the storage contract
identity.

Replay rows are insert-only. A future online rewrite of a row's stored bytes,
such as settled `turn.upsert` compaction, must keep the row's original digest
or accept one reset per client across the next epoch change. Either way no
client diverges silently.

Alternatives considered:

- A per-row epoch stamp or a chained digest needs write-path and on-disk format
  changes in both authorities.
- Re-encoding the materialized event would hash a representation that the
  diagnostic projection and engine normalization are free to change.
- Storing the referenced event's content digest in the reference envelope at
  write time changes the stored reference format, which the rollback engine
  rejects (its reference parser admits only the event's sequence) or would
  publish as an unknown public field.

Specs: `packages/tofu-db/src/turn_sync_lineage_tests.rs` (stability across
merge, checkpoint, history retirement and reopen, and reuse after a restored
durable point) and `tests/conversation/test_conversation_sync_cursor_identity.py`.
