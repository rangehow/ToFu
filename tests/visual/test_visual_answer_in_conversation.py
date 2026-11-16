""""Answer in conversation" lands on the question, not the bottom, and replies to it.

Owner complaint 2026-11-16: from the studio's question card, 在对话中回答 jumped to
the bottom of the lead's conversation with no way to quote the question. Real
Chromium, the shipped stylesheet, the production Surface and classic renderers
(the question card is the shared answer control over the production
choice-interaction adapter), the retained `openConversationAtQuestion` owner
(runtime ui/turn_nav.js) and the production reply-quote chip renderer.
Synthetic Turns stay inside a disposable iframe.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

QUESTION = ('All experiments are ready, but the only 910C queue we can use is full. '
            'Do you have other compute we could use?')


def _mount(page, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'answer-in-chat-fixture';
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html lang="en"><body style="display:block;margin:0;padding:0">'
                      '<main id="chatContainer" style="height:640px;overflow:auto;overflow-anchor:none">'
                      '<div id="chatInner"></div></main>'
                      '<div class="input-box"><textarea id="userInput"></textarea>'
                      '<div class="input-actions"></div></div></body></html>')
    frame.evaluate("document.documentElement.dataset.theme='dark'")
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate(r"""locale => {
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g, (match, name) => params?.[name] ?? match);
      window.escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope = {}; window._TOOL_DISPLAY = {};
      window.errorEnvelopeMessage = String;
    }""", locale)
    for module in ['frontend/src/conversation/index.ts',
                   'frontend/src/conversation/application/question-focus.ts',
                   'frontend/src/conversation/application/choice-interaction.ts',
                   'frontend/src/selection-popup.ts']:
        name = module.removeprefix('frontend/src/').replace('/', '-').removesuffix('.ts')
        frame.add_script_tag(path=native_module_path(f'.native/answer-in-chat-{name}.js', ROOT / module))
    frame.evaluate(r"""question => {
      const cid = 'lead';
      const text = (id, ordinal, actor) => ({turnId:id,conversationId:cid,laneId:'main',ordinal,actor,
        kind:actor==='human'?'input':'reply',status:'completed',projectionRevision:1,currentAttemptId:null,
        createdAt:ordinal,updatedAt:ordinal,settlement:{outcome:'completed'},projection:{segments:[{
          type:'text',blockId:id+':answer',terminal:true,deliverable:true,
          text:`Progress report ${ordinal}\n\n`+'Readable progress evidence. '.repeat(60)}]}});
      const round = {toolName:'ask_human',toolCallId:'ask',roundNum:1,llmRound:0,taskId:'run',status:'done',
        guidanceId:'hg_queue',guidanceType:'choice',guidanceBlocking:false,guidanceQuestion:question,
        guidanceOptions:[{label:'Use the H800/A100 queue'},{label:'Ask for a priority bump'}],
        guidanceDefault:'Keep everything queued'};
      const ask = {...text('ask', 3, 'assistant'), projection:{toolRounds:[round],segments:[{type:'tool_use',
        blockId:'tool:ask',id:'ask',name:'ask_human',taskId:'run',input:{},result:{status:'done',content:''}}]}};
      window.fixtureTurns = [text('t1',1,'human'), text('t2',2,'assistant'), ask,
        ...Array.from({length:12}, (_, i) => text('later-'+i, 4+i, i%2 ? 'human' : 'assistant'))];
      // A later human Turn would answer a non-blocking question; keep the reports assistant-only after it.
      fixtureTurns.slice(3).forEach(turn => { turn.actor = 'assistant'; turn.kind = 'reply'; });
      const state = {conversationId:cid,conversationRevision:1,transport:'live',
        turnsById:Object.fromEntries(fixtureTurns.map(turn => [turn.turnId, turn])),
        laneOrder:{main:fixtureTurns.map(turn => turn.turnId)},attemptsById:{},queueItems:[],
        pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
      const surface = createConversationSurface(document.getElementById('chatInner'), {...createClassicConversationRenderers({
        renderSafeMarkdownHtml: escapeHtml,
        renderToolBlockHtml: () => '',
        localizedText: (key, fallback) => (t(key) !== key ? t(key) : fallback),
        choiceInteractions: (conversationId, turnId) => createChoiceInteractionPorts({
          conversationId, turnId, commandId: () => 'cmd',
          text: (key, fallback) => (t(key) !== key ? t(key) : fallback),
          readTurn: async () => fixtureTurns.find(turn => turn.turnId === turnId),
          replyInChat: async () => 'sent',
        }),
      }), scrollViewport: document.getElementById('chatContainer')});
      surface.render(selectConversationViewModel(state));
      const viewport = document.getElementById('chatContainer');
      viewport.scrollTop = viewport.scrollHeight;
      window.fixtureSurface = surface;
      window.fixtureConv = {id: cid};
      window.getActiveConv = () => fixtureConv;
      window.loadConversation = async () => {};
      window._pendingReplyQuotes = [];
      window._addReplyQuote = quote => {
        if (_pendingReplyQuotes.includes(quote)) return;
        _pendingReplyQuotes.push(quote);
        let container = document.getElementById('reply-quote-container');
        if (!container) {
          container = document.createElement('div');
          container.id = 'reply-quote-container'; container.className = 'reply-quote-container';
          const actions = document.querySelector('.input-box .input-actions');
          actions.parentElement.insertBefore(container, actions);
        }
        renderReplyQuotePreviews(container, _pendingReplyQuotes, t, index => {
          _pendingReplyQuotes.splice(index, 1); container.replaceChildren(); container.hidden = true;
        });
      };
      Object.assign(runtimeScope, {
        ConversationSurfacePresentation: {revealTurn: (id, turnId, target) => id === cid && surface.revealTurn(turnId, target)},
        requestAuthoritativeConversationRender: () => true,
        ConversationTurnStore: {hydrateConversationRound: async () => {}},
        ConversationTurnRead: {ordered: () => fixtureTurns},
      });
    }""", QUESTION)
    frame.add_script_tag(content=runtime_section('ui/turn_nav.js', scope_prelude=False))
    return frame


@pytest.mark.parametrize('width', [1180, 390])
def test_answer_in_conversation_reveals_highlights_and_quotes_the_question(frontend_visual_page, tmp_path, width):
    frame = _mount(frontend_visual_page, width)
    frame.wait_for_function("document.querySelector('#chatInner [data-turn-id=\"later-11\"]')")
    before = frame.evaluate("""async () => {
      const viewport = document.getElementById('chatContainer');
      viewport.scrollTop = viewport.scrollHeight;
      await new Promise(requestAnimationFrame);
      return {bottomGap: viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight};
    }""")
    assert before['bottomGap'] <= 2, 'the fixture starts at the bottom, as the owner saw it'
    frontend_visual_page.locator('#answer-in-chat-fixture').screenshot(path=str(tmp_path / f'answer-in-chat-before-{width}.png'))

    opened = frame.evaluate("() => openConversationAtQuestion('lead', {turnId: 'ask', ordinal: 3, guidanceId: 'hg_queue'})")
    assert opened is True
    frame.wait_for_function("document.querySelector('[data-guidance-id=\"hg_queue\"] .rich-choice-option')")
    result = frame.evaluate("""async () => {
      for (let i = 0; i < 8; i += 1) await new Promise(requestAnimationFrame);
      const viewport = document.getElementById('chatContainer');
      const block = document.querySelector('#chatInner [data-guidance-id="hg_queue"]');
      const chip = document.querySelector('#reply-quote-container .reply-quote-preview');
      return {
        offset: block.getBoundingClientRect().top - viewport.getBoundingClientRect().top,
        visible: block.getBoundingClientRect().top < viewport.getBoundingClientRect().bottom,
        bottomGap: viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight,
        highlight: block.dataset.revealHighlight || '',
        chip: chip?.querySelector('blockquote')?.textContent || '',
        removable: Boolean(chip?.querySelector('button')),
        quotes: [..._pendingReplyQuotes],
        focused: document.activeElement?.id || '',
      };
    }""")
    frontend_visual_page.locator('#answer-in-chat-fixture').screenshot(path=str(tmp_path / f'answer-in-chat-after-{width}.png'))
    print(f'Answer-in-conversation screenshots: {tmp_path}')
    assert abs(result['offset']) <= 2, result
    assert result['visible'] and result['bottomGap'] > 200, 'the question, not the bottom, is shown'
    assert result['highlight'] == 'question'
    expected_quote = f'{QUESTION}\n\n[question hg_queue]'
    assert result['quotes'] == [expected_quote], 'the composer replies to that question, with its id'
    assert result['chip'] == expected_quote and result['removable']
    assert result['focused'] == 'userInput'
    frame.evaluate('fixtureSurface.dispose()')
