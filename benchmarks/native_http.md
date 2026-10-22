# Native HTTP experiment

`native_http.py` compares owned Hypercorn and Granian2.8.4 embedded ASGI using
the same Headless application, authentication, Python asyncio loop and synthetic
compute. Granian uses one native runtime thread. A small adapter hands over an
owned inherited socket through SocketHolder; this is version-sensitive experiment
code, not a production transport switch. The native dependency lives outside the
checkout and is not added to application requirements.

Default invocation runs real H1/TLS H2 pause, backpressure, disconnect, reconnect,
abort and RST_STREAM/same-connection reuse checks. `--measure` compares short
requests, authentication rejection and SSE output at1000 observations per variant
and three alternating independent-process pairs. Separate server runs sample RSS
at5ms; CPU includes declared warmup/cohort requests. Client pools renew every256
requests in both variants, retaining the owned server keepalive policy.

Run `python benchmarks/native_http.py --output /tmp/native-lifecycle.json`, then
`python benchmarks/native_http.py --measure --output /tmp/native-http.json` with
Granian available on the experiment Python path. Source/runtime identity and all
raw request samples belong to the external artifact. Logs and temporary sockets,
certificates and processes are owned and graceful shutdown is joined.

Granian embedded mode is experimental upstream. These results exclude its normal
multiprocess CLI deployment and the full Tofu application/agent/storage. Cold
startup, whole application recovery, deployment integration and resource-scaled
runtime policies remain required before promotion. Official capability source:
https://github.com/emmett-framework/granian

`native_http_task_admission.py --output /tmp/native-task-admission.json` executes
the installed embedded server's task-mode admission with transport startup
intercepted on that server instance. Granian2.8.4 changes a requested Rust task
mode to asyncio on Python >=3.12; the application's supported floor is >=3.12.
The report records the requested/effective mode and package/project source
digests. A silent fallback is not a separate Rust performance observation.
This does not evaluate older unsupported app runtimes or establish lifecycle
compatibility if a future version retains Rust mode.

`native_http_cli.py --output /tmp/native-cli-lifecycle.json` runs the normal
pinned Granian CLI with an ASGI factory for the same real Headless application.
One worker, asyncio task/loop and one native runtime thread are explicit.
It checks H1 and TLS H2 pause/backpressure, disconnect/reconnect, abort and
H2 RST_STREAM with same-connection reuse, then verifies successful CLI exit and
joined worker descendants. There is no private inherited-socket adapter or
transport monkeypatch. Use `TOFU_BENCHMARK_PYTHON` for the explicit CLI launcher.
The CLI owns binding after an ephemeral-port handoff; a competing binder causes
a visible failure without retry. A dedicated process group bounds forced cleanup,
and a cleanup failure is reported without hiding an existing primary exception.
This is normal-process-model lifecycle evidence, not latency/CPU/RSS, scaled
workers, full durable-app integration or production deployment admission.

`native_http_cli_measure.py --output /tmp/native-cli-measure.json` compares the
normal CLI compute factory with the owned server on short/auth/SSE cases under
H1 and TLS H2. Defaults are three alternating pairs of 1,000 requests per arm
and independent RSS trials. CLI CPU/RSS includes leader plus worker; the owned
control has one server process. Process identities cannot change mid-cohort.
Both reuse the same public request/content/protocol oracles and pool-renewal
policy. CLI cleanup reuses the validated joined process-group owner. An explicit
Python launcher also reaches the owned control child without global patching.
`--samples 2 --pairs 1 --protocol h2 --case sse_fanout` is smoke only. CPU includes
declared warmups; no raw per-operation CPU vector, cold/scaled-worker/full-app
or recovery admission is claimed. Source identities are checked around each arm.

The normal-CLI callback-owner regression check observed one TLS H2 same-connection
abort request timeout while its reconnected stream remained open. H1 passed;
an unchanged-sequence repeat passed H1/H2 and joined shutdown. Both observations
remain evidence: intermittent cancellation reliability is unresolved, and a
passing repeat or speed result cannot authorize switching the product default.

