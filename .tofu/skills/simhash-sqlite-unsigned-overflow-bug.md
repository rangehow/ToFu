---
name: simhash-bigint-unsigned-overflow-bug
description: Bug fix: 64-bit unsigned SimHash overflows PostgreSQL signed BIGINT (~50% of hashes) — use to_signed64/to_unsigned64 conversion for storage
enabled: true
tags: [python, sqlite, simhash, overflow, unsigned, bug-fix, intel-crawler, db-corruption]
created: 2026-03-24T03:06:19Z
updated: 2026-04-02T23:42:00Z
---

# SimHash SQLite Unsigned Integer Overflow Bug

## Problem
`compute_simhash()` returns an **unsigned** 64-bit integer (0 to 2^64 - 1).
SQLite's INTEGER type is **signed** 64-bit (max 2^63 - 1 = 9223372036854775807).

When bit 63 is set (~50% of hash values), the Python int exceeds SQLite's range:
```
OverflowError: Python int too large to convert to SQLite INTEGER
```

This was happening on every intel crawl cycle and the cascading `disk I/O error` from
unhandled exceptions in the crawl thread may have contributed to full DB corruption.

## Fix
Added `to_signed64()` / `to_unsigned64()` in `lib/trading/simhash.py`:

```python
def to_signed64(h: int) -> int:
    """Convert unsigned 64-bit SimHash → signed 64-bit for SQLite storage."""
    return h - (1 << 64) if h >= (1 << 63) else h

def to_unsigned64(h: int) -> int:
    """Convert signed 64-bit (from SQLite) → unsigned 64-bit SimHash."""
    return h + (1 << 64) if h < 0 else h
```

Applied in:
- `lib/trading/intel.py` — `to_signed64()` before INSERT, `to_unsigned64()` after SELECT
- `lib/database.py` — `to_signed64()` in migration backfill

## Key insight
Hamming distance is identical on signed vs unsigned representations (XOR doesn't care about sign interpretation), so the dedup logic works perfectly with signed storage.

## Files changed
- `lib/trading/simhash.py` — added conversion helpers
- `lib/trading/intel.py` lines ~921, ~365 — convert on INSERT/SELECT
- `lib/database.py` line ~443 — convert in backfill migration
