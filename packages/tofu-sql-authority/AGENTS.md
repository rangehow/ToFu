# Rust SQL authority guidance

## Scope

This crate is the Rust port of `lib/storage_authority_sql` (Rust rewrite P3b,
`docs/RUST_REWRITE_EVIDENCE.md`): the same `storage.v1` wire contract, the
same SQLite schema (`contracts/sql_authority_schema_v1.json`, generated from
`schema.py` by `scripts/gen_sql_authority_schema.py`) and the same semantics
per catalog operation (`contracts/storage_operations_v1.json`). The Python
authority stays the oracle and the only one any product launch selects; the
Rust binary runs only under a test project root via explicit
`StorageSupervisor(..., sql_authority_command=...)`. Only test fixtures read
`TOFU_TEST_SQL_AUTHORITY_BIN`; product and maintenance launchers ignore it.

## Porting rules

- One `src/ops/<domain>.rs` per `operation_domains/<domain>.py`; submodules
  under `src/ops/<domain>/`. Register every operation with exactly the
  catalog's kind, receipt and timeout (`catalog_tests` enforces it).
- Keep each Python SQL statement verbatim, including whitespace that SQLite
  persists (DDL), parameter order and `ORDER BY`/`LIMIT` placement. Port
  control flow line by line: the same validation order, the same error code
  AND message, the same result keys, the same int-vs-float JSON types.
- Python semantics live in shared helpers: `payload::{required_text, integer,
  number, expected_version}`, `json::{truthy, python_str, python_repr,
  python_float_repr, dumps_sorted}`, `codec::{dump, load, py_eq}`,
  `session::Row` (`dict(sqlite3.Row)`), `params![]`. Stored JSON is orjson
  bytes bound as BLOB (`codec::dump_param`).
- Clocks: `int(time.time() * 1000)` is wall ms; worker leases use
  `session.worker_lease_authority()`. Keep `faults::inject_once` points.
- Never weaken durability: commands run in `BEGIN IMMEDIATE`; a failure rolls
  back before classification; receipts are written in the same transaction.

## Verification

Run build/test/clippy only with `python3 scripts/remote_cargo.py -C
packages/tofu-sql-authority <cargo arguments>`. `cargo fmt --check` is local.
The temporary per-domain local build helper has been retired.

For the retained Python oracle, use the same-node post-build runner:

```sh
python3 scripts/remote_cargo.py -C packages/tofu-sql-authority \
  --after-build release/tofu-sql-authority \
  tests/storage_authority_sql/run_rust_sql_parity.sh --clean-source \
  build --release --bin tofu-sql-authority --locked --offline
```

Use `run_rust_sql_changed.sh` instead of `run_rust_sql_parity.sh` for the
bounded identity/wire/replay/launch witnesses. Both runners require a clean
commit/tree and verified source archive, and the paired fixtures check the
live candidate SHA-256 against the post-build SHA-256. Ordinary work keeps
both `(PID, start ticks)` generations; a SIGKILL replaces only the named role,
with no executable/command drift, and both identities hold through replay.

`run_rust_sql_domain.sh` runs the wider authority-boundary lane;
`run_rust_sql_release_load.sh` reproduces large-turn reads at eight-worker
concurrency with independent directories. The binary comes from that exact
snapshot, not the live install. Tests require an explicit
`TOFU_TEST_SQL_AUTHORITY_BIN`; they never implicitly compile locally.
The differential requires equal values or equal error codes/messages and
equal durable tables. The outbox replay test uses the Python shadow reader,
decryptor and passive replay target on Rust-produced records.
