---
name: run-command-grep-hardening
description: tool_run_command auto-injects -I/--exclude-dir/--color=never on bare `grep -r` (real grep kept, non-silent, kill switch)
enabled: true
tags: [run_command, grep, performance, fuse]
created: 2026-05-11T03:15:26Z
updated: 2026-05-11T03:15:26Z
---

# run_command grep -r hardening (Strategy 2, accepted 2026-05-11)

User-approved alternative to "hijack grep → rg" rewrite. Real GNU grep is
KEPT — we only inject flags the user did not pass. No regex-flavor change,
no `.gitignore` respect change, no symlink-follow change.

## Helper
`lib/project_mod/tools.py`:
- `_has_unquoted_shell_metachars(cmd)` — scans for `|`, `;`, `&`, `<`, `>`,
  backtick, `$(` outside quotes. Pipeline detection.
- `_maybe_harden_grep_command(command)` — returns possibly-augmented command.
- `_GREP_HARDEN_BINARIES = {'grep','egrep','fgrep'}`

## Activation conditions (ALL must hold)
- `TOFU_RUN_HARDEN_GREP != '0'`
- non-interactive (no stdin_callback)
- first token (after `/usr/bin/` strip) is in `_GREP_HARDEN_BINARIES`
- `shlex.split(command)` parses cleanly
- no unquoted shell metachars (pipelines/redirects/cmd-subst → no-op)
- user passed `-r` / `-R` / `--recursive` (combined like `-rIn` also detected)

## Injected flags (only if not already specified)
- `-I` (skip binary)
- `--color=never`
- `--exclude-dir=<name>` for each entry in `IGNORE_DIRS`
  (config.py — already mirrors .gitignore for grep_search)

## Hook point
`tool_run_command` at the top of the function, BEFORE building `full_command`.
Logs `[run_command] Hardened grep: ...` and emits a one-line
`[run_command] auto-added grep flags ...` on the stderr chunk stream so
the user sees the change in the streaming UI. The injected flags also
appear in the `$ ...` echo at the head of the result text (non-silent).

## Why this respects "no silent exclusion" memo
- Exclude-dir list = same set already used by `grep_search` (IGNORE_DIRS in
  config.py), which the user has already accepted.
- The change is visible: `$` echo shows full augmented command, plus
  logger.info, plus stderr-chunk hint.
- Kill switch: `TOFU_RUN_HARDEN_GREP=0`.
- Real grep semantics preserved (regex flavor, symlink-follow, gitignore
  ignored unless `--exclude-dir` already covers it).

## Measured impact
On the dolphinfs cross-DC mount: `grep -rn deepseek lib/ routes/` went
from 25s+ timeout → **0.41s** (60x faster, same results).

## Pitfalls / what NOT to do
- Do NOT rewrite to rg — flag semantics diverge silently (`-r`, `-L`, `-z`,
  `-P`, regex flavor, .gitignore respect). User explicitly chose Strategy 2
  over the rg rewrite.
- Do NOT touch pipelines or redirections — bails to no-op when shell
  metachars present.
- Do NOT add `--include='*.py'` — would silently drop non-Python matches.
- Do NOT remove the stderr-chunk hint — that's the user-visible cue.

