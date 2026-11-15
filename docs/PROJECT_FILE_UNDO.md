# Project command snapshots and undo

The runtime owners are `lib/project_mod/run_command.py` (change observations),
`snapshot_content.py` (bounded pre-images), `modifications.py` (history and undo
receipts), and `undo_filesystem.py` (anchored mutations and post-image evidence).
The persisted `postImage` field follows
[`project_undo_postimage_v1.schema.json`](../contracts/project_undo_postimage_v1.schema.json).

## Change observations


### Linked worktrees and file-change presentation

Problem: temporary linked checkouts were mistaken for scratch files, so edits
could be merged successfully without a File Change Panel. Decision: file writes
recognize same-repository worktrees before the temporary-directory exemption,
register the checkout root in the execution's scoped registry, and journal clean
root-relative paths. Ordinary scratch and unrelated temporary repositories remain
untracked; existing restricted-caller, read-only and task-checkout admission still
run first. Checkout identity does not grant access to another task's workspace.

Settlement discovers runtime roots and committed live journal addresses as well
as configured roots, then rereads conversation/task-stamped journals. A live row
is a discovery hint, never independent proof of a file change. Explicit foreign
task IDs never enter legacy timestamp fallback, and dynamically discovered roots
require exact task IDs. Journals survive Git checkout removal; recorded command
actions cannot be changed into deletions merely because a worktree was removed.
Orphaned-turn restart recovery uses a separate explicit conversation-journal mode
with the logical turn's creation floor to carry prior physical attempts; that
mode requires an empty task ID and never changes normal settlement isolation.

Auxiliary manually added worktrees carry durable per-file `undoAvailable: false`.
Their original root identity remains visible after integration; the single-root
undo gateway must not imply it can reverse the canonical Git merge. Normal
primary/managed task checkouts retain their undo behavior. Any denied row disables
the block's undo/redo, at both projection and command boundaries. Alternatives
rejected: scanning a shared Git diff misattributes concurrent work, remapping
worktree receipts to the main checkout invents undo authority, and treating every
temporary path as a project pollutes scratch history. No new store, worker, or
persistent ledger is introduced. Verify with worktree path, round-derivation and
conversation file-command tests.
Command tracking does not infer writes for opaque commands. Up to 64 literal
relative write targets use a deduplicated scoped snapshot; a literal file never
walks unrelated directories. Globs and other ambiguous target sets retain the
bounded whole-tree fallback. Both paths share one entry budget.
In-place editors are parsed by their documented grammar so their operands become
literal targets: `sed -i`, awk `-i inplace`, and `sd FIND REPLACE FILES...`
(`_sd_invocation` in `command_analysis.py`). A stdin-only or `-p/--preview` `sd`
is a read-only filter; an `sd` option outside its grammar keeps the command opaque.


**Post-commit handoff:** stop lane ownership once the requested ticket completes; draining later callers' listeners delays completed edits. Later callers await earlier tickets and their own callback; nested work stays synchronous. History staging is unchanged. Phases `post_commit_wait`/`post_commit_callback` separate waiting from execution; `test_post_commit_file_release.py` proves earlier return while a later listener is held.

**Lockfile I/O isolation — Problem:** slow filesystem opens held the process-wide lease registry, coupling independent edits. **Decision:** one counter/condition under the existing registry reserves each opening. Open outside registry ownership, then register or close the FD before retiring its reservation; POSIX flock follows registration. Fork waits for pending handoffs and closes inherited FDs in the child without unlocking the parent. Interrupted retirement completes before propagating its error; the at-fork barrier finishes despite callback interruptions because CPython ignores callback errors. Blocked filesystem calls can still delay fork, not unrelated lease admissions. **Alternatives rejected:** removing the fork guard leaks unregistered inherited FDs; moving namespaces breaks running-process cooperation; retaining filesystem I/O under registry serializes independent work. No thread, queue or durable owner registry is added. `test_workspace_lock_io_isolation.py` covers held opens, isolated real forks, interruptions, cancellation, pre/post-mutation registration faults and directory-close failure.

