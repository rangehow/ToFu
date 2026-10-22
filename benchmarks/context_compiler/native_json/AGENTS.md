# Native JSON-carrier experiment

`src/lib.rs` owns a read-only CPython stable-ABI finite graph walker and
source numeric-token guard. The token guard requires successful strict JSON
parsing first and preserves overflow evidence discarded by duplicate keys.
This experiment owns neither provider translation nor production selection.
Only exact builtin carriers are supported. Unsupported/cyclic graphs return
false. All borrowed pointers are call-local under the GIL, without Python
callbacks, reference ownership or global caches.

Build with Cargo into an external `CARGO_TARGET_DIR` using the compatible
Python launcher in `PYO3_PYTHON`. Keep the compared binary/source digests frozen.
Run `check.py` before measuring. Measurements must include full actual argument
validation, canonical malformed/surrogate/overflow fallbacks, no-tool/small
controls, cold activation and separate memory workers. Native decode timing
alone never authorizes a production default.

`lazy.py` owns experiment-local once-only initialization and same-thread reentry.
`controlled.py` owns independent-worker short batches plus separate full-cohort
memory workers; `cold.py` includes first binary/factory activation. Check exact
output/runtime/activation receipts and retain all adverse prior measurements.

`retained.py` measures unique-input post-cleanup residency in separate paired
processes, with input/output trace parity and per-worker source/binary/runtime
witnesses. Traced deltas include observer records; cold activation and complete
context/resource admission remain separate.
