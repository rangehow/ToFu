"""The ordinary dispatch/resume projection retains exact checklist slots in the DOM."""
import json

import pytest

from tests._jsdom import run_harness
from tests.frontend.test_frontend_segment_timeline import _extract_timeline_fns, _STUBS
from tests.tasks_pkg.test_resume_view_context import archives, _checklist_dispatch_resume
__all__ = ['archives']

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('lead', [False, True], ids=['owner', 'project-lead'])
@pytest.mark.parametrize('coarse', [False, True], ids=['exact', 'coarse-history'])
def test_ordinary_checklist_resume_renders_exact_slots_and_payloads(
        monkeypatch, archives, tmp_path, lead, coarse):
    projection, expected, payloads = _checklist_dispatch_resume(
        monkeypatch, archives, lead, coarse)
    source = tmp_path / 'checklist-timeline.js'
    source.write_text(_STUBS + '\n' + _extract_timeline_fns() + r'''
_renderToolGroupsHTML = rounds => rounds.map(round =>
  '<div class="fixture-tool" data-call="' + round.toolCallId + '"></div>').join('');
function _renderToolSlot(){ return ''; }
window.renderChecklistTimeline = renderSegmentTimelineHTML;
''', encoding='utf-8')
    run_harness(target_js=str(source), label='ordinary checklist resume DOM', expect_pass=4,
                body_js='const projection = ' + json.dumps(projection) + ';\n'
                + 'const expected = ' + json.dumps(expected) + ';\n'
                + 'const payloads = ' + json.dumps(payloads) + ';\n' + r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3], targets: [process.argv[2]]});
const before = JSON.stringify(projection);
function render() {
  const host = document.createElement('main');
  host.innerHTML = window.renderChecklistTimeline(projection.segments, projection, 0);
  return host;
}
const host = render();
const sequence = [...host.querySelector('.ptool-panel-body').children].map(node => {
  if (node.matches('.sw-system-note-row')) return ['system_note', node.querySelector('pre').textContent];
  if (node.matches('.seg-thinking')) return ['thinking', node.querySelector('.thinking-text').textContent];
  if (node.matches('.seg-narration')) return ['text', node.textContent];
  if (node.matches('.fixture-tool')) return ['tool_use', node.dataset.call];
  return ['unexpected', node.className];
});
check('exact_previous_completion_note_thinking_narration_tools_sequence',
  JSON.stringify(sequence) === JSON.stringify(expected));
check('complete_historical_and_latest_payloads',
  JSON.stringify([...host.querySelectorAll('.sw-system-note-row pre')].map(node => node.textContent))
    === JSON.stringify(payloads));
check('replay_is_deterministic_and_projection_immutable',
  render().innerHTML === host.innerHTML && JSON.stringify(projection) === before);
const rows = [...host.querySelectorAll('.sw-system-note-row')];
check('three_separate_collapsed_audit_disclosures', rows.length === 3
  && rows.every(row => !row.open && row.dataset.reason === 'active_checklist')
  && host.querySelectorAll('.sw-note-group').length === 0);
report();
''')
