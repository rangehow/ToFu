---
name: compaction-tolerates-malformed-tool-call-args
description: compaction _extract_recently_accessed_files must handle BOTH: string CONTAINER (reads='[...]' → coerce/skip) AND bare-string ELEMENTS (reads=["a.py"] → keep)
enabled: true
tags: [compaction, tool-calls, defensive, bug-pattern]
created: 2026-04-19T16:06:32Z
updated: 2026-07-06T11:13:55Z
---

# Compaction must tolerate malformed tool_call args — TWO distinct shapes

`lib/tasks_pkg/compaction/_layer2.py::_extract_recently_accessed_files`
re-scans `msg['tool_calls']` for file paths. LLMs emit two DIFFERENT
malformed-but-real shapes that must be handled SEPARATELY (they share the
surface type `str`, so it's easy to conflate them and break one):

## Shape 1 — the CONTAINER is a string  (char-garbage bug)
```json
{"reads": "[{\"path\": \"a.py\", \"end_line\": 4]"}   // a JSON STRING, sometimes truncated
```
Iterating `for spec in reads` over a STRING yields single CHARACTERS →
each char accepted as a "path" → deduped → rendered one-per-line
(`  - [` / `  - {` / `  - p` …). This is the "one letter per line"
modified-files reminder (conv `mr4e8pnxbv440z`, 2026-07-06) AND the
garbled tool-display `Read 24 files: ; < p a +20 more`.
**Fix:** `_coerce_spec_list(value)` — list→list; str→`json.loads` and
return ONLY if it decodes to a list, else `[]`. NEVER iterate the raw str.

## Shape 2 — a list ELEMENT is a bare string  (legitimate!)
```json
{"reads": ["human_eval/foo.py", "scripts/bar.py"]}   // real full paths, NOT chars
```
Documented Claude-Opus output shape. `lib/project_mod/tools.py::execute_tool`
(~line 1560) normalizes bare-string specs → dicts so the READ succeeds.
Any re-scan MUST likewise KEEP these — the string IS the path.
**Fix:** AFTER `_coerce_spec_list` guarantees a list, keep the element
branch: `if isinstance(spec, dict): p=spec.get('path','')`
`elif isinstance(spec, str): p=spec.strip()`.

## The trap (2026-07-06 regression)
While fixing Shape 1, the element-level `elif isinstance(spec, str)` branch
was DELETED as "never a real path" — that broke Shape 2: `["a.py"]` → `[]`,
paths silently vanished. Container-is-a-string ≠ element-is-a-string:
**coerce the container, keep the element.**

## Guardrails
- Wrap `compute_turn_attachments` in orchestrator (~line 701) in try/except —
  attachment building is advisory, must never abort a healthy task (a
  bare-string `spec.get('path')` once raised AttributeError → killed a task
  at round 50, conv `mo5adw4vyfm6qf`).
- Add `if not isinstance(args, dict): continue` after `json.loads(arguments)`
  (a bare-string/list decode otherwise AttributeErrors on `args.get`).
- Same "iterate a list-typed arg" hazard exists in `project_tool_display`
  (`lib/project_mod/tools.py`) for `reads`/`edits`/`searches`/`urls`/`queries`;
  the streaming early-announce path runs BEFORE schema repair, so coerce there too.
- Double-neuter must be TWO-SIDED: one neuter per invariant. Neuter the
  container-coerce → char-garbage tests fail; neuter the element branch →
  bare-string-list tests fail. If a neuter flips NO test, that invariant isn't
  protected (or you've deleted a real feature).

## Tests
`tests/test_recently_accessed_files_string_reads.py` (7): malformed-string
container→[]; valid-JSON-string container→path; normal list; bare-string
elements→both kept; mixed dict+string list; non-dict args→[]; string `edits`.

