# Old repository recovery: owner-memory work (2026-11-17)

Point-in-time report on the memory-mechanism work done on the zw05 host in the old checkout
`/path/to/your/data (symlink to `…/INS/your-username/tofu`).
It complements [`OLD_REPO_RECOVERY_INVENTORY.md`](OLD_REPO_RECOVERY_INVENTORY.md). Delete it with that file once the triage is done.
Report only: no code was changed.

Verdicts were checked against the shared branch `eval/opencode-glm53-tb21` at `b38b99f700` and re-checked at `c00ac710fe` (fast-forward,
no memory-path change in between).

## Answer

Most of the work was picked up. Three parts were not:

1. **Phase 1, the always-present owner profile, is implemented and tested but NOT merged.** It is on two recovered WIP refs only.
   The current docs still say "Phase 1: not yet authorized". The unmerged source says the Coordinator dispatched it on 2026-11-13.
2. **The experiment arm code is missing from this repository.** That covers experiments 1, 1b and 1c, the K3 resolution and FM-2x2
   (`evaluations/owner_profile_arm/`, PREREG/RESULTS/RUN).
   - Its five `refs/worktree/memory-design/*` refs are per-worktree refs. The recovery's `for-each-ref` import did not see them.
   - Their commits are absent from this object store. They survive only in the old object store and in three bundles under
     the old `.eval-runs/`.
3. **All experiment data stays on the old path.** The current `.eval-runs/owner-memory/` is an empty directory. The benchmark doc's
   reproduce section and its "accepted baseline" path therefore point at nothing on this host.

Already merged: the benchmark harness, the design record and every experiment result (as prose), MY-CONTEXT-FIX (Phase 0),
and MEMORY-SELF-QUERY (Phases 2 and 4: `search_memories mode=owner`, trust classes, lesson lifecycle, secret scan).

The sibling Worker (assignment 5060301b) has no memory item in its list. Its transcript covers Tofu-DB hotfixes, router residue, mailbox
and write-amp lines, sync cursor-lineage and the R4b/R5 release-candidate lines. Its two input checkpoints (`c8bc9206ca`, `40f815781f`)
change nothing under `lib/memory`, `tests/memory` or `evaluations/owner_memory`. One caution: Phase 1 is also merged into the
unpublished R4e integration WIP (`recovered/workspace-wip/project-worker-146c74d4…`, "R4e: merge MEMORY-PHASE1-1 … (item 4)").
If anyone ports R4e as a whole, Phase 1 comes with it. Coordinate before porting it separately.

## Method (read-only on the old path)

- **Current repo.** `git log --all --not HEAD --source` over `lib/memory`, `evaluations/owner_memory`,
  `docs/evaluations/OWNER_MEMORY_BENCHMARK.md`, `tests/memory` and `tests/evaluations` found 252 unmerged commits.
  - This covers all imported `recovered/*`, `refs/tofu/*`, `gate-*` and other refs.
  - The commits were then filtered to memory subjects and to work after the 2026-10-22 rebuild.
- **Inventory TSV.** Searched with `memor|lesson|my.?context|recall|owner.memory` (65 rows, mostly RAM-"memory" perf work).
- **Old repo.** Only `cat`/`ls`/`head` of `.git/worktrees/*/{HEAD,gitdir,refs/worktree/*}`, `.eval-runs/owner-memory/**` and the bundle headers.
  - Old commit objects were read from this repo with `GIT_ALTERNATE_OBJECT_DIRECTORIES=<old>/.git/objects`, which reads and never writes.
  - No git command ran with the old repo as its repository.
- **Residue test.** Per ref: added lines of `merge-base(HEAD, ref)..ref` on memory paths that do not exist in HEAD's file.
  The scratch script is `residue.py`; it was not committed.
- **Port check for Phase 1.** `git apply --cached --3way` of `4250f8c566^..75e4fc604e` into a throwaway index (`GIT_INDEX_FILE` in `$TMPDIR`).
  - This wrote only loose blobs into this repo's object store.
  - Git 2.34 has no `merge-tree --write-tree`.
- **Host-local zw05 paths are gone on this host.** These are the fm2x2 worktree gitdir
  `/home/your-user/.local/state/tofu/workspaces/40b71a32e552/…/project-worker-16e81e08…` and `~/.local/state/tofu/lanes`.

## Items and verdicts

Dates are commit dates (+0800). Author is `your-username` unless stated; "worker" means the platform worker id.

### A. Not merged — product code

| Item | Tip / date | What | Evidence | Verdict |
|---|---|---|---|---|
| MEMORY-PHASE1-1 (worker 848a7f63) — `recovered/workspace-wip/project-worker-848a7f63-998e-5b16-8680-a828a4030-16d1ebb837` | `5ffb087afa` 2026-11-14 08:25; WIP `977d15e04b` adds no file change | Phase 1 owner profile: `4250f8c566` failing-first specs (`tests/memory/test_owner_profile.py`, 307 lines), `75e4fc604e` implementation, `a4e11e0d64` + `5ffb087afa` docs/measurements | not an ancestor; only refs containing `75e4fc604e` are this WIP and 146c74d4; HEAD has no `tests/memory/test_owner_profile.py`, still has `CONTEXT_CHAR_CAP = 2500` (`lib/memory/user_profile/_context.py:72`) and `USER_PROFILE_CHAR_CAP = 2500`; residue 465 of 1,377 memory-path lines missing (all of `_render.py` 84/91, `_providers.py` 23/26, `contracts.py` 21/22, user_profile 60/61, tests 245/245). 3-way apply on HEAD: every source, test and doc file applies; only the 9 generated i18n catalogs conflict (rebuilt from sources anyway) | **NOT merged** |
| R4e integration WIP (worker 146c74d4) — `recovered/workspace-wip/project-worker-146c74d4-764f-59cd-b59d-6bfc27ce5-7ec0d2047f` | `2c60c31434` 2026-11-14 10:59 | R4e release integration; item 4 is `31af55d4e8` "merge MEMORY-PHASE1-1 5ffb087afa" | same Phase 1 residue; R4d was published to canonical as `d12cd290ae`, R4e never was | NOT merged (memory content = row above) |

What Phase 1 does (`75e4fc604e`, design text at `5ffb087afa:docs/modules/context_engineering.md` "Owner profile (Phase 1)"):

- **Carrier.** My Context renders as experiment 1c's carrier, from production owners only (`lib/memory/user_profile`, the composer's `user_context`).
- **Content.** Only active items whose trust class is owner-stated or verified, each whole, with id, date and source.
  An inferred, needs-confirmation or superseded item never renders.
- **Grants.** A new `delegation_grant` item type renders only inside the bounded section with the five verbatim policy quotes that 1c used.
  A spec checks the quotes against their authorities. Settings gains a section for it.
- **Roles.**
  - Receive it: Global Router, Project Coordinators, Workers (without identity items) and the owner's chats.
  - Never receive it: the Watchdog, sub-agents, third-party text and headless callers.
  - A disabled block records no `preferencesApplied`.
- **Size.** The 1,000-token `user_context` ceiling and the 2,500-character caps are removed. The block is whole, or omitted whole
  under global budget pressure (`global_budget_policy='omit'`).
- **Shared owners.** The source-class mapping and the identity audience move to `lib/memory/contracts.py` and `coordinator_policy`.
- **Measured (scratch store, 30 active statements).**
  - The carrier is 1,900 tokens. That is 0.46% of the Coordinator's round-0 request (410k) and 0.60% of the Router's (314k).
  - Directive lines are byte-identical to 1c's tested carrier.
  - Output length on the full-context Coordinator rose +75% (95% CI +45% to +117%), almost all of it from the bounded delegation section.
    1b's directives alone gave +7%.
  - The registered time-matched run could not run: the benchmark AppId returned 401, then 429.

### B. Not in this repository at all — experiment code (lane-only)

| Ref (old per-worktree ref file) | SHA / date | Contents | Present here? | Verdict |
|---|---|---|---|---|
| `refs/worktree/memory-design/exp1` (worktree project-worker-3c5cec07, HEAD `81c381f522`) | `ef96531e2d` 2026-11-09 21:11 | arm v1 + PREREG + RESULTS "killed by K5" (`bc041a4bc3`…`40e742182e`); 11 files, +1,364 | **missing** (`git cat-file -e` fails) | NOT merged; results merged as prose |
| `refs/worktree/memory-design/exp1b` (same worktree) | `163974e265` 2026-11-10 08:09 | 1b arm, PREREG, RESULTS (INCONCLUSIVE K4) | **missing** | NOT merged; results as prose |
| `refs/worktree/memory-design/exp1c-proto-prev` (worktree fae36d7e) | `64aa54510c` 2026-11-11 16:43 | pre-registration of 1b K4 resolution + 1c, plus an old lane/delivery base | **missing**; superseded by the rebased `bd75dd8b23` inside exp1c | superseded |
| `refs/worktree/memory-design/exp1c` (worktree fae36d7e) | `0748e1f064` 2026-11-13 01:11 | 1c arm, results, K3 amendment A3 + resolution (`bfb118b431`); `owner_profile_arm/` 8 files, RESULTS 661 lines | **missing** | NOT merged; results as prose |
| `refs/worktree/memory-design/exp1c-harness` (worktree fae36d7e = its HEAD) | `97122348ba` 2026-11-13 01:11 | harness + docs checkpoint | present as `recovered/wt/project-worker-fae36d7e…-head`; residue 16 lines (doc wording) | merged (content) |
| `refs/worktree/memory-design/fm2x2` (worktree `-preparing-01794529001583891392-d0643158`, HEAD `a46fc77986`, gitdir host-local and gone) | `7f641bf390` 2026-11-13 14:07 | FM-2x2: `stale2x2.py` (881 lines), `label_render.py`, PREREG A4 (1,129 lines), RESULTS (793 lines), `test_owner_profile_arm_stale2x2.py`; 23 files, +5,665 | **missing** | NOT merged; results as prose |
| `recovered/wt/project-worker-efd83bf5-…-head` | `5d4eca36af` 2026-11-11 | the only imported copy of `evaluations/owner_profile_arm/` (as of 1c pre-registration) | residue 1,934 of 2,609 lines: all of `owner_profile_arm/*` and `tests/evaluations/test_owner_profile_arm.py` | NOT merged (older subset of exp1c) |

Bundles that hold the missing commits (verified headers):

| Bundle | Size (bytes) | Ref → SHA |
|---|---:|---|
| `<old>/.eval-runs/owner-memory/design-arm-1b/private/lane-experiments.bundle` | 64,627 | exp1 → `ef96531e2d`, exp1b → `163974e265` |
| `<old>/.eval-runs/owner-memory/design-arm-1c/private/lane-experiments-1c.bundle` | 152,791 | exp1c → `0748e1f064`, exp1c-harness → `97122348ba` |
| `<old>/.eval-runs/owner-memory/fm2x2/lane-experiments-fm2x2.bundle` | 153,751,493 | fm2x2 → `7f641bf390` |

The current docs (`docs/modules/context_engineering.md`, FM-2x2 entry) name `refs/worktree/memory-design/fm2x2` and that bundle as
the home of the code. The ref no longer exists anywhere reachable from this repository.

### C. Merged (content on the branch) or superseded

| Item | Tip / date | Evidence | Verdict |
|---|---|---|---|
| MEMORY-EVAL harness — `recovered/wt/project-worker-c7ab3291-…-wip` (uncommitted) | base `33ce4175d7` | delivered as `b8fa0d18a1` (2026-11-09, 15 files +2,402); 109/2,126 lines differ = later edits | superseded by `b8fa0d18a1` + later |
| MEMORY-EVAL follow-up — `recovered/wt/project-worker-816d6a09-…-wip` | base `fdc544fc4a` | delivered as `04c92cd98d`; residue 0/120 | merged |
| MY-CONTEXT-FIX — `recovered/wt/project-worker-cea3e236-…-head` | `dc05508ee3` 2026-11-09 | same subject landed as `66169ec9bb`; 37/336 residue is in `_legacy_audit.py`/`_context.py`, rewritten by `2b51946690` "fail closed without the install audit" | superseded by `66169ec9bb` + `2b51946690` |
| MY-CONTEXT-FIX fail-closed — `recovered/wt/project-worker-129781c6-…-head` | `ef8ca28828` 2026-11-09 | residue 0/77; delivered `2b51946690` | merged |
| MEMORY-SELF-QUERY — `recovered/wt/project-worker-735b5f36-…-head` | `524844f02e` 2026-11-13 | delivered in `1d5db9eae8` (`lib/memory/owner_memory.py`, `secret_scan.py`, prefetch); residue 15/1,562: 8 lines of `prefetch/_query.py` replaced by `6a66c62932` (2026-11-16), 6 test lines | superseded by `1d5db9eae8` + `6a66c62932` |
| MEMORY-SELF-QUERY earlier heads — `recovered/wt/project-worker-32c9503b-…-head` (`eb415abf37`), `recovered/wt/project-worker-a85f0e9b-…-wip` (uncommitted, base `81c381f522`) | 2026-11-12 / 11-10 | drafts of the same work; residue 32 and 113 lines in files `1d5db9eae8` rewrote | superseded by `1d5db9eae8` |
| MEMORY-1D (design record + FM-2x2 docs) — checkpoint `a46fc77986` | 2026-11-13 14:18 | merged in R4d `9a5425dcb6` (second parent `a46fc77986`) and published as `d12cd290ae`; benchmark doc and harness files identical to HEAD, design record differs only in unrelated tokenizer text, `corpus.py` only in a 2-line docstring | merged (via `d12cd290ae`) |
| MEMORY-1D WIP — `recovered/workspace-wip/project-worker-16e81e08-…` | `dd5d2875f6` 2026-11-13 | residue 2/914 (the same `corpus.py` docstring) | merged |
| 1b doc lane — `recovered/wt/project-worker-2700fd47-…-head`; `recovered/wt/project-worker-fae36d7e-…-head` | 2026-11-10 / 11-13 | residue 10 and 16 lines, doc wording only | merged (content) |
| `gate-r4b/v10` memory commits (`70ea2b5c93`, `0bf4cab5ce`, `e3d4c6cb61`, …) | 2026-11-10…14 | residue 4/938 (`tests/memory/test_memory_role_fit.py`) | merged |

### D. Pre-rewrite (before 2026-10-22), obsolete

These are exploration/Evolution archive-recall memory and swarm agent memory: `codex/exploration-memory-20260930` (`9b47cf53ab`),
`task/semantic-exploration-memory-20261001` (`f63ed3b7fd`), `task/long-exploration-invention-20261001`, `task/program-replanning-20261001`,
`fix/exploration-{canonical-project-config,hypothesis-review,question-read-recovery,semantic-original-sources}-20261002`,
`codex/retained-agents-20260923` ("retained Swarm agents with durable memory"), `codex/profile-demand-20261010` (profile-review slots),
and the uncommitted `recovered/wt/af3244a4-…-wip` (2026-10-16, `_render.py` reread avoidance; no merge base with HEAD).

- Inventory verdict: obsolete, because they predate the history rebuild.
- Evolution/Goal Studio were removed 2026-11-15 (`0d299c9610`, `91b5bee3f5`), so the exploration-memory line has no host.
- **Drop.**

The many other "memory" hits in the inventory are RAM/RSS performance work (native, ingest, HTTP, frame), not the memory mechanism.

## Experiment data (old path only)

Root: `/path/to/your/project (mode 0700; `du` 46M as reported by FUSE).
Current root `/mnt/your-fs` is empty.

| Directory | Contents |
|---|---|
| `private/` | MEMORY-EVAL: `ground_truth.json` (private owner ledger), `scenarios.json`, `bases/` (recorded round-0 requests per role, 0.9 MB/0.7 MB/0.2 MB), `runs/baseline-union` (accepted baseline), `runs/run-20261109-175002-prefetch-off`, 11 more runs, `baseline-report.txt`, `injection_review.json`, `judge.log`, `prefetch-off*.log`, `request_cache.json` |
| `design-arm/private/` | experiment 1: `owner_profile_carrier.txt`, `compare.json`, `arm.log`, judge logs, `runs/` |
| `design-arm-1b/private/` | 1b: `screening.json/.log`, `full.json/.log`, `analysis/`, judge logs, `lane-experiments.bundle` |
| `design-baseline-topup/private/` | baseline top-up runs |
| `design-arm-1c/private/` | 1c: `analysis-1c.json/.txt`, `full-vs-1b-union.{json,md}`, `full-vs-exp1-union.{json,md}`, `k3-compare.{json,md}`, `k3-run.log`, the six 1b/1c carriers per role, `lane-experiments-1c.bundle` |
| `k3-1c/` | 1c K3 resolution: `REPORT-k3.md`, `k3-analysis.json`, `handlabels.json`, `handlabel-{key,sheet,score}.json`, `baseline/` |
| `k4-1c/` | frozen lesson corpora (`corpus-1b-state/`, `corpus-live-20261111T005242Z/`), `parity-*.json/.log`, `tools/{parity,crash_probe}.py` |
| `fm2x2/` | `lane-experiments-fm2x2.bundle`; `private/analysis.json`, `freeze.json`, `labels/`, `handlabel/`, `runs/`, `run-t*.log`, `prod429.jsonl`, `scenarios.json`, `ground_truth.json` |
| `self-query/` | MEMORY-SELF-QUERY "before" arm only: `run_arm.py` (frozen-corpus runner, never committed), `before.log`, `before-judge.log` (16 rows: S16/S32/S33/S34; S16 reduced shows falseMemory in 1 of 2 trials), `corpus/`; no "after" arm run was found |

`ground_truth.json` and `bases/` hold private owner statements and recorded production requests. They must stay in a private `.eval-runs/` and never be committed.
`fm2x2/private/interim.json` showed in the first listing and was "No such file" on a second read minutes later. This is an unexplained FUSE inconsistency; nothing was written.

## Experiment findings (numbers)

These figures come from the commit messages of the missing lane commits and from `k3-1c/REPORT-k3.md`. They agree with the prose in
`docs/evaluations/OWNER_MEMORY_BENCHMARK.md` and `docs/modules/context_engineering.md`.

**Baseline (MEMORY-EVAL, 2026-11-09).** 150 responses, 38 scenarios × 2 context arms × 2 trials, claude-opus-5.5, hand-labelled.

| Measure | Result |
|---|---|
| Applied (120 apply rows) | 31.7% |
| False memory (negative rows) | 2/14, both the stale "no SSH key" lesson |
| Correct should-ask | 25% |
| Prefetch precision | 33.3% (7/21) |
| Prefetch recall | 100% (7/7) |
| Cost | $196.5 |

- With prefetch off, application was 28.3% and Coordinator full-context fell 56.2% → 43.8%. Router reduced-context re-asks rose 21.4% → 42.9%.
- The LLM judge agrees with the hand labels at 86.7% on `applied` (κ 0.745).

**Experiments.**

| Experiment | Verdict | Key numbers | Cost |
|---|---|---|---|
| 1 (all 31 directives incl. grants) | **killed by K5** | application 28.3% → 50.0% (+21.7 pp, CI [+10, +34]); unnecessary asks 23% → 7%; necessary asks 31% → 13%: the grants read as "do not ask" | — |
| 1b (no grants, role-scoped, usage rule) | **inconclusive (K4)** | K1 +20.0 pp [+8.3, +33.3]; K2 +13.3 [0.0, +28.3]; false memory 0 → 0; unnecessary asks 14.0 → 15.0 cell-events (+2.0, a kill, without the 5 drifted cells); refusals 3.5 → 0.5 | $206.3 |
| 1c (1b + grants bounded by verbatim policy quotes) | **not passed (K3)** | K1 +16.1 pp [+5.4, +28.6]; K2 +14.3 [−3.6, +32.1]; K4 12.5 → 12.0; K5 2.5 → 3.0; K6 2.5 → 0.5. K3 after resolution: false memory 0.0 → 0.2 cell-events over trials 0–4, the single event is S16/reduced t0 | ≈$244 + $77.52 |
| FM-2x2 (does the profile make a stale lesson sound authoritative?) | **PASS, both label formats** | 6 stale scenarios × 4 conditions × 25 trials = 600 judged + 474 blind hand labels. Profile effect with `inferred` labels: −2.0 pp [−8.8, +5.1] judge, 0.0 [−4.1, +4.1] hand; without labels: −6.7 [−13.1, +0.7] and −1.9 [−6.7, +3.3]. Labels alone lower the judged rate by 11.0 pp [5.3, 15.1]; all 6 strict assertions were in unlabelled arms | $354.58 (1,861 calls, 0 retries, 0 429s) |

Caveats recorded with FM-2x2:

- Without S16's request, the labelled profile effect is +3.2 [−4.3, +10.2].
- Only two stale facts were tested, in reduced context only.
- Coordinator output is about +47% with the profile.
- About 39% of responses state the stale claim and then hedge it, with or without the profile.

## Recommendations

1. **Phase 1 owner profile (`5ffb087afa`): port.**
   - Evidence: FM-2x2 answered 1c's only open rule. Phase 2/4 (trust labels, lifecycle) is already canonical (`1d5db9eae8`), which was
     FM-2x2's precondition. The unmerged source records the Coordinator's 2026-11-13 dispatch.
   - Effort: the diff applies to the current branch except the 9 generated i18n catalogs, which are rebuilt from sources. Port
     `4250f8c566` (specs first), `75e4fc604e`, `a4e11e0d64` and `5ffb087afa` into a lane, regenerate i18n and run `tests/memory`,
     `tests/users`, `tests/frontend/test_frontend_user_context.py` and the context-composer tests.
   - Gate before shipping: the measured +75% output-length cost on the full-context Coordinator, and the time-matched run that never ran
     (401/429). Confirm with the Coordinator that the +75% is acceptable, or ship the directives without the bounded delegation section
     (1b's +7%) as a smaller first step. This is a product decision, not an agent's.
   - Also: the current docs' "Phase 1: not yet authorized" (`docs/modules/context_engineering.md`) contradicts the 11-13 dispatch.
     The port replaces that text.
   - Also: My Context today holds no active owner item, because the audit activated 0 of 17. Phase 1 renders nothing until the owner
     confirms or enters items. Importing the benchmark ledger is a production write that needs the owner's go-ahead
     (stated in `5ffb087afa`'s "Not shipped").
2. **Experiment arm code (`evaluations/owner_profile_arm/`, FM-2x2's `stale2x2.py`): preserve, do not land.**
   - It is evaluation-only by design ("lane-only"), and its results are already in the docs.
   - But its only copies are the old object store and three bundles on the write-protected path. Import the refs into this repo
     read-only, e.g. `git fetch <old>/.eval-runs/owner-memory/fm2x2/lane-experiments-fm2x2.bundle 'refs/worktree/memory-design/*:refs/heads/recovered/memory-design/*'`
     and the same for the 1b and 1c bundles. That makes the docs' references resolvable and keeps the PREREG/RESULTS audit trail.
   - This is a write to this repository, not the old one. It needs the Coordinator's go-ahead.
   - Also: the recovery's import missed per-worktree `refs/worktree/*` in general. Other worktrees had them too: `tb21-lean-build`
     (project-worker-60eed29d), `polish/selfcheck-zh` (project-worker-bdfac7e8). The inventory should note that gap.
3. **Experiment data: copy the private tree, do not commit it.**
   - The benchmark's accepted baseline, frozen corpora, bases and ground truth exist only under the old `.eval-runs/owner-memory/`.
   - Copying it (mode 0700) to the current `.eval-runs/owner-memory/` restores the documented reproduction (`TOFU_OWNER_MEMORY_PRIVATE`)
     and any future arm.
   - Data copy without spend, but private: owner's call.
4. **MEMORY-SELF-QUERY "after" arm, 1b K4 resolution: drop.** The before arm alone has no comparison. Both are superseded by 1c and
   FM-2x2, which used the frozen corpus.
5. **Everything in section C and D: drop.** Section C is merged or superseded (current file/commit named per row). Section D is
   pre-rewrite, and its Evolution host was removed.

## Acceptance evidence

1. **Listing.** Sections A–D cover the recovered branches, task refs and WIP. The inventory TSV's memory rows are mapped in C/D.
   The 478-worktree inventory rows 243/244/273/288/326/328 are covered in A–C. Old per-worktree refs are listed in B. Experiment
   outputs are listed under "Experiment data". The lanes (`refs/tofu/lanes/*`, 6) hold no memory work. `refs/tofu/checkpoints/*`
   memory-path hits are 2026-11-16 current-era merges, not old work.
2. **Verdicts.** Each row cites an ancestor or containment check, a residue count, a content diff against HEAD, or the
   delivering commit.
3. **Findings.** The numbers and paths are above. Recommendations: port Phase 1; preserve the arm code as refs; copy the data
   privately; drop the rest.
4. **Read-only.** No write under the old path. The only command touching it read files. Old objects were read through an alternate
   object directory. No code change in this assignment; this file is the only change.
