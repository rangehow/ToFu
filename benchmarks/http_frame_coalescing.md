# Headless SSE frame coalescing experiment

`http_frame_coalescing.py` privately binds the actual Headless application to
stream functions that send one complete successful `id`/`data` frame instead of
two yields. JSON formatting, sequence conversion, terminal markers, source
ownership and partial ID bytes on JSON failure remain canonical. Production
module globals and defaults are untouched. Yield boundaries are intentionally
different; complete wire bytes remain exact.

Run `python benchmarks/http_frame_coalescing_check.py` for stream byte/failure/
closure equivalence, then `python benchmarks/http_frame_coalescing_lifecycle.py
--output /tmp/http-frame-lifecycle.json` for real HTTP/1 and HTTP/2 TLS checks:
slow consumers, disconnect, cancellation, reconnect and HTTP/2 reset. Every
recorded server must join and exit zero. Cleanup failure is attached to a primary
request failure rather than replacing it.

Run the paired transport measurement:

```sh
python benchmarks/http_frame_coalescing_measure.py --samples 1000 --pairs 3 \
  --concurrency 16 --output /tmp/http-frame-pairs.json
```

Both arms use the actual owned Hypercorn transport with the same asyncio loop,
Headless auth/routes, local TLS and deterministic compute fixtures. Three
alternating pairs cover short requests, authentication and SSE fanout under
HTTP/1 and HTTP/2. Separate memory servers execute the same request count; CPU
includes declared warmups. Raw client wall samples and server identities are
retained; source hashes are checked around every cohort.

Post-workload server RSS is a snapshot, not peak RSS or retained-growth proof.
Server CPU is an interval average, not a raw per-request CPU vector. Synthetic
compute excludes actual inference, durable storage and full-app execution.
Lifecycle checks are functional prerequisites, not latency admission for all
slow-consumer/cancellation workloads. Reduced runs are pilots. No microbenchmark
or favorable packet-count ratio establishes whole HTTP/system admission.

`http_cpu_clock.py` uses libc `clock_getcpuclockid(pid, &clockid)` and Linux
`clock_gettime` to observe an explicitly owned live server. The caller keeps
the subprocess alive; missing/unsupported clocks fail rather than invent CPU.
See [Linux process CPU clocks](https://man7.org/linux/man-pages/man3/clock_getcpuclockid.3.html).
`tests/meta/test_http_cpu_clock.py` witnesses child CPU work, independent parent
work, graceful child join and rejected PID shapes.

Problem: overlapping concurrent requests cannot each own the same process CPU
interval. Decision: `http_cohort_cpu_measure.py` samples CPU and wall time at
barriers around one full concurrent cohort. Rejected alternatives: repeated
interval attribution double-counts CPU; coarse process tick averages cannot
supply raw per-operation vectors. Per-request latency remains diagnostic.
Initial warmup is twenty cohorts; canonical pool rotations and their declared
request warmups remain outside measured cohorts. Owned server exit must be zero.

Pilot: `--samples 2 --pairs 1 --concurrency 4 --output /tmp/http-cpu-pilot.json`.
Default diagnostic pairs use 100 cohorts, three pairs and sixteen concurrent
requests under both HTTP/1 and HTTP/2 TLS. Run exact frame and real transport
lifecycle checks separately. This driver covers short/auth/SSE cohorts only;
slow-consumer/cancellation performance, peak/retained memory and full app/storage
remain gaps. Post-workload RSS and synthetic compute never imply admission.

`http_pressure_measure.py` repeats real TCP/TLS slow-reader release and explicit
abort/transport-close quiescence. Every operation has fresh stream and observer
clients; the observer's TLS setup is primed before the measured phase. Twenty
warmups precede samples. Slow setup holds an unread 64-KiB-event stream for
50ms and witnesses active/bounded production; the retained line iterator must
live until connection release. Timed phases include observer status polling.
CPU uses the owned process clock; raw terminal witnesses must match across
latency/memory servers and arms. Memory remains a postworkload snapshot.

Repeated H2 cancellation exposed late request DATA after an early response and
a secondary TLS-close exception. The connection owner now preserves flow
credit and contains only the known already-closing TLS-peer outcome. Verify
`tests/server/test_hypercorn_connection_shutdown.py` and the original same-
connection abort/reconnect/H2-reset lifecycle check; fresh-client measurement
does not replace that gate. Pilot with `--samples 2 --pairs 1 --output /tmp/
http-pressure.json`, then default 100-sample/three-pair runs. `--diagnose-h2`
instruments only owned child processes and is never performance admission.
Failures retain the server log beside the requested output. Synthetic compute,
full-app/storage and peak/retained memory remain explicit limits.

`http_domain_measure.py` adds the standard five-case report with both HTTP/1
and HTTP/2 measurements. Short/auth/SSE operations are full concurrent batches;
pressure operations remain release/abort-and-close to quiescence. Twenty warmup
operations precede each cohort. Fresh client pools every sixteen measured
batches preserve canonical server keepalive limits; their request warmups are
excluded and declared. Synthetic compute excludes model/full-app/storage claims.

Problem: RSS snapshots cannot prove peak or retained-growth admission. Decision:
separate servers execute the complete matched cohort, sampling server RSS every
5ms and reading Linux VmHWM. Post-warmup and post-workload owned diagnostic GC
receipts require zero active producers; positive idle RSS growth is the retained
metric. Alternatives rejected: profiler/tracemalloc in latency contaminates
timing; client RSS substitutes the wrong owner; post-shutdown disappearance
masks live-server residency. One interval is not long-run leak proof.

Run exact frame/lifecycle checks and the two-sample/one-pair pilot before default
100-sample/three-pair measurements using `--output-prefix /tmp/http-domain`.
Every operation validates auth/status, protocol and full SSE order/content or
terminal producer state. Raw wall/CPU vectors, independent memory server PID,
GC baseline/final RSS and clean joined shutdown are retained. Each case has
three H1 and three H2 pairs; other domains remain incomplete in the shared gate.
