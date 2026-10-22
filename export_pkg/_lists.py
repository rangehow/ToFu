"""Constant registries, terminal/color helpers, logger, and the shared ROOT
for the export pipeline (split out of ``export.py`` for size hygiene).

``ROOT`` is the project root (one level above this package). This module has no
dependency on the other ``export_pkg`` modules, so it sits at the bottom of the
import graph.
"""
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Central registry of agent-written project-local artifacts (.tofu*). Used so
# the export sanitizer recognises present + future artifacts by their reserved
# prefix instead of re-listing each name (see lib/agent_loop/agent_artifacts.py).
sys.path.insert(0, str(ROOT))
from lib.agent_loop.agent_artifacts import GITIGNORE_PATTERN as _AGENT_ARTIFACT_GLOB  # noqa: E402, F401
from lib.agent_loop.agent_artifacts import is_agent_artifact  # noqa: E402, F401


# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════
# ANSI — only emit color codes when writing to a real terminal. When stdout
# is piped/captured (e.g. run_command, CI, redirected to a file) the escape
# sequences would otherwise show up as literal "[2m…[0m" garbage, since
# nothing interprets them. Honor the NO_COLOR convention (https://no-color.org).
_USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
if _USE_COLOR:
    C_RED    = '\033[91m'
    C_GREEN  = '\033[92m'
    C_YELLOW = '\033[93m'
    C_CYAN   = '\033[96m'
    C_BOLD   = '\033[1m'
    C_DIM    = '\033[2m'
    C_END    = '\033[0m'
else:
    C_RED = C_GREEN = C_YELLOW = C_CYAN = C_BOLD = C_DIM = C_END = ''

# ── Logger ──
logger = logging.getLogger('export')
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

#  Exclusion lists — what to NOT copy
# ═══════════════════════════════════════════════════════════

# ── Level 0: Personal — only skip bulky regenerable junk ──
PERSONAL_EXCLUDE_DIRS = {
    '__pycache__',                 # Python bytecode
    '.pytest_cache',               # pytest cache
    '.hypothesis',                 # property-test example database
    '.playwright-cli',             # browser probe page snapshots
    '.venv',                       # local Python environment
    'venv',                        # alternate local Python environment name
    'htmlcov',                     # generated HTML coverage report
    'build',                       # setuptools build artifacts (regenerable)
    'dist',                        # wheel/sdist build output (regenerable)
    'tofu.egg-info',               # setuptools egg metadata (regenerable)
    '.git',                        # git history (large, clone fresh is better)
    'node_modules',                # (shouldn't exist, but just in case)
    'offline_pkgs',                # offline pip packages
    '.migrate_backup',             # migration backups
    '.update_backup',              # self-update tarball backups (per-host safety net)
    '.project_indexes',            # project index caches (auto-rebuilt)
    'logs',                        # application logs (regenerable)
    # NOTE: pgdata/ holds the chat-history database and is NEVER copied —
    # personal mode intentionally ships config + credentials only, so the
    # destination boots with a fresh empty DB. (Raw-copying a live pgdata
    # across FUSE/NFS would also cause TOAST chunk corruption regardless.)
    'pgdata',                      # PG cluster directory (chat history — never copied)
    'pg_backups',                  # timestamped pg_dumpall SQL dumps (chat history)
    'sundries',                    # unrelated scripts/files (not part of tofu)
    'swebench_workdir',            # SWE-bench working directory (large, transient)
    'swebench_rerun_workdir',      # SWE-bench rerun results (thousands of venvs)
    'swebench_pilot_workdir',      # SWE-bench pilot working directory (transient)
    'swebench_glm_ab_workdir',     # SWE-bench GLM A/B working directory (transient)
    'swebench_full',               # SWE-bench full dataset working directory
    'abtest_workdir',              # A/B-test working directory (many conda envs + repos)
    '.eval-runs',                  # Hermetic evaluation artifacts (self-ignored too)
    'eval-runs',                   # Explicit in-repo TOFU_EVAL_ROOT fallback
    'evaluation_results',          # Upstream SWE-bench reports
    'sb-cli-reports',              # Hosted SWE-bench reports
    'jobs',                        # Harbor job artifacts
    'trials',                      # Harbor standalone-trial artifacts
    'overleaf_cache',              # Overleaf project cache (~89MB, regenerable)
    'niumark',                     # unrelated project (not part of tofu)
    'outputs',                     # smoke-test / eval output (thousands of files, transient)
    'output',                      # local browser/test screenshots (transient)
    'autoharness',                 # separate internal LongCat harness workspace
    'promo',                       # promotional slides/screenshots (~10MB, not runtime)
    'propaganda',                  # retired root-level README screenshot dump
    'django',                      # stray empty dir (not part of tofu)
    '.tofu_trash',                 # trash/undo bin (transient, per-host)
    '_candidates',                 # raw AI-gen asset candidates + proof strips (review-only, multi-MB)
}
# Dirs excluded ONLY at the project root (not inside subdirs like lib/)
_TOP_LEVEL_ONLY_EXCLUDE_DIRS = {
    'tools',                       # separate project (md2cards), NOT lib/tools/
    'paper',                       # academic paper draft, NOT lib/paper/ (core package)
    'codex',                       # external source checkout used for local comparison
}
# Like _TOP_LEVEL_ONLY_EXCLUDE_DIRS but honored ONLY in opensource mode:
# bench_real_datasets.py and friends carry hardcoded internal paths that must
# never publish, while personal/internal exports keep the whole scripts/ tree
# (build-desktop.yml calls scripts/release_assets.py from a personal export).
# Must stay root-ANCHORED — a bare component match silently strips the nested
# git-tracked packages/tofu-db/scripts/ package (the lib/paper drop-bug class;
# was OPENSOURCE_EXTRA_EXCLUDE_DIRS until 2026-09-18).
_TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS = {
    'scripts',
}
# Like _TOP_LEVEL_ONLY_EXCLUDE_DIRS but honored in internal + opensource modes
# (personal deliberately KEEPS its own data/, so it is NOT unioned into the
# personal sets). 'data' MUST be root-anchored: as a nested (path-component)
# exclusion it strips legitimate nested packages — the android/ subtree's
# Room package android/app/src/.../client/data/ lost 3 tracked files to it
# (the lib/paper drop-bug class). Root data/ (databases, configs, runtime
# state) stays fully excluded in both modes.
ALWAYS_EXCLUDE_ROOT_ONLY_DIRS = {
    'data',                        # databases (*.db), configs, runtime state — ROOT only
}

