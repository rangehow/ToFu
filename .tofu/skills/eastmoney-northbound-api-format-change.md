---
name: eastmoney-northbound-api-format-change
description: Bug fix: EastMoney kamt.rtmin API changed s2n/n2s from dict {f1,f2,f3} to list of minute-level CSV strings — old parser silently discards all northbound capital data via isinstance(s2n, dict) guard
enabled: true
tags: [python, eastmoney, api-change, northbound, bug-fix, parsing, trading]
created: 2026-03-25T09:11:59Z
updated: 2026-03-25T09:11:59Z
---

# EastMoney Northbound Capital API Format Change

## Bug
The `push2.eastmoney.com/api/qt/kamt.rtmin/get` API changed its response format:

**Old format (dict):**
```json
{"data": {"s2n": {"f1": 12345, "f2": 6789, "f3": 19134, "f4": [...minutes]}}}
```

**New format (list of CSV strings):**
```json
{"data": {"s2n": ["9:30,0.00,5200000.00,0.00,5200000.00,0.00", ...], "n2s": ["9:00,0.00,4200000.00,..."], "s2nDate": "03-25", "n2sDate": "03-25"}}
```

CSV column format: `time,channel1_net,channel1_quota,channel2_net,channel2_quota,total_net`

For northbound (`n2s`): `time,沪股通净流入,沪股通额度,深股通净流入,深股通额度,合计净流入`

Values are in 万元.

## Root Cause
The guard `if not isinstance(s2n, dict): s2n = {}` silently discards the entire list, producing empty northbound data.

## Fix
Parse the last entry of the list to get aggregate totals:
```python
if isinstance(n2s_raw, list) and n2s_raw:
    last_entry = n2s_raw[-1]
    parts = last_entry.split(',')
    if len(parts) >= 6:
        sh_net = float(parts[1])
        sz_net = float(parts[3])
        total_net = float(parts[5])
```

## Additional Issues Found
1. **Network check timeout too short**: `check_network()` uses 2s timeout but proxy takes 4.1s → always fails
2. **Double network gating**: radar route checked network AND each sub-function checked again → remove the outer check

