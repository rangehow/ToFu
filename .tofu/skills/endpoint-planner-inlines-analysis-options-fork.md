---
name: endpoint-planner-inlines-analysis-options-fork
description: Endpoint prompts 2026-04-30 redesign: intent discrimination (design/audit → modifications, not docs); ## Options + [WORKER_DECISION] handoff; no human input
enabled: true
tags: [endpoint, planner, worker, critic, prompts, analysis-design, options-fork, decision-rights]
created: 2026-04-30T04:40:32Z
updated: 2026-04-30T05:24:57Z
---

# Endpoint mode — prompt redesign (2026-04-30)

## Design goals (from user)

1. **Intent discrimination.** If the user asks a design/query/audit-type
   question, the Planner must discern the real intent — which is
   almost always "apply the improvements", NOT "produce a document".
   "Audit" / "design" / "review" are how users NAME the work; the
   intent is that the code gets better.
2. **Normal modification requests execute normally.**
3. **No plan-to-plan → worker-plan → critic-approve meaningless loops.**
   Planner does analysis inline; checklist ships CODE CHANGES.
4. **Zero human involvement.** Someone must catch every fork. Never
   ask the user. Planner anticipates with ``## Options``; Worker
   picks silently (tactical) or with ``[WORKER_DECISION: ...]``
   (strategic mid-flight); Critic audits picks + answers on behalf
   of user if a question slipped through.
5. **Project-agnostic.** No hardcoded refs to CLAUDE.md, TRADING_ENABLED,
   or project-specific files in the prompts — but it's OK to mention
   "CLAUDE.md / AGENTS.md / CONTRIBUTING.md, if any" as examples of
   where conventions might live.

## Role allocation matrix

| Concern | Planner | Worker | Critic |
|---|---|---|---|
| Discern real intent (query/design/audit → modifications) | ✅ | reads | verifies |
| Inline analysis/design body | ✅ | reads | verifies citations |
| Checklist = concrete code changes (default) | ✅ | executes | verifies |
| Doc as deliverable (only if user explicitly asked) | ✅ | executes | verifies |
| ``## Options`` (Planner-anticipated fork) | surfaces ≤3 | ✅ picks long-term | audits, no re-open |
| Mid-flight strategic fork | — | ✅ picks + ``[WORKER_DECISION: ...]`` | audits, no re-open |
| Tactical code-taste fork | — | ✅ picks silently | — |
| Human involvement | forbidden | forbidden | answers on user's behalf if slips through |
| Verdict | — | — | ✅ |

## Planner intent-discrimination taxonomy

- **Modification request** ("fix X", "add Y"): plan changes directly.
- **Analytical-surface** ("audit X", "design v2", "is there room to
  optimise Y?"): **default assumption = user wants code improved, not
  a doc.**  Planner does analysis inline in ``## Analysis`` /
  ``## Design`` / ``## Audit`` / ``## Spec`` sections, then checklist
  ships CODE CHANGES derived from it. 10-20 read-only tool calls
  normal during planning.
- **Explicit-document request** ("write me an audit doc"): doc IS
  deliverable. Only case where "write docs/*.md" checklist item is OK.
- **Pure information request** ("how does X work?"): ``## Analysis``
  answers, checklist empty or single-item, Critic STOPs once satisfied.

Soft char cap: 6000 for modification tasks, up to ~12000 for
analytical ones (extra budget is for the Planner's own inline
analysis, NOT for a longer checklist).

## Worker rules (updated)

- Rule 6: **NEVER ASK THE USER.** Make the call, tag the decision,
  continue.
- Rule 7: ``## Options`` picks → long-term maintenance basis, one-line
  "Picked Option X because ...", final for the loop.
- Rule 8: Mid-flight forks:
  - tactical (naming, formatting, local structure) → pick silently.
  - strategic (changes plan blast radius, adds/removes files, data
    shape, public API) → ``[WORKER_DECISION: <label>] Picked <choice>
    because <long-term reason>`` then continue.
- Rule 9: no ``## Options`` + no strategic fork → just execute.

## Critic rules (updated)

- Option choice / ``[WORKER_DECISION]`` is FINAL. Only exception:
  technically impossible after ≥2 iterations OR demonstrably breaks a
  hard project rule → ``[PLAN_DEFECT: ...]`` + CONTINUE_PLANNER.
- **No human input allowed.** If Worker asked user a question,
  emit CONTINUE_WORKER with the answer (pick + one-sentence reason)
  inline so Worker is unblocked.

New Good PLAN_DEFECT examples (project-agnostic):
- `plan ships a document the user did not request — user asked for
  improvements to X, plan's checklist only writes docs/x_audit.md`
- `plan asks the user to pick between two options — endpoint mode
  runs without human input`

## Verification

- `python -c "import lib.tasks_pkg.endpoint_prompts"` OK.
- New sizes: PLANNER=11044, WORKER=4667, CRITIC=11230.
- `tests/test_endpoint_messages.py` → 28/28 pass.

## Files

- Only `lib/tasks_pkg/endpoint_prompts.py` changed. No endpoint.py /
  endpoint_review.py logic changes; PLAN_DEFECT gate, STOP-with-❌
  override, CHATUI_ENDPOINT_REPLAN kill switch all still apply.

## Triggering conversation

`mokz19og5mgfjk` / task `03e12e57` — user asked to audit + redesign
"My Day"; old Planner produced a 7k-char plan whose checklist was
"produce audit doc with ≥12 findings" + "append v2 design spec",
delegating all thinking to the Worker. New prompts would produce
inline analysis + checklist that directly modifies `routes/daily_report.py`.

