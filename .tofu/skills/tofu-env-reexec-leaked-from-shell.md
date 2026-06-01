---
name: tofu-env-reexec-leaked-from-shell
description: Symptom: server.py reports missing packages even when .tofu_env.json + conda env are intact — _TOFU_ENV_REEXEC leaked into the user's shell short-circuits re-exec
enabled: true
tags: [bootstrap, env-vars, diagnostics, tofu-env-marker]
created: 2026-05-22T01:17:00Z
updated: 2026-05-22T01:42:12Z
---

# Tofu env re-exec guard leaking into user shell

## Symptom

User runs `python server.py` from a project whose `.tofu_env.json` is fine
(marker present, `python` field points at a live conda binary that has
flask installed) and STILL gets `ModuleNotFoundError: No module named
'flask'` — sometimes only after they previously tried a sibling project
(`tofu-meituan` etc.) that lacked a marker.

## Root cause

`_TOFU_ENV_REEXEC=1` was leaked from the user's shell. It's meant to be a
process-internal loop guard set by `os.execv` / `Popen`, never exported
in a user's shell. When leaked, the early-return at the top of the guard
function silently disabled the re-exec, leaving the process running on
whatever python was on PATH (often `/usr/bin/python`) → "missing flask."

The companion guards `_TOFU_VIA_BOOTSTRAP` / `_CHATUI_VIA_BOOTSTRAP`
have the same failure mode for the bootstrap.py auto-recovery hand-off.

## Fix shipped (2026-05)

**`server.py:65-92` and `bootstrap.py:53-82`** — replaced the silent
early-return on `_TOFU_ENV_REEXEC=1` with self-healing behaviour:

- Removed the unconditional early-return at the TOP of `_tofu_maybe_reexec_into_env`.
- The `same = realpath(target_py) == realpath(sys.executable)` check
  remains — and is the REAL loop guard, because after a successful
  `os.execv` we ARE running under target_py, so `same == True` → return.
- After the `same` check, if `_TOFU_ENV_REEXEC=1` is still in the env,
  it means we're NOT under target_py AND someone leaked the var → print
  a yellow ANSI warning naming the exact `unset` command and override
  the guard (re-exec anyway). Self-healing.

**`server.py:138-159`** — same treatment for `_TOFU_VIA_BOOTSTRAP` /
`_CHATUI_VIA_BOOTSTRAP`. Heuristic: a real bootstrap-spawned child also
has `BOOTSTRAP_LAUNCHER_PID` set, so when those guards exist WITHOUT
the sentinel, they're leaked → warn loudly and pop them off os.environ
so the bootstrap excepthook auto-recovery can still trigger.

**`bootstrap.py:1585`** — `_try_start_server` now sets
`BOOTSTRAP_LAUNCHER_PID=str(os.getpid())` on the child env dict so
server.py's heuristic above stays accurate.

## Verification done

Reproduced the leak in /tmp/tofu_test with a stub `os.execv`. The new
warning fires verbatim with the unset command, then proceeds to
re-exec. No regressions in the normal happy path (when the var is NOT
in the env, behaviour is unchanged).

## Triage in 30 seconds (still useful for OLDER builds)

```bash
env | grep -E '_TOFU|_CHATUI'
```

If `_TOFU_ENV_REEXEC=1` (or `_TOFU_VIA_BOOTSTRAP=1` / `_CHATUI_VIA_BOOTSTRAP=1`)
shows up:

```bash
unset _TOFU_ENV_REEXEC _TOFU_VIA_BOOTSTRAP _CHATUI_VIA_BOOTSTRAP
python server.py
```

After the 2026-05 fix this is no longer required — the guard self-heals
and prints the unset command on stderr.

## Sibling-project trap

Exported variants (`tofu-meituan`, `tofu-open`, …) ship WITHOUT a
`.tofu_env.json` because the marker is in `ALWAYS_EXCLUDE_FILES` in
`export.py` and `.gitignore`. So a fresh checkout of a variant runs
under system python → "missing flask." Run `bash install.sh` inside
the variant to fix.

The user typically discovers the leaked-guard bug AFTER trying the
variant — the failed run somewhere set `_TOFU_ENV_REEXEC` (e.g. from a
debug snippet they tried), and now the chatui main project starts
failing too.

