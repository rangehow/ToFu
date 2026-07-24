# Turn-Settlement Verdict — a single backend-authoritative fact per assistant turn

> **Status:** architecture contract (P1 landed — the pure verdict module + tests).
> **Epic:** `pt_a4484f3ad3134ea8`.
> **Companion to (not a duplicate of):** `pt_e1c4693341b24730` (`pt_conv_state_ssot`).
> That epic builds the authoritative channel for *which convs are busy*; this one
> defines the authoritative content of *how one turn ended and how it can be
> resumed*. They share the "compute once server-side, propagate as a frame,
> render — never re-infer — on the client" philosophy.

---

## 1. The problem (why these three things must be done together)

Three UI behaviours all read the **same loosely-controlled `finishReason`
string**, each re-deriving its own conclusion:

| Consumer | File | What it infers today |
|---|---|---|
| Interrupt bubble label | `static/js/ui/finish_info.js:790` | maps `finishReason` → a label/tooltip |
| Continue-button gate | `static/js/ui/chat_render.js:1586` | "not in the clean list → show Continue" |
| Resume-mode decision | `lib/tasks_pkg/segments/_types.py:57` + `lib/chat/turn_builder.py:443` | "in the resumable list → prefill, else checkpoint-scan, else regenerate" |

`finishReason` itself is stamped by **≈5 different code paths** (orchestrator
finalize, endpoint runner, abort handlers, the frontend's optimistic Stop,
the GET/startup reconcile, the poll-fallback). There is no single authority
and no normalization. The consequences:

1. **The Continue button over-promises, then silently degrades.** It shows
   "Continue generating from where it left off" for `aborted` / `error` /
   `abnormal_stop` / a *missing* `finishReason` alike — but it never checks
   whether a recoverable checkpoint actually exists. When it doesn't
   (`scan_continue_checkpoint` → `None` and prefill declined), the click falls
   through to `data.fallback === 'regenerate'` (`main_regen_continue.js:344`)
   and **regenerates the whole turn** — a different operation than the button
   advertised.

2. **Resume is not honestly lossless, and one common case loses everything.**
   A **manual Stop** stamps `finishReason='aborted'`, which is **not** in
   `RESUMABLE_FINISH_REASONS`. On a no-tools turn with a prefill-capable model,
   Stop → Continue therefore regenerates from scratch and **discards the
   partial prose** — the single most common "resume my interrupted answer"
   action loses the answer. The user cannot tell in advance whether Continue
   will be lossless, lossy (checkpoint), or a full regeneration.

3. **Cross-device, the bubble shows a stale truncated state** (the ssot epic's
   symptom driver) because the settled fact is re-inferred per client instead
   of propagated as one frame.

## 2. The verdict

Compute **once**, at settle time (and identically on a cold reopen from the
persisted message fields), a typed fact persisted on the assistant message as
`msg['_settlement']` and carried in the `done` / poll payload:

```
_settlement = {
  # WHAT happened — one normalized outcome (the closed enum):
  'outcome': 'completed' | 'interrupted' | 'truncated' | 'failed',

  # the raw provider/transport reason, verbatim, for debugging + compat:
  'finishReason': '<raw>',

  # WHY — a single cause dimension (replaces the scattered interruptedReason
  # + reason-string sniffing):
  'cause': 'manual' | 'killed' | 'restart' | 'offline' | 'gateway'
         | 'max_tokens' | 'tool_cap' | 'safety_cap'
         | 'content_filter' | 'error' | null,

  # CAN it be resumed, and HOW — decided HERE, once, not per consumer:
  'resume': {
    'mode': 'prefill' | 'checkpoint' | 'regenerate' | 'none',
    'lossless': bool,      # prefill=True; checkpoint=False (drops the prose tail)
    'keptRounds': int,     # checkpoint mode
    'prefillChars': int,   # prefill mode
    'reason': str,         # why this mode (esp. why regenerate / none)
  },
}
```

### 2.1 Mapping (raw `finishReason` → outcome / cause)

