# Captured pre-CAS history source

These text fixtures are source evidence for
`tests/project_mod/test_coordinator_legacy_history_evidence.py`, not importable
runtime modules, production records, transcripts or a backfill tool. The owning
contract and offline reconciliation limits are in
`docs/PROJECT_COORDINATION.md`, “Pre-CAS evidence and offline reconciliation
boundary”.

| Fixture | Git source | Capture |
|---|---|---|
| `pre_cas_d12cd290.txt` | `d12cd290ae69d77639c683ca870be9396b8bc667:lib/conversations/coordinator_task_history.py` | Full exact blob `b20c6ddf1cdc8a157d370982b8e3fce657a60a16` |
| `pre_cas_d1422634.txt` | `d142263462235523f510e02e68e27b59cecc50f7:lib/conversations/coordinator_task_history.py` | Full exact blob `ef9043dbe5607a1c449fcf0c4d572c3e69edbbd9` |
| `pre_cas_publish_d12cd290.txt` | `d12cd290ae69d77639c683ca870be9396b8bc667:lib/conversations/coordinator_progress.py` | `CoordinatorTaskProgress._publish`, `ast.get_source_segment` plus newline |
| `pre_cas_prepare_d1422634.txt` | `d142263462235523f510e02e68e27b59cecc50f7:lib/conversations/coordinator_progress.py` | `CoordinatorTaskProgress._prepare_write`, `ast.get_source_segment` plus newline |

The test pins SHA-256 of every fixture and compiles captured functions and the
`_Records` class unchanged. It supplies current module globals for standard
library dependencies, validation and storage policy, while the captured history
functions replace their current counterparts. Retired reader-only query-limit
constants and the old HTTP reader are not executed. The current reader projects
the final synthetic records. This tests the relevant historical write algorithm,
not a complete frozen old release.

The early captured `_publish` drives its own CAS loop over the in-memory store.
For the later version, the current retry loop calls the captured `_prepare_write`
(which writes history before the CAS); no atomic delta is supplied by that method.
Both receive full ordinary board requests through `_publish`, without the newer
public expected-revision API. Explicit in-memory bindings, a synthetic clock and
no-op notification are injected. The transcript read seam fails the test if used.
The store records every history/head value and CAS version; only synthetic head
CAS conflicts are injected. No process is started or restarted, no native engine
is run and no storage backend, production index, chat or board data is accessed.

The tests document an evidence limitation, not desired acceptance of abandoned
rows. If a future reviewed repair changes the projection, preserve the historical
indistinguishability assertion; do not weaken it to declare ambiguous old rows
successful. The immediate pre-repair capture is the first parent of
`8239d651ee57a80f2dd5f5548a75f27df98ec33f`, included in the de694408 delivered
checkpoint `2f54211b89af34073423d6bb12e1fdface545fff` and integration
`614a5f1d673fb9e871b23d8a0f0c7eb26a9950dd`.
