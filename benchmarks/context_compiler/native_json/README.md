# Native argument validation experiment

This candidate pairs Pydantic Core's existing Rust JSON parser with a specialized
Rust numeric-token guard, rather than its slower generic recursive union schema.
The graph walker remains a separately checked kernel; source-token validation
preserves overflow evidence even when a later duplicate key overwrites a value.
Production protocol conversion and backend selection remain unchanged.

Build outside the checkout:

```sh
PYO3_PYTHON=/path/to/python CARGO_TARGET_DIR=/tmp/tofu-json-target \
  cargo build --offline --release --manifest-path benchmarks/context_compiler/native_json/Cargo.toml
python benchmarks/context_compiler/native_json/check.py \
  --library /tmp/tofu-json-target/release/libtofu_json_finite.so \
  --output /tmp/tofu-json-check.json
```

The walker holds the GIL while borrowing exact builtin graph references. It
preserves arbitrary-size integers and rejects nonfinite floats, unsupported
carriers and cycles without executing Python hooks. Its call-local stack and
active-container set grow with depth, not array width; allocation growth uses
fallible reserves and propagates a fixed-content `MemoryError`. It retains no
caller references, persistent graph cache, background worker or storage authority.

`decoder.py` supplies a private explicit activation factory. Small, non-ASCII and Unicode-escaped
inputs retain canonical parsing; ASCII input avoids a persistent UTF-8 cache on
caller strings. Native string caching is disabled. Native syntax, depth, integer
range and surrogate failures retry the canonical validator; root-shape error
priority and interpreter integer limits stay authoritative. Standard decoder,
constructor/scanner, math and validator overrides retain their original ports.
The initial 512-character crossover is experimental, not a production policy.

Verify exact output types/key order/float bits, custom hooks and resource failure
behavior before paired actual protocol conversions. Include no-tool, unique and
aliased arguments, wide integer/float graphs, escaped/direct Unicode, malformed
input and first activation. Record complete traces, source/binary identities,
raw CPU/wall distributions and separate peak/retained memory workers. Decoder
microbenchmarks alone do not establish whole-context or six-domain admission.

`controlled.py` alternates short batches between two independently warmed,
same-CPU workers; each operation keeps its own wall/CPU sample. Separate memory
workers use the same service, fixture, lazy predicate and activation state.
Pydantic Core version/binary identities must match across all paired workers.
Both arms preload the native binary/parser for equal code residency; the reported
`native_activations` counts lazy callable publication, not DLL first loading.
Actual binary/factory cold activation requires its separate cold cohort.

Lazy initialization state and its per-service reentrant lock are allocated only
on the first eligible argument. A process-owned publication lock protects that
state allocation; native loading runs outside it under the per-service lock,
so independent services can initialize concurrently. Allocation failure leaves
no published state, and loading failure retains retry/reentry behavior. Cold
construction and peak residency remain measured, not inferred from this design.

`retained.py` measures unique-input post-cleanup residency in separate paired
processes, with input/output trace parity and per-worker source/binary/runtime
witnesses. Traced deltas include observer records; cold activation and complete
context/resource admission remain separate.

The retained deferred-state candidate is **not admitted**: its complete warm
20-case cohort (44,400 observations, 240 processes) fails 12 guards. Cold controls
were stopped after 2,400 fresh processes because small-input tails also regress;
unmeasured cold cases remain incomplete. The 120,000-operation retention probe
is bounded memory evidence only. See
`system_native_argument_deferred_state_20261009.json`.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