Write exclusion is independent of this observation policy. Model tool calls take no workspace lease ([`Git workspace guard`](runbooks/GIT_WORKSPACE_GUARD.md#tool-calls-take-no-workspace-leases-owner-decision-2026-10-28)): one write call holds only the in-process edit mutex of each resolved target (`file_edit_mutex.py`) from preimage read through publication and journal acknowledgement, so two concurrent edits of one file both apply, while shell commands are not coordinated with it. Literal command write targets still feed snapshots and undo history; they no longer request any admission. A read/test verification is never silently promoted to an immutable snapshot or granted extra filesystem authority.

`TOFU_COMMAND_SNAPSHOT_MAX_ENTRIES` counts directories and ignored entries too:
personal defaults are probe-derived 5,000–20,000, lean fallback is 5,000, and
distributed default/hard ceiling is 20,000. The existing 5,000-file and depth-6
caps remain. Directory iteration streams rather than materializing wide lists;
iterators are released with the walk. Whole-tree and scoped metadata scans
open directories through no-follow handles; a directory swapped into a link
cannot reveal external child names. Fingerprint metadata shares the 5,000-file
cap and command lifetime; the synthetic allocation test stays below 4 MiB.

`TOFU_COMMAND_SNAPSHOT_MAX_SECONDS` adds a monotonic soft deadline (personal
probe-derived 1–5 seconds, lean fallback 1, distributed/default hard ceiling 5).
Checks occur between filesystem operations; the deadline cannot interrupt a
blocked kernel filesystem call. Errors, elapsed deadlines and budget exhaustion
mark a snapshot incomplete. An omitted path then
cannot prove creation or deletion. Common entries compare both mtime and size.
A command result explicitly reports incomplete coverage.

## Pre-image and post-image evidence

Pre-images retain the existing 100-KiB per-file and 20-MiB per-command limits.
No-follow descriptor opens pin the root and parent chain, reject non-regular
files, and avoid blocking on a substituted FIFO. Reads are limited to budget+1;
identity/change metadata and the current parent chain are checked afterwards.
Missing originals are visible in command results and never mean a file was
created. These metadata checks are not transactional filesystem snapshots.

New modification records separately capture a post-image: a BLAKE2b-256 content
digest and byte count, explicit absence, or unavailable evidence. Root device
and inode bind this evidence to the selected root. Content hashing streams in
64-KiB chunks and uses the existing 512-KiB project-file cap. Command batches and
undo selections each share the existing 20-MiB byte budget, defined once by
`UNDO_BATCH_CONTENT_BYTES`; commit rechecks hash only admitted files. Oversized,
unreadable, changed-during-read or over-budget files do not get a stat-only
substitute. Moving/replacing a project root invalidates its old root evidence.

Only explicit command creation evidence permits undo-by-delete. A missing
pre-image cannot authorize deletion of an old file. Before undo, current content
must match the recorded post-image even when size and timestamps were preserved.
Content evidence deliberately excludes the file inode so multiple changes to
one file can unwind in reverse order across atomic replacements.

Legacy full-file undo without post-image evidence fails visibly. Legacy reverse
patches can still match their exact replacement against a bounded live read,
but cannot bypass that check using a full backup blob. They must decode as UTF-8
and compare the live bytes again before publishing.

## Authority, mutation and journal publication

Undo validates each record's project attribution and target before accessing
it. Traversal, cross-root absolute paths, symlink/reparse components and special
files cannot expand the selected project authority. Actual reads, deletion and
restoration use directory handles. Restores fsync a private same-directory
temporary, revalidate the parent and current evidence, atomically replace the
name, and fsync the directory; failed temporaries are cleaned through the same
handle. Editor timestamp notifications use that same anchored authority and
never reopen a substituted path. The final content check is optimistic: external writers do not
participate in a cross-process transaction.

Snapshot rewind and task redo reuse this same mutation boundary, including
no-follow timestamp notifications and metadata refresh. Stored snapshot keys
must be relative without empty, dot or traversal components. A refused file
retains tracked evidence; partial redo reports applied paths and failure reasons
with `ok: false`, without appending a successful redo snapshot. There is no
whole-round rollback promise. See [file history](modules/file_history.md).

Platforms without the anchored-open primitives decline capture/mutation
visibly rather than falling back to path-based writes. Records remain available
for inspection. No automatic override authorizes stale or unavailable evidence.

Only a missing journal file means empty history. Invalid envelopes/records,
parse failures and permission errors preserve the original file and any
recovery evidence, fail visibly, and never populate an empty cache that could
replace existing history. Journal cache and listeners publish only after persistence succeeds. A failed
write retains the previous cached and durable evidence. Successful command
execution whose history cannot persist explicitly reports unavailable history.
Undo consumes only successful `changeId` receipts from its original selection;
failed and concurrently appended records remain. A failed newer change blocks
older undo on the same path. A partial undo can therefore leave both changed
files and retained failure records; failure is not reported as successful undo.

## Refusals and faults

Problem: undo and redo reported every failure as `ok: false` data, so the
conversation gateway answered all of them as the user's 400. That channel also
carried engine faults (file history failing to load, a restore crash's error
envelope), and per-file rows carried raw exception text, including server
paths, into the 400 body and the turn's durable `fileChanges.error`.

Decision: refusals are typed data; faults are typed server errors.

- A refusal is the engine's authored answer, marked `refused: true` on the
  result (no session, no snapshot for the task, history disabled) or on a
  `details.failed` row: missing or stale evidence (no post-image, original,
  reverse patch or backup; the file changed after the recorded change; a
  later change blocks it) or a target that the path validators or the
  anchored undo filesystem refuse with `ValueError` (escape, symlink,
  non-regular file, a target that changed during the restore).
- Every other failure is a fault. A fault row has `refused: false` and a
  reason that is either fixed engine text or `internal error (<type>)`; the
  exception and traceback are logged where it was caught. A fault with no
  per-file row (file history unavailable, a restore crash) raises
  `modifications.FileHistoryFault`, a `RuntimeError` whose message is fixed,
  disclosable text.
- The conversation gateway (`lib/conversation_sync/runtime.py`) translates
  only typed refusals to `BadRequest`. A fault row, an untyped `ok: false`
  result, or any other engine exception (an unreadable journal, a busy
  session lock) becomes a `FileHistoryFault`. The file command records its
  message as `fileChanges.error`, returns the block to its source state, and
  the command boundary answers a logged, redacted 500.
- Undo semantics are unchanged: completed records are consumed, failed ones
  retained, and a refused or faulted undo never settles as `undone`.

Alternatives rejected: classifying `ok: false` text in the gateway cannot tell
an authored reason from an exception string. Raising refusals from the engine
would turn the v1 project routes' refusal receipts into 500s and drop
partial-undo receipts. Redacting every reason would hide user-actionable
answers such as a file changed after the turn. Verify with
`tests/conversation/test_conversation_file_change_undo_outcome.py`,
`tests/conversation/test_conversation_file_change_fault_boundary.py` (SQLite
Sidecar, real route) and the redo-scope and undo-resolution suites.

## Undo index and immutable contents

Problem: a 318 MB inline undo journal was reparsed and rewritten for every
small file edit; a five-file edit spent 30 seconds on unrelated prior content.
Decision: `modifications.json` remains the sole commit authority, but contains
lightweight references for `originalContent` and `reversePatch`. Immutable
`undo-payloads-v1/*.pack` files contain packed ASCII JSON values. References
follow `contracts/project_undo_payload_ref_v1.schema.json`: pack name, byte
range and SHA-256 for that exact value. Packs are fsynced before the atomic
index publication; failed migration leaves the previous inline journal valid.
Older inline records are converted once on the next confirmed journal update.
There is no change to owner/root attribution or the selected storage backend.

## Per-file journal locks (no project-wide admission)

Problem: every writer of a project shared one journal admission
(`session_lock.locked_session`, a flock on `.journal.lock`) from intent
recovery through acknowledgement. One slow writer (a stalled storage ACK, a
slow filesystem, a long undo) therefore stalled every write to every other file
of the project, and unrelated edits failed with `[journal-busy]` after the 30 s
`lockWaitSeconds`. Shorter holds (append commits, identity validation, ACK
before admission, fair admission order) reduced the hold time but kept the
coupling.

Decision (owner decision 2026-11-10, taken knowing the trade-offs): remove the
project-wide admission from the write path; writes to different files never
wait for each other. `journal_locks.py` owns two kinds of lock:

- **File locks.** A writer holds the cross-process `flock`
  `<session>/file-locks/<sha256(attributed relpath)>.lock`, behind an
  in-process per-path lock, from recovery and intent preparation through
  replacement, journal commit and intent acknowledgement. Same path
  serializes; different paths never contend; multi-file holders (edit batches,
  undo) lock in one sorted order; a thread re-enters locks it holds. A busy
  refusal after `lockWaitSeconds` is `journal-busy` naming the file and the
  holder (tool call, task relation, thread, pid, hold time). The lock file also
  keeps that path's last commit sequence and time (`next_journal_key`), so one
  file's commit keys are strictly increasing even if the clock steps back;
  keys of different files are ordered by time only. Lock files persist (one
  per path ever written).
- **The rewrite lock** (`<session>/.journal-rewrite.lock`) serializes folds,
  undo and the other whole-history rewrites (`_locked_rmw`) and the log's
  appends among themselves. Writers never wait for it: a commit **tries** it
  without waiting. Free, it appends the row to `modifications.journal` (one
  fsynced line, the single-appender log, so a torn suffix can be cut back and
  a failed append truncated), together with any record files left meanwhile,
  and folds the log into the index once it passes `_JOURNAL_TAIL_MAX_BYTES`
  (or once to migrate a legacy inline index). Busy, the row is published as
  its own immutable file `<session>/records/<key>-<changeId>.json` (same-dir
  temp, fsync, rename, directory fsync); the next appending writer moves it
  into the log (row durable in the log before the file is unlinked). Content
  hydration holds the rewrite lock shared, so an undo cannot reclaim a selected
  pack before its exact read. Undo holds it from selection through its index
  rewrite and additionally holds the file locks of every selected path while
  it restores contents. Lock order is rewrite then files; writers only try the
  rewrite lock, so the order cannot deadlock.

A rewrite reads the exact history (index + fold artifact + log + every record
file then listed), publishes an index that records the record names it
absorbed (`absorbedRecords`) and the fold artifact (`absorbedFold`), then
unlinks exactly those. Records committed meanwhile are not in that set and
stay authoritative. Readers take no lock: they replay index, unabsorbed fold
artifact, record files and then the log (a record moves into the log before
its file disappears), order rows by commit key (rows without one, from older
journals, first as stored) and drop duplicate `changeId`s; a read that
overlapped a rewrite (index or artifact replaced, a listed record gone) is
repeated. Commits still detect a replayed `changeId` without materializing the
history (`_RecoveryIdentityConflict` for different evidence). Payload packs
belong to the one commit or rewrite that wrote them (salted names), so an undo
reclaiming a removed row's pack can never take another commit's content.

**Portable journal locks — Problem:** `journal_locks.py` and
`undo_payloads.py` imported `fcntl` at module level, so `lib.desktop_agent`
and the packaged Windows server child could not import (`fcntl` is POSIX-only).
**Decision:** both take every lock through `lib/project_mod/file_lock.py`
(`lock(descriptor, shared=, blocking=)`, `unlock`): `flock` on POSIX,
`LockFileEx`/`UnlockFileEx` via `ctypes` on Windows, each imported lazily for
the running platform. A busy non-blocking lock raises `BlockingIOError` on
both (Windows `ERROR_LOCK_VIOLATION`/`ERROR_IO_PENDING`), so callers keep one
busy path. Windows byte-range locks are mandatory, and both owners do I/O on
locked files: a pack's commit writes it while holding its own shared lock
(Windows denies writes under a shared lock even to the holder), and a busy
waiter reads the holder diagnostics from the path's lock file. The Windows
lock therefore covers one byte at the largest signed 64-bit offset, which no
content reaches, so it excludes only other lockers, as `flock` does. Path
locks are unlocked explicitly before close because Windows may release a
closed handle's lock late. **Alternatives rejected:** a no-op lock on Windows
loses concurrent-writer exclusion; locking offset 0 through the maximum length
breaks the pack write and the diagnostics read; `msvcrt.locking` has no shared
mode and locks from the current position. `test_project_file_lock_portable.py`
specifies the Windows branch against a fake kernel32; native Windows locking
is not exercised in CI. The journal still uses other POSIX-only calls at run
time (`os.pread`/`pwrite`, `dir_fd`, `O_NOFOLLOW`/`O_DIRECTORY`, unlinking an
open file), so importing it on Windows does not make undo history work there.

Recovery: an intent whose file lock can be taken without waiting has no live
writer and is reconciled under that lock with the existing classifier; a held
lock is a live writer and its intent is skipped, never delayed or consumed.
A writer reconciles a crashed writer's intent on its own file before preparing,
so that record precedes its own. Listing intents takes no lock either, so
another path's owner may acknowledge (unlink) or abort (atomic replace) its
intent while it is read: a vanished intent is skipped and a replaced one is
read again, while a change on the same inode (intents are never rewritten in
place) still fails as corrupt evidence. Inspection
(`effect_reconciliation.inspect_prepared_write`) reports
`unknown/writer_active` only while that file's lock is held.

Cache identity covers the record listing and the log length (index and fold
artifact by file identity; no directory timestamp), so another process's
commit invalidates the API host's and the execution host's caches; a warm entry
catches up by parsing only appended log bytes and new record files.

Alternatives rejected: shorter holds of the project admission only (the
previous decisions: the coupling remains, so one stall still blocks every
file); dropping undo history or write-intent recovery to avoid ordering (loses
durable user state); a record file for every commit (on this host's DolphinFS
mount each small-file open/read/unlink costs 15–50 ms: a cold read of 400
record files took 11.9 s and their fold 7.3 s, against 0.09 s and 0.15 s for
the same rows in the log); unlocked concurrent appends to one log (a torn
suffix cannot be cut back safely while another writer appends); `fsync` of
the per-file sequence (one more flush per edit for a host crash plus a
backwards clock step).

File-history staging (`file_history.track_edit`) runs under the file lock,
which orders one file's pre-images across processes; its project-wide
`tracked.json` read-modify-write now takes `json_store.locked_path` so two
processes staging different files cannot lose an entry.

Measurement (`benchmarks/project_journal_records.py`, 1,000 commits from four
threads to distinct files of one journal, folds disabled; fresh processes, base
tree = project admission with append log): two runs each
(after this change vs before), medians of per-commit wall time.
Local disk: one thread, 300 commits, p50 8.6 vs 8.7 ms; four threads, p50
53–55 vs 24–33 ms and wall 14.1–14.8 vs 8.0–10.0 s (writers now overlap and
pay a file lock plus a rewrite-lock try each); cold read of the 1,000 rows
20 vs 21–26 ms, warm read 0.3 ms both, read after one more commit 0.9–1.0 vs
0.9 ms, fold 57 vs 29–51 ms. DolphinFS FUSE: four threads, p50 324–334 vs
277–285 ms, wall 93–98 vs 81–89 s; cold read 157–176 vs 70–77 ms, warm read
54 vs 17 ms (one more directory listing per identity check). Worst case,
200 commits while a rewrite is held (all become record files): local cold read
9 ms; FUSE cold read 4.1 s and the next commit, which moves the 200 files
into the log, 3.1 s. The gain is not throughput: no write of one file ever
waits for another file's writer, a fold or an undo (`[journal-busy]` on an
unrelated file is gone), at the cost of a per-commit constant above.

Executable specs: `test_project_journal_file_locks.py` (stalled writer of one
file does not delay another; same-file writers serialize and busy names the
file; threads plus a second process commit every record exactly once;
concurrent folds and undo lose no record; a SIGKILLed writer is recovered while
a live writer's intent elsewhere is untouched; `writer_active` only for a live
writer of that path; a legacy index plus log loads identically),
`test_project_journal_records.py`, `test_project_journal_file_lock_admission.py`
and `test_project_journal_lock_fork.py`.

Internal projections request metadata only. Original contents are materialized
after conversation/path selection, and undo verifies each selected range before
mutation. Missing, changed or symlinked payloads fail visibly and block older
undo on the same path. An unreferenced pack is reclaimed only after its final
reference is removed by a confirmed journal transition; shared packs survive
partial undo. Aged unreferenced crash artifacts are reclaimed under the rewrite
lock as described in "Crash-artifact sweep" below; referenced packs and young
artifacts are excluded. Cache capacity and bytes retain their launch-resolved total;
LRU sessions share that total rather than reserving a fixed fraction per root.

Completion diagnostics use `get_task_modification_paths`: exact task/optional conversation, distinct addresses, remaining count bound (200 across roots), recovery before selection (only of intents whose file lock is free); the history read itself takes no lock (see "History reads take no journal admission"). Detached addresses survive tool release without copying historical rows or hydrating content. Cold/invalidated caches still validate the entire JSON authority: projection allocations, not cold I/O, are bounded.
Moving capture after release or reusing truncated diagnostics for asynchronous presentation was rejected: it loses capture timing or complete/legacy-attributed file changes. Tests: `test_task_modification_paths.py` and `test_project_write_recovery_crashes.py`.

Alternatives rejected: deleting history loses undo; raising the old content cache retains hundreds of MB unnecessarily; one payload file/fsync per record slows migration; unconfirmed background writes weaken acknowledgement. Packing uses the existing history cache byte budget or one required value, with no idle worker or retained payload cache.
Verification: `tests/project_mod/test_project_undo_payloads.py`, journal
failure/recovery, identity, concurrent writers and undo evidence suites.

## History reads take no journal admission

Problem (2026-11-01): every read-only projection of the undo history held the
project's exclusive journal admission, the writers' lock, while it parsed the
whole history. After each foreground `run_command` the tool round re-read the
complete history only to rebuild its own file-change card; settlement,
completion diagnostics and the turn diff did the same per root. A 16,787-record,
15 MB history exceeds the history cache budget, so nothing was cached and each
read repeated a parse, validation and cache-weight walk (about 0.47 s of CPU)
plus crash-artifact cleanup under the lock. Concurrent commands convoyed on
it, and an edit of an unrelated file in another conversation failed with
`[journal-busy]` after the 30 s admission budget. The admission is legitimate for writers, not readers.

Decision (owner decision 2026-11-01: remove writer admission from reads):
the admission orders writers only. A `run_command` round lists the changes
that command itself recorded for undo (`LocalProjectResult.recorded_file_changes`)
instead of reading history. Metadata reads (`get_modifications(include_content=False)`,
`get_task_modification_paths`, `get_conv_ids_with_modifications`) first
reconcile pending write intents without a live writer (each under its free file
lock; `write_recovery.has_pending_intents` skips everything when there is no
evidence), then read the committed history without any lock
(`modifications._committed_rows`; see "Per-file journal locks" for the
consistency rule). A read that overlapped a fold, undo or replacement is
repeated, never served mixed. A reader publishes
its snapshot only while the session's cache generation, advanced by every
publication, extension and invalidation, is unchanged, so it never replaces a
newer writer's entry. Content hydration (`include_content=True`) holds the
rewrite lock shared and undo holds it exclusively: a selected payload pack must
not be reclaimed before its exact read.

Alternatives rejected: a shared/exclusive lock (a reader would still delay
writers for a whole-history parse); raising `lockWaitSeconds` or the cache
budget (hides the convoy behind tuning knobs, and histories outgrow any budget);
trimming history (durable undo state); keeping journal-derived run cards (cost
proportional to history per command, and every later command repeated earlier
commands' changes); dropping recovery from reads (crash evidence must be
reconciled before history is presented, as `test_project_write_recovery_crashes.py`
requires).

Measurement (one run each, local copy of the real 16,787-record journal, one
warm commit first): 10 edits committed while 4 threads read metadata in a
loop. Before, the median commit took 16 ms but the slowest waited 21.1 s behind
the reader convoy; after, no commit waits on reader admission (slowest 1.09 s)
and reads rose from 2.0 to 3.7 per second. The after-median of 0.93 s is GIL
contention from four concurrent full parses, a stress rate far above
production once run cards stopped reading. Executable specs:
`tests/project_mod/test_project_journal_read_admission.py`,
`tests/tasks_pkg/test_run_command_file_changes_meta.py` and the
recovery-before-listing cases in `test_project_write_recovery_crashes.py`.

**Oversized history verdict.** Problem: a history whose decoded rows exceed the
cache budget is never admitted, yet every cold read walked all rows to weigh
them again: about 0.25 s of the 0.47 s of CPU per read of that history
(50,331,746 bytes against a 50,331,648-byte budget). Decision: remember per
session the journal identity and budget of the refused snapshot. A later
snapshot with the same index and fold artifact and a log of the same inode at
least as long only adds rows, so it is refused without a walk. A fold, undo or
replacement changes the index identity and is weighed again; a different budget
never matches. Alternatives rejected: raising the budget (a knob that growing
histories outrun); comparing JSON bytes with the budget (decoded rows outweigh
their JSON, but a 15 MB journal under a 48 MiB budget still needs the walk).
An ABAB run of the benchmark above (16,845 records) measured, before both
changes, a median commit of 9/13 ms, slowest 13.7/12.7 s, at 2.1/2.3 reads
per second; after, 0.58/0.71 s, slowest 0.92/1.05 s, at 4.6/4.4 reads per
second. Verify `test_oversized_history_is_weighed_once_until_its_index_is_rewritten`.

**Key strings are charged once.** Problem: the cache weight charged every dict
key at every row, but decoded index rows share their key strings. The live
17,413-row history retains 41.0 MB (tracemalloc and an identity-deduplicated
walk agree) yet weighed 62.6 MB against its 48 MiB budget, so it was never
admitted and every settlement metadata read re-parsed and re-validated all of
it (about 240 ms of CPU per read). Decision: a weight walk charges each key
object once; values are still charged at every occurrence, so shared values
stay over-counted. The set of charged keys is bounded by the record schema and
the rows of the bounded append log. The same history weighs 42.6 MB and is
admitted; budgets are unchanged. Alternatives rejected: an identity-deduplicated
walk of every object (exact, but its seen-set grows with the history); raising
the budget (a knob the history outgrows). Once the retained rows outgrow the
budget (near 20,500 rows of this history, at about 2.4 KB per row), conversation
reads use the located index described below; reads of the whole history decode
it again. Verify `test_budget_admits_a_history_by_its_retained_size`.

**Appends never send a warm history cold.** Problem: the cache identity covers
the whole log, so a read between another writer's append and that writer's
cache extension, or after another process's append, missed and parsed the
entire history; a reader that then published its older snapshot made the next
writer's extension drop the entry, so under continuous writes the history
thrashed between warm and cold (31 of 40 reads parsed it in one probe round).
Decision: when the current identity only added to the cached prefix (same
index and fold artifact, the same log grown from a complete line, new record
files, and every vanished record file accounted for by a row the log gained),
a reader extends the warm rows with only the appended log lines and new record
files, weighing only those rows, and publishes the extension unless the entry
changed meanwhile. Commits do not touch the cache; they advance its generation,
so a reader that sampled it before a commit never publishes its older
snapshot. A replaced index, a torn prefix or a fold artifact still takes the
full read. Alternatives rejected: skipping the generation bump (lets a reader
publish over a newer commit, which
`test_reader_never_publishes_its_snapshot_over_a_newer_commit` forbids); a
reader lock (reads take no lock by owner decision).
Measurement of both changes (`benchmarks/project_journal_write_read.py`, a copy
of the live journal with 17,629 rows and a 15 MB index on this host's FUSE
mount, 4 threads with different conversations each making 10 `write_file`
calls followed by the settlement metadata read, fresh alternating processes,
three rounds, medians): wall 12.3 to 0.94 s; write p50 630 to 85 ms and p95
1,930 to 119 ms; read p50 262 to 2.8 ms with CPU 248 to 1.9 ms; full history
parses during the 40 reads 41 to 0. The first write of a process still
validates and weighs the history once (0.6 to 1.2 s). Verify
`test_reads_after_unextended_appends_parse_only_the_appended_lines` and
`test_append_catches_a_stale_warm_prefix_up_instead_of_dropping_it`.

**Conversation reads of an over-budget history.** Problem: accurate accounting
only moves the cliff. A history whose retained rows exceed the cache budget is
never admitted, so each metadata read decoded and validated all of it again,
and settlement (`commit_round` `_derive`, `_commit`, `_turn_diff`) and
workspace evidence (`get_task_modification_paths`) pay that per call and root.
Decision: those reads name a conversation. Once a history is known to exceed
the budget (the verdict that already skips its weight walk), a conversation
read scans the index once into an `_IndexLocator`: two byte offsets per row,
grouped by conversation and keyed by the exact index file identity. Later
reads decode only that conversation's index rows, adjacent rows with one
`pread` after `fstat` proves the index is still that file, plus the parsed
append log, which is kept and advanced by appended bytes only and is bounded by
`_JOURNAL_TAIL_MAX_BYTES`. A fold, undo or replacement changes the index
identity and is located once more; a fold artifact or a non-ASCII legacy index
takes the whole-history read, and in-budget histories keep the row cache.
Alternatives rejected: keeping over-budget rows resident (an unbounded cache);
settlement from in-process recorded changes (settlement deliberately rebuilds
from the journal authority, and its tests inject rows at `get_modifications`);
a summary of the task ids in the index (tasks spanning a fold and the
unattributed-timestamp fallback would still decode everything); reading the
whole index file per read (I/O proportional to the history).
Measurement: the live 17,719-row index locates in 0.32 s into 365 KB (386
conversations, about 20 bytes per row); its largest conversation (870 rows)
reads in 9 ms against 232 ms for the whole history. The write/read probe above
with a 32 MiB budget below the copied 41 MB history (three ABAB rounds,
medians): wall 11.5 to 1.3 s; write p50 534 to 72 ms and p95 1,709 to 600 ms;
read p50 251 to 2.7 ms with CPU 242 to 0.4 ms; whole-history decodes during the
40 reads 41 to 0, plus one or two index scans per run (concurrent first reads
each scan). Verify
`test_oversized_history_serves_conversations_without_reparsing_it`.

**Crash-artifact sweep.** Problem: every admitted cold history load (undo,
project open, content hydration, and the scoped edit batch, which loaded the
history on every call) scanned the payload directory for unreferenced crash
artifacts while holding the project admission; the 2026-11-01 stall dump caught
the holder in that scan while seven writers waited. An over-budget history is
cold on every load, so the scan repeated per call, and its entry window
(`_MODS_CACHE_CAPACITY * 8`, 1,536 entries at the default capacity) examined
only the same leading entries of an 11,935-entry directory, so later artifacts
were never reclaimed. Decision: the sweep holds the rewrite lock (taken without
waiting; a running fold or undo defers it) and the complete committed rows read
under it. A commit still between packing and publishing its row holds a shared
`flock` on its pack, taken before the pack had a name, and the sweep unlinks a
candidate only while holding it exclusively, so no per-file lock is needed
(holding every file lock cost one FUSE round trip per path ever written: 40 s
for 1,000 paths). A commit can publish its row (a record file, since the sweep
holds the rewrite lock) and drop its pack lock after the sweep read history,
so once every candidate is held the sweep reads the rows committed since that
read (`_rows_appended_since` from the read's journal identity) and keeps the
packs they reference. Artifacts only appear when a commit crashes or fails. A
process sweeps a session once, in one complete pass, on its first complete
load (the first write's validation or a cold read); write recovery sweeps again
whenever it reconciles a crashed writer's intents. Later loads and writes never scan.
The scoped edit batch validates the journal by identity (`_assert_journal_ready`)
like every other writer instead of loading it. An artifact of a failed commit
that leaves no intent (a command recording, an undo) waits for the next
process's sweep: it is reconstructible garbage, never history. Alternatives
rejected: the per-load pass (repeats a directory scan under the writers' lock
without new evidence); a crash marker written before each commit (one more
fsync per edit); sweeping without excluding in-flight commits (a fresh pack
looks unreferenced, and the age rule is not exclusion); keeping the entry
window (an arbitrary bound that leaves a once-per-process pass incomplete).
On the live session (11,935 packs, 17,268 rows) a complete pass is one
directory scan of about 0.1 s; it found no unreferenced pack. Verify
`test_crash_artifacts_are_swept_once_per_process_and_on_crash_evidence`,
`test_crash_artifact_sweep_is_one_complete_pass`,
`test_sweep_keeps_pack_whose_commit_published_after_the_history_read` and
`test_scoped_edit_batches_never_reread_or_sweep_an_oversized_history`.

## Verification

Executable contracts include `tests/tasks_pkg/test_run_command_scoped_snapshot.py`,
`tests/tasks_pkg/test_run_command_snapshot_bounds.py`, `tests/tasks_pkg/test_run_command_snapshot_content.py`,
`tests/tasks_pkg/test_run_command_snapshot_path_races.py`,
`tests/tasks_pkg/test_run_command_undo_evidence.py`, `tests/project_mod/test_project_undo_journal_receipts.py`,
`tests/project_mod/test_project_modification_durability.py`, `tests/project_mod/test_project_undo_path_boundary.py`,
and `tests/project_mod/test_project_undo_postimage.py`. Keep real filesystem substitutions,
FIFO races, disk-failure injection, same-size/coarse-mtime external edits,
concurrent append, reverse-order restoration and allocation measurements.

## Edit-batch preflight

`edit_file` defaults to `atomic: true`: all operations, canonical destinations,
source reads, matches, final UTF-8 encoding, intent-relative destination validation
and tracked file journal locks are planned before any requested file edit is
published. Canonical paths identify shared buffers and leases; each original
lexical destination retains its root containment and tracking policy. A symlink
cannot turn an attributed project write into untracked temporary scratch. Operations on each physical file see earlier
in-memory edits, even when different path aliases name that file. A failed
preflight returns zero applied edits, a failure for the rejected operations and
`SKIP` receipts for otherwise valid operations, in their original request order.
No undo, freshness or syntax-success receipt is earned by a skipped operation.
Only explicit `atomic: false` requests the legacy partial-application behavior.
The file-only scoped handler uses the same compute functions and whole-batch
preflight policy, while retaining descriptor-fenced I/O and its prohibition on
replaying pending recovery outside the grant.

This is **preflight atomicity, not a cross-file transaction**. A later actual I/O
or journal-publication failure can follow an earlier committed file; retain the
existing per-file intent/undo evidence and report applied-but-unconfirmed writes
rather than claiming rollback or suggesting a blind retry. Reconciliation of
pre-existing write intents remains the existing recovery owner's responsibility.

Problem: eager per-file publication left a partly changed project after a later
missing anchor, invalid path or unavailable journal. Decision: plan the complete
batch under the per-file edit mutexes, take the tracked file journal locks only
for a batch that will publish, then publish one transition per physical file
(see "Per-file journal locks"). Alternatives rejected: looser semantic matching can target
unrelated code; rollback after an external writer races us can destroy user work;
a second patch writer would duplicate authority, freshness and undo owners.
Verify `test_edit_file_atomic_preflight.py`, `test_project_dispatch_scope.py`,
`test_project_edit_demand.py` and `tests/tools/test_edit_result_meta.py`.

## Direct-write journal failure

Text writes, uploads, replacements, and batch edit groups require
an explicit successful journal acknowledgement when the target is tracked.
A missing acknowledgement or journal exception after atomic file replacement
returns `undo-record-failed`, with `mutationApplied: true` and
`undoRecorded: false` on structured results. Batch tool text retains that
failure tag and explicitly says the mutation was already applied and must not
be retried automatically. No rollback overwrites a concurrent user edit.

Tracked direct writes now persist a bounded intent before replacing the file.
The intent remains until the undo journal confirms the same stable `changeId`.
`lib/project_mod/write_recovery.py` reconciles intents when a project is opened,
its modifications are read, undo is requested, or another tracked write starts;
only intents whose file lock is free (no live writer) are reconciled.
Task/root lookup also recognizes pending intent attribution. Shell commands
retain their separate post-execution capture contract; this is not a transaction
for arbitrary subprocess or external filesystem mutations.

`contracts/project_write_intent_v1.schema.json` defines the intent. Preparation
preserves exact original bytes or explicit absence, logical project attribution,
the resolved root path and identity, the staged inode, content digest, and
conversation/task attribution. A symlinked project keeps its existing journal
identity, but a retargeted alias is a recovery conflict. The caller fsyncs the
stage; preparation fsyncs its directory before durably publishing the intent.

Direct edits retain complete original bytes, staged post-images and exact undo
identity without a private byte quota. `TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB` is
retired and ignored; changing it cannot hide pending durable evidence after
restart. Pending-intent and directory-entry counts no longer refuse preparation
or limit recovery discovery. Every recognized record still validates schema,
base64, root/parent/inode identity, content digest and stage ownership before it
can classify or authorize cleanup. Corrupt, retargeted and conflicting evidence
fails closed. Actual write/flush/validation failure still prevents publication.

Problem: a launch-derived 1–1024 MiB ceiling and 64-record/130-entry quotas
rejected valid writes and could make older evidence inaccessible after a lower
restart setting. Decision: preserve exact evidence and consume the complete
owned recovery set. Alternatives considered: larger constants repeat the same
failure; omitting preimages or deleting pending rows weakens recovery and undo.
This remains an inline exact-preimage journal and materializes complete bytes;
removing admission quotas does not imply constant memory. Command snapshots and
namespace transaction policies retain their separate owners/audit targets.

Verification: `test_project_write_intents.py` reopens 137 real pending records
alongside 180 auxiliary entries and classifies applied/unpublished/conflicting
states without losing evidence. `test_write_recovery_budget.py` covers original
and post-image bytes above the retired configured ceiling, exact multibyte undo,
large base64 evidence and recovery after a lower legacy setting. Crash, symlink,
inode-retarget, malformed-record and real evidence-I/O failure tests stay
mandatory. No durable history is reclaimed to satisfy a resource target.


Recovery never restores or overwrites user files. A matching staged inode and
content at the target proves publication. A matching stage still at its private
name plus unchanged original proves non-publication. Missing stages, replaced
roots, content-only matches on other inodes, and later edits remain conflicts;
the original bytes remain available and new tracked writes fail visibly.
Non-publication is durably marked `aborted` before reclaiming the identified
private stage, so interruption during cleanup is itself recoverable. Cleanup
never removes another inode that reused the stage name.

`journal_locks.py` provides the bounded, thread-reentrant file locks and the
rewrite lock (see "Per-file journal locks"). They are acquired before the cache
lock, which protects memory only, never filesystem I/O. Fork does not transfer
ownership: descriptor registration is serialized with fork, and the child
closes inherited lock descriptors from every thread without unlocking the
parent's open file descriptions, clears inherited reentrant state and must
acquire its own locks. Context cleanup releases only in the owning process, so
a reused child descriptor is not mistaken for an inherited lock. The registry
contains only active descriptors and in-process entries only while a lock is
held or awaited; it adds no retained lock cache.
Recovery publishes the prepared post-image proof, not a later user edit sampled
at replay time. Repeated stable IDs must match their original evidence and
confirm the journal again without duplicate history or listener publication.
After a confirmed journal commit, failed intent cleanup retains the intent for
idempotent cleanup on the next access; it never retries the file mutation.

### Contention and projection cost

The per-file edit mutex protects the exact canonical target; the file journal
lock covers intent recovery/preparation, atomic publication, the journal commit,
file-history staging and acknowledgement of that one attributed file. Writers
of other files never wait and never consume another live intent. Presentation
tickets stay unready until the edit mutex is released and write-freshness
evidence is current; listeners run after the thread's last journal lock is
released (`journal_locks.after_release`), in per-path, in-process commit order
(`post_commit.py`), with reentrant notifications following the current change's
complete listener list and no cross-path wait cycles. Failures of these
projections cannot revoke an already durable undo record.

Every tracked write validates its authority **before** preparing or replacing
the target, by identity (index, fold artifact, validated log offset, parsed
record files) rather than a full parse per edit; a malformed journal preserves
target bytes and recovery evidence unchanged. Prepared post-image evidence
avoids hashing again. Cache lookup validates the journal identity; a failed or
uncertain rewrite invalidates the projection. Count and conservative
resident-byte budgets both apply, derived from the host cache capacity (256 KiB
per capacity unit; a session may use the aggregate byte budget). Oversized
histories stay readable from disk, never resident in the LRU or the active
projection; durable history is never evicted. Compact JSON output is streamed
in 64 KiB chunks. After fork the shared cache RLock is reinitialized in place.

Contention waits up to the contract's `lockWaitSeconds` (30 s) for the same
file only. A pre-mutation timeout returns `journal-busy`, `retryable: true`,
`mutationApplied: false` and measured `waitedSeconds`, naming the file and its
holder; text batch receipts preserve the category and re-read guidance. Retry
requires a fresh read and recomputation, never blind replay. Partial or
unconfirmed writes retain their distinct non-retryable result semantics.
`edit_file` plans every file under its edit mutexes, takes the destinations'
file locks only when the batch will publish (a busy file fails only its own
edits), publishes, then reports and runs the syntax check after release. The
scoped file handler holds its granted files' locks and refuses only when an
ownerless (crashed) intent exists.

Executable coverage: `test_project_journal_contention.py` (listener isolation,
cross-project I/O independence, busy semantics, parallel undo, stale-write
rejection), `test_project_post_commit_order.py` (corrupt-journal
prevalidation, reentrant notification order, cross-process same-file history
order), `test_project_journal_cache_budget.py`, `test_project_journal_cache_records.py`,
`test_project_cache_lock_fork.py` and the specs listed under "Per-file journal
locks".

Recovery has no private original/post-image, encoded-record or
pending/directory count quota. Lock contention follows the file lock
contract. Malformed or conflicting evidence fails closed without eviction;
this is durable recovery data, not a cache. Descriptor lifetimes end with the
context, including process exit.

Journal snapshot publication flushes the JSON file before rename and then
flushes its parent directory. Removing an empty journal also flushes the
parent. A failed or unsupported directory flush is reported as a journal
failure; it is never silently treated as durable success. Any failed journal
mutation invalidates its cache, including interruption: rename/unlink may
already have happened, so subsequent operations reload disk rather than
publishing stale cached history over an uncertain commit.

Creating a session or direct-write parent directory publishes missing ancestors one at a time and
flushes each parent before creating the next child or writing a project marker.
A retry also flushes the deepest existing ancestor: existence alone does not
prove that an earlier failed flush persisted its name. Flush errors stop journal
initialization visibly. The walk uses memory bounded by path depth and opens
only one flush descriptor at a time; existing sessions require one parent flush.

Direct atomic writes set final permission bits on the staged file before its
file flush, replace the target, then flush the already-open parent directory.
A failure before replacement preserves the original. A directory flush failure
after replacement returns `write-durability-unconfirmed` with
`mutationApplied: true`, `durabilityConfirmed: false`, and `undoRecorded: false`;
batch outputs preserve the failure category. This outcome never suggests
retrying the mutation or moving a staged salvage file over the current target.
Each write owns one additional directory descriptor and releases it on every
terminal path. These guarantees do not make file and journal one transaction.

New-file staging uses exclusive random-name creation with requested mode
`0666`, so the kernel applies the current umask without changing process-global
permissions. Existing-file staging starts at `0600` and receives the original
mode before flushing. Name collisions retry at most 32 times and never truncate
or unlink the colliding file; failed allocation leaves the target unchanged.

Original-file reads are bounded before decoding or allocating recovery evidence and are required before overwrite: text writes and upload
replacement fail before mutation when the existing file cannot be read.
Text overwrite preserves the exact UTF-8 pre-image (including CR/LF and BOM)
or stores undecodable originals using the existing binary history encoding.
Append and replacement edits require strict UTF-8 and preserve
newline bytes during reads; lossy replacement decoding never becomes the
content to publish or the evidence to undo. Missing original bytes are never
interpreted as proof that an existing file was newly created.

Edit computations retain the original input before matching normalization.
The compact inverse is checked by applying it to the computed post-image;
if it would not reproduce the original (for example, repeated replacement
text, multi-match replacement, deletion, or newline normalization), the journal
uses a complete exact pre/post-image inverse. Actual evidence capture or
validation failure rejects the edit before write; no private byte cap clips it.
This keeps fallback undo exact even when optional file-history blobs are off.

## Atomic edit groups

A batch that computes several edits to one file publishes only one physical
replacement. Its undo journal likewise records one transition from the file's
original bytes to the final bytes, rather than recording nonexistent disk
states for each intermediate computation. Per-edit OK/FAIL output remains
ordered and unchanged. Attribution and the combined inverse are prepared
before writing; both sides of a multi-edit inverse retain complete bytes.
Failure to preserve that exact evidence refuses the group without mutation.
This also avoids partially persisting a chain of intermediate undo receipts.
Same-file exclusion covers preimage/freshness checks, computation, replacement,
persistence and journal acknowledgement. The edit mutex and atomic replacement do not by themselves make a multi-file
batch all-or-nothing or commit file and journal atomically; the separate batch
preflight/failure policy above still applies. Post-replacement durability/journal failures retain the
explicit mutation-applied outcomes above; external writers remain outside the
cooperative transaction.

## Complete edit batch admission

Problem: legacy replacement batches silently discarded entries after
30, while unified `edit_file` both declared and enforced the same private count
ceiling. A late failed preflight could therefore disappear and let an atomic
call publish changes whose complete requested batch was invalid. Decision:
process the entire ordered batch. Unified tool schemas no longer reject the
31st operation; result lines preserve every operation's original position,
including late invalid entries and subsequent valid work.

Grouped replacements still compute in order and publish one physical replacement
and one verified undo transition per file. Atomic preflight considers every
requested operation before publication. Path/read-only authority, freshness, real write failures and
recovery-evidence checks retain their owners; count admission grants no write
authority. Alternatives considered: raising the threshold still drops work;
separate small calls erase batch-wide preflight and grouping semantics.

`test_project_edit_demand.py` verifies schema admission and actual 64-edit
chains with exact undo, a failed 41st atomic preflight with zero publication,
a late invalid operation followed by successful work, 32 ordered additions
with one complete undo transition, and a physical failure on the 32nd of 40 files that leaves
39 completed files independently undoable and no false success history. Atomicity, write-failure, ownership and recovery neighbors
remain required; independent byte/recovery policies are separate audit targets.
