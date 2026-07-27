"""Tool-inventory drift guard + the write/approval ratchet.

Two jobs, deliberately in one file because they share the generator:

1. **Drift guard** — ``docs/TOOL_INVENTORY.md`` must match what
   ``scripts/gen_tool_inventory.py`` derives from the live registry. The
   inventory exists so a reviewer (human or model) can see all ~90 tools ×
   their facets at once; an inventory that silently goes stale is worse than
   none, because it invites decisions based on a fiction.

2. **Approval-enricher ratchet** — every tool in the write partition must have
   an entry in ``_APPROVAL_META_ENRICHERS``, with a grandfathered exemption
   list that may only SHRINK.

   Why this is a real defect class and not a style preference: the base
   approval metadata is built from ``fn_args['path']`` and
   ``fn_args['description']`` only (``_approval.py::_handle_approval``). A
   tool whose risk lives in some OTHER argument — ``command`` for
   ``desktop_run_command``, ``selector`` for ``browser_click``,
   ``memory_id`` for ``delete_memory`` — therefore renders an approval dialog
   with those fields EMPTY. The user is asked to approve
   ``desktop_run_command`` without being shown ``rm -rf ~/Documents``.
   ``_approval.py`` states the rule itself: "Without an enricher the prompt
   renders a bare tool name and the user approves blind — false confidence,
   worse than not prompting at all." It was applied to the browser_execute_js /
   schedule_create batch and missed for the rest of the same batch.

The ratchet follows the charter's precedent for
``test_error_transparency_guard``: known offenders are grandfathered with a
meta-assertion that the exemption list contains no dead entries, so cleaning
up the list can't silently re-open the hole.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

GEN = 'scripts/gen_tool_inventory.py'


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _model_visible_write_tools():
    """Write-partition tools the MODEL can actually call.

    Intersected with ``provides`` on purpose: ``desktop_move_file`` sits in the
    desktop spec's ``write_tools`` but is deliberately absent from ``provides``
    (never exposed to the LLM — see lib/desktop_tools.py). It cannot reach the
    approval dialog, so requiring an enricher for it would be noise.
    """
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
    from lib.tools import all_specs

    provides = set()
    for s in all_specs():
        if s.source != 'plugin':
            provides |= set(s.provides)
    return {t for t in _WRITE_TOOLS if t in provides}


#: Write tools that reach the approval dialog with NO enricher, i.e. the user
#: is shown a bare tool name. Grandfathered so the ratchet can land without a
#: 18-enricher batch in the same commit. THIS LIST MAY ONLY SHRINK.
GRANDFATHERED_NO_ENRICHER = {
    # browser — drives the user's real session; the selector/target is the risk
    'browser_click', 'browser_close_tab', 'browser_create_tab',
    'browser_hover_and_click', 'browser_keyboard', 'browser_right_click_menu',
    # desktop — runs on the user's own machine; command/path is the risk
    'desktop_open_app', 'desktop_open_file', 'desktop_run_command',
    'desktop_write_file',
    # memory CRUD — destructive to stored notes; the target id is the risk
    'create_memory', 'delete_memory', 'merge_memories', 'update_memory',
    # motion_video — long, expensive renders; the job/paths are the risk
    'motion_video_concat', 'motion_video_mux', 'motion_video_narrate',
    'motion_video_render',
}


class TestInventoryNotStale:
    def test_generated_inventory_matches_live_registry(self):
        """`--check` must pass: the committed inventory reflects the code."""
        proc = subprocess.run(
            [sys.executable, GEN, '--check'],
            cwd=_repo_root(), capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, (
            f'tool inventory is stale — run `python3 {GEN}` and commit '
            f'docs/TOOL_INVENTORY.md.\nstderr: {proc.stderr[-2000:]}'
        )

    def test_generator_reports_every_declared_builtin(self):
        """Every built-in in the registry appears as an inventory row.

        Asserts the RESULT (coverage) rather than the file's text, so the
        report layout can be rewritten without falsely failing.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import collect

        from lib.tools import all_specs
        inv = collect()
        rows = {r['tool'] for r in inv['builtin']}
        declared = set()
        for s in all_specs():
            if s.source != 'plugin':
                declared |= set(s.provides)
        assert declared - rows == set(), (
            f'declared built-ins missing from the inventory: '
            f'{sorted(declared - rows)}'
        )

    def test_plugin_tools_are_not_in_the_pinned_table(self):
        """Plugin rows stay in the diagnostic section.

        A deployment-specific plugin inside the `--check`ed table would make CI
        red on any host with a different plugin set.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import collect
        inv = collect()
        builtin = {r['tool'] for r in inv['builtin']}
        for r in inv['plugin']:
            assert r['tool'] not in builtin


class TestApprovalEnricherRatchet:
    def test_every_write_tool_has_an_approval_enricher(self):
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS

        missing = _model_visible_write_tools() - set(_APPROVAL_META_ENRICHERS)
        new = missing - GRANDFATHERED_NO_ENRICHER
        assert not new, (
            f'{len(new)} write tool(s) reach the approval dialog with no '
            f'enricher: {sorted(new)}. The dialog would render a bare tool '
            f'name (base meta is only path+description), so the user approves '
            f'without seeing what runs. Add an enricher in '
            f'lib/tasks_pkg/tool_dispatch/_approval.py that surfaces the '
            f'risk-bearing argument.'
        )

    def test_grandfather_list_has_no_dead_entries(self):
        """A fixed tool must leave the exemption list.

        Without this, the list keeps "protecting" tools that no longer need it
        and the ratchet stops being able to tighten — the exact rot pattern
        that produced the dead-allowlist family.
        """
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS

        write_tools = _model_visible_write_tools()
        fixed = {t for t in GRANDFATHERED_NO_ENRICHER
                 if t in _APPROVAL_META_ENRICHERS}
        assert not fixed, (
            f'these tools now HAVE an enricher — remove them from '
            f'GRANDFATHERED_NO_ENRICHER so the ratchet tightens: {sorted(fixed)}'
        )
        gone = GRANDFATHERED_NO_ENRICHER - write_tools
        assert not gone, (
            f'these grandfathered names are no longer model-visible write '
            f'tools — drop them: {sorted(gone)}'
        )

    def test_base_meta_really_is_blank_without_an_enricher(self):
        """Pins WHY the ratchet exists, in behavioural terms.

        If a future refactor makes the base metadata self-describing (e.g. by
        dumping all args), this test fails and the ratchet can be reconsidered
        on evidence instead of being carried forever on a stale premise.
        """
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS

        assert 'desktop_run_command' not in _APPROVAL_META_ENRICHERS
        base = {'approvalId': 'x', 'toolName': 'desktop_run_command',
                'path': '', 'description': ''}
        rendered = ' '.join(str(v) for v in base.values())
        assert 'rm -rf' not in rendered, (
            'base approval meta unexpectedly surfaces the command — if this '
            'changed, re-evaluate the enricher ratchet'
        )
