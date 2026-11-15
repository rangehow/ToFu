# Owner-understanding benchmark (MEMORY-EVAL)

Measures whether each role applies the owner's already-stated preferences in a
later, unrelated turn without re-asking, and whether it avoids confidently
applying a disproved or superseded belief. This is evidence infrastructure, not
a production feature: no production code, memory, preference or setting is
changed by it.

Harness: `evaluations/owner_memory/` (README there owns invocation).
Owner-memory domain map: `docs/modules/context_engineering.md`.

## Ground truth (private)

Extracted only from the owner's own human turns (`origin.initiator=human`, or
absent legacy UI turns), using `originalContent` for auto-translated UI turns.
Peer/brain/channel relays and `Original human request` carriers are excluded, as
are sensitive personal turns and anything resembling a credential.

Location: `<shared checkout>/.eval-runs/owner-memory/private/ground_truth.json`
(mode 0700 directory, 0600 file). `.eval-runs/` is root-ignored by
`.gitignore` and excluded from every export mode. The preflight checks what a
shipped checkout can see: the root is outside every Git work tree, or under a
top-level private artifact directory (`.eval-runs/`, `eval-runs/`) that
`git check-ignore` confirms (exit 2 otherwise). The export-mode half is proven
by `test_export_exclusion_covers_every_mode` against the live export rules.
Only the harness, synthetic fixtures and tests are committed.

**Decision: privacy check without the export toolchain.** Problem: the harness
ships to Open, but the export toolchain does not, so a harness that imported it
failed there at runtime (and failed the shipped-module export guard). Decision:
the harness declares its private artifact directories, Git stays the live
authority for "never committed", and a test cross-checks the declaration
against every export mode wherever the export tool exists (the established
pattern for shipped declarations checked against export rules). Alternatives
rejected: a conditional import of the export tool from shipped code (still a
dangling dependency, merely hidden); excluding `evaluations/owner_memory/` from
Open (hides useful evidence code, and the private ledger was never in that
tree anyway, so it buys no privacy).

**32 items** (31 active, 1 revoked): 26 global, 6 project-scoped. 13 were
restated verbatim in a later owner message. Categories: authorization
boundaries 3, operational rules 12, report format 4, product decisions 9,
product direction 1, research direction 1, communication style 1, release
policy 1. Plus a **disproved set of 4**: the stale "no SSH key on this host"
memory (D01, still `enabled: true`), the canvas-evaluation belief corrected by
the owner on 11-06 (D02), the Watchdog WeChat re-login advice contradicted by
its own later re-check (D03), and the dev-permission-closed instruction revoked
on 11-08 (D04).

Only 2 of the 32 (P14 own-launchers, P32 git_land-no-tests) have a durable home
in the checkout today. Seven more live in mechanism docs (ROUTER.md R10,
CHANNELS.md C6, RUN_CONTINUITY.md, export guidance), which no role can query as
owner preference. The rest are Router-note-only or nowhere.

## Scenarios

38 scenarios, each a later task that does not restate the preference:
32 apply (one per active item), 4 negative (one per disproved item), 4
should-ask controls (including one that "you decide" is not a blank cheque to
publish private memories). Spread across Global Router (14), tofu Project
Coordinator (19), Worker (5). Relevance of injected memories is hand-labelled
per scenario, which is what makes injection precision/recall computable.

## Prompt fidelity

Each role's prompt starts from a real recorded round-0 provider request
(Request Inspector raw archive, read-only loopback v4). The scenario replaces
only the last user message's authored text; `relevant_memories` is recomputed
with the production prefetch and rendered through the production composer.
`--input-mode direct` (primary) is the owner typing in that chat;
`--input-mode forwarded` is the Router delegation envelope, whose boilerplate
also becomes the prefetch query and measurably selects unrelated lessons.

The Worker's round-1 provider body is not archived while its attempt runs, so
its base is the Inspector's round-1 pre-request snapshot translated with the
production `openai_body_to_anthropic` at `workerModel=claude-opus-5.5`. A
Worker's first turn is one user message, so full≈reduced for that role; read no
dilution effect into Worker numbers.

No tool executes. Read-only tools are stubbed synthetically; terminal actions
(delegation, report, ask_human, channel_send) get a synthetic receipt so the
model finishes its intent.

## Baseline (current HEAD, 2026-11-09)

