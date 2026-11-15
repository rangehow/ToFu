"""Tool-row roles inside a grouped reply's tool panel, in real Chromium.

Owner report 2026-11-01: the accent-colored identity on a running step and the
tinted token pills on every call were redundant. Rows keep metadata quiet,
reserve chip chrome for outcomes that need the reader, and take every value
from one role vocabulary (`--tool-row-*`, 00-design-tokens.css).
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.frontend.test_frontend_tool_rounds_wire_parity import _run_harness
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page

pytestmark = pytest.mark.visual

PREFIX = 'cd /workspace/tofu && '


@pytest.fixture(scope='module')
def panel_rounds(tmp_path_factory):
    rounds = [
        {'_name': 'read', 'toolName': 'read_files', 'query': 'Read the tool summary owner',
         'toolArgs': {'path': 'frontend/src/conversation/ui/tool-summary.ts'}, 'status': 'done',
         'roundNum': 1, 'toolTokens': 1234, 'toolContent': 'export function decorateToolSummary() {}',
         'results': [{}]},
        {'_name': 'skillLoad', 'toolName': 'load_skill',
         'query': 'Loaded skill: tofu-frontend',
         'toolArgs': {'skill_id': 'tofu-frontend'},
         'status': 'done', 'roundNum': 1, 'toolTokens': 8800,
         'toolContent': 'Frontend guide.', 'results': [{'badge': '📦 loaded'}]},
        {'_name': 'skillResource', 'toolName': 'read_skill_resource',
         'query': 'Read skill resource: skill://skillhub/references/search-and-discover.md',
         'toolArgs': {'skill_id': 'skillhub', 'resource': 'skill://skillhub/references/search-and-discover.md'},
         'status': 'done', 'roundNum': 1, 'toolTokens': 1530,
         'toolContent': 'Search guide.', 'results': [{'badge': '📦 ready', 'fetchedChars': 6123}]},
        {'_name': 'search', 'toolName': 'search_files', 'query': 'Search for badge styles',
         'toolArgs': {'pattern': 'ptool-badge'}, 'status': 'done', 'roundNum': 2,
         'toolTokens': 31, 'toolContent': 'a.css:1: .ptool-badge {}', 'results': [{}]},
        {'_name': 'fetch', 'toolName': 'fetch_url', 'query': 'Fetch the design reference',
         'toolArgs': {'url': 'https://example.com/design'}, 'status': 'done', 'roundNum': 2,
         'toolTokens': 2400, 'toolContent': 'Reference text.', 'results': [{}]},
        {'_name': 'command', 'toolName': 'run_command', 'query': PREFIX + 'npm run check:styles',
         'status': 'done', 'roundNum': 3, 'results': [{'command': PREFIX + 'npm run check:styles',
         'description': 'Run the style token gate', 'output': 'Style checks passed.', 'exitCode': '0'}]},
        {'_name': 'failed', 'toolName': 'run_command', 'query': PREFIX + 'npm run typecheck',
         'status': 'done', 'roundNum': 4, 'results': [{'command': PREFIX + 'npm run typecheck',
         'description': 'Type-check the conversation modules', 'output': 'error TS2322', 'exitCode': '2'}]},
        {'_name': 'running', 'toolName': 'run_command', 'query': PREFIX + 'rg -n schedule frontend/src',
         'toolArgs': {'command': PREFIX + 'rg -n schedule frontend/src',
                      'description': 'Inspect surface controller render scheduling'},
         'status': 'searching', '_isSearching': True, 'roundNum': 5,
         '_partialOutput': 'conversation-surface-controller.ts:43:  schedule(render: () => void): () => void;'},
    ]
    for round_ in rounds:
        round_['toolCallId'] = round_['_name']
    path = tmp_path_factory.mktemp('tool-row-roles') / 'rounds.json'
    path.write_text(json.dumps(rounds))
    rendered = _run_harness(path)
    assert not any(row.get('err') for row in rendered), rendered
    return {row['name']: row['html'] for row in rendered}, {round_['_name']: round_ for round_ in rounds}


@pytest.fixture(scope='module')
def roles_bundle(tmp_path_factory):
    entry = tmp_path_factory.mktemp('tool-row-roles-module') / 'entry.ts'
    entry.write_text(f"""
