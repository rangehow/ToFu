"""Retained scanner: only a newly settled live Overleaf edit dispatches.

Pins frontend/src/runtime/sections/overleaf-preview-hook.js in jsdom: the
renderToolRoundsHTML seam hands every painted round to the scanner, and
exactly the done/successful mcp__overleaf__ edit rounds during a live stream
reach the preview owner — deduped by toolCallId, with the SyncTeX needle
taken from the edit's new content (or section title / rewrite head).

Local workspace .tex edits (write_file / apply_diff / edit_file batch)
dispatch the same way with source='local', tolerate foreign-harness arg
aliases (file_path / new_string), and skip non-TeX, refused and unsettled
rounds.
"""

from __future__ import annotations

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HOOK_JS = runtime_section_path('overleaf-preview-hook.js')

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2]],
  globals: { runtimeScope: {}, console },
});

try {
  const dispatched = [];
  runtimeScope._overleafPreviewRounds = (cands) => dispatched.push(cands);
  const pid = 'b'.repeat(24);
  const round = (over) => Object.assign({
    toolName: 'mcp__overleaf__edit_file',
    toolCallId: 'call-' + Math.random().toString(36).slice(2),
    status: 'done',
    toolArgs: { project_id: pid, file_path: 'main.tex',
                new_string: '\\section{New}' },
  }, over || {});

  _overleafPreviewObserveRounds([round()], true);
  check('done_live_edit_dispatches_with_needle',
    dispatched.length === 1
      && dispatched[0][0].projectId === pid
      && dispatched[0][0].file === 'main.tex'
      && dispatched[0][0].needle === '\\section{New}');

  const dup = round({ toolCallId: 'call-dup' });
  _overleafPreviewObserveRounds([dup], true);
  _overleafPreviewObserveRounds([dup], true);
  check('tool_call_id_dedupes_across_paints', dispatched.length === 2);

  _overleafPreviewObserveRounds([round()], false);
  check('history_paint_never_compiles', dispatched.length === 2);

  _overleafPreviewObserveRounds([
    round({ status: 'running' }),
    round({ _rejected: true }),
    round({ toolName: 'mcp__overleaf__read_file' }),
    round({ toolArgs: { project_id: 'not-hex', file_path: 'x',
                        new_string: 'y' } }),
    round({ toolArgs: '{broken json' }),
  ], true);
  check('unsettled_rejected_nonedit_badargs_all_skipped',
    dispatched.length === 2);

  _overleafPreviewObserveRounds([round({
    toolName: 'mcp__overleaf__update_section',
    toolArgs: JSON.stringify({ project_id: pid, file_path: 'main.tex',
                              section_title: 'Compaction ablation',
                              new_content: 'body' }),
  })], true);
  check('update_section_needle_is_title_args_may_be_json_string',
    dispatched.length === 3
      && dispatched[2][0].needle === 'Compaction ablation');

  _overleafPreviewObserveRounds([round({
    toolName: 'mcp__overleaf__rewrite_file',
    toolArgs: { project_id: pid, file_path: 'main.tex',
                content: 'x'.repeat(5000) },
  })], true);
  check('rewrite_needle_bounded_to_content_head',
    dispatched.length === 4
      && dispatched[3][0].needle.length === 800);

  /* ── local workspace .tex edits (the no-Overleaf default) ── */
  const localRound = (over) => Object.assign({
    toolName: 'write_file',
    toolCallId: 'call-' + Math.random().toString(36).slice(2),
    status: 'done',
    toolArgs: { path: 'paper/main.tex', content: '\\section{Local}' },
  }, over || {});

  _overleafPreviewObserveRounds([localRound()], true);
  check('local_write_file_tex_dispatches_with_path_and_needle',
    dispatched.length === 5
      && dispatched[4][0].source === 'local'
      && dispatched[4][0].path === 'paper/main.tex'
      && dispatched[4][0].needle === '\\section{Local}');

  _overleafPreviewObserveRounds([localRound({
    toolName: 'apply_diff',
    toolArgs: { file_path: 'chapters/intro.ltx',
                new_string: 'aliased replacement text' },
  })], true);
  check('local_apply_diff_alias_path_and_needle',
    dispatched.length === 6
      && dispatched[5][0].path === 'chapters/intro.ltx'
      && dispatched[5][0].needle === 'aliased replacement text');

  _overleafPreviewObserveRounds([localRound({
    toolName: 'edit_file',
    toolArgs: { edits: [
      { path: 'src/app.py', search: 'a', replace: 'b' },
      { path: 'paper.tex', search: 'old', replace: 'new TeX' },
    ] },
  })], true);
  check('local_batch_edit_picks_tex_entry_only',
    dispatched.length === 7
      && dispatched[6][0].path === 'paper.tex'
      && dispatched[6][0].needle === 'new TeX');

  _overleafPreviewObserveRounds([
    localRound({ toolArgs: { path: 'src/app.py', content: 'print(1)' } }),
    localRound({ refusal: { kind: 'read_first' } }),
    localRound({ rejection: { kind: 'policy' } }),
    localRound({ status: 'running' }),
  ], true);
  check('local_nontex_refused_unsettled_all_skipped',
    dispatched.length === 7);

  runtimeScope._overleafPreviewRounds = () => { throw new Error('boom'); };
  let threw = false;
  try { _overleafPreviewObserveRounds([round()], true); }
  catch (_) { threw = true; }
  check('owner_exception_does_not_break_paint', !threw);
} catch (error) {
  check('harness_threw: ' + (error && error.message), false);
} finally {
  report();
}
'''


def test_overleaf_preview_hook_scanner():
    run_harness(
        target_js=HOOK_JS,
        body_js=_BODY,
        expect_pass=11,
        label='overleaf-preview-hook',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