150 scored responses (38 scenarios × 2 context arms × 2 trials; two rows
unusable after gateway refusals). In 28 of the 75 cells, trial 1 replayed
trial 0 from the response cache, so those cells hold one sample, not two;
trials are independent samples since 2026-11-10. Models: claude-opus-5.5 (effort max) for all
three roles. Scoring below is **hand-labelled on all 150 rows**; the fixed LLM
judge (claude-opus-5.5, pinned rubric) agrees at 86.7% on `applied`
(κ=0.745), 93.3% on `interruption` (κ=0.787), 98.7% on `falseMemory`.
On FM-2x2's stale-memory rubrics (2026-11-13, 474 blind hand labels), the
judge's `falseMemory` also counts a stale claim that is stated as current and
then hedged.
- Against the strict reading (stated without a hedge), agreement is 74.1%
  (κ=0.07).
- Against the lenient reading (hedged or not), agreement is 87.3% (κ=0.72).
- The judge missed no strict assertion.

Decide false-memory rules on both readings.

| Role | Context | Applied | Partial | Re-ask (interruption) | False memory | Correct should-ask | Cost |
|---|---|---|---|---|---|---|---|
| Coordinator | full | 56.2% | 6.2% | 26.3% | 25.0% (1/4) | 100% | $108.5 |
| Coordinator | reduced | 25.0% | 0.0% | 22.2% | 50.0% (2/4) | 0% | $15.0 |
| Global Router | full | 36.4% | 0.0% | 7.1% | 0% | 0% | $58.2 |
| Global Router | reduced | 18.2% | 0.0% | 21.4% | 0% | 50% | $7.4 |
| Worker | full | 0.0% | 33.3% | 0% | 0% | 0% | $3.7 |
| Worker | reduced | 0.0% | 33.3% | 0% | 0% | 0% | $3.7 |

Total for the 150 responses: **$196.5** at the provider-override price card
(input $4/M, cached read $0.4/M, cache write $5/M, output $20/M), 2.25M uncached
input + 12.5M cached-read + 20.1M cache-write tokens, 1.44M output tokens.
Full-context arm is ~7× the reduced arm's cost per response.

**Full context helps application** (Coordinator 56.2% vs 25.0%, Router 36.4% vs
18.2%). On the Coordinator's negative cases the reduced arm is worse on false
memory (50.0% vs 25.0%): the disproved SSH-key memory is what prefetch injects,
and reduced context leaves nothing to contradict it. Re-ask rate roughly
doubles for the Router in reduced context (7.1% → 21.4%).

Overall across all 120 apply rows: **31.7% applied**, 5.0% partial. Across the
14 negative rows: **14.3% false memory** (2/14, both the stale SSH-key claim in
S16). Across the 16 should-ask rows: only **25.0%** asked the right thing —
two controls (S35 ambiguous recipient, S37 unspecified deploy target) were never
asked in any arm, so the model is not merely over-asking, it also fails to ask
when it should.

Worst-failing items (0% applied in both arms): S05 weekly summary without
cache/cost, S06 deck not sent to the chat, S10 two-paper split not recalled,
S11 no A100/H910C fallback offered, S12 OAuth Codex not ruled out, S14 vendor
baseline not used as the report bar, S17 bot-identity notification not applied,
S20 「我的一天」 old direction, S23 asks for a path instead of creating the
project, S24 no systematic robustness investigation, S25 no cost analysis,
S27 APK hardening dropped, S28 Android direction ignores the harness decision,
S30 no new benchmark coverage, S31 git_land test-ladder requirement invented.

Two findings that are not preference recall at all:
- **Worker scenarios never reach the action.** All 5 Worker scenarios scored
  0% applied because the model spent its whole turn on discovery
  (`git ls-files`, `search_files`, `load_skill`) and the harness stops after 4
  rounds. This is a measurement limit, recorded as such, not a product verdict.
- **False memory is a retrieval problem, not a reasoning one.** In S16 the
  Coordinator repeated "this host has no registered SSH key" as current fact
  (2 of 4 runs) — the stale memory that prefetch injected. It never claimed the
  dev permission was closed, and it correctly rejected the canvas platform in
  S33 and avoided the WeChat re-login advice in S34.

## Injection relevance

14 of 38 scenarios had memories injected; all 14 were hand-labelled for
relevance. Micro precision is **33.3%** (7 of 21 injected items relevant) and
micro recall **100%** (7 of 7 scenarios where a relevant memory existed in the
corpus had it injected). `report` reproduces these figures from run evidence
since 2026-11-10; before that it compared absolute prefetch paths with
file-name labels and scored every injection irrelevant — but recall is only measured over the 7 scenarios
where a relevant memory exists at all, and in 31 of 38 scenarios there is
nothing relevant to retrieve because the preference is stored nowhere. The
dominant selection reason is `distinct_token_overlap:2`, and the tokens are
boilerplate ("Coordinator", "turn", "project"), not intent. Two of the 21
injected items were the disproved no-SSH-key memory (S16), which is how the
false-memory rate arises.

