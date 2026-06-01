---
name: timer-skip-heartbeat-and-check-command-bash-trap
description: Timer UI skip heartbeat (Option B) + bash precedence pitfall in check_command that freezes polls
enabled: true
tags: timer, scheduler, frontend, bug-fix, bash, ui
created: 2026-04-24T21:04:54Z
updated: 2026-04-24T21:04:54Z
---

# Timer Skip Heartbeat + Bash Precedence Pitfall

## Problem
Timer watcher appeared "stuck" at the end of long conversations:
1. `check_command` output stopped changing after training finished → dedup in
   `poll_timer()` returned `skipped=True` for every subsequent poll
2. In `lib/scheduler/executor.py::_execute_timer_create`, the skip path had
   NO SSE event, NO DB record, NO round metadata update → frontend UI
   froze on the last real `Poll #N wait` line for tens of minutes.

## Fix (Option B — skip heartbeat)

### Backend (`lib/scheduler/executor.py`)
When `skipped=True`, attach skip metadata to the toolRound AND emit a
lightweight `timer_poll_check` SSE event with `decision='skipped'`:

```python
if skipped:
    if parent_task and round_num is not None:
        _now_ms = int(_time.time() * 1000)
        for sr in parent_task.get('toolRounds', []):
            if sr.get('roundNum') == round_num:
                sr['_timerSkipCount'] = sr.get('_timerSkipCount', 0) + 1
                sr['_timerLastSkipTs'] = _now_ms
                sr['_timerLastSkipPollNum'] = poll_count
                sr['_timerTimerId'] = timer_id
                break
        append_event(parent_task, {
            'type': 'timer_poll_check',
            'roundNum': round_num, 'timerId': timer_id,
            'pollNum': poll_count, 'decision': 'skipped',
            'reason': 'check_command output unchanged — LLM call skipped',
        })
    continue
```

### Frontend (`static/js/ui.js`)
- In `timer_poll_check` handler: route `decision==='skipped'` to bump
  `_timerSkipCount` / `_timerLastSkipTs` / `_timerLastSkipPollNum` on
  the round (NOT push to `_timerPolls[]` — would spam).
- In `_syncToolRoundsDOM` fingerprint: include `_timerSkipCount`.
- Render dispatch (`_renderUnifiedToolLine`) and in-place re-render
  branch: trigger on `_timerSkipCount` too, not just `_timerPolls.length`.
- In `_renderTimerWatcherBlock`:
  - Header adds ", N skipped" suffix while active.
  - Body appends a subdued `💤 N polls skipped — check_command output
    unchanged` trailer line.

### CSS (`static/styles.css`)
`.timer-poll-skipped` class: opacity 0.55, dashed top border, grayed text.

## ★ Secondary bug exposed — bash script precedence in check_command
Timer `tmr_aca6022f` used this check_command:
```bash
[[ -d .../adapter_config.json || -f .../adapter_model.safetensors \
   || -f .../adapter_config.json ]] \
  && echo 'PRW_DONE=1' || echo 'PRW_DONE=0' \
  && ls -la checkpoints/prw_3b/ | tail -5
```

Bash parses `A && B || C && D` left-to-right as `((A&&B) || C) && D`:
- **Training done** (test is true): `echo PRW_DONE=1` runs; `echo PRW_DONE=0`
  is skipped; THEN `&& ls -la` runs on the success of the prior chain →
  actually DOES run in the true branch on modern bash BUT still fooled
  the LLM when the listing happened to be empty/identical.
- The LLM's `reason` kept saying `"e6_active=0 and PRW_DONE=1, but
  adapter_config.json missing"` — it didn't trust the echo without seeing
  the listing it expected.
- Output text became byte-identical every poll → dedup skipped every LLM
  call forever → UI froze.

## Lesson: writing timer check_commands
- Prefer explicit if/else blocks over `&&/||` chains for decision logic.
- Always include a directory listing / file existence output UNCONDITIONALLY
  so the LLM has consistent evidence to evaluate.
- Test: `cmd` output should change predictably across state transitions
  AND should not be byte-identical for extended periods while waiting —
  or the dedup will silence the timer.

