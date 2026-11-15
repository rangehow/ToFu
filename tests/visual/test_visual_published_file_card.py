"""Actual application: a published .pptx opens as a visible card with a usable
download action and a rendered, clickable page preview (desktop and phone)."""
import io
import os
from types import SimpleNamespace

import pytest
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]

_LINK = 'https://jobs.bytedance.com/campus/position/7628619894381136133/detail'


def _deck_bytes():
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(960), Pt(540)
    for title in ('字节跳动 · Seed 大模型实习', '腾讯 · 青云计划'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        run = slide.shapes.add_textbox(Pt(48), Pt(48), Pt(600), Pt(60)).text_frame.paragraphs[0].add_run()
        run.text = title
        run.font.size = Pt(28)
        run.hyperlink.address = _LINK
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


@pytest.mark.parametrize('width', [1280, 390])
def test_published_presentation_card_download_and_preview(page, owner, tmp_path, width, monkeypatch):
    from playwright.sync_api import expect
    from lib import response_assets, turn_lifecycle
    from lib.artifacts.response_files import publish_file
    from lib.storage import get_storage_client
    from tests.conftest import _dismiss_onboarding_modals
    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-published-pptx')
    source = tmp_path / '实习岗位_LLM_Agent.pptx'
    source.write_bytes(_deck_bytes())
    receipt = publish_file(str(source), 'Internship deck', conv_id=task['convId'], user_id=1,
                           roots=[str(tmp_path)], task_id=task['id'])
    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'Deck published'})
    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate("localStorage.setItem('tofu_ui_lang','en')")
        page.add_init_script("window.__cardAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__cardAppReady=true})")
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__cardAppReady === true'); _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for(state='attached')
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        page.locator(f'[data-conversation-action="open-artifact"][data-operation="{receipt["id"]}"]').click()
        panel = page.locator('#artifact-panel')
        card = panel.locator('.artifact-file-card')
        expect(card).to_be_visible()
        expect(card.locator('.artifact-file-card-name')).to_have_text('实习岗位_LLM_Agent.pptx')
        download = card.get_by_role('button', name='Download original')
        expect(download).to_be_visible()
        box = download.bounding_box()
        assert box and box['width'] >= 44 and box['height'] >= 28
        # Card content is inset from the panel edge, not flush against it.
        body = panel.locator('[data-art-body]').bounding_box()
        name = card.locator('.artifact-file-card-name').bounding_box()
        assert name['x'] - body['x'] >= 12
        preview = card.locator('.artifact-page-preview')
        expect(preview).to_have_attribute('data-state', 'ready', timeout=60_000)
        image = preview.locator('img.artifact-page-preview-image')
        page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=image.element_handle())
        link = preview.locator('a.artifact-page-preview-link')
        expect(link).to_have_count(1)
        expect(link).to_have_attribute('href', _LINK)
        link_box = link.bounding_box()
        stage_box = preview.locator('.artifact-page-preview-stage').bounding_box()
        assert link_box['width'] > 0 and link_box['height'] > 0
        assert stage_box['x'] <= link_box['x'] and link_box['x'] + link_box['width'] <= stage_box['x'] + stage_box['width'] + 1
        expect(preview.locator('.artifact-page-preview-indicator')).to_have_text('1 / 2')
        preview.get_by_role('button', name='Next page').click()
        expect(preview.locator('.artifact-page-preview-indicator')).to_have_text('2 / 2')
        expect(preview).to_have_attribute('data-state', 'ready', timeout=60_000)
        with page.expect_download() as pending:
            download.click()
        assert pending.value.suggested_filename == '实习岗位_LLM_Agent.pptx'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shots = os.environ.get('TOFU_VISUAL_SHOT_DIR') or str(tmp_path)
        panel.screenshot(path=os.path.join(shots, f'published-pptx-card-{width}.png'))
    finally:
        task['aborted'] = True
        _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')