export * from '{ROOT}/frontend/src/conversation/index.ts';
export {{createTurnState}} from '{ROOT}/frontend/src/core/turn-state.ts';
""")
    return native_module_path('.native/tool-row-roles.js', entry)


# Resolves a CSS color expression against the node's own cascade, so theme and
# panel scopes apply exactly as they do to the real element.
RESOLVE = """(node, value) => {
  const probe = document.createElement('span');
  probe.style.color = value;
  node.append(probe);
  const color = getComputedStyle(probe).color;
  probe.remove();
  return color;
}"""
PAINT = """node => {
  const style = getComputedStyle(node);
  return {color: style.color, background: style.backgroundColor, border: style.borderTopWidth,
          family: style.fontFamily, display: style.display};
}"""


def transparent(color):
    return color == 'transparent' or color.replace(' ', '').endswith(',0)')


ROW_GEOMETRY = """(row, outcome) => {
  const intent = row.querySelector(':scope > :is(.ptool-text, .ptool-cmd-desc-inline)').getBoundingClientRect();
  const box = row.getBoundingClientRect(), style = getComputedStyle(row);
  return {intentLeft: intent.left, intentWidth: intent.width,
          end: row.querySelector(outcome).getBoundingClientRect().right,
          column: box.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth)};
}"""


def assert_row_unchanged(before, after):
    assert all(abs(after[key] - before[key]) <= 0.5 for key in before), (before, after)
    assert abs(after['end'] - after['column']) <= 1, after


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390])
def test_panel_rows_keep_metadata_quiet_and_chips_for_exceptions(
        frontend_visual_page, panel_rounds, roles_bundle, tmp_path, theme, width):
    from playwright.sync_api import expect

    html, rounds = panel_rounds
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.emulate_media(reduced_motion='reduce')
    page.set_content(f'<!doctype html><html data-theme="{theme}"><head><link rel="stylesheet" href="/static/styles.css">'
                     '</head><body class="cv-off"><main id="surface"></main></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_script_tag(path=roles_bundle)
    page.evaluate("""([tools, rounds]) => {
      const scope = {taskId: 'roles-task', attemptId: 'roles-attempt', llmRound: 0};
      const tool = blockId => ({type: 'tool_use', blockId, id: blockId, name: rounds[blockId].toolName, ...scope,
        _round: {...rounds[blockId], ...scope}});
      const turn = {turnId: 'turn', conversationId: 'tool-row-roles', actor: 'assistant', role: 'assistant',
        laneId: 'main', ordinal: 0, status: 'running', projectionRevision: 1,
        projection: {segments: Object.keys(rounds).map(tool)}};
      window.rolesSurface = createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({
          renderSafeMarkdownHtml: text => { const p = document.createElement('p'); p.textContent = text; return p.outerHTML; },
          renderToolBlockHtml: block => tools[block.blockId] || ''}));
      rolesSurface.render(selectConversationViewModel({...createTurnState('tool-row-roles'),
        turnsById: {turn}, laneOrder: {main: ['turn']}}));
    }""", [html, rounds])
    expect(page.locator('.conversation-tool-panel__toggle')).to_have_attribute('aria-expanded', 'true')
    block = lambda name: page.locator(f'[data-block-id="{name}"]')
    summary = lambda name: block(name).locator('[data-action-summary]').first
    running = block('running')
    expect(running).to_have_attribute('data-action-state', 'active')
    expect(block('failed')).to_have_attribute('data-action-state', 'attention')

    settled_intent = summary('read').locator('.ptool-text').first
    intent_paint = settled_intent.evaluate(PAINT)
    accent = settled_intent.evaluate(RESOLVE, 'var(--accent)')

    # Telemetry and routine counts are metadata: the same quiet ink and face
    # as a settled intent, no pill, never the accent.
    for name, label in [('read', '1.2k tok'), ('skillLoad', '8.8k tok'),
                        ('skillResource', '1.5k tok'),
                        ('search', '31 tok'), ('fetch', '1 result')]:
        badge = summary(name).locator('.ptool-badge-info')
        expect(badge).to_be_visible()
        expect(badge).to_have_text(label)
        paint = badge.evaluate(PAINT)
        assert transparent(paint['background']) and paint['border'] == '0px', (name, paint)
        assert paint['color'] == intent_paint['color'] != accent, (name, paint, intent_paint)
        assert paint['family'] == intent_paint['family'], (name, paint, intent_paint)

    expect(summary('skillLoad')).not_to_contain_text('loaded')
    expect(summary('skillLoad')).not_to_contain_text('📦')
    expect(summary('skillResource').locator('.ptool-text')).to_have_text(
        'Read skillhub: references/search-and-discover.md')
    expect(summary('skillResource')).not_to_contain_text('ready')
    expect(summary('skillResource').locator('.ptool-badge-info')).to_have_attribute(
        'title', 'Tokens consumed by this tool result (6,123 chars)')

    # Routine success is a bare check; its exit code stays in the accessible text.
    success = summary('command').locator('.ptool-cmd-status')
    expect(success).to_be_visible()
    expect(success).to_have_text('exit 0')
    success_paint = success.evaluate(PAINT)
    assert transparent(success_paint['background']) and success_paint['border'] == '0px', success_paint

    # A chip always means "look here": the failure keeps its chip.
    failure = summary('failed').locator('.ptool-cmd-status')
    expect(failure).to_be_visible()
    failure_paint = failure.evaluate(PAINT)
    assert not transparent(failure_paint['background']), failure_paint
    assert failure_paint['border'] != '0px', failure_paint
    assert failure_paint['color'] == failure.evaluate(RESOLVE, 'var(--chip-err-color)'), failure_paint

    # The running step: intent and glyph carry liveness; the exact command
    # stays one gesture away in its own pane instead of repeating in accent ink.
    expect(summary('running').locator('.ptool-cmd-fold-cmd')).to_have_count(1)
    expect(running.locator('.ptool-cmd-code')).to_contain_text('rg -n schedule frontend/src')
    live_glyph = summary('running').locator('.ptool-cmd-icon')
    expect(live_glyph).to_be_visible()
    assert live_glyph.evaluate(PAINT)['color'] == accent

    # Running, failed, open and command-evidence states never add a duplicate
    # identity line; the native evidence still holds the exact invocation.
    # Opening a step or expanding a fold moves nothing in its row: the intent
    # keeps its box and the outcome its shared end column (owner report
    # 2026-11-11: expanding the output left a gap at the row's end).
    redundant_identities = page.locator('.conversation-tool-identity')
    expect(redundant_identities).to_have_count(0)
    summary('read').hover()
    settled = summary('read').evaluate(ROW_GEOMETRY, '.ptool-badge-info')
    summary('read').click()
    expect(block('read').locator('details').first).to_have_attribute('open', '')
    assert_row_unchanged(settled, summary('read').evaluate(ROW_GEOMETRY, '.ptool-badge-info'))
    expect(redundant_identities).to_have_count(0)
    summary('read').click()
    summary('command').hover()
    settled = summary('command').evaluate(ROW_GEOMETRY, '.ptool-cmd-status')
    # The same DOM effect as the output chip's _cmdOutputOpenToggle.
    block('command').locator('.ptool-cmd-fold-output').evaluate("""n => {
      const command = n.closest('.ptool-cmd-block');
      command.classList.add('output-open');
      n.setAttribute('aria-expanded', 'true');
      const lazy = command.querySelector(':scope > details[data-lazy-tool-result]');
      if (lazy) lazy.open = true;
    }""")
    expect(block('command').locator('.ptool-cmd-output')).to_be_visible()
    assert_row_unchanged(settled, summary('command').evaluate(ROW_GEOMETRY, '.ptool-cmd-status'))
    block('command').locator('.ptool-cmd-block').evaluate("n => n.classList.add('cmd-open')")
    block('command').locator('.ptool-cmd-fold-cmd').evaluate("n => n.setAttribute('aria-expanded', 'true')")
    expect(block('command').locator('.ptool-cmd-code')).to_be_visible()
    assert_row_unchanged(settled, summary('command').evaluate(ROW_GEOMETRY, '.ptool-cmd-status'))
    expect(redundant_identities).to_have_count(0)
    block('command').locator('.ptool-cmd-block').evaluate("""n => {
      n.classList.remove('cmd-open', 'output-open');
      const lazy = n.querySelector(':scope > details[data-lazy-tool-result]');
      if (lazy) lazy.open = false;
    }""")
    block('command').locator('.ptool-cmd-fold').evaluate_all("nodes => nodes.forEach(n => n.setAttribute('aria-expanded', 'false'))")

    if width > 600:
        heights = page.evaluate("""() => [...document.querySelectorAll('#surface [data-action-summary]')]
          .filter(n => n.getClientRects().length && !n.closest('[data-action-state="attention"]'))
          .map(n => Math.round(n.getBoundingClientRect().height))""")
        assert heights and max(heights) <= 36, heights
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(tmp_path / f'tool-row-roles-{theme}-{width}.png'), full_page=True)

    # One vocabulary steers every family: retuning a role token repaints
    # telemetry and the live glyph without touching tool selectors.
    page.evaluate("""() => {
      const root = document.documentElement.style;
      root.setProperty('--tool-row-meta-ink', 'rgb(1, 2, 3)');
      root.setProperty('--tool-row-glyph-ink-live', 'rgb(4, 5, 6)');
    }""")
    expect(summary('read').locator('.ptool-badge-info')).to_have_css('color', 'rgb(1, 2, 3)')
    expect(live_glyph).to_have_css('color', 'rgb(4, 5, 6)')
    page.evaluate('rolesSurface.dispose()')