PERSONAL_EXCLUDE_GLOBS = {
    '*.pyc',
    '*.pyo',
    '*.log',                       # log files anywhere
    '.coverage*',                  # coverage DBs, including parallel-worker shards
    'coverage.xml',                # generated XML coverage report
}

PERSONAL_EXCLUDE_FILES = {
    'pg_backup.sql',               # staged PG restore dump (chat history)
    'nohup.out',
    'server.log',
    'server.log.1',
    'copy_pid.txt',
    'import_pid.txt',
    'import_status.txt',
    'import.log',
}

PERSONAL_EXCLUDE_GLOBS_HEAVY = {
    # Old/broken database files and SQL dumps (can be hundreds of MB)
    '*.db',
    '*.db-journal',
    '*.db-wal',
    '*.db-shm',
    '*.db.tmp',
    '*.db.tmp-journal',
    '*.db.corrupted_*',
    'dump.sql',
    'recover.sql',
}

# ── Level 1: Always excluded (internal & opensource) ──
# These contain personal data, chat history, runtime state
ALWAYS_EXCLUDE_DIRS = {

    'export_pkg',                  # maintainer-only private sanitization markers
    # Personal data / runtime
    'logs',                        # application logs (contain queries, API calls, personal content)
    '.chatui',                     # skills, error resolutions (personal)
    '.tofu',                       # agent state: file-history undo store, skills, error resolutions (personal)
    '.codex',                      # Codex CLI per-machine skills/state (see lib/skills/paths.py
                                   # PROJECT_SKILL_ROOTS); never part of the shipped product
    '.project_sessions',           # session caches (contain conversation context)
    '.project_indexes',            # project index caches
    'uploads',                     # uploaded images (personal)
    '__pycache__',                 # Python bytecode
    '.pytest_cache',               # pytest cache
    '.hypothesis',                 # property-test example database
    '.playwright-cli',             # browser probe page snapshots
    '.ruff_cache',                 # ruff linter cache
    '.venv',                       # local Python environment
    'venv',                        # alternate local Python environment name
    'htmlcov',                     # generated HTML coverage report
    'build',                       # setuptools build artifacts (regenerable)
    'dist',                        # wheel/sdist build output (regenerable)
    'tofu.egg-info',               # setuptools egg metadata (regenerable)
    '.git',                        # git history (may contain old secrets)
    '.migrate_backup',             # migration backups
    '.update_backup',              # self-update tarball backups (per-host safety net)
    'node_modules',                # (shouldn't exist, but just in case)
    'offline_pkgs',                # offline pip packages
    'sundries',                    # unrelated scripts/files (not part of tofu)
    'swebench_workdir',            # SWE-bench working directory (large, transient)
    'swebench_rerun_workdir',      # SWE-bench rerun results (thousands of venvs)
    'swebench_pilot_workdir',      # SWE-bench pilot working directory (transient)
    'swebench_glm_ab_workdir',     # SWE-bench GLM A/B working directory (transient)
    'swebench_full',               # SWE-bench full dataset working directory
    'abtest_workdir',              # A/B-test working directory (many conda envs + repos)
    '.eval-runs',                  # Hermetic evaluation artifacts (self-ignored too)
    'eval-runs',                   # Explicit in-repo TOFU_EVAL_ROOT fallback
    'evaluation_results',          # Upstream SWE-bench reports
    'sb-cli-reports',              # Hosted SWE-bench reports
    'jobs',                        # Harbor job artifacts
    'trials',                      # Harbor standalone-trial artifacts
    'overleaf_cache',              # Overleaf project cache (~89MB, regenerable)
    'niumark',                     # unrelated project (not part of tofu)
    'outputs',                     # smoke-test / eval output (thousands of files, transient)
    'output',                      # local browser/test screenshots (transient)
    'autoharness',                 # separate internal LongCat harness workspace
    'promo',                       # promotional slides/screenshots (~10MB, not runtime)
    'propaganda',                  # retired root-level README screenshot dump
    'django',                      # stray empty dir (not part of tofu)
    '.tofu_trash',                 # trash/undo bin (transient, per-host)
    '_candidates',                 # raw AI-gen asset candidates + proof strips (review-only, multi-MB)
}

ALWAYS_EXCLUDE_FILES = {
    'CLAUDE.md',
    # Legacy database file remnants (if any)
    'claude_dialogue.db.broken_20260324',
    'claude_dialogue.db.corrupt',
    'claude_dialogue.db.corrupt.bak',
    # Personal notes / scratch / research (moved to sundries/ but keep exclusion for safety)
    'rebuttal_2Bg4.md',
    'rebuttal_ruxi.md',
    'rebuttal_ruxi_fixed.md',
    'rebuttal_ysym.md',
    'rebuttal_ysym_fixed.md',
    'fix_openreview_latex.py',
    'split_long_image.py',
    'markdown-image-243521.png',
    'a.md',
    'o.md',
    'a.py',
    'output.md',
    'dump_errors.txt',
    'recover_errors.txt',
    'bench_1B.log',
    'bench_sgla',
    # Build artifacts
    'tofu-browser-extension.zip',
    'chatui-browser-extension.zip',  # legacy filename
    # Runtime state
    '.env',
    '.tofu_env.json',     # per-host conda env marker (absolute paths inside)
    'load_env.py',
    'copy_pid.txt',
    # NOTE: setup_bashrc.sh is intentionally NOT excluded here — it's kept for
    # personal + internal exports (proxy + conda auto-activate helper). It IS
    # stripped from opensource via OPENSOURCE_EXTRA_EXCLUDE_FILES (hardcoded
    # corp proxy IP must not ship publicly).
    'copy_to_dst.sh',     # per-host copy helper (hardcoded internal mount paths)
    'copy_to_chenxin.sh', # per-host copy helper (hardcoded internal mount paths)
    'server.log',
    'server.log.1',
    'prompt_output.txt',
    'nohup.out',
    # Scratch / personal artifacts
    'overleaf-mcp:cat_dog_manor.png',          # stray scratch image
    'claude_code_run_command_report.html',     # generated scratch report
    'thesis_references.bib',                   # personal academic file
    '解答260505.pdf',                          # scratch answer PDF (personal)
    '解答260505_v2.pdf',                       # scratch answer PDF (personal)
}

