# Stream tail: caller CPU and client transport follow-up

Nonformal, private local XFS only. One leg per backend from `6a3f2a8f82`, using the source-qualified native executable `71b12889…`, 1,000 conversations and four stream events per conversation. All 4,000 synthetic event IDs per leg joined; all instrumented calls returned without an exception and all foreground event calls returned without a false rejection. Both legs passed integrity and source qualification. No new winner or scale verdict.

| Matched mean (ms), slowest 40 by scheduled arrival to return | Native | SQLite |
|---|---:|---:|
| Scheduled arrival to return | 1915.131 | 2041.777 |
| Before foreground entry | 51.089 | 190.391 |
| Projection body outside RPC: caller thread CPU | 0.701 | 0.655 |
| Projection body outside RPC: wall minus caller CPU | 1857.919 | 1189.120 |
| Shared client-pool acquisition | 0.052 | 4.237 |
| Client wire envelope excluding pool | 5.089 | 650.826 |

Wall and thread CPU are subtracted within each synthetic event, not across percentiles. Pool acquisition is separately measured on the shared pool owner, not native-only. Native wraps the session request after acquire; SQLite sums the client `send_frame` and `recv_frame` calls, excluding acquisition and the gap between send and receive. Thus the envelopes exclude pool on both sides, but are not identical codec/transport boundaries. SQLite's send and receive means were 4.158 and 646.668 ms respectively. Receive includes server work and waiting for the client thread to run; it does not isolate commit or response transfer. The table is not an exhaustive partition. `returned` describes Python control flow, not an independent protocol-level success status for each nested frame.

The largest measured component is off-CPU time outside RPC while in the projection body, in both observed slow cohorts. This rules against treating projection computation on the calling thread as the dominant cost in these legs, but does not distinguish lock/GIL blocking from OS scheduling or other waits. Instrumentation overhead was not measured against a matched trace-off control. No product change is supported.

**Unmet diagnostic boundary:** no SQLite server spans were acquired. The next join should retain the existing v1 `request_id` at client send/receive and server `_dispatch`, propagate that opaque ID through writer job enqueue/dequeue, and attach `_commit_segment` start/end to participating jobs (shared batch duration, never summed per job as throughput). Capture server send separately and verify same-host monotonic namespace and causal containment before deriving any server-residence split. For the projection residual, collect caller scheduler and blocking evidence only for the owned diagnostic process during the event window. These are proposed measurements, not established causes.

[Raw archive](stream-cpu-original.tar.gz) SHA-256 `6a5e3c234a820931a88c44b0b9f9d260499e91f25d1ece5c8e0a35c7887e630b`. Verify `MANIFEST.sha256`; replay with `PYTHONPATH=$PWD .venv/bin/python3 -m benchmarks.conversation_stream_cpu_summary <extracted-report.json>`. The prior archive and formal local-XFS FAIL are unchanged.
