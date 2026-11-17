# Project modification guidance

## Scope

This package owns model-facing project reads, writes, and bounded command
execution. Tool schemas/approval live in `lib/tools/`; project coordination and
Git publication have separate owners.

## Editing rules

- Resolve every target beneath an explicit authorized project root. Reject
  traversal, unsafe symlinks, ambiguous roots, and path changes between check
  and use.
- Reads and writes carry project/root attribution and freshness evidence. A
  stale snapshot or mismatched write set fails visibly instead of overwriting
  concurrent user work.
- Prefer atomic replacement and recoverable file history. Multi-file changes
  define rollback behavior; never broaden a requested deletion or cleanup.
- Command execution uses structured arguments where possible and explicit cwd,
  environment, timeout, output, process-tree, and cancellation bounds.
- Schema validation and model intent are not authorization. Preserve approval
  receipts and deny unattended side effects that require user consent.
- Redact secrets and bound diffs, file content, command output, artifacts, and
  retained history.

`write_intents.py` owns bounded prepared-write evidence under
`contracts/project_write_intent_v1.schema.json`; `write_recovery.py` reconciles
it on project access and before tracked writes. `modifications.py` owns the
committed undo index; `undo_payloads.py` owns immutable packed content and
selection-time hydration under `contracts/project_undo_payload_ref_v1.schema.json`. Both use `journal_locks.py` before the cache lock.
`undo_payloads.reclaim_orphans` runs once per process per session and again
when write recovery reconciles intent evidence ("Crash-artifact sweep"); never
put the payload directory scan back on a per-load or per-write path.
Read `docs/PROJECT_FILE_UNDO.md` for acknowledgement and conflict rules.

There is no project-wide journal admission (owner decision 2026-11-10,
"Per-file journal locks" in `docs/PROJECT_FILE_UNDO.md`): writes to different
files never wait for each other. `journal_locks.py` owns a cross-process file
lock per attributed path (`file-locks/<sha256>.lock`, held by a writer from
recovery and intent preparation through acknowledgement; busy refusal names the
file and holder) and the rewrite lock that serializes folds, undo and log
appends among themselves. Writers only ever *try* the rewrite lock: free, the
commit appends its row to `modifications.journal`; busy, it publishes
`records/<key>-<changeId>.json` and the next appending writer moves it into the
log. Never make a writer wait for the rewrite lock or for another file's lock,
and never reintroduce a project-wide lock around commits, recovery or reads.
Recovery reconciles only intents whose file lock is free; inspection reports
`writer_active` only while that file's lock is held. Commit cost stays
proportional to the edit: validate by identity (`_assert_journal_ready`), never
substitute a full history read. Reads take no lock and go through
`_metadata_history`/`_committed_rows`; never wrap a read-only projection in a
journal lock. Verify `test_project_journal_file_locks.py`,
`test_project_journal_records.py`, `test_project_journal_read_admission.py`
and the contention, payload and post-commit neighbors.

