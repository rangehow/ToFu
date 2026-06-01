---
name: toISOString-utc-timezone-bug-pattern
description: Bug pattern: JavaScript toISOString().slice(0,10) returns UTC date not local date, causing wrong day classification in UTC+N timezones — use getFullYear/getMonth/getDate instead
enabled: true
tags: [javascript, timezone, UTC, bug-pattern, toISOString, date]
created: 2026-03-23T16:29:51Z
updated: 2026-03-23T16:29:51Z
---

# toISOString() UTC Timezone Bug Pattern

## The Bug
`new Date().toISOString().slice(0, 10)` returns the **UTC** date, not the local date.
In UTC+8 (China), between midnight and 8AM local time, this returns **yesterday's date**.

## Common Symptom
Two different parts of the UI show different day labels ("今天" vs "昨天") for the same item,
because one uses `toISOString()` (UTC) and another uses local time arithmetic.

## Fix
Replace:
```javascript
const todayStr = now.toISOString().slice(0, 10);  // ❌ UTC
```
With:
```javascript
function _localDateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
const todayStr = _localDateStr(now);  // ✅ Local
```

## Instance Found
`static/js/fund/intel.js` — `_formatDayLabel()` (group headers) used UTC, while `_formatIntelDate()` (card footers) used local time diff, causing mismatched day labels.

