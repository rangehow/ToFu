"""Context generation for project co-pilot.

The LLM relies entirely on tool-based exploration (grep_search, find_files,
list_dir, read_files) to understand project structure at runtime.

This module provides ``get_context_for_prompt()`` which assembles the
SYSTEM-LEVEL context block: the project header, multi-root workspace topology,
and any project intelligence file (CLAUDE.md / .cursorrules / AGENTS.md /
COPILOT.md) that lives in the workspace.

It does NOT enumerate per-tool descriptions — each tool's own usage prose now
lives in its API-level ``description`` field (see ``lib/tools/*.py``), which
the model receives as part of the standard ``tools: [...]`` parameter on every
request.  Cross-cutting routing meta lives in
``lib.tasks_pkg.system_prompt_cc.section_using_tools``.

This split mirrors Claude Code's architecture (per-tool ``prompt()`` methods +
small ``getUsingYourToolsSection`` cross-cutting policy) and avoids duplicating
tool docs in the cache-sensitive system prefix.
"""
import os

from lib.log import get_logger
from lib.project_mod.config import (
    _lock,
    _roots,
    _state,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Context for Chat
# ═══════════════════════════════════════════════════════

def get_context_for_prompt(base_path=None):
    """Build the system-prompt project-context block for a session.

    Contains only *system-level* context — the project header, multi-root
    topology, and auto-detected project intelligence files.  Per-tool usage
    prose lives in each tool's own ``description`` field (see
    ``lib/tools/*.py``); cross-cutting routing meta lives in
    ``lib.tasks_pkg.system_prompt_cc.section_using_tools``.
    """
    with _lock:
        path = base_path or _state['path']
        # Collect extra roots for multi-root workspace
        extra_roots = {}
        _roots_snapshot = {}
        for rn, rs in _roots.items():
            _roots_snapshot[rn] = rs.copy()
            if rs['path'] != path:
                extra_roots[rn] = rs.copy()
    if not path:
        return None

    logger.debug('[Context] Building prompt for path=%s, extra_roots=%s',
                 path, list(extra_roots.keys()) if extra_roots else '[]')

    ctx = (f"[PROJECT CO-PILOT MODE]\n"
           f"Project: {path}\n\n")

    # ★ Cross-DC warning — let the LLM know about latency constraints
    try:
        from lib.cross_dc import get_latency_class, get_timeout_multiplier
        lat_class = get_latency_class(path)
        if lat_class in ('slow', 'very_slow'):
            multiplier = get_timeout_multiplier(path)
            ctx += (
                f"⚠️ CROSS-DATACENTER PROJECT — This project is on a remote DolphinFS cluster.\n"
                f"File I/O latency is {lat_class.replace('_', ' ')} (~{multiplier:.0f}x normal).\n"
                f"Timeouts are auto-adjusted but operations may still be slow.\n"
                f"Optimize by: batching reads, using targeted grep paths, avoiding deep tree walks.\n\n"
            )
    except Exception as e:
        logger.debug('[Indexer] cross_dc info unavailable: %s', e)

    # ═══════════════════════════════════════════════════════
    #  Multi-Root: append extra workspace roots
    # ═══════════════════════════════════════════════════════
    if extra_roots:
        primary_name = None
        for _rn, _rs in _roots_snapshot.items():
            if _rs.get('path') == path:
                primary_name = _rn
                break
        primary_name = primary_name or os.path.basename(path)

        ctx += f"\n{'='*50}\n"
        ctx += f"⚠️ MULTI-ROOT WORKSPACE — {1 + len(extra_roots)} roots active\n"
        ctx += f"{'='*50}\n"
        ctx += (
            f"MANDATORY: You MUST use 'rootname:path' prefix for ALL file operations\n"
            f"targeting non-primary roots. This applies to BOTH reading AND writing\n"
            f"(including creating new files). Without the prefix, paths resolve under\n"
            f"the PRIMARY root ({primary_name}).\n\n"
            f"⚠️ CREATING NEW FILES: When creating a new file in a non-primary root,\n"
            f"you MUST use the rootname prefix. There is no auto-detection for new files\n"
            f"(the file doesn't exist yet to check). Forgetting the prefix will create\n"
            f"the file in the wrong project.\n\n"
            f"Root prefix table:\n"
            f"  {primary_name}: → {path} (PRIMARY — default when no prefix)\n"
        )
        for rn, rs in extra_roots.items():
            ctx += f"  {rn}: → {rs['path']}\n"
        ctx += (
            f"\nExamples:\n"
            f"  read_files([{{path: '{primary_name}:src/main.py'}}])   — read from primary\n"
        )
        first_extra = next(iter(extra_roots))
        ctx += (
            f"  read_files([{{path: '{first_extra}:src/main.py'}}])   — read from extra root\n"
            f"  write_file(path='{first_extra}:config.yaml', ...)     — write to extra root\n"
            f"  write_file(path='{first_extra}:src/new_file.py', ...) — CREATE in extra root\n"
            f"  run_command(command='npm test', working_dir='{first_extra}:')  — run in extra root\n"
            f"  grep_search(pattern='TODO', path='{first_extra}:src') — search in extra root\n"
            f"  apply_diff(edits=[{{path: '{first_extra}:file.py', ...}}])  — batch edit in extra root\n"
        )
        ctx += "\n"
        for rn, rs in extra_roots.items():
            ctx += f"[{rn}] {rs['path']}\n\n"

    # ═══════════════════════════════════════════════════════
    #  CLAUDE.md / Project Intelligence auto-detection
    # ═══════════════════════════════════════════════════════
    _INTELLIGENCE_FILES = ['CLAUDE.md', '.cursorrules', 'AGENTS.md', 'COPILOT.md']
    for intel_name in _INTELLIGENCE_FILES:
        intel_path = os.path.join(path, intel_name)
        if os.path.isfile(intel_path):
            try:
                with open(intel_path, encoding='utf-8', errors='replace') as f:
                    intel_content = f.read(32_000)
                if intel_content.strip():
                    ctx += (f"\n{'='*50}\n"
                            f"📋 Project Intelligence — {intel_name}\n"
                            f"{'='*50}\n"
                            f"(Auto-detected from {intel_path})\n"
                            f"⚠️ MANDATORY: All code changes in this project MUST comply with the rules below.\n\n"
                            f"{intel_content.strip()}\n")
                    logger.info('[Context] Injected project intelligence file: %s (%d chars)',
                                intel_path, len(intel_content))
            except OSError as e:
                logger.warning('[Context] Failed to read project intelligence file %s: %s',
                               intel_path, e)

    return ctx
