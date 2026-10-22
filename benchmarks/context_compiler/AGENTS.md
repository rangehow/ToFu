# Incremental context experiment guidance

This directory owns an offline immutable-history and sanitized-wire prototype.
It does not own durable history, source permissions, token admission, compaction
settlement, or provider translation. Production still uses the existing owners.

`compiler.py` retains input revisions and bounded group caches; canonical
`apply_wire_sanitize` is the differential oracle and unsafe-shape fallback.
`check.py` exercises revision and lifecycle boundaries. `measure.py` compares
raw producer history with the proposed writer/compiler costs, including first use.
Outputs stay outside the repository. Freeze sources during paired measurements.
Read README.md before interpreting the five named workload results as coverage.

`cold.py` measures empty-cache first builds with producer revision costs and
cleanup included; its bulk factory is experimental and uses the same record
admission. It never authorizes production history or context integration.

`provider_measure.py` measures raw-source IR capture/materialization through
actual native request owners, including rejection/loss receipts. Separate memory
workers run full append cohorts so history growth matches latency workers.
Token counts are supplied; durable producers and token admission stay separate.

`native_copy.py` isolates only native-sidecar copying inside actual request
construction, preserving canonical custom hooks/failures. `native_copy_measure.py`
borrows worker accounting through a private function namespace; both arms use
raw histories, and no production module global or default is replaced.

`conversion_copy.py` isolates checked conversion's whole-body snapshot while
keeping canonical body construction and protocol adapters. Its nine-profile/
density checks include custom-copy hooks, aliases/cycles and fixed-code failures.
`conversion_copy_measure.py` reuses the existing paired accounting with private
function namespaces; run its two-sample smoke before full independent pairs.

`admission_project.py` includes canonical final adapted-body token recount and
budget admission in both protocol-copy arms. Verify stale-count mutation denial
and actual local tokenizer before `admission_measure.py` small/full independent
pairs. Final count receipts are part of the oracle; supplied preliminary build
counts cannot authorize reuse. Other context ownership boundaries remain gaps.

`native_producer.py` / `native_producer_measure.py` add real settled Turn writes,
owner-bound native conversation reads and the actual message producer before
final admission. Verify `check_native_producer.py` and the default-owner HTML
neighbor after changing shared native fixture ownership. Both protocol-copy arms
reread the same static history; population is outside timing. Preserve native
continuity, typed owner rejection and close/reopen; compare build/library/Turn/
tokenizer witnesses across latency and memory workers. This is static native
producer coverage, not append/edit/compaction/media or whole-context admission.

`mutations.py` binds canonical native Turn append/edit and atomic compaction
through private owned clients. Verify `check_mutations.py` before the small/full
`mutations_measure.py` cohorts. README.md defines units, lifecycle, supplied
summary/inline-image limits and post-disposal RSS accounting. Preserve public
projection privacy, revision conflicts and opening/retained-order invariants.

`tool_argument_measure.py` compares frozen original argument validation with the
current implementation through real checked Anthropic/Responses adapters. Private
function namespaces preserve production globals. Separate full-cohort RSS workers,
exact output traces and input/source digests are required. Raw output stays outside
the checkout; this is protocol conversion, not whole-context or inference evidence.
