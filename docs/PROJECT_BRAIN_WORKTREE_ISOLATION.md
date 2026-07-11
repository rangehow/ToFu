# Project Brain — Per-Conversation Worktree Isolation

> **Status:** DESIGN — owner-directed 2026-07-11. Q1/Q2 RATIFIED; FUSE
> validation V2–V4 + the land primitive RUN FOR REAL and CONTENT-verified (§7.1
> — the git worktree model SURVIVES on the beegfs-fuse mount; no fallback to
> lighter isolation needed). The §5.1 land primitive was CORRECTED after the
> owner caught that a `commit-tree <conv-tree>` "merge" silently discarded 7 of
> 8 landers' content (last-tree-wins) while all 8 stayed reachable — the land
> now does a REAL 3-way merge and the gate is CONTENT-verified, not reachability.
> No refactor code until the model is ratified in full on paper. Tier-2
> bleeding-control (class-aware backoff + event-driven wait_paths) landed
> already (`a73f824`) and is NOT a substitute for this.
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
  conversations rebase onto and land into. **RATIFIED (Q1, owner 2026-07-11):**
  `tofu/integration` is a DEDICATED branch — autonomous merges NEVER land
  directly on the branch the human builds from. The human periodically
  fast-forwards `tofu/integration` into their working branch at their own
  cadence, giving a review / rollback seam and blast-radius isolation between
  the autonomous fleet and the human's trunk. Landing = CAS-merge into
  `tofu/integration` only (§5).
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
| `land_worktree(conv_id, files, msg)` | replaces `project_commit.do_commit` | commit conv branch, run acceptance gate at the *prospective* merge HEAD, then a REAL 3-way merge into integration under CAS (§5.1). Success = merge-result tree contains every landed change (content-verified). Conflict → report, do not force. |
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

- **RATIFIED (Q2, owner 2026-07-11):** each epic DECLARES its intended
  **write-set** at post/dispatch time — the set of files it expects to touch —
  and the dispatcher partitions on THAT declared write-set, with a coarse
  subsystem tag (e.g. `css`, `routes/api_v1`, `lib/conversations`) as the
  fallback when a precise set can't be given. `wait_paths` alone is explicitly
  REJECTED as the source: it only captures declared *sibling-blocks*, not the
  epic's full write footprint, so it under-counts and lets two epics that will
  both edit `styles.css` co-dispatch. The declared write-set is a new epic
  field (`write_set` JSON), set by `post_task` / `dispatch_epic`.
- `select_dispatchable` prefers epics whose declared write-set is **disjoint**
  from every currently-claimed epic's write-set — so two *live* conversations
  rarely touch the same file by construction. Missing write-set → fall back to
  the subsystem tag; missing both → treat as touching-everything (dispatch
  serially, conservative).
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
  4. ok? → CAS-serialized REAL 3-way merge into tofu/integration (§5.1);
           success = the merge-result tree contains every landed change
           (CONTENT-verified), NOT merely reachable/ancestor
  5. STOP. The human fast-forwards tofu/integration → their build branch at
     their own cadence (Q1). Autonomous landing NEVER writes the human's trunk.
```

- **Authorship** is the merge commit's parentage — structural, not inferred.
- **No contamination possible**: the conv branch contains only that conv's
  commits by construction.
- **The acceptance gate is the ONLY quality gate** — it already catches the
  split-brain (removed-symbol-still-referenced) that byte-identity could not.
- Agent-authored merges keep `--author 'Tofu Agent'` (the existing
  `_agent_author()` convention); the human remains committer.

### 5.1 Integration-ref land serialization — the primitive the file model needs

**Per-conv worktrees move collision off the FILES but reintroduce it on the
INTEGRATION REF** (owner's catch). N worktrees share one `.git`; two
conversations merging into `tofu/integration` at the same instant race on the
single ref update. Without serialization, "merge the branch" just relocates the
stall to `refs/heads/tofu/integration`.

**This is measured, not assumed** (§7.1): a BLIND concurrent `update-ref new`
loses 7 of 8 updates (last-writer-wins → silent lost merges). The fix is a REAL
3-way merge whose *result tree* is committed under git's native compare-and-swap
plus a bounded retry, NOT a home-grown lock.

> ⚠️ **CORRECTED PRIMITIVE (owner catch 2026-07-11).** The first draft used
> `commit-tree <conv-tree> -p OLD -p conv_branch`. That does NOT merge: it
> fabricates a commit whose TREE is the conv's OWN tree while merely *pointing*
> at `OLD` as a parent. Ancestry links up (so an `--is-ancestor` / reachability
> check passes for ALL landers — a FALSE green), but each lander's tree
> OVERWRITES the previous lander's — measured: 8 distinct-file landers → final
> tree held only `base.txt` + the last winner's file. **Seven of eight landers'
> content silently discarded, reported as success.** That is data loss, worse
> than the stall. The land MUST compute a genuine merged tree.

```
CAS-merge(conv_branch):                    # the land critical section
  for attempt in 1..MAX_LAND_RETRIES (50):
    OLD = rev-parse refs/heads/tofu/integration
    if is-ancestor(OLD, conv_branch):      # trivially fast-forwardable
        NEW = conv_branch tip              # tree already includes OLD's content
    else:                                  # true divergence → REAL 3-way merge
        lw = git worktree add --detach <scratch> OLD    # throwaway land-worktree
        if NOT git -C lw merge --no-edit conv_branch:   # genuine content merge
            git -C lw merge --abort; rm -rf lw; prune
            return CONFLICT → sync_worktree(step 1): resolve in the CONV's
                              worktree, never on integration (no clobber)
        NEW = git -C lw rev-parse HEAD     # the MERGE-RESULT tree (all content)
        rm -rf lw; git worktree prune
    if update-ref refs/heads/tofu/integration NEW OLD:   # CAS: only if unchanged
        return landed
    # lost the CAS race: integration moved under us → re-read OLD and re-merge
    jittered-backoff()
  return land_exhausted → post a board block, do not force
