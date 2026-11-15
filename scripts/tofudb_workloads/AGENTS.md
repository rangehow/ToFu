# Storage comparison workloads

## Owners

`catalog.py` assembles the ordered phases consumed by
`scripts/tofudb_perf_compare.py`. `inputs.py` carries invocation identity,
scale, timestamps and explicit execution/measurement ports. Concrete database
adapters, binary admission, CLI selection and reporting remain in that script.

- `records.py`: record/header operations and independent Turn append targets.
- `history.py`: large payloads, windowed history, sidebar recency and bulk hydration.
- `search.py`: asynchronous projection preparation and its readiness witness.
- `concurrency.py`: declared reader/writer populations, operation mixes and clocks.
- `workers.py`: startup barrier, owned sessions, phase window and joined teardown.
- `recovery.py`: exact cold/warm reads and acknowledged-state crash controls.
- `witnesses.py`: joined charter witnesses on fresh authorities beside the
  measured ones (matched history sweeps, actual-volume lock refusal plus
  mid-write SIGKILL recovery, idle-versus-pressure cohorts on one small CPU
  set, writer-only versus writer-plus-N-reader scaling). Engines own kill,
  reopen and second-authority admission; `HostControls` owns affinity,
  harness-owned stressors and /proc observations; one engine's failure is
  recorded for that engine only. Acceptance stays in
  `scripts/tofudb_charter_assess.py`.

These modules do not import the CLI or concrete engines. Preserve workload
order, owner/run identities, payloads, setup outside measured windows and every
failure/refusal distinction. Adding a workload does not create a new engine or
measurement policy. The public export retains this complete source package.

## Verification

Run `tests/tofudb/test_tofudb_perf_compare.py`,
`test_comparison_worker_lifecycle.py`, `test_comparison_phase_lifecycle.py`,
`test_tofudb_footprint_accounting.py`, and for witnesses
`test_charter_witnesses.py` plus `test_tofudb_charter_assess.py`.
For moves, compare complete operation traces and reports against frozen source,
including failures; run real disposable authorities for execution parity.
Never claim production performance from fixture timings. The authority is
`docs/STORAGE_PERFORMANCE_CHARTER.md`.
