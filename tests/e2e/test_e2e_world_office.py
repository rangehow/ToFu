"""Office world in the real application shell, on the disposable test instance (private storage, stub model).

Specifies the owner's 11-13 contract for the game-led landing (docs/FRONTEND_ARCHITECTURE.md "Tofu office"):
- the office is the default landing workspace; one always-visible HUD button opens the classic Session UI,
  one top-bar link returns, and the choice persists across reloads; an explicit route is honoured as given;
- clicking a person opens that person's panel; "Chat" docks the classic ConversationSurface and composer for
  that person's real conversation, and sending from it works with the classic authority (stub model);
- the office itself reads only: opening people and panels sends no write;
- the first office facts read waits for a cold server baseline (no client deadline) while the office says it
  is loading, never empty and never an error.
Sending is tested here only, never on live data.
"""
from __future__ import annotations

import pytest

from tests.conftest import _attach_js_error_capture, _dismiss_onboarding_modals
from tests.e2e.test_e2e_smoke import (  # noqa: F401 -- imported fixtures are load-bearing
    _SENTINEL,
    _disable_open_mode_rate_limit,
    _install_llm_stubs,
    _select_stub_model,
)

pytestmark = [pytest.mark.visual]

SHELL = """() => {
  const shown = selector => { const node = document.querySelector(selector); return !!node && node.checkVisibility(); };
  return {
    workspace: document.body.dataset.workspace || '', hash: location.hash,
    landing: localStorage.getItem('tofu_landing_workspace'), dock: document.body.dataset.conversationDock || null,
    office: shown('.w-stage'), classicSwitch: shown('.w-hud [data-world-classic]'), officeLink: shown('#worldBtn'),
    chat: shown('.chat-wrapper'), composer: shown('#userInput'), sidebar: shown('.sidebar'), topbar: shown('.main > .topbar'),
    mainDisplay: getComputedStyle(document.querySelector('.main')).display,
  };
}"""


LOADING = """() => ({
  busy: document.querySelector('.w-stage')?.getAttribute('aria-busy') ?? null,
  loading: !!document.querySelector('.w-hud .w-chip[data-tone="loading"]')?.checkVisibility(),
  empty: !!document.querySelector('.w-empty')?.checkVisibility(),
})"""


def _boot(page, url):
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_function('window.TofuModules && window.TofuModules.version === 3', timeout=15000)