# Glob patterns always excluded
ALWAYS_EXCLUDE_GLOBS = {
    '*.pyc',
    '*.pyo',
    'node_modules*',  # npm deps AND timestamped local backups (node_modules.local_bak.*) — regenerable, not Tofu source
    '*.db',
    '*.db-journal',
    '*.db-wal',
    '*.db-shm',
    '*.db.tmp',
    '*.db.tmp-journal',
    '*.db.corrupted_*',
    'dump.sql',
    '*.log',
    '.coverage*',     # coverage DBs, including parallel-worker shards
    'coverage.xml',   # generated XML coverage report
    '*.bib',          # personal bibliography files
}

# Level 2: Additional exclusions for opensource only
OPENSOURCE_EXTRA_EXCLUDE_FILES = {

    # Provider-specific canvas acceptance stays with its internal adapter.
    'test_meituan_sandbox_submit_watch.py',
    # Security reports (now in docs/ — reveals attack surface)
    'SECURITY_AUDIT_REPORT.md',
    'RATE_LIMITING_DOS_AUDIT_REPORT.md',
    # Internal dev summaries / project intelligence (now in docs/)
    'BROWSER_ENHANCEMENTS_SUMMARY.md',
    'README_BROWSER_ENHANCEMENTS.md',
    'README_EXTENSION.md',
    # Maintainer-only settings panel fragment (cost A/B experiment —
    # lib/settings_panels.MAINTAINER_ONLY_TABS). The runtime strip in
    # lib/settings_panels.py hides the tab even if this file somehow ships;
    # excluding it here keeps the public tree free of the markup entirely.
    'experiments.html',
}

# Root-anchored FILE exclusions (the ALWAYS_EXCLUDE_ROOT_ONLY_DIRS twin for
# files). The export toolchain's own export.py must never ship, but a bare
# basename exclusion also stripped packages/tofu-overleaf-mcp's
# tofu_overleaf_mcp/export.py — a shipped product module whose absence broke
# 40 public-CI tests (the lib/paper drop-bug class, file edition).
ALWAYS_EXCLUDE_ROOT_ONLY_FILES = {
    'export.py',               # the export toolchain itself — ROOT only
}

OPENSOURCE_EXTRA_EXCLUDE_DIRS = {
    # 'scripts' moved to _TOP_LEVEL_ONLY_EXCLUDE_DIRS (nested
    # packages/tofu-db/scripts collision — see there).
    'benchmarks',               # benchmark scripts with internal API keys
    'outputs',                  # evaluation reports
    'posters',                  # personal scratch poster images (not part of the app)
    'debug',                    # standalone scratch/benchmark scripts (never imported by the
                                # app); 148 files that trip `ruff check .` in CI lint
    'bundles',                  # release-tiered content packs (docs/SITE_PACKS.md): company
                                # site packs, provider templates and intranet acceptance tests
                                # ship only in the internal/personal builds
}

