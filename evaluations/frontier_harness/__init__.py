"""FrontierHarness Eval v1 on the native Terminal-Bench runner.

Responsibility: everything FHE-specific around the shared native runner
(``evaluations.terminal_bench``): the v1 task identity (FHE e837a70 published
``task.toml``/``instruction.md`` over the ``terminal-bench@2.0`` registry pin),
the FHE egress allowlist, and the export of native trials to FHE ``trial.json``
records priced at the frozen FHE Kimi K3 table, which the FHE repository's own
``normalize-results.mjs`` then scores unchanged.

Entry point: ``python -m evaluations.frontier_harness prepare-tasks|export``.
Owner document: ``evaluations/frontier_harness/README.md``.
"""