## Variants

`--prefetch off` removes `relevant_memories` for coordination roles only
(Global Router, Project Coordinator), everything else unchanged; run dir suffix
`--run-name prefetch-off`. 152 responses, hand-labelled with the same rubric.

| Metric | Baseline (prefetch on) | Prefetch off |
|---|---|---|
| Applied (120 apply rows) | 31.7% | 28.3% |
| False memory (negative rows) | 2/14 | 2/16 |
| Correct should-ask (16 rows) | 4 yes + 4 partial | 3 yes + 1 partial |
| Coordinator full applied | 56.2% | 43.8% |
| Coordinator reduced applied | 25.0% | 25.0% |
| Router full applied | 36.4% | 36.4% |
| Router reduced applied | 18.2% | 18.2% |
| Coordinator full re-ask | 26.3% | 31.6% |
| Router reduced re-ask | 21.4% | 42.9% |
| Total cost | $196.5 | $167.3 |

Reading: prefetch is **not** the main reason preferences are missed — turning
it off costs only 3.4 points of application, because most preferences are not
in the corpus to begin with. It **is** the source of the false-memory cases:
the Coordinator's stale-SSH-key false memory disappears with prefetch off
(0/4 vs 2/4), while a new false memory appears in S33 where, with nothing
injected, the model proposed investigating the canvas platform instead of
rejecting it. So the disproved-memory problem is fixed by correcting or
deprecating the stale entry, not by disabling retrieval. Re-ask rate rises
without prefetch, especially for the Router in reduced context (21.4% → 42.9%).

The post-fix legacy My Context profile variant is **skipped**: MY-CONTEXT-FIX's
audit found 0 of 17 legacy entries owner-stated and valid (12 stale, 1
conflicting, 4 needs-confirmation), so that variant emits no block and would
produce byte-identical requests. MEMORY-DESIGN runs the ledger-derived
active-preference profile arm separately against these same base digests
(31 active items, production `render_profile_block` + composer `user_context`
envelope), which is the informative comparison for the self-query/design lane.
Its results (experiment 1 killed, experiment 1b inconclusive, experiment 1c not passed, and FM-2x2,
the powered stale-memory follow-up to 1c's K3, passed) are recorded in
[`modules/context_engineering.md`](../modules/context_engineering.md), "Owner-profile experiments".

## Reproduce

```bash
export PYTHONPATH=$PWD
export TOFU_OWNER_MEMORY_PRIVATE=<shared>/.eval-runs/owner-memory/private
.venv/bin/python3 -m evaluations.owner_memory preflight
.venv/bin/python3 -m evaluations.owner_memory capture --roles global_router,coordinator,worker
.venv/bin/python3 -m evaluations.owner_memory run --trials 2 --modes full,reduced --resume --jobs 4
.venv/bin/python3 -m evaluations.owner_memory judge --run <run-dir> --jobs 4
.venv/bin/python3 -m evaluations.owner_memory handlabel --run <run-dir> --scenarios S01,S04,S32
.venv/bin/python3 -m evaluations.owner_memory agree --run <run-dir>
.venv/bin/python3 -m evaluations.owner_memory report --run <run-dir>   # hand-labelled by default
.venv/bin/python3 -m pytest -q tests/evaluations/test_owner_memory_benchmark.py
```

The AppIds that serve the benchmark model also carry production traffic and
share its per-minute limits. On them, always run and judge with `--key-hints
<tail> --stop-on-429`; the README's key policy says what each flag records.

Size concurrency to what the AppId has been measured to sustain:
- **…`0733` (2026-11-13, FM-2x2):** 16 calls in flight and 12 request starts
  per minute ran 1,861 calls with no retry and no 429 (HTTP or in-stream),
  beside 11–35 production opus rounds per minute on the same AppId.
- **Unmeasured AppIds:** start at two calls in flight and let the first 429
  end the run.

Accepted baseline: `private/runs/baseline-union` (union of stopped/resumed runs
with identical base digests and harness source; per-dir provenance recorded in
the manifest). Prefetch-off arm: `private/runs/run-20261109-175002-prefetch-off`.
`report --score verdict` switches to the LLM judge's numbers; the default is
the hand-labelled score. Hand verdicts are preserved in `hand_verdicts.json`
next to `records.json` so a later judge pass cannot clobber them.

Kill condition for any first experiment built on this: stop if it does not beat
31.7% applied on the same 32 apply scenarios at equal or lower cost per
response, or if false-memory rate rises above the 2/14 baseline.