# Files that live INSIDE an opensource-excluded dir but are still required by
# the public build (e.g. the desktop-installer CI calls
# ``python scripts/gen_desktop_icons.py``). The whole dir is excluded for
# safety, then these specific files are restored verbatim after the tar copy.
# Each entry MUST be a project-root-relative path that is verified clean of
# secrets/internal paths before being added here.
_OPENSOURCE_KEEP_FILES = {

    # Public contract generators and shipped tests require this exact helper
    # closure. Secret-scanned independently; no maintainer data or credentials.
    'benchmarks/conversation_provenance.py',

    'benchmarks/http_h2_credit.py',
    'benchmarks/native_application_root.py',
    'benchmarks/native_application_root_measure.py',
    'benchmarks/native_completion.py',
    'benchmarks/native_http.py',
    'benchmarks/native_http_cli.py',
    'benchmarks/native_sidecar_stages.py',
    'benchmarks/native_terminal_delivery.py',
    'benchmarks/repository_index/check_native_extraction.py',
    'benchmarks/repository_index/check_native_extraction_tool.py',
    'benchmarks/repository_index/domain_fixtures.py',
    'benchmarks/repository_index/native_discovery_measure.py',
    'benchmarks/repository_index/native_domain_check.py',
    'benchmarks/repository_index/native_domain_measure.py',
    'benchmarks/repository_index/native_extraction_scan.py',
    'benchmarks/repository_index/publication_port.py',
    'benchmarks/storage_write_pipeline.py',
    'benchmarks/tofudb_point_boundary_measure.py',
    'benchmarks/token_scratch.py',
    # Synthetic measurement/qualification tests import these portable helpers;
    # keep their transitive helpers, never measurements or runtime data.
    'benchmarks/harness_graph_controls.py',
    'benchmarks/knowledge_html/native_ingest_check.py',
    'benchmarks/knowledge_html/native_ingest_interrupt.py',
    'benchmarks/knowledge_html/native_ingest_measure.py',
    'benchmarks/knowledge_html/native_ingest_memory.py',
    'benchmarks/knowledge_html/native_ingest_provenance.py',
    'benchmarks/knowledge_html/native_ingest_worker.py',
    'benchmarks/live_pipeline.py',
    'benchmarks/native_agent_root.py',
    'benchmarks/native_agent_root_measure.py',
    'benchmarks/native_application_authority.py',
    'benchmarks/native_application_stages.py',
    'benchmarks/native_root_memory.py',
    'benchmarks/native_shared_library.py',
    'benchmarks/process_memory_counters.py',
    'benchmarks/process_vm_memory.py',
    'benchmarks/repository_index/child_memory.py',
    'benchmarks/repository_index/exit_memory.py',
    'benchmarks/repository_index/native_domain_worker.py',
    'benchmarks/repository_index/record_journal.py',
    'benchmarks/tofudb_cached_read.py',
    'benchmarks/tofudb_context_controls.py',
    'benchmarks/tofudb_context_read.py',
    'benchmarks/token_cold_start.py',
    # Portable diagnostics referenced by shipped tests; no captured data.
    'benchmarks/search_html_reference.py',
    'benchmarks/search_relevance.py',
    'benchmarks/tofudb_context_progression.py',
    'scripts/fetch_chatgpt_share.py',
    'scripts/inspect_native_crash.py',
    'scripts/trace_experiment_reads.py',
    'scripts/tofudb_profile_report.py',
    'scripts/check_i18n_dead_keys.mjs',
    'scripts/check_native_token_binding.py',
    'scripts/check_native_token_count_abi.py',
    'scripts/db_performance_catalog.py',
    'scripts/gen_execution_account_frontier.py',
    'scripts/gen_native_checkpoint_storage_abi.py',
    'scripts/gen_native_flow_abi.py',
    'scripts/gen_native_html_projection.py',
    'scripts/gen_native_html_projection_abi.py',
    'scripts/gen_native_loop_policy.py',
    'scripts/gen_native_token_count_abi.py',
    'scripts/gen_native_token_rank_tables.py',
    'scripts/gen_native_usage_aggregate.py',
    'scripts/gen_native_verdict_policy.py',
    'scripts/gen_project_continuity_contract.py',
    'scripts/gen_project_dispatch_contract.py',
    'scripts/gen_project_dispatch_storage.py',
    'scripts/gen_rust_chat_wire_oracle.py',
    'scripts/gen_rust_pricing_oracle.py',
    'scripts/gen_working_facts_contract.py',
    'scripts/json_schema_types.py',
    'scripts/python_with_glibc.py',
    'scripts/report_tool_search_outcomes.py',
    'scripts/tofudb_charter_assess.py',
    'benchmarks/conversation_acceptance.py',
    'benchmarks/conversation_authority.py',
    'benchmarks/conversation_compare.py',
    'benchmarks/conversation_concurrency.py',
    'benchmarks/conversation_concurrency_contract.json',
    'benchmarks/conversation_wake.py',
    'benchmarks/system_performance_contract.json',
    'benchmarks/evolution_long_horizon.py',
    'benchmarks/research_quality_compare.py',
    'debug/tool_call_success_report.py',
    # Shipped tests and build generators require these owned source dependencies.
    'benchmarks/http_cpu_clock.py',
    'benchmarks/http_domain_measure.py',
    'benchmarks/http_frame_coalescing.py',
    'benchmarks/http_frame_coalescing_lifecycle.py',
    'benchmarks/http_frame_coalescing_measure.py',
    'benchmarks/http_pressure_measure.py',
    'benchmarks/http_runtime.py',
    'benchmarks/http_stream_lifecycle.py',
    'benchmarks/knowledge_html/AGENTS.md',
    'benchmarks/knowledge_html/README.md',
    'benchmarks/knowledge_html/check.py',
    'benchmarks/knowledge_html/check_durable.py',
    'benchmarks/knowledge_html/check_ingest_domain.py',
    'benchmarks/knowledge_html/check_interruption.py',
    'benchmarks/knowledge_html/check_tokens.py',
    'benchmarks/knowledge_html/cold.py',
    'benchmarks/knowledge_html/domain_measure.py',
    'benchmarks/knowledge_html/durable.py',
    'benchmarks/knowledge_html/durable_measure.py',
    'benchmarks/knowledge_html/extractor.py',
    'benchmarks/knowledge_html/fixture_pdf.py',
    'benchmarks/knowledge_html/interruption.py',
    'benchmarks/knowledge_html/measure.py',
    'benchmarks/knowledge_html/native_profile_measure.py',
    'benchmarks/knowledge_html/reference.py',
    'benchmarks/knowledge_html/token_measure.py',
    'benchmarks/knowledge_html/token_projection.py',
    'benchmarks/knowledge_html/upload_phases.py',
    'benchmarks/knowledge_html/upload_phases_measure.py',
    'benchmarks/markdown_worker/AGENTS.md',
    'benchmarks/markdown_worker/README.md',
    'benchmarks/markdown_worker/browser_measure.py',
    'benchmarks/markdown_worker/check_inline_cache.mjs',
    'benchmarks/markdown_worker/client.js',
    'benchmarks/markdown_worker/domain_reader.js',
    'benchmarks/markdown_worker/domain_reader.py',
    'benchmarks/markdown_worker/inline_cache.js',
    'benchmarks/markdown_worker/inplace_measure.py',
    'benchmarks/markdown_worker/inplace_sanitizer.js',
    'benchmarks/markdown_worker/reader_input.js',
    'benchmarks/markdown_worker/reader_input.py',
    'benchmarks/markdown_worker/reader_measure.js',
    'benchmarks/markdown_worker/reader_measure.py',
    'benchmarks/markdown_worker/reader_memory_profile.py',
    'benchmarks/research_observation_contract.py',
    'benchmarks/system_performance.py',
    'scripts/build_native_runtime.py',
    'scripts/check_rust_model_account.py',
    'scripts/check_rust_token_ingress.py',
    'scripts/check_rust_usage_oracle.py',
    'scripts/check_tool_environment.py',
    'scripts/frontend_api_inventory.mjs',
    'scripts/gen_api_endpoint_index.py',
    'scripts/gen_native_checkpoint_control.py',
    'scripts/gen_packaged_demand_workers.py',
    'scripts/gen_project_coordination_contract.py',
    'scripts/gen_resource_policy.py',
    'scripts/gen_rust_anthropic_oracle.py',
    'scripts/gen_rust_execution_contract.py',
    'scripts/gen_rust_progress_oracle.py',
    'scripts/gen_rust_responses_oracle.py',
    'scripts/gen_rust_responses_request_oracle.py',
    'scripts/gen_task_event_delivery_contract.py',
    'scripts/generate_evolution_game_frames.py',
    'scripts/style_token_policy.mjs',
    'scripts/gen_desktop_icons.py',   # CI icon generation (PIL only, no secrets)
    # Brand asset generation is called by the shipped image executor and is
    # documented beside the role icon sources.
    'scripts/gen_tofu_icons.py',
    'scripts/png_to_svg.py',
    # The prebuilt engine install path: the public install.sh downloads a
    # verified per-platform engine archive instead of compiling on the user's
    # machine; the producer half publishes those archives from release CI.
    'scripts/install_tofudb_prebuilt.py',
    'scripts/build_tofudb_prebuilt.py',
    'scripts/build_python_wheels.py',  # Public release dependency bundle producer.
    # Evolution office sprite/props atlases are committed outputs of these
    # deterministic generators; the drift tests
    # (tests/evolution/test_evolution_props_generated.py et al.) re-run them in the
    # public suite, so the generators must ship.
    'scripts/generate_evolution_people.py',
    'scripts/generate_evolution_props.py',
    # The tofu-db differential perf/soak harnesses are imported by the
    # exported tests (tests/tofudb/test_tofudb_bench.py, test_tofudb_soak.py) —
    # stdlib+engine only, no credentials.
    'scripts/tofudb_bench.py',
    'scripts/tofudb_soak.py',
    # Vite is the sole frontend build graph. These scripts publish a complete
    # content-hashed graph atomically, verify packaged graphs without Node,
    # and keep the migrated runtime's private action map deterministic.
    'scripts/build_frontend.mjs',
    'scripts/check_frontend_tdz.mjs',
    'scripts/frontend_bundle_canary.py',
    'scripts/gen_api_v1_model_routing_contract.py',
    'scripts/gen_event_contract.py',
    'scripts/gen_agent_machine_tokens.py',
    'scripts/gen_artifact_review_contract.py',
    'scripts/compose_frontend_runtime.mjs',
    'scripts/compose_frontend_styles.mjs',

    'scripts/compose_settings_panels.mjs',
    'scripts/frontend_budget.py',
    'scripts/update_runtime_actions.mjs',
    'scripts/verify_frontend_dist.py',
    'scripts/install_frontend_dist.py',
    # The generated orchestration contracts are checked directly by the public
    # test suite, so their stdlib-only generators are part of the source graph.
    'scripts/gen_orchestration_authoring_metadata.py',
    'scripts/gen_orchestration_compatibility_defaults.py',
    'scripts/gen_orchestration_http_contract.py',
    'scripts/gen_orchestration_wire_formats.py',
    # Generates both backend schemas and the browser client from the canonical
    # conversation-sync OpenAPI document. A public checkout must be able to
    # verify and regenerate both sides of this wire contract.
    'scripts/gen_conversation_sync_contract.py',
    # OpenAPI 3.1 is the sole v4 authority. Public source and every client CI
    # lane must be able to regenerate and drift-check its boundary artifacts.
    'scripts/gen_api_v4_contract.py',
    # Locale contract generation/checking is part of every frontend build and
    # typecheck. Public source must be able to reproduce the shipped catalogs.
    'scripts/check_i18n_usage.mjs',
    'scripts/gen_i18n_contract.mjs',
    # Current-only docs and source-architecture hygiene are CI gates and must
    # remain runnable from a public checkout.
    'scripts/check_architecture.py',
    # Verification ratchets are wired into the public CI gates (coverage
    # floor, typing baseline, perf ratio, env-knob registry, v1 endpoint
    # index) — the public build runs the same gates, so it needs the same
    # stdlib-only scripts and their data files.
    'scripts/check_typing_baseline.py',
    'scripts/check_coverage.py',
    'scripts/check_perf_regression.py',
    'scripts/check_config_knobs.py',
    'benchmarks/perf_baseline.json',
    # Public release workflows execute these semantic artifact checks. They
    # contain no credentials and a clean exported checkout must retain them.
    'scripts/check_helm_render.py',
    'scripts/check_developer_runtime_artifacts.py',
    # Derives docs/contracts/TOOL_INVENTORY.md — one row per tool × its facets (label,
    # approval enricher, gates, dispatch path). CI pins it with --check
    # (tests/tools/test_tool_inventory_generated.py), so the public build needs the
    # generator. Introspective import of lib.tools.registry + tool_dispatch
    # tables -- no secrets, no internal paths.
    'scripts/gen_tool_inventory.py',

    # Pure-data result contract projection is required by clone-free wheels.
    'scripts/gen_tool_result_query_contract.py',
    # Required by the retained public frontend build graph.
    'scripts/check_conversation_renderer_boundaries.mjs',
    'scripts/check_note_presentation_parity.mjs',
    'scripts/gen_automation_contract.py',
    'scripts/gen_model_context_projection.py',
    'scripts/gen_system_note_presentation.mjs',
    'scripts/gen_tool_result_surfaces.py',
    # Tofu-DB contract generators + wire client. The public test suite runs
    # every generator with --check (tests/tofudb/test_tofudb_pre_authority.py) and
    # imports the wire client / comparison harness at module level
    # (tests/tofudb/test_tofudb_differential.py, test_tofudb_perf_compare.py,
    # test_tofudb_migration_import.py), so the public source graph needs
    # them. Stdlib-only; the token/secret strings inside are protocol
    # vocabulary, not credentials.
    'scripts/gen_storage_operation_catalog.py',
    'scripts/gen_storage_v2_contract.py',
    'scripts/gen_tofudb_ir_contract.py',
    'scripts/gen_tofudb_unicode_casefold.py',
    'scripts/gen_tofudb_unicode_simple_fold.py',
    'scripts/gen_tofusql_catalog.py',
    'scripts/tofudb_v2_client.py',
    'scripts/tofudb_perf_compare.py',
    # The ONE supported certified-engine upgrade path: installs the binary,
    # its <binary>.sha256 sidecar pin (verified by lib/storage/supervisor.py,
    # which ships in the public build) and build.json provenance atomically.
    'scripts/deploy_tofudb_engine.py',
    # Behavioral companion to the content pin: resolves the deployment engine
    # exactly like the app and replays the current contract probe set against
    # a disposable authority, catching semantic drift a hash pin cannot see.
    # Probes a temp-dir authority only; never opens the real data root.
    'scripts/check_tofudb_engine_drift.py',
    # Test-suite health census (AST-only scan of tests/, no imports/execution).
    # CI gates on it via tests/meta/test_suite_health_ratchet.py, so the public build
    # needs the tool too. Reads only tests/*.py + lib//routes source paths --
    # no secrets, no internal paths.
    'scripts/audit_tests.py',
    # Affected-test selection (P2-3, docs/TESTING_STRATEGY.md). Loaded by
    # tests/meta/test_test_select.py via spec_from_file_location and driven by the
    # Makefile test-affected target — the public tree ships both consumers, so
    # it must ship the tool. Pure stdlib AST/git subprocess -- no secrets, no
    # internal paths.
    'scripts/test_select.py',
    # Bundles native TS/ESM owners into isolated classic-script test views for
    # tests/_runtime_sections.py consumers (jsdom/node harnesses). The public
    # tree ships those tests, so it must ship the bundler they shell out to.
    # Node+esbuild already required by the frontend test toolchain.
    'scripts/vite_test_bundle.mjs',
    # Asset-retention gate consumed by the frontend build (build_frontend.mjs)
    # and tests/static_serving/test_bundle_concurrency.py — the public build ships both.
    'scripts/vite_asset_retention.mjs',
    # UI-nav targets generator + its freshness gate (npm generate:ui-nav /
    # check:ui-nav); the public tree ships the contract and the consumers.
    'scripts/gen_ui_nav_targets.mjs',
    # SQLite is the default authority. These dry-run-first Sidecar/offline
    # migration, certification, and control tools are the supported upgrade
    # and recovery surface without application backend discovery.
    'scripts/migrate_pg_to_sqlite.py',
    'scripts/migrate_sqlite_to_postgres.py',
    'scripts/activate_sqlite_cutover.py',
    'scripts/storagectl.py',
    'scripts/storage_certify.py',
    # Offline authority-bloat reclamation (--analyze read-only; --offline
    # verify-gated VACUUM INTO with pre-image retention). Part of the same
    # supported maintenance surface as storagectl/storage_certify; operators
    # upgrading from a bloated authority need it in the public tree. Pure
    # stdlib + sqlite3, no secrets, no internal paths.
    'scripts/storage_deep_clean.py',
    # Rootless VM documentation promises a reproducible pinned bootstrap and
    # benchmark. The bridge source and lock file are inputs, not build output.
    'scripts/bootstrap_rootless_qemu.sh',
    'scripts/rootless_egress_bridge.c',
    'scripts/rootless_qemu-conda-linux-64.lock',
    # Public CI/build consumers: monorepo boundary check (publish workflow +
    # Makefile), style-token gate (npm check:styles), wire-fixture and
    # paper-build contract freshness (make contracts-check / npm
    # check:paper-build), and the concept-index / research-progress /
    # cert-binding generators whose generated artifacts ship tracked.
    'scripts/check_monorepo.py',
    'scripts/check_style_tokens.mjs',
    'scripts/gen_wire_fixtures.py',
    'scripts/generate_paper_build_contract.py',
    'scripts/gen_concept_index.py',
    'scripts/gen_tofudb_cert_binding.py',
    'scripts/generate_research_progress.py',
    # Storage graph modules imported by shipped runtime code
    # (lib/storage/v2_client.py, routes/api_v4/bootstrap.py) and by public
    # contract tests; generated counterparts regenerate via
    # gen_storage_operation_catalog.py / gen_storage_v2_contract.py.
    'scripts/generated_storage_operations.py',
    'scripts/generated_storage_v2.py',
    # Public test consumers: migration gate CLI (tests/tofudb/test_tofudb_migration*),
    # backend certification (test_storage_certification_gate.py), function-size
    # ratchet (test_function_size_ratchet.py), model-authority and storage-log
    # read-only audits (test_model_metadata_authority.py,
    # test_storage_log_audit.py), checkout-relocation tool
    # (test_checkout_path_migration.py), and the release checksum signer used
    # by the desktop publish workflow (test_release_sign.py).
    'scripts/tofudb_migrate.py',
    'scripts/certify_storage_backends.py',
    'scripts/check_function_sizes.py',
    'scripts/audit_model_configuration.py',
    'scripts/audit_storage_logs.py',
    'scripts/migrate_checkout_path.py',
    'scripts/release_sign.py',
    'scripts/rootless_terminal_bench_21.py',
    # Public install/runtime entry points consume these helpers directly.
    'scripts/vendor_mcp.sh',
    'scripts/verify_pdf_stack.py',
    'scripts/ptc_live_smoke.py',
    # Reproducible public Tofu-vs-Codex context-efficiency experiment. The
    # benchmark root is excluded by default because historical scripts embed
    # private paths and credentials; this closed set is path-relative, uses a
    # frozen public SWE-bench dataset/price card, and is required by the public
    # optimization documentation. Run artifacts remain ignored separately.
    'benchmarks/context_efficiency/README.md',
    'benchmarks/context_efficiency/__init__.py',
    'benchmarks/context_efficiency/analyze.py',
    'benchmarks/context_efficiency/cli.py',
    'benchmarks/context_efficiency/dataset.py',
    'benchmarks/context_efficiency/runtime.py',
    'benchmarks/context_efficiency_bench.py',
    'benchmarks/context_efficiency_manifest.json',
    'benchmarks/task_replay_bench.py',
    # Builds the extension upload zip (dev or --store trimmed manifest). The
    # docs/chrome-web-store/ kit SHIPS in the opensource export and its
    # checklist step 2 is "bash scripts/package_extension.sh --store", so
    # without this the public tree documents a command it does not contain.
    # Pure REPO_ROOT-relative bash + zip -- no secrets, no internal paths.
    'scripts/package_extension.sh',
    # Server-side installer entry point. Same shape as the packaging script:
    # a documented command a user is told to run, so it must exist in both a
    # clean clone and the public tree. Pure bash -- no secrets, no internal
    # paths, no absolute host paths.
    'scripts/install_on_server.sh',
    # The single source of truth for "is a Tofu release complete?". Both gates
    # in .github/workflows/build-desktop.yml call it, and that workflow ships
    # in the opensource export — so without this the public tree would carry a
    # release pipeline that dies on a missing file the first time it runs.
    # Pure stdlib (argparse/fnmatch/json/pathlib), no secrets, no internal paths.
    'scripts/release_assets.py',
    # The single source of truth for "is this VERSION documented?". The version
    # gate in that same workflow calls it before any build, so it ships for the
    # same reason as release_assets.py above: the public tree would otherwise
    # carry a release pipeline that dies on a missing file the first time it
    # runs. Pure stdlib (argparse/re/pathlib), no secrets, no internal paths.
    'scripts/changelog_gate.py',
    # The CI wizard pixel probe (windows runner: build the TOFU_DIAG
    # installer + PrintWindow frames + BM_CLICK page turns). The shipped
    # .github/workflows/installer-ui-probe.yml drives it — without this the
    # public tree carries a workflow that dies on a missing file. Pure
    # stdlib + PIL ctypes, no secrets, no internal paths.
    'debug/win_ci_shot.py',
    # Canonical read-only operator diagnostic behind
    # ``serverctl.py inspect-conversation``. It is stdlib-only, contains no
    # environment-specific paths, and must survive the otherwise wholesale
    # debug/ exclusion in source releases.
    'debug/inspect_conversation.py',
    # Vite runtime-graph CI checks wired into public workflows/typecheck
    # lanes. Pure static analysis, no credentials; an exported checkout must
    # keep them runnable like the sibling checks above.
    'scripts/check_lazy_runtime_bindings.mjs',
    'scripts/check_runtime_reachability.mjs',
    'scripts/runtime_action_analysis.mjs',
    # Nested repository guides mandated by root AGENTS.md ("add or revise a
    # child guide when a directory gains a distinct owner"). They document
    # owner/source-of-truth boundaries for contributors and carry no
    # secrets; strip their directories but keep the guides.
    'benchmarks/AGENTS.md',
    'benchmarks/desktop_runtime.py',
    'debug/AGENTS.md',
    'scripts/AGENTS.md',
}

