# Project Brain — Per-Conversation Worktree Isolation

> **Status:** DESIGN — owner-directed 2026-07-11. No refactor code until this
> model is ratified on paper. Tier-2 bleeding-control (class-aware backoff +
> event-driven wait_paths) lands in parallel and is NOT a substitute for this.
>
> **Scope:** replaces the entire commit-time contention apparatus
> (`project_commit` byte-identity gate, `project_ready` overlap-hold,
> contamination classifier) with structural authorship via isolated git
> worktrees, plus dispatch-time file-ownership partitioning.

---

## 0. The defect, named precisely

The pain — "autonomous promotion always gets blocked by other conversations" —
is **not** fundamentally "a shared working tree." It is **authorship-by-inference**.

`project_commit` decides "is this file mine to commit?" by *inferring* authorship
from byte-identity: a file is clean iff its on-disk bytes equal *this*
conversation's last recorded post-image (`_build_conv_version_map` →
`_classify`, `lib/conversations/project_commit.py`). That inference **cannot in
principle** distinguish:

- *"this hunk is mine"* — I wrote it, from
- *"this hunk is a sibling's that predated my first touch"* — my recorded
  post-image already *contained* the sibling bytes, so `disk == blob` holds and
  the file is declared CLEAN, and `git add` sweeps the sibling's hunks into my
  commit (the **false-clean trap**, journal 2026-07-11 passim).

No amount of tuning fixes an inference that structurally lacks the information.
Byte-identity proves "matches MY last write," never "contains ONLY my work."

**Worktrees are the right fix because they make authorship STRUCTURAL rather
than inferred.** Your worktree = your branch = your commits, by construction.
There is nothing to infer: the git DAG *is* the authorship record. Once that
holds, the byte-identity gate, the overlap-hold serializer, and the
contamination classifier all collapse into a single well-understood operation —
**merge the branch** — and real conflicts surface as ordinary merge conflicts, a
solved and finite event instead of a permanent land-time jail.

### 0.1 Consequent design rule: RETIRE, do not port

The following mechanisms exist ONLY to survive N writers in one tree. Under
worktree isolation they have no job. They are **retired**, not ported into the
new model:

| Retired | File | Why it dies |
|---|---|---|
| Byte-identity contamination gate | `project_commit.py` `_classify` / `_build_conv_version_map` | Authorship is structural — nothing to classify |
| Overlap-hold serializer | `project_ready.py` `_partition_by_overlap` | Conflicts are merges, not held clusters |
| "false-clean" hunk-audit discipline | (human ritual + memories) | No shared tree to contaminate |
| `wait_paths` as a *land-time* gate | `project_board.py` `_paths_waited_but_held` used at commit | Superseded by dispatch-time ownership (§4) |

**Kept and reused** (they are correct and worktree-orthogonal):
- `run_acceptance_gate` / `detect_orphaned_callers`
  (`project_acceptance.py`) — the fresh-worktree gate is *already* the proven
  `git worktree add --detach` seam on this FUSE tree; it becomes the pre-merge
  CI check.
- The board's soft-lease + `_effective_status` at-read-time expiry model — sound,
  deadlock-free; reused for worktree GC leases.

---

## 1. Why the shared tree is still the mechanism to remove

Even though the *defect* is inference, the *enabler* is the single tree: the
server is ONE process with ONE checkout, and every conversation's file tools
mutate it concurrently. Isolation removes the enabler so the inference is never
needed. The rest of this doc is the concrete plan.

---

## 2. (a) Worktree lifecycle per conversation

### 2.1 Model

- One long-lived **integration branch** (default `tofu/integration`, env
  `TOFU_WORKTREE_INTEGRATION_BRANCH`) is the project's moving trunk that all
  conversations rebase onto and land into. It tracks (or *is*) the human's
  working branch; landing = fast-forward/merge integration → the branch the
  human actually builds from.
