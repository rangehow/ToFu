"""System intervention blocks render action facts and untrusted text safely."""
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_system_interventions_are_collapsed_safe_and_action_specific():
    target = native_module_path(
        '.native/system-intervention-renderer.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts')
    run_harness(target_js=str(target), label='system intervention UI', expect_pass=4,
                body_js=r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3], targets: [process.argv[2]]});
const renderers = createClassicConversationRenderers({renderSafeMarkdownHtml: () => {throw Error('intervention must not reach markdown');}});
const node = document.createElement('div');
const block = {kind: 'system-note', noteKind: 'system-intervention', action: 'stop',
  reason: '<img src=x onerror=bad>', text: '<script>bad()</script> **raw**'};
renderers.renderBlock(node, block, {turn: {actor: 'assistant'}});
check('collapsed_by_default', !!node.querySelector('details') && !node.querySelector('details').open);
check('stop_not_injection', node.textContent.includes('Stopped') && !node.textContent.includes('Context appended'));
check('raw_text_safe', !node.querySelector('script,img') && node.querySelector('pre').textContent === block.text
  && node.textContent.includes(block.reason));
renderers.renderBlock(node, {...block, action: 'prompt'}, {turn: {actor: 'assistant'}});
check('rerender_updates_action_without_duplicates', node.textContent.includes('Context appended')
  && !node.textContent.includes('Stopped') && node.querySelectorAll('details').length === 1);
report();
""")
