---
name: inline-math-regex-newline-table-garble-bug
description: Bug fix: inline math $...$ regex matched across newlines, eating table structure when cells contained dollar signs like $0.40
enabled: true
tags: [javascript, bug-fix, markdown, regex, rendering]
created: 2026-04-05T04:27:17Z
updated: 2026-04-05T04:27:17Z
---

# Inline Math Regex Newline Bug — Table Garbling

## Symptom
Markdown tables containing `$` characters (e.g., `$0.40`) render as garbled text with raw pipe characters visible.

## Root Cause
The inline math extraction regex in `renderMarkdown()` (core.js):
```js
// OLD (buggy):
/\$(?!\$)((?:[^$\\]|\\.)+?)\$(?!\$)/g
```

The character class `[^$\\]` matches **any character except `$` and `\`** — including newlines.
So `$0.40` in one table cell would match all the way to the next `$` — potentially spanning
multiple rows, paragraphs, or even separate tables.

This replaces large chunks of table markdown with `\x02MATH0\x03` placeholders,
leaving behind broken pipe syntax that `marked.parse()` can't recognize as a table.

## Fix
Add `\n` to the exclusion class — inline math should never span lines:
```js
// NEW (fixed):
/\$(?!\$)((?:[^$\\\n]|\\.)+?)\$(?!\$)/g
//                  ^^-- added \n
```

## Verification
```js
// Old regex matches across lines:
"$0.40 |\n| A | NEW | $".match(OLD_REGEX) // ⚠️ matches entire span

// New regex stops at newline:
"$0.40 |\n| A | NEW | $".match(NEW_REGEX) // ✅ no match (correct)

// Legitimate inline math still works:
"$E = mc^2$".match(NEW_REGEX) // ✅ matches
```

## Files Changed
- `static/js/core.js` line ~2101 — the `$...$` inline math regex
- Bundle auto-rebuilt by `lib/js_bundler.py`

## Related
The `\\\(.*?\\\)` regex for `\(...\)` math is safe — `.*?` doesn't match newlines by default.
The `$$...$$` and `\[...\]` regexes use `[\s\S]*?` intentionally since display math CAN span lines.

