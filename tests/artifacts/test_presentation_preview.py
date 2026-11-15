"""Published .pptx attachments expose owner-checked page previews."""
import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from quart import Quart, g, request

from lib import response_assets
from lib.artifacts import response_files as service
from routes.api_v1.artifacts import api_v1_artifacts_bp

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._artifact_sidecar',)

_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


@pytest.fixture
def app(artifact_storage, monkeypatch):
    live = {'owned': 7}
    monkeypatch.setattr(response_assets, 'get_conversation', lambda conv, user_id, **kw:
                        {'id': conv} if live.get(conv) == user_id else None)
    app = Quart(__name__)
    app.register_blueprint(api_v1_artifacts_bp)

    @app.before_request
    async def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner),
                                         key_id='test-owner', tenant_id=None, scopes=[])
    return app


def _pptx(slides):
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(960), Pt(540)
    for _ in range(slides):
        prs.slides.add_slide(prs.slide_layouts[6])
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def _publish(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return service.publish_file(str(path), '', conv_id='owned', user_id=7, roots=[str(tmp_path)])


@pytest.fixture
def rendered(monkeypatch):
    calls = []

    def render(path, index, out_png, **_kwargs):
        calls.append(index)
        with open(out_png, 'wb') as handle:
            handle.write(_PNG)
        return {'width': 960.0, 'height': 540.0, 'links': [
            {'href': 'https://jobs.example.com/1', 'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.05},
            {'href': 'javascript:alert(1)', 'x': 0.1, 'y': 0.3, 'w': 0.3, 'h': 0.05},
            {'href': 'mailto:hr@example.com', 'x': -0.5, 'y': 0.9, 'w': 2.0, 'h': 0.05},
        ]}
    monkeypatch.setattr('lib.slides.api.render_pptx_page', render)
    return calls


def test_owner_reads_page_geometry_and_rendered_page(app, tmp_path, rendered):
    deck = _publish(tmp_path, 'deck.pptx', _pptx(3))
    h7 = {'X-Test-Owner': '7'}

    async def exercise():
        client = app.test_client()
        pages = await client.get(f'/api/v1/artifacts/{deck["id"]}/pages', headers=h7)
        assert pages.status_code == 200
        data = await pages.get_json()
        assert data.pop('ok') is True
        assert data == {'version': 1, 'artifactId': deck['id'], 'sha256': deck['sha256'],
                        'pageCount': 3, 'width': 960.0, 'height': 540.0}
        page = await client.get(f'/api/v1/artifacts/{deck["id"]}/pages/2', headers=h7)
        assert page.status_code == 200
        body = await page.get_json()
        assert body['index'] == 2 and body['pageCount'] == 3
        assert body['image'] == 'data:image/png;base64,' + base64.b64encode(_PNG).decode('ascii')
        assert body['links'] == [
            {'href': 'https://jobs.example.com/1', 'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.05},
            {'href': 'mailto:hr@example.com', 'x': 0.0, 'y': 0.9, 'w': 1.0, 'h': 0.05},
        ]
    asyncio.run(exercise())
    assert rendered == [2]


def test_other_owner_anonymous_bad_index_and_non_presentation(app, tmp_path, rendered):
    deck = _publish(tmp_path, 'deck.pptx', _pptx(2))
    pdf = _publish(tmp_path, 'receipt.pdf', b'%PDF-1.7\x00\xff%%EOF')

    async def exercise():
        client = app.test_client()
        for url in (f'/api/v1/artifacts/{deck["id"]}/pages', f'/api/v1/artifacts/{deck["id"]}/pages/0'):
            assert (await client.get(url)).status_code == 401
            assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        h7 = {'X-Test-Owner': '7'}
        assert (await client.get(f'/api/v1/artifacts/{deck["id"]}/pages/2', headers=h7)).status_code == 404
        unsupported = await client.get(f'/api/v1/artifacts/{pdf["id"]}/pages', headers=h7)
        assert unsupported.status_code == 400
        assert b'preview_unsupported' in await unsupported.get_data()
        assert (await client.get(f'/api/v1/artifacts/{pdf["id"]}/pages/0', headers=h7)).status_code == 400
    asyncio.run(exercise())
    assert rendered == []


def test_render_failure_is_a_typed_unavailable_error(app, tmp_path, monkeypatch):
    deck = _publish(tmp_path, 'deck.pptx', _pptx(1))

    def broken(*_args, **_kwargs):
        raise RuntimeError('chromium missing')
    monkeypatch.setattr('lib.slides.api.render_pptx_page', broken)

    async def exercise():
        response = await app.test_client().get(f'/api/v1/artifacts/{deck["id"]}/pages/0',
                                               headers={'X-Test-Owner': '7'})
        assert response.status_code == 503
        body = await response.get_data()
        assert b'preview_render_failed' in body and b'chromium missing' not in body
    asyncio.run(exercise())


def test_an_index_error_while_rendering_is_a_render_failure_not_a_missing_page(
        app, tmp_path, monkeypatch):
    """Only the caller's page index past the deck is a 404; an IndexError inside the
    renderer is a server fault like any other render failure."""
    deck = _publish(tmp_path, 'deck.pptx', _pptx(2))

    def broken(*_args, **_kwargs):
        raise IndexError('list index out of range')
    monkeypatch.setattr('lib.slides.api.render_pptx_page', broken)

    async def exercise():
        client, h7 = app.test_client(), {'X-Test-Owner': '7'}
        past = await client.get(f'/api/v1/artifacts/{deck["id"]}/pages/2', headers=h7)
        assert past.status_code == 404 and b'not_found' in await past.get_data()
        response = await client.get(f'/api/v1/artifacts/{deck["id"]}/pages/1', headers=h7)
        assert response.status_code == 503
        body = await response.get_data()
        assert b'preview_render_failed' in body and b'list index' not in body
    asyncio.run(exercise())


def test_an_index_error_reading_the_attachment_is_a_server_fault(app, tmp_path, monkeypatch):
    deck = _publish(tmp_path, 'deck.pptx', _pptx(2))

    def corrupt(*_args, **_kwargs):
        raise IndexError('attachment index row 3 is corrupt')
    monkeypatch.setattr(service, 'load_file', corrupt)

    async def exercise():
        response = await app.test_client().get(f'/api/v1/artifacts/{deck["id"]}/pages/0',
                                               headers={'X-Test-Owner': '7'})
        assert response.status_code == 500
        assert b'row 3 is corrupt' not in await response.get_data()
    asyncio.run(exercise())


@pytest.mark.slow
def test_real_render_through_route_returns_png_and_clickable_link(app, tmp_path):
    pytest.importorskip('playwright')
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Pt(960), Pt(540)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    run = slide.shapes.add_textbox(Pt(40), Pt(40), Pt(400), Pt(60)).text_frame.paragraphs[0].add_run()
    run.text = 'Seed 大模型实习'
    run.font.size = Pt(24)
    run.hyperlink.address = 'https://jobs.bytedance.com/campus/position/1/detail'
    out = io.BytesIO()
    prs.save(out)
    deck = _publish(tmp_path, 'real.pptx', out.getvalue())

    async def exercise():
        response = await app.test_client().get(f'/api/v1/artifacts/{deck["id"]}/pages/0',
                                               headers={'X-Test-Owner': '7'})
        assert response.status_code == 200
        return await response.get_json()
    body = asyncio.run(exercise())
    png = base64.b64decode(body['image'].split(',', 1)[1])
    assert png[:8] == b'\x89PNG\r\n\x1a\n' and len(png) > 1000
    [link] = body['links']
    assert link['href'] == 'https://jobs.bytedance.com/campus/position/1/detail'
    assert 0.03 < link['x'] < 0.1 and 0.05 < link['y'] < 0.2 and link['w'] > 0.1