`workspace_lease.py` owns cooperative checkout/file exclusion and descriptor
lifetime for Tofu's own whole-workspace operations (delivery, integration
snapshots, checkpoints, dependency setup, Git maintenance), not authorization.
Model tool calls take no workspace lease (owner decision 2026-10-28): never add
lease admission, claim derivation or admission waits to `run_command`, the write
tools, MCP local paths or project dispatch. `git_guard.py` is the only workspace
protection for tool calls; `file_edit_mutex.py` keeps one write call's
read-modify-write atomic per resolved path and is never held across commands or
calls. See "Tool calls take no workspace leases" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`; verify
`test_tool_calls_take_no_workspace_lease.py`, `test_file_scoped_write_lease.py`
and the `test_workspace_lease*.py` suites.

`command_network_probe.py` owns the bounded complete HTTP-probe plan and the
curl/wget transfer plan (`prepare_file_transfers`) that execution uses;
first-option config suppression stays execution-only. See
`docs/runbooks/GIT_WORKSPACE_GUARD.md` and verify
`tests/project_mod/test_network_probe_lease.py`. It grants no authorization.

`process_group.py` owns bounded POSIX descendant retirement for foreground and
recovered commands. Its `descendant_pids` is the one owned-subtree discovery
(stdin sensors, search cleanup, activity supervision): it reads per-thread
kernel child lists and scans `/proc` only on a kernel without them, so never add
a per-caller whole-host scan ("Command stdin sensor discovery" in
`docs/RESOURCE_CONTROL.md`; verify `tests/tasks_pkg/test_run_command_stdin_probe.py`).
Leader exit is not group quiescence; cancellation must
await inherited descriptor closure. Stop, deadlines, the `kill` tool and the
cancel callback also retire groups that still-linked descendants created
(`timeout`, `setsid`): `escaped_descendant_groups` finds them before any signal
("Groups a command's descendants created retire with it" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`). Retirement never replaces a command's
decided result: `run_command` stops what a finished command left in its group
and turns an unconfirmed retirement into a typed warning that keeps the record
("A finished command's result survives its group's retirement" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`). Recovered records verify PID starttime
and the observed group before signalling. No arbitrary recorded PID is authority.
Use `post_commit.defer_publication` outside the edit mutex and lease scopes so
optional callbacks run after freshness and file release. Coordination decisions live in
`docs/modules/project_integration.md`, not a duplicate policy in callers.

`foreground_command_records.py` records each foreground command group (owner
incarnation, leader start time, tool-call id) until it retires or exits; a failed
retirement keeps the record. The boot step retires a dead owner's groups before
task and swarm recovery, and swarm recovery refuses to resume an interrupted round
whose record survives. Background commands keep their own records. Decision:
"Interrupted tool rounds resume their own agent" in `docs/RUN_CONTINUITY.md`;
verify `tests/project_mod/test_foreground_command_records.py`.

`foreground_command_slots.py` owns the per-task registry behind the task slots
that address one foreground command (`_subprocess_pid`/`_subprocess_pgid`
mirror, pid-stamped `_cmd_interrupt`, `_cmd_handoff_control`). Both runners
always run against a `CommandTaskView`, on every execution path, so concurrent
commands of one task stay individually interruptible, watched and stoppable;
never write those slots on a task directly. Decision: "Per-command slots on
every execution path" in `docs/contracts/TOOL_EXECUTION_POLICY.md`; verify
section 7 of `tests/tasks_pkg/test_run_command_interrupt.py` and
`tests/tasks_pkg/test_run_command_background_handoff.py`.

`activity_supervised_process.py` runs Git whose duration follows the volume
(the integration snapshot's whole-tree commands and the identity/ref reads
task admission depends on) without a total deadline: one fixed stall window,
`lib/git_runtime.GIT_STALL_SECONDS`, renews whenever any thread of its process
tree executes, and retirement reuses `process_group`. Never add a wall-clock
refusal there. `run_activity_supervised(input=...)` supplies a batched check's
stdin from an anonymous file, so a child that reads late never blocks the
waiting thread. `ActivitySupervisedExchange` applies the same window to one
long-lived child answering request after request (`check-ignore --stdin`
during discovery): the window runs only while an answer is awaited, and Stop,
a stall or close retires its group.
Verify `test_activity_supervised_process.py` and
`tests/storage/test_integration_snapshot_liveness.py`.

`post_commit.py` owns process-local ordered presentation notifications after
journal release; it is not a cross-process durable-history fence. File-history
staging remains under the file's journal lock. Cache identity/byte bounds stay in
`modifications.py`; authority is always read before direct file publication.

`write_tools/` search/replace matching is a ladder: exact → CRLF →
trailing-whitespace → unicode-escape → uniform-indent (relative-indent
re-framing) → unicode+indent, each rung ambiguity-guarded with located
errors; a miss returns the closest block to re-anchor from. Indent fallback
requires one uniform literal prefix shift, never independently stripped nesting.
`edit_file` defaults to whole-batch atomic preflight; only explicit `atomic=false`
permits partial application. It plans under its edit mutexes and takes the destinations' file
journal locks only to publish; report-only checks run after release. A
`journal-busy` refusal names the file, the holding tool call and its hold time;
verify `test_project_journal_file_lock_admission.py`. `apply_diffs` retains its default per-edit failure
isolation and accepts `atomic=true`. Both group one undo transition per physical
file; neither promises cross-file rollback after later I/O failures. Read
`docs/PROJECT_FILE_UNDO.md` for the preflight and applied-failure contract.

`edit_file` only replaces (owner decision 2026-11-06, "Replace-only edit_file"
in `docs/modules/tools_execution.md`); never reintroduce an insert operation or
an echo/newline guard. Verify the unified-edit, atomic-preflight and error-kind
tests.

`git_command_scope.py` is the single Git target resolver for approval, the
Git guard and the workspace-isolation guard, including shell cwd control,
prospective worktrees and Git metadata redirects. `command_analysis.py` owns risk/read-effect classification;
`git_inspection_execution.py` prepares shell and internal argv reads without
refreshing the writer index, replaying writes or changing config/path results.
Porcelain diff/dirty describe require private indexes even on modern Git.
`git_index_snapshot_exec.py` snapshots immediately before each shell query so
preceding writes remain visible; preparation owns cleanup after process retirement.
Preparation spawns the copy of that child pinned at its own import, never the
checkout file; the child refuses another argv shape with restart guidance. Read
"Long-running processes spawn helpers from their loaded source" in
`docs/DEFENSIVE_PATTERNS.md` before adding any by-path helper spawn.
New Git shapes extend these owners,
not a new parser in dispatch/runner. Verify `test_git_command_scope.py`,
`test_git_inspection_execution.py` and command/guard concurrency suites.

`command_structure.py` is the bounded shell-structure parser the
catastrophic-delete guard (``_catastrophic_delete_detail`` / ``_is_catastrophic_delete``
in `command_analysis.py`) and the dangerous-command guard (``dangerous_command_rule``,
the one judge of run_command, the desktop agent and the safety pre-hook) are built
on: it delimits simple commands, ``( … )`` groups, substitution bodies, heredocs,
``case`` clauses, ``[[ … ]]`` tests, arithmetic and function definitions the way
a shell does, and raises ``ShellStructureUnknown`` for what it cannot delimit.
Heredoc bodies, quoted strings and Git pathspec magic stay data; a shell's stdin
heredoc or ``-c`` string is judged as commands. Every run_command rewrite (grep
splice, find budget, cp splice, grep -r flags, Git read hardening/preparation,
curl/wget hardening) and the grep/scan guards read only
``command_analysis.command_rewrite_text`` (``shell_rewrite_text`` with
``command_heredoc.shell_script_heredocs``); never give a rewrite its own view or
splitter ("Command rewrites edit only command text" in
`docs/modules/tools_execution.md`). Both guards read programs through
``_program_views`` (assignments, wrappers, ``xargs``, ``eval`` and the runners of
``_RUNNER_GRAMMARS``: ``ssh``, ``su -c``, ``find -exec``, ``docker exec`` …); add a
runner there with its documented option grammar, never a second view. sudo/doas
options follow ``_PRIVILEGE_GRAMMARS``, which ``_unwrap_command_parts`` shares. Never reintroduce an ad-hoc string splitter or a
raw-text match for delete-target or dangerous-command judgement, and never turn
a parse failure or an unknown wrapper/runner option into a refusal unless the
text could run rm/rmdir/unlink or matches `DANGEROUS_PATTERNS`. The delete
guard follows ``control_structure`` (the parse's reserved words paired into the
compound commands a shell runs; ``case`` clauses are ``CaseClause``/``CaseEnd``
marker items) as an abstract interpreter: each path through a construct runs on
its own state copy and the paths join after it. Never mark single assignments as
"maybe" instead. Variables the command never binds come from the environment its
shell starts with, an explicit input: run_command passes the child environment it
built, the cwd-less pre-hook passes none and defers those operands ("Catastrophic-
delete guard control flow" in `docs/modules/tools_execution.md`). Words,
builtins, program words, aliases and the scripts a shell reads follow the shell
(``_expand_word``, ``_BUILTIN_EFFECTS``, ``_expanding_program_word``;
"Catastrophic-delete guard shell semantics"): a program word with expansions is
judged as each command its fields make, never by its raw text. Verify
`tests/project_mod/test_delete_guard.py`,
`tests/project_mod/test_delete_guard_structure.py`,
`tests/project_mod/test_delete_guard_control_flow.py`,
`tests/project_mod/test_delete_guard_shell_semantics.py`,
`tests/project_mod/test_delete_guard_shell_oracle.py` and
`tests/project_mod/test_dangerous_command_structure.py`.

`git_guard.py` owns the shared-checkout Git guard: ack-nonce lifecycle,
refusal text, and the read-only mergedness probe that downgrades
`git branch -D` on provably merged branches for tree-destroying Git commands
intercepted by the tool pipeline. Classification stays pure in
`command_analysis.py` (plain `branch -d` is self-guarding `safe_write`);
presence and probe subprocesses are only read. Read
`docs/modules/project_integration.md` for the coordination contract.

`integration_radar.py` records each conversation's written paths per Git checkout
under `<git-common-dir>/tofu-radar/` and turns them into notify-only advice and
notices ("Integration radar" in `docs/modules/conversations_project_brain.md`).
Keep the ordinary write path free of Git processes and whole-tree walks: Git runs
only for a candidate overlap, a first touch or a landing. It never waits, locks or
refuses. `landing_verification.py` verifies merged trees for the CI candidate
check and linear-checkpoint gate attribution; `git_land` runs no tests (owner
decision 2026-11-08). Only the test files the merge can affect run (recorded
runs decide), a record at the current tip answers its outcome without
rerunning, and only a reproduced regression counts as `regressed`. `impact_recorder.py` (pytest plugin, `sys.monitoring` plus an audit
hook) and `impact_selection.py` own the observations and selection policy;
`lib/storage_authority_sql/impact_cache.py` owns their reconstructible cache
next to the warm merge checkout, keyed by the project interpreter, a canonical
test environment (never the launching conversation's variables) and the
recording semantics only — selection/orchestration edits must not invalidate
evidence. Conftest dependencies are unit-level, but a helper reached from
import-time setup is still an input to every test in scope. No records means the static selector answers, and an unrecorded or
partially executed file is never trusted. `lanes.py` owns a branch plus a
recycled local checkout per unit of work, its saved uncommitted state
(`refs/tofu/lanes/<branch>`, written during settlement by
`commit_round/_commit.py`) and its placement (`PURPOSE_LANES` in
`serverctl_pkg/local_placement.py`). Contracts: "Landing verification" and
"Lanes" in `docs/modules/project_integration.md`. Verify
`test_integration_radar.py`, `test_landing_verification.py`,
`test_impact_selection.py`, `test_lanes.py` and both `test_git_land_*` suites.

`task_spares.py` owns the prepared checkouts task workspaces take instead of
checking out a whole tree, the recycling of retired task checkouts into them,
and their eviction near the placement reserve ("Task checkouts start at once"
in `docs/modules/project_integration.md`). Task startup never takes
`_repo_lock`, never captures the working tree of a repository that commits
its deliveries, and never lists every worktree; keep anything that waits or
walks the tree off that path. Verify `tests/storage/test_task_spares.py` and
`test_local_task_checkouts.py`.

`task_delta_delivery.py` owns automatic task-only three-way workspace merging/publication and proven-recoverable clone
reclamation; `workspace_runtime.py` owns lazy reconstructible dependency bridges (the task
`.venv` mirrors the source's `include-system-site-packages`; `suite_dependencies.py` checks and
installs the declared `test` extra; verify `tests/project_mod/test_worktree_test_environment.py`). Durable intent,
revocation and recovery stay in `delivery.py`; gates/snapshots stay in `integration_control.py`.
`delivery_retry.py` owns condition fingerprints, bounded conflict evidence and immutable scoped
repair proposals under the workspace delivery retry/repair schemas. Verify `test_continuous_delivery.py`.
Task completion only records intent (`integration_control.settle_task_workspace`); the receiver
lanes capture, merge, publish and reclaim. `_repo_lock` covers delta-sized publication: never put a
whole-checkout capture, merge computation or checkout removal back under it, and recompute a
changed capture under admission rather than retrying it unlocked ("Completion acknowledges durable
intent" in `docs/modules/project_integration.md`; verify `test_delivery_nonblocking.py`).
A `working` receipt's terminal intent takes only its checkout's `_destination_admission`; only a
revocation of any other receipt also takes `_repo_lock`, inside it. Per-task refs go only through
`integration_control.retire_task_refs` once the branch holds the delivered commit ("Repository
admission at scale"; verify `tests/storage/test_repository_admission_scale.py`).
Triage a `task_delta_conflict` from the receipt's
`origin.delivery.retry.conflictPaths`/`conflictPathCount`
(`GET /api/v4/project/integration/status?path=`), not from feed narratives,
which are capped at 720 bytes. A reason `three-way merge conflicts at:` names
the unmerged paths; `task and canonical both created different content at:` is
an add/add conflict, checked before the three-way apply. Both are computed from
`_delta_base` (the merge base of checkpoint and canonical when the Worker merged
the branch), never from the registered baseline alone ("A Worker that merged
the branch delivers from the merge base" in `docs/modules/project_integration.md`;
verify `test_delivery_merged_branch.py`).
The resume pass reads only `integration.workspace.delivery_action_page` rows (complete rows
whose receipt asks for action) and runs only when due: start/error recovery, a committed
`DELIVERY_STATE_OPERATIONS` hint, or rows left by the last pass. Never reintroduce a
per-tick history scan or a per-row reread; a new receipt state that needs action changes
both authorities' filter. Decision "Delivery resume demand" in
`docs/modules/project_integration.md`; verify `test_delivery_resume_demand.py`.
The worker loop only dispatches: rows run on per-project receiver lanes
(`_queue_on_project_lanes`), one at a time per canonical project root. Never run row
work on the loop thread or let one project's row wait for another project's lane; never
add a lane cap, capture size limit or deadline. A settled conflict is rechecked at its
task-delta paths (`task_delta_delivery._canonical_path_entries`) before any whole-checkout
capture, and only once an input `_hold_observation` sees has changed: never move that
gate under the claims lease, and never let a lane's own lease release wake the worker.
Decisions "Receiver lanes" in `docs/modules/project_integration.md`, "A settled
conflict is rechecked at its own paths" and "A held conflict is rechecked when an input
changes" in `docs/CONTINUOUS_DELIVERY.md`; verify `test_delivery_project_lanes.py`,
`test_continuous_delivery.py` and `test_delivery_integrity.py`.

`managed_repository.py` prepares a plain folder or empty repository for isolated workers
so people never run Git: one baseline commit, a content policy in `.git/tofu-managed.json`
rendered into `.git/info/exclude`, and a create-only default `AGENTS.md`
(`managed_repository_guidance.py`). A repository with its own history is changed once, when it
adopts delivery commits at its first delegation (`_record_for_workers`; Workers start from the
branch).
Bulky data is shared through links in worker checkouts and never enters a snapshot. The
decision record is "Worker workspace preflight" in `docs/PROJECT_COORDINATION.md`; verify
`test_managed_repository.py`, `test_managed_repository_large_folders.py`,
`test_worker_workspace_refusal.py` and the plain-folder file-lifecycle and reclamation tests.
Every staging of a managed repository (`integration_control._alternate_index_tree`,
`lanes.capture_working_tree`, `linear_git_checkpoint`) withholds the untracked paths the
recorded policy never versions (`unversioned_paths`, a private `core.excludesFile` from
`excludes_file`), unread, and `checkout_reconstructible` keeps a checkout that holds them.
Never add a size or count cutoff to staging or record those paths in the manifest. Decision
"Staging withholds what the content policy never versions" in
`docs/modules/project_integration.md`; verify `test_managed_repository_staging.py`.

`managed_repository_discovery.py` owns the pruned, resumable walk for folders whose root
holds no project manifest (`PROJECT_MANIFESTS`), or whose owner chose to share nested
projects: it never enters nested repositories, environments, nested projects or paths the
folder's own ignore files hide, and journals each listing outside the folder
(`integration_control._control_root()/preparations/`). Preparation supplies the skip
predicate (`_ignore_rules`: one supervised `check-ignore --stdin` exchange plus the
runtime patterns), progress and the journal; discovery stays standard-library only. A
folder with other projects but no root manifest is refused as `ambiguous_project_root`
until the owner's answer is supplied (`nested_projects=shared|versioned`, recorded as
`nestedProjects`), never guessed. `shared_folders=[...]` (recorded as `sharedFolders`)
additionally names whole read-only run/reference folders to share, the answer for a
project whose `work/` holds distributions, wheels, bundles and reports rather than
nested projects. `preview_preparation` / `scripts/prepare_project_folder.py` report a
folder read-only (scratch repository and journal only) and apply those answers.

`glob_match.py` is the one dialect for the `search_files` include and the
`find_files` pattern on every backend: the index prefilter and Python walkers
call `ProjectGlob.matches`, rg gets the validated glob, fd only `name_only`
globs, GNU grep only `grep_includes`. Never filter those with `fnmatch` or a
second translation; the run-command `find -name` lane keeps GNU fnmatch.
Verify `test_glob_match.py` and `test_tree_index.py::TestGlobSyntax`.

`tree_index.py` owns coalesced root builds and directory scans; `read_tools.py`
owns index-search/count chunks. Both use the shared demand executor with idle
reuse hints, never active count quotas. Incomplete index candidates stay out of
the cache; index search failure uses the live backend. Verify tree-index and
grep-withheld tests plus the demand/cleanup boundary in the tools domain map.

`tree_index_coverage.py` owns which files the index lists: Git's view of the
work tree (tracked files plus untracked files Git does not ignore) under Git's
exact ignore semantics, with `.ignore` outranking Git as in ripgrep and
`IGNORE_DIRS` keeping untracked bulk unwalked. Never approximate gitignore
matching or drop tracked files for a pattern shape; extend the Git oracle specs
in `test_tree_index_git_coverage.py` instead ("The file index lists what Git
lists" in `docs/modules/tools_execution.md`).

`tree_index_storage.py` owns automatic local index placement and evict-only
blob retention, using `serverctl_pkg/local_placement.py`. Capacity failure
skips persistence, never truncates index contents or denies file work. See
`docs/INDEX_FASTPATH.md` and `tests/project_mod/test_tree_index_storage.py`.

`content_shadow.py` owns demand-filled read-only search copies and cross-process
cache leases. `read_tools.py` retains complete search, paging and error behavior.
Keep source freshness checks, live retry, original deadlines and exact filename
rewriting; never turn a cache-size limit into a candidate/work limit. See
`docs/INDEX_FASTPATH.md` and `test_content_shadow.py`.

## Verification

`verification.py` is the default incremental development front door (`make test`);
explicit and changed-snapshot scopes share the selector and recorded dependencies. It shares lane
snapshot capture, landing execution and evidence storage. `verification_index.py`
owns immutable source-index preparation, with the selector retaining import
policy. `verification_dependencies.py` expands cold selection through Python
imports; `scripts/test_execution.py` owns runtime arguments separately from
selection. `landing_verification check` is the clean CI candidate entrypoint.
Read "Incremental verification" in `docs/modules/project_integration.md`;
verify `test_verification_defaults.py`, `test_verification_engine.py`, `test_verification_index.py`, completion,
cache, landing and lane tests. Measurements use `scripts/benchmark_verification.py`.
CI scope, cold dependency closure and execution-owner migration have focused
specifications in `test_verification_ci.py`, `test_verification_dependencies.py`
and `tests/meta/test_verification_policy.py`.

`receipts.py` owns optional device write envelopes defined by
`contracts/project_file_receipts_v1.schema.json`; verify with
`tests/project_mod/test_project_file_receipts.py` and the remote paper browser journey.

Run focused project read/write/command tests, then atomicity, rollback, root
attribution, freshness, symlink/traversal, approval, cancellation, and concurrent
writer tests.

`tree_index.py` walks complete authorized trees with windowed directory Futures.
Entry retention targets cannot truncate builds, reject persisted indexes, or
prevent write-fresh checkpoints. Deep trees and expired legacy walk deadlines
remain complete; actual read failures never install a partial index. Verify
`test_tree_index.py` and `test_tree_index_scan_pressure.py`.

Project grep searches complete source files, including those above the retired
2 MiB default. `grep_candidates` sizes are scheduling observations. Python find
has no private entry-count cutoff; result/time policies remain with search.

Batched read/grep/find operations execute every entry and retain every receipt.
Presentation quotas belong to the shared result envelope, never batch slicing
or early loop termination. Verify `test_project_batch_demand.py`, range,
projection, source-recovery and settlement neighbors.

Whole plain-text reads retain their complete source regardless of size or
data-file heuristics. Explicit ranges remain exact; presentation belongs to
the result envelope. `MAX_FILE_SIZE` still belongs to modification capture,
not reader admission. Verify `test_project_file_demand.py` and range/facade
contracts before changing that independent history owner.

Edit batches process every requested operation. Count thresholds cannot cut off
preflight, hide late failures, or remove result lines. Preserve grouped physical
writes/undo and whole-batch atomic preflight; verify `test_project_edit_demand.py`
with batch atomicity, write-failure and recovery neighbors.

Write intents preserve complete byte evidence and discover all pending records;
legacy recovery-budget values cannot hide durable state. Identity, digest,
regular-file, corruption and real I/O guards remain mandatory. The physical
extent compatibility helper is not an admission knob. Verify write-intent,
crash-recovery, byte-roundtrip and grouped-undo tests.

`delivery_acceptance.py` binds actual immutable-candidate checks to source and
checker revisions. The existing workspace origin retains one bounded receipt;
missing checks never inherit task completion. Read `docs/WORKSPACE_ACCEPTANCE.md`;
verify the SQLite/native acceptance tests and built-app integration browser test.

`effect_reconciliation.py` binds tracked local write publication to the common
execution effect and reads existing undo/intent evidence without replay. Read
`docs/contracts/TOOL_EFFECT_RECOVERY.md`; verify file-effect reconciliation,
selected-store restart, and owner-scoped inspection tests.
