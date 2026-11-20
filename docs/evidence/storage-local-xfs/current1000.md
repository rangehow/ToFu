# Source-qualified local XFS scale comparison: FAIL

The supported runner completed two reversed-order pairs of 1,000 simultaneous conversations per leg at `b96f3847b4cd7e20225edd1f1c5d0744ba8cba2d` (tree `29067bbb8ada4c088e153f1198c8560cd3ee5e7d`). All four legs qualified for comparison but missed the unchanged **1,000 ms scheduled-stream P99** budget. All leg exits and the pair exit were 1 for completed acceptance failures, not timeouts.

| Order | Metric (P99, ms) | Native Strict | SQLite WAL/FULL | Native / SQLite |
|---|---|---:|---:|---:|
| Native → SQLite | Submit to running | 1600.34 | 3656.49 | 0.438 |
| | Scheduled stream | 1826.32 | 4139.13 | 0.441 |
| | Settlement call | 1956.53 | 2129.74 | 0.919 |
| SQLite → Native | Submit to running | 1457.64 | 3115.89 | 0.468 |
| | Scheduled stream | 1532.97 | 2843.18 | 0.539 |
| | Settlement call | 1574.66 | 2176.43 | 0.724 |

Native had lower P99 on all six eligible rows. Reversing the order balances which backend runs first; it does not establish statistical independence. This does not establish a scale pass, sustained capacity, or a DolphinFS performance result.

## Execution and evidence

The clean checkout, private native executable, scratch and fresh stores were on local XFS. A scrubbed environment excluded inherited configuration, loader overrides and FAULTFS. The native SHA-256 `71b128898a97183548b78403a639202ebfb9b262e375b5ba48b7dfd7095bf78c` and compiled fingerprint `09b0f2accb856c9b4044176ae98f1b9a10666e5746ad014591ebe64e6df3b34a` matched before launch, without rebuilding or selecting a production engine. The four-conversation paired smoke qualified both legs' source/producer evidence and missed only per-leg scale coverage; its single pair was not eligible for formal ratios.

Each scale leg observed 4,000 stream events, recovered 1,000 running Attempts before stream release and 1,000 completed Attempts after settlement, matched all final texts and passed integrity. Workload and unhealthy-readiness counts were zero; console warnings remain in the archive. Both same-store reopens record signal exit `-9`. Receipts bind native Strict and SQLite writer WAL/FULL with coalescing selected to the measured daemon incarnations, and retain SQLite/VFS mapping hashes. These are configuration and process-recovery observations, not per-ACK sync traces or power-cut evidence.

## Bounded diagnostic, not acceptance timing

After the formal run, one fresh 1,000-conversation leg per backend used the supported `conversation_concurrency.py --profile-client` mode with four 8192-byte rounds and both reopen cutpoints. The resulting profile legs are not comparable acceptance samples. Native `turn.event.record` measured 5,000 calls (stream and settlement mixed), with RPC P99 37.4 ms, wire P99 34.0 ms and pool P99 1 ms (histogram estimate), versus foreground stream-call P99 1,628 ms. The RPC, wire and pool distributions do not account for the foreground tail by themselves. The aggregate populations do not identify a dominant causal stage or support subtracting percentiles. The SQLite diagnostic has no operation-scoped client spans. The shared `pool:unscoped` histogram records 19,045 observations with a P99 bucket estimate of 1,156.27 ms (`profile-sqlite.json`); these observations are not joined to foreground calls and do not attribute their tail. Its productive writer recorded queue high-water 781, with maximum sampled commit 16.5 ms versus foreground stream-call P99 2,715 ms. The unprofiled SQLite legs likewise saw queue high-waters 887/873 versus commit maxima 9.0/6.5 ms. These support substantial backlog outside individual commits, not attribution of the whole tail to queue wait, sync, interpreter scheduling or checkpoint interference. No tuning or structural repair was justified by these diagnostic spans.

## Reproducibility

[Original archive](current1000-original.tar.gz), SHA-256 `752ebaa4e0f2db0bdf6f44e6e1444160bb466e9ffb4c2779ef004ea6b8f42597`. Extract privately and verify `MANIFEST.sha256`. It contains raw smoke, scale and diagnostic reports, logs and exits, native daemon logs, invocation and identity receipts. At the measured revision, recapture the source inventory and run `benchmarks.conversation_compare.compare` against the archived legs with `repeats=2` and `filesystem='xfs'`. `replay.log` records an exact verdict replay, all four standalone reports matching their aggregate copies, and all 4,000 independently regenerated final-content hashes matching. The six ratios reproduce from the original P99 summaries; individual request timings were not retained, so the percentiles cannot be rebuilt from samples. Executable and library identities are retained as hash receipts, not original binary bytes. Local XFS describes the measured stores and private staging, not every loaded dependency. Neither effective durability settings nor process-crash recovery establishes per-ACK sync behavior or power-loss durability.