| raw finishReason | outcome | cause | resumable via |
|---|---|---|---|
| `stop` / `end_turn` / `stop_sequence` | `completed` | — | — (nothing to resume) |
| `length` / `max_tokens` | `truncated` | `max_tokens` | prefill |
| `tool_rounds_exhausted` | `truncated` | `tool_cap` | checkpoint |
| `incomplete` (endpoint/autopilot cap) | `truncated` | `safety_cap` | checkpoint |
| `interrupted` | `interrupted` | `killed`/`restart` (from `interruptedReason`) | prefill |
| `server_offline` | `interrupted` | `offline` | prefill |
| `premature_close` | `interrupted` | `gateway` | prefill |
| `aborted` (manual Stop) | `interrupted` | `manual` | **prefill** *(the gap fix)* |
| `error` | `failed` | `error` | — |
| `content_filter` | `failed` | `content_filter` | — |
| `abnormal_stop` | `failed` | `error` | — |
| *missing / unknown* | `interrupted` | `null` | best-effort (prefill/checkpoint) |

The *missing / unknown* row deliberately keeps the recovery path open — it
mirrors today's "a legacy turn with no finishReason still shows Continue".

### 2.2 `resume.mode` precedence

1. `outcome == 'completed'` → `none`
2. `outcome == 'failed'` → `regenerate` (`reason='not_resumable_<cause>'`)
3. empty turn (no content / thinking / real tool round) → `regenerate` (`'empty_turn'`)
4. completed tool rounds exist (`toolCallId`s) → **`checkpoint`** (`lossless=False`)
5. prefill-capable model + resumable outcome + terminal deliverable text →
   **`prefill`** (`lossless=True`)
6. else → `regenerate` (`'no_checkpoint_no_prefill'`)

Precedence **preserves today's behaviour** (checkpoint before prefill, per
`lib/chat_dispatch.py:946`). The verdict is a faithful SSOT of the existing
resume algorithm — it does not re-litigate it. *Preferring prefill over
checkpoint for capable models (true losslessness on tool turns) is a separate,
gated follow-up (§5 P5), not smuggled into this SSOT.*

## 3. The three consumers stop re-inferring

- **Bubble label** (`finish_info.js`) reads `outcome` + `cause` → one label
  map. The `interruptedReason`-sniffing branch is deleted.
- **Continue button** (`chat_render.js`) reads `resume.mode`:
  `prefill` → "Continue (lossless)"; `checkpoint` → "Continue (from round N)";
  `regenerate` → relabel honestly to "Regenerate"; `none` → hidden. The button
  can no longer over-promise.
- **Continue executor** (`main_regen_continue.js`) reads the stamped
  `resume.mode` and executes it directly, instead of POSTing and re-deriving
  the boundary. The `/api/chat/continue` route returns the SAME verdict, so
  client and server can never disagree about the resume point.

## 4. Robustness properties

- **Deterministic & pure** — `compute_turn_settlement(msg, *, model, segments)`
  is a pure function over the persisted message fields; a cold reopen recomputes
  the identical verdict for legacy messages that lack `_settlement`.
- **Cache-prefix neutral** — the verdict is a NEW key on the message dict; it
  is not part of the wire fingerprint, so stamping it never busts the prompt
  cache.
- **Fail-closed** — any uncertainty (unknown model, missing segments) degrades
  to the *current* behaviour (checkpoint-scan / regenerate), never to a
  riskier resume.

## 5. Phasing (one commit per phase, failing-first + NEUTER + collect gate)

- **P1** *(this commit)* — `lib/conversations/turn_settlement.py`: the pure
  verdict SSOT + comprehensive tests. Zero production-behaviour change.
- **P1b** *(this commit)* — extend `RESUMABLE_FINISH_REASONS` with `'aborted'`
  (manual Stop) so a stopped turn with content on a capable model resumes via
  lossless prefill instead of full regeneration. The single concrete lossless
  gap fix.
- **P2** — stamp `_settlement` at settle: compute in `_finalize`, persist in
  `_sync_result_to_conversation`, include in the `done` / poll payload.
- **P3** — frontend reads the verdict (finish label, Continue affordance,
  Continue executor) via a reducer; bifurcated from legacy `finishReason`.
- **P4** — sweep the duplicate client-side inference (`_FINISH_CLEAN` gate,
  the finishReason→label sniffing, `interruptedReason` branch), with a
  per-branch justification in each commit body.
- **P5** *(gated follow-up)* — prefer prefill over checkpoint for capable
  models so tool-turn resumes are also lossless; needs prefill+toolHistory
  parity validation.