@pytest.fixture()
def office_page(browser, live_server):
    from tests._jsdom import require_shipped_graph

    require_shipped_graph()
    context = browser.new_context(office=True, viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    _attach_js_error_capture(page)
    _boot(page, live_server)
    _dismiss_onboarding_modals(page)
    try:
        yield page
    finally:
        context.close()


def test_office_is_the_landing_and_the_classic_choice_persists(office_page, live_server):
    page = office_page
    # Painted, not merely mounted: the office art (atlas, sprites) decodes from the published graph.
    page.wait_for_selector('.w-office[data-art="ready"]', state='attached', timeout=30000)
    landed = page.evaluate(SHELL)
    assert landed['workspace'] == 'world' and landed['hash'] == '#/world' and landed['office'], landed
    assert landed['classicSwitch'] and not landed['sidebar'] and not landed['topbar'] and not landed['chat'], landed

    page.click('.w-hud [data-world-classic]')
    page.wait_for_function("document.body.dataset.workspace === 'chat'")
    classic = page.evaluate(SHELL)
    assert classic['landing'] == 'chat' and classic['hash'] == '#/chat', classic
    assert classic['sidebar'] and classic['topbar'] and classic['composer'] and not classic['office'], classic
    assert classic['officeLink'] and classic['dock'] is None and classic['mainDisplay'] != 'grid', classic

    _boot(page, live_server)
    page.wait_for_selector('#userInput', state='visible')
    reloaded = page.evaluate(SHELL)
    # The classic boot is untouched: it names no workspace and shows no office.
    assert reloaded['workspace'] in ('', 'chat') and reloaded['hash'] == '' and not reloaded['office'], reloaded

    page.click('#worldBtn')
    page.wait_for_function("document.body.dataset.workspace === 'world'")
    page.wait_for_selector('.w-stage')
    assert page.evaluate(SHELL)['landing'] == 'world'
    _boot(page, live_server)
    page.wait_for_function("document.body.dataset.workspace === 'world'")

    _boot(page, f'{live_server}/#/chat')
    page.wait_for_selector('#userInput', state='visible')
    assert page.evaluate(SHELL)['workspace'] in ('', 'chat') and not page.evaluate(SHELL)['office']


def test_a_person_opens_their_real_conversation_and_the_classic_composer_sends(office_page, live_server):
    page = office_page
    ensured = page.request.post(f'{live_server}/api/v4/router', data={}, headers={'Idempotency-Key': 'world-office-e2e'})
    assert ensured.ok, ensured.text()
    router_id = ensured.json()['conversationId'] if 'conversationId' in ensured.json() else ensured.json()['data']['conversationId']
    writes = []
    page.on('request', lambda request: writes.append(f'{request.method} {request.url}') if request.method not in ('GET', 'HEAD') else None)
    try:
        _boot(page, live_server)
        # People are drawn on the canvas; each has an accessible button over them for the keyboard.
        person = page.locator(f'.evo-target[data-person="{router_id}"]')
        person.wait_for(timeout=30000)

        person.focus()
        page.keyboard.press('Enter')
        page.wait_for_function("(id) => document.querySelector('.w-panel-head:not([hidden])')?.dataset.person === id", arg=router_id)
        assert page.evaluate("document.body.dataset.conversationDock") == 'brief'
        page.keyboard.press('Escape')
        page.wait_for_function("document.body.dataset.conversationDock === 'closed'")
        assert page.evaluate("document.activeElement?.dataset.person") == router_id

        # A pointer clicks the canvas where the person is drawn; the scene hit-tests it.
        box = person.bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.wait_for_selector('.w-panel-head:not([hidden])')
        page.wait_for_timeout(500)
        assert writes == [], 'the office itself reads only'

        page.click('.w-tab >> nth=1')
        page.wait_for_function("document.body.dataset.conversationDock === 'chat'")
        page.wait_for_selector('#userInput', state='visible')
        page.wait_for_function("(id) => sessionStorage.getItem('tofu_activeConvId') === id", arg=router_id, timeout=15000)
        assert page.evaluate(SHELL)['office'], 'the office stays beside the docked chat'

        _select_stub_model(page)
        calls = _SENTINEL['stream_calls']
        page.locator('#userInput').fill('A note from the office')
        page.locator('#sendBtn').click()
        page.wait_for_function("""() => {
          const body = document.querySelector('#chatInner')?.innerText || '';
          const send = document.querySelector('#sendBtn');
          return body.includes('A note from the office') && body.includes('stubbed model') && send && !send.classList.contains('stop-btn');
        }""", timeout=30000)
        assert _SENTINEL['stream_calls'] > calls
        snapshot = page.request.get(f'{live_server}/api/v4/conversations/{router_id}/sync?segmentPayload=refs&toolPayload=lazy&turnWindow=tail-96')
        assert snapshot.ok and 'A note from the office' in snapshot.text()
    finally:
        page.request.delete(f'{live_server}/api/v4/conversations/{router_id}')


def test_a_cold_office_read_is_awaited_while_the_office_shows_it_is_loading(office_page, live_server):
    # Production's cold office baseline took 13-20 s (2026-11-13); this read is held past the 12 s deadline
    # every other coordination read keeps.
    page = office_page
    ensured = page.request.post(f'{live_server}/api/v4/router', data={}, headers={'Idempotency-Key': 'world-office-cold-read'})
    assert ensured.ok, ensured.text()
    router_id = ensured.json()['conversationId'] if 'conversationId' in ensured.json() else ensured.json()['data']['conversationId']
    held = []
    page.route('**/api/v4/router/office', lambda route: held.append(route))
    try:
        _boot(page, live_server)
        failed = []
        page.on('requestfailed', lambda request: failed.append(request.failure) if '/api/v4/router/office' in request.url else None)
        page.locator(f'.evo-target[data-person="{router_id}"]').wait_for(timeout=30000)
        assert page.evaluate(LOADING) == {'busy': 'true', 'loading': True, 'empty': False}

        page.wait_for_timeout(13_000)
        assert len(held) == 1 and failed == [], (len(held), failed)
        assert page.evaluate(LOADING) == {'busy': 'true', 'loading': True, 'empty': False}

        held[0].fulfill(response=held[0].fetch())
        page.wait_for_selector('.w-stage[aria-busy="false"]', timeout=15000)
        assert page.evaluate(LOADING) == {'busy': 'false', 'loading': False, 'empty': False}
        assert failed == [] and page.locator(f'.evo-target[data-person="{router_id}"]').is_visible()
    finally:
        page.unroute('**/api/v4/router/office')
        page.request.delete(f'{live_server}/api/v4/conversations/{router_id}')
