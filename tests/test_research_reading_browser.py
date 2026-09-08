"""Real-browser reading continuity for research telemetry; no server or model."""
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def research_dom_bundle(tmp_path_factory):
    output = tmp_path_factory.mktemp('research-browser') / 'dom.js'
    subprocess.run([
        'node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/features/research/components/stable-dom.ts'),
        '--bundle', '--format=iife', '--global-name=ResearchDom',
        f'--outfile={output}',
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    return output


@pytest.mark.parametrize('width', [390, 1280])
def test_progress_preserves_reading_context(browser, research_dom_bundle, width):
    context = browser.new_context(viewport={'width': width, 'height': 800},
                                  reduced_motion='reduce')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<main id="host"></main>')
        page.add_script_tag(path=str(research_dom_bundle))
        result = page.evaluate('''() => {
          const host = document.querySelector('#host');
          const markup = n => `<header><h1>研究问题：${'长中文标题'.repeat(16)}</h1>
            <span id="progress" role="status">已读取 ${n} 篇</span></header>
            <input id="filter" aria-label="筛选证据" value="">
            <div id="evidence" style="height:300px;overflow:auto">
            ${Array.from({length:200}, (_,i) => `<details id="paper-${i}">
              <summary>文献 ${i}</summary><p id="quote-${i}">证据片段 ${i} 不代表已经验证的科学结论</p>
            </details>`).join('')}</div>`;
          ResearchDom.patchResearchDom(host, markup(0));
          const input = document.querySelector('#filter');
          const quote = document.querySelector('#quote-0');
          const disclosure = document.querySelector('#paper-0');
          disclosure.open = true;
          input.value = '保留筛选'; input.focus();
          const range = document.createRange(); range.selectNodeContents(quote);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          const selected = selection.toString();
          const scroller = document.querySelector('#evidence'); scroller.scrollTop = 180;
          const scroll = scroller.scrollTop;
          const start = performance.now();
          for (let n=1;n<=30;n++) ResearchDom.patchResearchDom(host, markup(n));
          return {
            sameInput: input === document.querySelector('#filter'),
            sameQuote: quote === document.querySelector('#quote-0'),
            focused: document.activeElement === input,
            filter: input.value, open: disclosure.open,
            selected: selection.toString() === selected && selected.length > 0,
            scroll: scroller.scrollTop === scroll,
            count: host.querySelectorAll('details').length,
            progress: document.querySelector('#progress').textContent,
            updateMs: (performance.now()-start)/30,
          };
        }''')
        assert all(result[key] for key in
                   ('sameInput', 'sameQuote', 'focused', 'open', 'selected', 'scroll')), result
        assert result['filter'] == '保留筛选'
        assert result['count'] == 200
        assert result['progress'] == '已读取 30 篇'
        assert result['updateMs'] < 250, result  # generous host-independent regression ceiling
        assert not errors
    finally:
        context.close()