OPENSOURCE_EXTRA_EXCLUDE_FILES = OPENSOURCE_EXTRA_EXCLUDE_FILES | {
    'meituan.json',             # internal provider template with corp gateway URL
    'setup_bashrc.sh',          # per-host .bashrc helper (hardcoded corp proxy IP)
    'JOURNAL.md',               # internal evolution journal: ops incidents, internal
                                # hosts/usernames/conversation ids. CHANGELOG.md ships
                                # for public history (same contract as CLAUDE.md).
    # Unreferenced marketing assets in static/images/ (~12.4MB total) — the
    # runtime UI uses static/icons/ (onigiri.svg etc.); NOTHING references
    # these (verified 2026-08-01: zero project-wide hits for the filenames /
    # 'images/<name>' paths). Dead weight in every source tarball and clone.
    'tofu-cache-article-cover.png',
    'tofu-cache-article-cover-zh.png',
    'tofu-poster-core-strength.png',
    'tofu-poster-v2.png',
    'attach-icon.png',
    'attach-icon.svg',
    'onigiri-icon.png',
    'onigiri-icon.svg',
}


# ═══════════════════════════════════════════════════════════
#  Source code sanitization patterns (opensource only)
# ═══════════════════════════════════════════════════════════

# Hardcoded secrets to redact
_SECRETS = {
    # API keys
    '2003386484270948427':      'YOUR_API_KEY_HERE',
    '2031327690221944861':      'YOUR_API_KEY_HERE',
    # Feishu credentials
    'q2E1KDjuDhasILLK0urXCgqbss6vhmXf': '',
    'cli_a924e60a7cb85bcc':     '',
    # FlyAI bundled trial credential (travel_flyai.py) — public npm trial,
    # but the opensource tree intentionally ships it blank (see origin/main).
    'sk-faRn8Kp2QzXvLm9YtA4EjHcWbS7oUdG5iF3xNqV6rZ': '',
    'XSbdYnucPARDc9knhD8+X6hxdD1Nh6ZGI6Hadg25kBw=': '',
}