- Each **active conversation** that does project work gets its own git worktree
  and a per-conversation branch `tofu/conv/<conv_id>` created off the current
  integration HEAD.
- Worktrees live OUTSIDE the primary checkout, under a project-scoped state dir
  (`.tofu_worktrees/<conv_id>/`, prefix per §3.6 artifact convention; it is a
  `.tofu*` name so every existing consumer already ignores/excludes it). They
  share the one `.git` object store via `git worktree` — cheap, no full clone.

### 2.2 Lifecycle operations (new module `lib/conversations/project_worktree.py`)

| Op | Trigger | Behavior |
|---|---|---|
| `ensure_worktree(project, conv_id)` | first project tool call of a task, or dispatch | create off integration HEAD if absent; reuse if present; return abs path. Idempotent. |
| `sync_worktree(conv_id)` | task start / before land | rebase conv branch onto latest integration HEAD (fast-forward when possible; on conflict, surface as a normal conflict for the conv to resolve — NOT a silent park) |
| `land_worktree(conv_id, files, msg)` | replaces `project_commit.do_commit` | commit conv branch, run acceptance gate at the *prospective* merge HEAD, then merge/ff into integration. Conflict → report, do not force. |
| `release_worktree(conv_id)` | task/conversation end + soft-lease TTL | GC: prune the worktree, delete the conv branch IF fully merged; keep if it has unmerged commits (never lose work). |

### 2.3 GC — reuse the board's proven soft-lease model, do NOT invent a reaper

A worktree is held by a **soft lease** exactly like a path lease
(`DEFAULT_LEASE_TTL_MS`, `_effective_status` at-read-time expiry). A crashed /
abandoned conversation's worktree is reclaimable after one TTL. GC NEVER deletes
a branch with unmerged commits — it downgrades to "orphaned, awaiting human or
re-dispatch." No background thread; prune piggybacks on the existing sweep tick
(`sweep_dispatch`), mirroring `_prune_expired_leases`.

---

## 3. (b) Worktree-scoped file tools — the actual hard part

### 3.1 Why this is the risk, and why it is tractable

The owner's flag is correct: the server is one process, one cwd. **BUT** —
verified in code — the project file tools do NOT depend on `os.getcwd()`.
`project_path` is threaded as an explicit **parameter**:

```
config['projectPath']            (lib/scheduler/_shared.py:52 — per-conversation)
  → _handle_project_tool(..., project_path)        (handlers/project.py:188)
    → execute_tool(fn, args, project_path, ...)    (handlers/project.py:307)
      → _EXEC_HANDLERS[fn](args, base_path, ...)   (project_mod/tools.py:894)
        → _resolve_base(base_path, rel)            (project_mod/tools.py:271)
```

So worktree-scoping is a **path-resolution change, not a `chdir`**: resolve the
task's `project_path` to *that conversation's worktree root* at task start, and
every read/write/grep/apply_diff already flows against it with zero further
change. There is no shared mutable cwd for file I/O to fight over.

### 3.2 The one real subtlety: `run_command`

`run_command` (`lib/project_mod/run_command.py`) DOES spawn a shell with a
`cwd`. That cwd must be the conv's worktree, not the primary checkout. It
already receives `base_path` — route its `cwd=` to the resolved worktree root.
Its process-tree kill / snapshot / destructive-guard logic is worktree-agnostic
and unchanged.

### 3.3 Multi-root `_roots` registry hazard

