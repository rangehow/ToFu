# Matched stream-tail diagnostic (local XFS only)

One instrumented leg per backend at `e3ab0c3aa7` joined precisely 4,000 synthetic stream calls per leg by task index and round, excluding terminal settlement. Both legs recovered 1,000 exact final texts and passed integrity. The native executable matched compiled inputs (`09b0f2ac…`, executable `71b12889…`). Both ran with fresh private stores and scrubbed environment. These are diagnostic observations, **not acceptance timings or a backend ranking**. The prior formal scale verdict remains FAIL.

| Mean milliseconds, same slowest 40 events within each leg | Native | SQLite |
|---|---:|---:|
| Scheduled arrival to return | 1918.06 | 3519.50 |
| Scheduled arrival to foreground entry | 152.83 | 1851.91 |
| Outside the locked function body after entry | 0.02 | 0.02 |
| Locked function body excluding client RPC | 1756.66 | 536.51 |
| Client RPC excluding exchange | 0.24 | 0.07 |
| Client exchange (inclusive of authority work) | 8.31 | 1130.99 |

The partition subtracts nested intervals **within each event** and then computes distributions. It does not subtract percentiles. Each leg had 1,000 first-round events with three RPCs and 3,000 later events with one RPC; the native slowest 40 contained 37 first-round events. The native tail is concentrated in wall time inside the projection function outside RPC; this is not a CPU profile or proof that projection computation rather than scheduling dominates. SQLite's tail is concentrated before foreground entry and in the exchange. Pre-entry includes accumulated lateness from previous rounds and fixture preparation; the exchange includes pool work on SQLite and server wait on both. The diagnostic cannot isolate server dispatch, writer queue, commit or response transfer per call. No evidence yet justifies a product fix. Next bounded investigation: split the dominant native function interval into on-thread CPU and off-CPU time, and join SQLite request admission/dequeue and commit/response clocks to the same event before attributing the exchange tail.

[Trace archive](stream-trace-original.tar.gz), SHA-256 `b7b8b0e408991c4c9da8ddb40b7b5a8796b83d517396c760712aedb64fff6374`. The manifest covers both raw reports, client clock stamps, consoles, exits, source hashes, and summaries. No payload or credential is added to the trace rows. Replay `PYTHONPATH=$PWD .venv/bin/python3 -m benchmarks.conversation_stream_trace_summary <extracted-report.json>` for either backend. The collector entry point is `python -m benchmarks.conversation_stream_trace --backend BACKEND --binary PRIVATE_BINARY --directory PRIVATE_XFS_DIRECTORY --output REPORT.json`, in a scrubbed environment and from the pinned checkout. No full campaign was rerun.