# Internal endpoints to replace
_ENDPOINTS = {
    'https://aigc.sankuai.com/v1/openai/native': 'https://api.openai.com/v1',
    'https://aigc.sankuai.com':                   'https://api.openai.com',
}

# Internal domain patterns (for proxy bypass code)
_INTERNAL_DOMAIN_LITERALS = [
    '.sankuai.com',
    '.meituan.com',
]

# Plain-substring internal identifiers scrubbed verbatim in opensource exports,
# each paired with its opensource-safe replacement. SINGLE SOURCE OF TRUTH: both
# the scrub (``_sanitize_source_opensource``) and the file-open trigger list
# (``_opensource_sanitize_triggers``) are driven from this tuple, so they can
# never drift. A drift would be a silent leak — a file whose secret IS scrubbed
# but whose trigger substring is missing never gets OPENED, so the scrub rule
# never runs. Add a new internal identifier HERE and it is both scrubbed and
# triggered automatically. (Endpoints/domains/paths handled by regex live in
# ``_ENDPOINTS`` / ``_INTERNAL_DOMAIN_LITERALS`` / the path regexes below.)
_INTERNAL_IDENTIFIER_REPLACEMENTS = (
    ('hadoop-aipnlp',              'your-user'),
    ('ruanjunhao04',              'your-username'),
    ('M-TransferContext-INF-CELL', 'X-Custom-Header'),
    ('gray-release-ai-gpt-test',  'your-header-value'),
)