```

- **The merge is a REAL `git merge`** in a throwaway detached land-worktree
  checked out at `OLD`, so `NEW` is a true 3-way merge tree containing EVERY
  landed change, and a genuine same-file conflict falls out of `git merge`
  (→ resolved in the conv's own worktree, never silently "last-tree-wins").
  `git merge` (not `merge-tree --write-tree`, absent on git 2.11) is the
  2.11-compatible way to get a real merged tree. The scratch worktree is
  detached + immediately pruned, so it never collides with a live conv edit.
- **CAS is the serialization** — `git update-ref <ref> <new> <old>` fails
  atomically if the ref moved since we read `OLD`; exactly one writer wins per
  round and the losers RE-READ `OLD`, RE-MERGE (against the new tip), and retry.
  This is a much lighter primitive than a board soft-lease on the ref and needs
  no external lock; the board lease is reserved for the *worktree* (GC), not the ref.
- **Re-merge on every retry, not re-CAS the stale tree** — because a lost CAS
  means integration advanced, the losing lander MUST recompute its merge on the
  new `OLD` (the loop re-enters at the top), or it would reintroduce the
  last-tree-wins clobber at the ref layer. This is the subtle invariant the
  broken draft violated.
- **CONTENT-verified, measured (§7.1):** 8 concurrent distinct-file landers →
  8/8 landed, `git ls-tree` shows ALL 8 files + base present (no loss); a
  same-file conflict → exactly one lands, the other REPORTS conflict (not
  clobbered), integration holds one intact winner; ≤8 retries, 0 exhausted,
  fsck clean.
- **The acceptance gate (the slow part) runs BEFORE the critical section**, on
  the conv's own worktree. The merge itself is bounded; the retry window is the
  merge time, still short.
- **Exhaustion is a real (rare) outcome** — 50 failed CAS rounds means
  pathological contention; it posts a board block (class `[sibling]`, retried by
  Tier-2 event-driven wake), never a forced blind write.

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

**Gate:** V2–V4 RAN FOR REAL on the `beegfs-fuse` mount 2026-07-11 (see §7.1) →
**PASS**. The `git worktree` model survives concurrent FUSE writes + concurrent
integration-ref merges. **No fallback to lighter isolation is needed.** V5–V6
remain to be exercised during the build (they gate their own steps, not the
go/no-go).

### 7.1 Empirical results (2026-07-11, `beegfs-fuse` / git 2.11.0)

Probe: 8 worktrees off one `.git`, 80 concurrent commits, then concurrent
integration-ref land under two strategies. All `.git` on the FUSE mount.

| Check | Result |
|---|---|
| **V2** 8 worktrees / one `.git`, 80 concurrent commits | **PASS** — 8/8 add, 80/80 commit, 0 bad branch tips |
| **V3** lock behavior under concurrency | **PASS** — 0 stale `.lock`, no `index.lock` deadlock |
| **V4** GC | **PASS with constraint** — `worktree remove` is UNSUPPORTED on git 2.11; GC = `rm -rf <wt>` + `git worktree prune` (verified clean) |
| **REF blind** `update-ref new` (no CAS) | **FAILS by design** — only 1 of 8 survives (silent lost merges). NEVER use. |
| **REF CAS** `commit-tree`-fake-merge + `update-ref new old` | **FALSE GREEN — data loss** — 8/8 "landed" + all reachable, but final tree held only base + last winner's file; 7/8 landers' content silently discarded (owner catch) |
| **REF CAS** REAL 3-way merge (`git merge` in scratch land-worktree) + `update-ref new old` + retry (§5.1) | **PASS, CONTENT-verified** — 8 distinct-file landers → 8/8 landed, `ls-tree` shows ALL 8 files + base (no loss); same-file conflict → one lands + one REPORTS conflict (not clobbered); ≤8 retries, 0 exhausted, fsck clean |
| "corruption" seen on first pass | **reflog garbling ONLY**, not DAG/object/ref corruption — eliminated by `core.logallrefupdates=false` on the server repo |

**Three hard environment constraints this surfaced (bind the build):**
1. **git is 2.11.0** — NO `merge-tree --write-tree`, NO `worktree remove`. Land
   MUST use `commit-tree` + CAS `update-ref` (§5.1); GC MUST use `rm -rf` +
   `worktree prune`. A newer git would simplify this but is not assumed.
2. **The server `.git` MUST set `core.logallrefupdates=false`.** Per-ref reflogs
   are the ONLY artifact that garbles under concurrent FUSE appends; with them
   off, `fsck --connectivity-only` is clean after 80 concurrent commits + 8
   concurrent CAS merges. (A dangling reflog is not DAG corruption, but turning
   it off removes the noise and the (harmless) fsck warnings entirely.)
3. **Integration-ref land is CAS-with-retry, never blind** — the blind path
   loses 7/8 merges silently. §5.1 is not optional; it is the primitive that
   makes the whole "merge the branch" story not relocate the stall to the ref.
4. **The land must do a REAL 3-way merge and the gate must be CONTENT-verified,
   never reachability.** `commit-tree <conv-tree>` links ancestry without
   merging trees → last-tree-wins data loss that a reachability/ancestor check
   reports as SUCCESS. The land runs `git merge` in a scratch detached
   land-worktree and the probe asserts every lander's file is in the final
   `ls-tree`; a lost CAS forces a RE-MERGE against the new tip, not a re-CAS of
   the stale tree.

Probe scripts kept at `/tmp/fuse_worktree_probe*.sh` (not committed — throwaway
validation harness; the results + constraints live here). Re-runnable.

---

## 8. Build order (proposed — ratify before code)

0. **FUSE validation V2–V4** (§7.1) — DONE 2026-07-11, PASS. (V5 rebase-latency
   / V6 per-conv `_roots` isolation are exercised inside steps 3/2 respectively.)
1. `lib/conversations/project_worktree.py` — lifecycle primitives, dark, behind
   `TOFU_WORKTREE_ISOLATION=inproc` (no behavior change), with the soft-lease GC.
   Sets `core.logallrefupdates=false` on the shared `.git` (§7.1 constraint 2);
   GC via `rm -rf` + `worktree prune` (§7.1 constraint 1, git 2.11).
2. Per-conv `_roots` binding fix (§3.3) — prerequisite, independently testable
   (this IS V6).
3. Worktree-scoped path resolution (§3.1) + `run_command` cwd (§3.2), flag-gated
   (rebase latency here IS V5).
4. `land_worktree` land flow (§5) + the CAS-with-retry integration-ref primitive
   (§5.1, `commit-tree` + `update-ref new old`) reusing the acceptance gate;
   `project_commit` becomes the `inproc`-mode path only.
5. Dispatch-time write-set partitioning (§4) in `select_dispatchable` — add the
   epic `write_set` field to `post_task` / `dispatch_epic`.
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

## 10. Open questions

1. ~~**Integration branch identity**~~ — **RESOLVED (Q1):** dedicated
   `tofu/integration`; the human fast-forwards it into their build branch at
   their own cadence. Autonomous landing never writes the human's trunk. (§2.1, §5)
2. ~~**Ownership-set source**~~ — **RESOLVED (Q2):** each epic declares its
   intended write-set at post/dispatch; the dispatcher partitions on it, with a
   coarse subsystem tag as fallback. `wait_paths` alone is rejected (too narrow). (§4)
3. ~~**FUSE fallback threshold**~~ — **RESOLVED empirically (§7.1):** V2–V4 PASS
   on the real mount; no fallback needed. The bounded-retry primitive is CAS on
   the integration ref (§5.1), not a commit retry, and it is required regardless.
4. **Worktree disk budget** (STILL OPEN) — N worktrees share objects but each
   has a working copy; cap on concurrent worktrees per project? Suggest a soft
   cap = max concurrent dispatched convs, with LRU GC of idle worktrees past
   their lease. (§2.3 GC tuning.)
5. **Ref-land contention ceiling** (NEW, low-risk) — measured ≤8 retries at 8-way
   concurrency with no exhaustion, CONTENT-verified (all landers preserved, real
   conflicts reported); confirm `MAX_LAND_RETRIES=50` + jittered backoff is ample
   at the real fleet's peak dispatch width, or make it adaptive. Note each retry
   now re-runs a `git merge` (not just a ref poke), so the per-retry cost is the
   merge time — cheap for small diffs, worth watching for large ones.
