"""Click and Enter expose a definitive backend failure and allow an explicit retry."""
import json

import pytest

pytestmark = pytest.mark.visual


def test_click_and_enter_restore_draft_and_show_backend_failure(page, screenshot_dir):
    page.wait_for_function('typeof newChat === "function"')
    page.evaluate('newChat()')
    requests = []
    def reject(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        requests.append(route.request.post_data_json)
        # A backend-attributed 500 is definitive. Transient answers (503,
        # database_unavailable) repeat without a cap behind the send.retrying
        # status since e38cc2e3f1 (docs/FRONTEND_ARCHITECTURE.md, "Send startup
        # repeats transient failures itself"), so they never reach a toast.
        route.fulfill(status=500, content_type='application/json', body=json.dumps({
            'ok': False, 'error': {'code': 'database_internal',
                'message': 'Storage write failed; retry your saved draft.'},
        }))
    page.route('**/api/v4/conversations/*/turns', reject)
    textarea = page.locator('#userInput')
    textarea.fill('Retain this message when storage fails')
    page.locator('#sendBtn').click()
    page.wait_for_function('''() => document.querySelector('#toastContainer')
        ?.textContent.includes('Storage write failed')''', timeout=30000)
    assert textarea.input_value() == 'Retain this message when storage fails'
    assert requests
    command_id = requests[0]['commandId']
    assert all(request['commandId'] == command_id for request in requests)
    first_attempt_count = len(requests)
    with page.expect_request(lambda request: request.method == 'POST'
            and request.url.endswith('/turns')):
        textarea.press('Enter')
    # Wait on the observable request/draft boundary, not a rendering delay.
    from playwright.sync_api import expect
    expect(textarea).to_have_value('Retain this message when storage fails', timeout=30000)
    assert len(requests) > first_attempt_count
    assert all(request['commandId'] == command_id for request in requests)
    page.screenshot(path=str(__import__('pathlib').Path(screenshot_dir) / 'send-failure-recovery.png'))