`native_http_cli.py --protocol h2 --repetitions 10 --logs-dir /tmp/cli-stress-logs
--output /tmp/cli-stress.json` runs bounded, independent normal-CLI lifecycle
trials, preserving each result/failure and its process log directory. Any failure
makes the final command fail; teardown failure stops new trial admission.
Abort timeouts retain their original exception and add a bounded execution-state
observation through a separate diagnostic connection after the timeout. The
tested same-connection cancellation sequence and timeout are not changed.
Ten passing trials did not reproduce the earlier H2 timeout; this does not
close the unresolved reliability gate or waive the recorded failure.

`http_h2_credit.py --pairs 3 --logs-dir /tmp/h2-credit-logs --output
/tmp/h2-credit.json` distinguishes cancellation handling from response delivery
through actual Headless routes on alternating owned/normal-CLI TLS servers.
**Problem:** an unread stream can exhaust connection DATA credit, so a timed-out
JSON response alone does not prove that abort handling stalled. **Decision:**
exhaust the default 65,535-byte connection and SSE windows, witness HTTP 200
headers and aborted state through an independent observer, then grant 4,096
bytes of connection credit without granting SSE stream credit. Require the
abort body to complete with unchanged SSE bytes, reset the paused stream, join
its producer and complete a new request on the same connection.
**Alternatives:** merely increasing the timeout or using a separate connection
for cancellation would not distinguish these states. Flow control semantics are
defined by [RFC 9113 §5.2](https://www.rfc-editor.org/rfc/rfc9113.html#section-5.2).
The original HTTPX sequence and timeout remain unchanged. This separate
diagnostic neither retroactively observes the historical failure's windows nor
qualifies server performance/defaults. The matching API tests inject an abort
that returns success without changing execution state and require rejection plus
socket/producer cleanup.

`system_http_h2_credit_20261015.json`
records all six independent trials passing across nine server processes. Both
servers deliver the 68-byte abort response after connection credit is released;
SSE stays at 65,535 bytes. Three fresh original lifecycle trials per H1/H2
protocol and 63 neighboring checks pass at the frozen source. The historical
HTTPX failure remains unassigned because its flow windows were not captured.

The full normal-CLI comparison
retains 36,000 latency observations across 72 independent server trials. CPU
means improve 23-53%, but process-tree RSS rises 69-84% and H2 SSE P50
regresses 21-22%. **Decision:** do not promote this configuration. **Alternative:**
changing the embedded adapter to the normal CLI removed that uncertainty but
did not satisfy memory/latency guards. Scaled policies and other native designs
still require measurement; the earlier intermittent H2 abort failure remains open.

`native_http_memory_profile.py` diagnoses resident memory independently of the
latency/RSS admission comparison. It uses the same actual Headless app and
short-auth/SSE protocol oracles under H1/H2, recording ready/post-request
RSS/PSS/USS/shared mappings and thread counts for each owned server or CLI
leader/worker. Alternating independent pairs retain package, upstream Python
source and project witnesses. Owned shutdown verifies exit zero and join,
preserving a primary error if cleanup also fails; CLI uses its existing group
owner. Instrumented latencies are discarded. `--samples 2 --pairs 1` validates
the diagnostic only; defaults are 100 requests and three pairs. These snapshots
are not peaks, growth or full-app evidence, and PSS varies with other host
processes sharing mappings. Existing RSS/latency/cancellation failures remain
unwaived; PSS/USS never replace the current RSS admission guard.

Three-pair resident diagnosis verifies 2,400 request observations in 36 distinct
server processes, with all request oracles and joined shutdowns passing.
`system_http_resident_profile_20261008.json`
retains ready/post-request RSS/PSS/USS profiles. PSS and USS remain higher for
the normal CLI: shared mappings explain part of summed RSS, but do not explain
all measured resident/unique overhead. These snapshots do not establish peaks
or full-app footprints; all prior admission failures remain unchanged.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