def _have_tool(name: str) -> bool:
    """Return True if `name` is on PATH (cheap check, used by rg/fd fast paths)."""
    return shutil.which(name) is not None


def _fast_count_files(root: Path) -> int:
    """Count regular files under ``root``. Uses Rust ``fd`` if available,
    else falls back to Python ``rglob``.

    On FUSE/DolphinFS, ``Path.rglob('*')`` is dramatically slower than fd
    (which uses parallel directory walking via the Rust ``ignore`` crate).
    For a tree with ~7k files we've measured 10-20× speedups.
    """
    if _have_tool('fd'):
        try:
            r = subprocess.run(
                ['fd', '--type', 'f', '--hidden', '--no-ignore', '.', str(root)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                return r.stdout.count('\n')
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug('fd count failed, falling back to rglob: %s', e)
    try:
        return sum(1 for _ in root.rglob('*') if _.is_file())
    except OSError as e:
        logger.debug('File count failed (non-fatal): %s', e)
        return 0


# ═══════════════════════════════════════════════════════════
#  Targeted post-copy sanitization (rg-driven)
# ═══════════════════════════════════════════════════════════
# After bulk-copying with tar (which preserves bytes 1:1), we walk over
# ONLY the files that contain something the sanitizer would touch.  This
# replaces a full ``os.walk`` + ``read_text`` + ``write_text`` over every
# text file in the tree — a huge win on FUSE since we avoid hammering the
# filesystem with thousands of pointless reads.
#
# The trigger list is the union of literal patterns that any sanitization
# rule looks for. As long as a file does NOT contain any of these
# substrings, the sanitize functions return unchanged content — so it's
# safe to skip.  rg picks up the matches in one parallel scan.

# Trigger substrings for `_sanitize_defaults_for_export` (internal mode).
# These are tiny — just enough to identify files that would be modified.
_INTERNAL_SANITIZE_TRIGGERS = [
    r'\?v=\d{8}',                                          # cache busters
    r"const _ICON_V = ",                                   # settings.js icon ver
    r"config\.defaultThinkingDepth = 'medium'",            # core.js default
    r'let serverModel = "aws\.claude-opus-4\.7"',          # core.js model
    r"_default_depth = cfg\.get\('defaultThinkingDepth', 'medium'\)",  # model_config.py
    r'<option value="medium"',                             # index.html medium option
    r'<option value="off"',                                # index.html off option
]

# Additional triggers for opensource mode (`_sanitize_source_opensource`).
# Anything in _SECRETS / _ENDPOINTS / _INTERNAL_DOMAIN_LITERALS forces
# the file open. We escape each literal for use in a regex alternation.
_RG_MAX_FILESIZE = '5M'
_RG_MAX_FILESIZE_BYTES = 5 * 1024 * 1024


# that imports static assets (currently just index.html). We build the
# candidate set directly and avoid a full-tree search.

_INTERNAL_FIXED_CANDIDATES = (
    'frontend/src/runtime/app-runtime.js',
    'lib/tasks_pkg/model_config.py',
    'index.html',
)


# Files the opensource sanitize pass MUST open even though they contain no
# secret/endpoint trigger substring: each carries a filepath-gated
# ``_OPENSOURCE_BUILD`` flag bake (rules 14 / 14b in
# ``_sanitize_source_opensource``). Without this list the rg trigger scan
# would skip them and the exported tree would silently ship with the runtime
# opensource filters defaulting to OFF. (registry.py currently matches a
# trigger only incidentally — an internal username in a comment; listing it
# here explicitly closes that drift class.)
_OPENSOURCE_FIXED_CANDIDATES = (
    'frontend/src/runtime/sections/main/main_input_handling.js',
    'frontend/src/runtime/sections/_epilogue.js',
    'lib/mcp/registry.py',
    'lib/settings_panels.py',
)



# Keys from server_config.json that are safe to share internally
# (API keys, endpoints, proxy, models — all company-shared resources)
_INTERNAL_CONFIG_KEYS = {
    'providers',             # API keys + endpoints (the whole point)
    'models',                # default model assignments
    'presets',               # quality-of-life model presets
    'search',                # search/fetch settings
    'proxy_config',          # internal proxy (needed for API access)
    'proxy_bypass_domains',  # proxy bypass list
    'model_defaults',        # fallback model config
    'hidden_models',         # UI cleanup
    'hidden_ig_models',      # UI cleanup
}

# Keys explicitly excluded from internal sharing:
#   feishu         — personal app_id, workspace paths, allowed_users
#   model_limits   — learned from personal usage patterns
#   mcp_servers    — stored separately, contains personal tokens (e.g. GitHub PAT)


# API keys that must never be seeded into internal exports — these are the
# exporter's own personal keys, not shared team credentials.
_INTERNAL_EXCLUDED_API_KEYS = frozenset({
    '22080658572602085409',
})


# ``opensource`` mode never touches install.sh — its install.sh ships
# stock for the public.

_INSTALL_SH_PROXY_BEGIN = '# ── BEGIN tofu-export injected proxy ──'
_INSTALL_SH_PROXY_END = '# ── END tofu-export injected proxy ──'


# install — preserved even by the opensource mirror cleanup. Everything else
# the export excludes is CONTENT (promo/, overleaf_cache/, workdirs, caches):
# content must not linger in a published mirror.
_OPENSOURCE_DEST_PRESERVE = {
    '.git',          # VCS metadata — required by the push flow itself
    'data',          # chat DB / configs (a dest doubling as a live install)
    'uploads',       # user uploads
    '.tofu',         # memories / skills / file-history (user data)
    '.chatui',       # legacy agent state (user data)
    '.codex',        # Codex CLI per-machine skills/state (user data)
    'pgdata',        # PostgreSQL cluster (chat history)
    'pg_backups',    # SQL dumps (chat history)
    'logs',          # runtime diagnostics
}

# Per-host files that must NEVER survive into a re-exported dest, even though
# they're not in the source tree (so the source-name check would otherwise
# skip them). .tofu_env.json pins an absolute interpreter path; if a stale
# one from another host/user lingers, server.py re-execs into the WRONG conda
# env (missing deps → ModuleNotFoundError). It's in ALWAYS_EXCLUDE_FILES so
# it's never copied — the cleanup actively strips a pre-existing one.
# A formerly exported toolchain must not survive internal re-publication.
# Personal backups copy a fresh toolchain after removing the stale directory.
_FORCE_STRIP_FROM_DEST = {'.tofu_env.json', 'export.py', 'export_pkg'}




_PUBLISH_MAX_FILE_BYTES = 50 * 1024 * 1024
_PUBLISH_MAX_TREE_BYTES = 500 * 1024 * 1024
_PUBLISH_FORBIDDEN_ROOTS = {
    'data', 'logs', 'uploads', 'outputs', 'overleaf_cache', 'pgdata',
    'pg_backups', '.chatui', '.tofu', '.tofu_trash', '.project_sessions',
    '.eval-runs', 'eval-runs', 'evaluation_results', 'sb-cli-reports',
    'jobs', 'trials',
}
_PUBLISH_FORBIDDEN_NAMES = {
    '.env', '.tofu_env.json', '.credentials_vault.key',
    'credentials_vault.json', 'server_config.json', 'mcp_servers.json',
}
# Maintained PostgreSQL extension installation source; database dumps remain forbidden.
_PUBLISH_SOURCE_SQL_FILES = frozenset({
    'lib/storage_authority_sql/native_worker_clock/tofu_worker_clock--1.0.sql',
})

_PUBLISH_FORBIDDEN_SUFFIXES = {
    '.db', '.sqlite', '.sqlite3', '.sql', '.log', '.pem', '.key',
    '.p12', '.pfx', '.bak',
}
_PUBLISH_PLACEHOLDERS = {
    'data/.gitkeep', 'data/config/.gitkeep', 'logs/.gitkeep',
    'uploads/images/.gitkeep', 'uploads/images/.gitignore',
}