`_resolve_base` consults a process-global `_roots` registry with a documented
"conv-state race workaround" (`project_mod/tools.py:314-328`) that can resolve an
unknown root to `base_path` anyway — a latent cross-root clobber (this is patch
register item #3). Under worktrees this becomes load-bearing: the registry MUST
be keyed per (conv_id / task), never a shared process global, or conv A's tool
could resolve into conv B's worktree. **This binding is a prerequisite of the
refactor, folded into §3.**

### 3.4 File-history / snapshots

`make_snapshot` records post-images per `conv_id` (`commit_round.py`). Under
worktrees these are still per-conv and still correct; they become a *diagnostic*
record rather than the *authorship source of truth* (which is now the branch).

---

## 4. (d) Dispatch-time file-ownership partitioning — shift coordination LEFT

Today the brain detects collisions at **land time** (the most expensive moment)
and parks. The board already knows epic → paths (`wait_paths`, `dispatch_target`
authorship). Move the coordination to **dispatch time**:

- When `dispatch_epic` (`project_dispatch.py`) assigns an epic to a
  conversation, it also records the epic's **declared file-ownership set**
  (derived from the epic's `wait_paths` / design-doc file list, or a coarse
  subsystem claim).
- `select_dispatchable` prefers epics whose ownership set is **disjoint** from
  every currently-claimed epic's set — so two *live* conversations rarely touch
  the same file by construction.
- Overlap becomes a **scheduler constraint** (don't co-dispatch two epics that
  both own `styles.css`), NOT a land-time jail. If ownership sets must overlap
  (unavoidable hot file), the epics are *serialized* at dispatch — one waits for
  the other to land — which is the correct, bounded behavior.

Net: landing conflicts go from the common case to the rare case, and the rare
case is a real, finite merge conflict resolved inside the owning conversation's
worktree — never a permanent stall.

**This is the piece that actually cures the reported symptom.** Isolation makes
conflicts *survivable*; dispatch-time partitioning makes them *rare*.

---

## 5. (c) Integration-branch land flow (replaces the byte-identity gate)

```
land_worktree(conv_id, files, test_paths, message):
  1. sync_worktree(conv_id)        # rebase conv branch onto integration HEAD
       └─ conflict? → report to conv, resolve in-worktree, retry (no park)
  2. run_acceptance_gate(conv_worktree, files, test_paths, at_ref=<merge-base>)
       └─ REUSED unchanged: green AND selfConsistent (orphan scan)
  3. not ok? → return why (tests red / orphaned callers). Do NOT merge.
  4. ok? → merge/ff conv branch into integration (agent author, --author Tofu Agent)
  5. integration → human's build branch (ff/merge; the human's normal flow)
```

- **Authorship** is the merge commit's parentage — structural, not inferred.
- **No contamination possible**: the conv branch contains only that conv's
  commits by construction.
- **The acceptance gate is the ONLY quality gate** — it already catches the
  split-brain (removed-symbol-still-referenced) that byte-identity could not.
- Agent-authored merges keep `--author 'Tofu Agent'` (the existing
  `_agent_author()` convention); the human remains committer.

---

## 6. (e) Env-gated rollout seam — single-box stays byte-identical

Mirror `lib/rate_limit_store.py` exactly (lazy `get_store()` + `reset_for_test()`,
env-selected backend). New env: **`TOFU_WORKTREE_ISOLATION`**.

- **`inproc` (default, OFF):** byte-identical to today. `project_path` resolves
  to the primary checkout; `project_commit`/`project_ready` behave exactly as
  now. A single-box / desktop install sees ZERO change. This is the fail-open
  default and the release-safety guarantee.
- **`on` (opt-in):** `ensure_worktree` resolves `project_path` per conversation;
  `land_worktree` replaces `do_commit`; dispatch-time partitioning active.

The seam sits at path-resolution + the land verb, so flipping the flag is a
localized behavior swap, not a fork of the tool surface. Both modes run the SAME
acceptance gate.

---

## 7. (f) FUSE / DolphinFS validation plan — validate BEFORE the refactor

This is where it breaks, if it breaks. Validate `git worktree` semantics on the
actual DolphinFS/FUSE mount before committing to the refactor:

| # | Check | Method | Pass criterion |
|---|---|---|---|
| V1 | `worktree add --detach` works on FUSE | Already proven — `run_acceptance_gate` uses it in production today | ✅ pre-confirmed; re-confirm under concurrency |
| V2 | N concurrent worktrees sharing one `.git` | Script: create 8 worktrees, each commits, in parallel | no `.git/worktrees/*` lock corruption; all commits land |
| V3 | `.git/worktrees/<id>/` lock-file behavior on FUSE | Stress `git commit` in two worktrees simultaneously | no `index.lock` deadlock / stale lock; retry-clean |
| V4 | `worktree remove` + `prune` cleanup on FUSE | GC 100 worktrees | no leaked dirs; `git worktree list` clean |
| V5 | rebase/merge across worktrees on FUSE latency | time `sync_worktree` under cross-DC mount | within `cross_dc.py` tolerance; no corruption |
| V6 | worktree path resolution under the multi-root `_roots` fix (§3.3) | unit + integration: conv A tool cannot resolve into conv B worktree | strict per-conv isolation |

**Gate:** V2–V4 must pass on a real DolphinFS mount before ANY refactor code
lands. If `git worktree` proves unsafe under concurrent FUSE writes, the
fallback is a lighter-weight isolation (per-conv index files / sparse overlays)
— but that decision is data-driven, made after V2–V4, not assumed now.

---

## 8. Build order (proposed — ratify before code)

0. **FUSE validation V2–V6** (§7) — hard gate. Data first.
1. `lib/conversations/project_worktree.py` — lifecycle primitives, dark, behind
   `TOFU_WORKTREE_ISOLATION=inproc` (no behavior change), with the soft-lease GC.
2. Per-conv `_roots` binding fix (§3.3) — prerequisite, independently testable.
3. Worktree-scoped path resolution (§3.1) + `run_command` cwd (§3.2), flag-gated.
4. `land_worktree` land flow (§5) reusing the acceptance gate; `project_commit`
   becomes the `inproc`-mode path only.
5. Dispatch-time file-ownership partitioning (§4) in `select_dispatchable`.
6. Retire (§0.1) the byte-identity gate + overlap-hold once (1)–(5) are green
   under the flag and the flag defaults `on` — behind the strangler-fig
   invariant (legacy path stays until every consumer is migrated).

No step starts before the prior is green. Each step is env-gated OFF by default
until the whole chain is validated.

---

## 9. Relationship to Tier-2 bleeding-control (parallel, NOT a substitute)

Two small corrections land immediately in the CURRENT model to stop the
oscillation while §8 is built. They are explicitly interim:

- **(a) class-aware backoff** — a `[sibling]` block tracks the lease clock (retry
  at ~lease-release, no escalation); the exponential curve
  (`_block_cooldown_ms`) is reserved strictly for `[human-gated]`. A
  collaboration event must never ratchet an epic toward a 24 h sleep.
- **(b) event-driven wait_paths** — an epic waiting on a sibling path re-checks
  when the lease releases, not on a separate exponential sleep, removing the
  "sibling freed the path in 30 min but the epic still sleeps ≥1 h" latency gap.

These reduce pain in the shared-tree model; they do NOT remove the shared tree.
Worktree isolation (§0–§8) remains THE fix.

---

## 10. Open questions for owner ratification

1. **Integration branch identity** — is `tofu/integration` a new branch the
   human periodically merges to their working branch, or should landing target
   the human's branch directly? (Affects §2.1 / §5 step 5.)
2. **Ownership-set source** — derive the epic's file-ownership set from
   `wait_paths` alone, from the epic's design-doc file list, or a coarse
   subsystem tag? (§4.)
3. **FUSE fallback threshold** — if V3 (concurrent lock behavior) is marginal
   rather than clean, do we accept a bounded commit-retry, or fall back to the
   lighter isolation? (§7.)
4. **Worktree disk budget** — N worktrees share objects but each has a working
   copy; cap on concurrent worktrees per project? (§2.3 GC tuning.)
