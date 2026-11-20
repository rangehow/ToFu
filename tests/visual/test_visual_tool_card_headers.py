"""Registered header fragments and settled cards share readable title chrome."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1180, 390])
@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
def test_tool_card_header_fragments_keep_title_separator(frontend_visual_page, tmp_path, width, theme):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.emulate_media(reduced_motion='reduce')
    page.add_script_tag(path=native_module_path(
        '.native/tool-card-header-visual.js', ROOT / 'frontend/src/conversation/ui/tool-cards/index.ts'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate('''({locale, theme}) => {
      document.documentElement.dataset.theme = theme;
      document.body.innerHTML = '<main class="conversation-surface" id="fixture"></main>';
      const t = (key, params) => (locale[key] || key).replace(/\\{(\\w+)\\}/g,
        (token, name) => params?.[name] === undefined ? token : String(params[name]));
      const cards = createToolCardPresentation({translate:t, markdown:s=>s, conversationTitle:()=>''});
      const host = document.getElementById('fixture');
      for (const status of ['searching', 'queued', 'error', 'done']) {
        const round = {toolName:'await_project_workers', status, roundNum:1,
          toolArgs:{assignment_ids:['one','two','three','four','five']},
          toolContent:status === 'done' ? '{}' : ''};
        const row = document.createElement('section');
        row.dataset.case = status;
        row.innerHTML = cards.renderCardHtml(round, {iconHtml:''})
          || '<div class="ptool-line"><span class="ptool-text">' + cards.headerHtml(round) + '</span></div>';
        row.querySelector('.ptool-line').setAttribute('data-action-summary', '');
        host.append(row);
      }
      const long = {toolName:'delegate_project_work',status:'searching',
        toolArgs:{title:'需要验证的长任务标题与说明'.repeat(20)}};
      host.insertAdjacentHTML('beforeend', '<section data-case="long"><div class="ptool-line" data-action-summary>'
        + '<span class="ptool-text">' + cards.headerHtml(long) + '</span></div></section>');
      host.insertAdjacentHTML('beforeend', '<section data-case="plain"><div class="ptool-line" data-action-summary>'
        + '<span class="ptool-text">普通工具标题</span></div></section>');
    }''', {'locale': locale, 'theme': theme})
    page.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);height:auto;overflow:auto}'
                               '#fixture{display:block;max-width:100%}')
    for status in ('searching', 'queued', 'error', 'done', 'long'):
        row = page.locator(f'[data-case="{status}"]')
        assert row.locator('.ptool-coord-sep').get_attribute('aria-hidden') == 'true'
        geometry = row.evaluate('''node => {
          const title = node.querySelector('.ptool-coord-title').getBoundingClientRect();
          const sepNode = node.querySelector('.ptool-coord-sep');
          const sep = sepNode.getBoundingClientRect();
          const subject = node.querySelector('.ptool-coord-summary').getBoundingClientRect();
          return {gap:subject.left-title.right, before:sep.left-title.right, after:subject.left-sep.right,
            width:sep.width, height:sep.height, ink:getComputedStyle(sepNode).backgroundColor,
            aligned:Math.abs(title.top-subject.top)<1, overflow:node.scrollWidth>node.clientWidth+1};
        }''')
        assert geometry['width'] > 0 and geometry['height'] > 0, geometry
        assert geometry['before'] > 0 and geometry['after'] > 0, geometry
        assert geometry['ink'] != 'rgba(0, 0, 0, 0)', geometry
        assert geometry['aligned'] and not geometry['overflow'], geometry
    assert page.locator('[data-case="plain"] .ptool-coord-sep').count() == 0
    assert page.locator('[data-case="plain"] .ptool-text').evaluate(
        "node=>getComputedStyle(node).getPropertyValue('-webkit-line-clamp')") in ('1', '2')
    assert page.locator('[data-case="long"] .ptool-coord-summary').evaluate('''node =>
      node.scrollWidth > node.clientWidth && getComputedStyle(node).textOverflow === 'ellipsis'
    ''')
    settled = page.locator('[data-case="done"] details')
    settled.locator('summary').focus()
    settled.locator('summary').press('Enter')
    assert settled.evaluate('node=>node.open')
    settled.locator('summary').press('Space')
    assert not settled.evaluate('node=>node.open')
    screenshot = tmp_path / f'tool-card-headers-{theme}-{width}.png'
    page.screenshot(path=str(screenshot), full_page=True)
    print(f'Tool header screenshot: {screenshot}')
