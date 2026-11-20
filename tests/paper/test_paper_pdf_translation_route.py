"""HTTP contract for full-document translation through translate_pdf.

The route resolves an owner-scoped stored PDF and submits it to the pipeline
with an injected translator. These tests use the pipeline's deterministic
offline fake translator and a synthetic PDF; no model, network or user file is
involved.
"""

from __future__ import annotations

import asyncio

import pymupdf
import pytest

from lib.paper.builds.pdf_translation import _fake_translator
from tests.pdf_parser.test_pdf_render_pipeline import _single_column_source

pytestmark = pytest.mark.unit


@pytest.fixture
def route():
    from routes.paper_pkg import _pdf_translation

    return _pdf_translation


@pytest.fixture
def app(route):
    from quart import Quart, g
    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from routes.api_v1.paper import api_v1_paper_bp

    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True

    @application.before_request
    def principal_fixture():
        g.auth_ctx = AuthContext(
            key_id='fixture', owner_user_id=7, scopes=frozenset({'paper'}))

    application.register_blueprint(api_v1_paper_bp)
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        api_v1_paper_bp, name='api_v1_paper_v4_fixture')))
    return application


def _run(coroutine):
    return asyncio.run(coroutine)


def test_status_reports_unconfigured_transport(app, route, monkeypatch):
    monkeypatch.setattr(route, '_pdf_translation_configured', lambda: False)

    async def exercise():
        response = await app.test_client().get(
            '/api/v1/paper/translate/pdf/status')
        return response, await response.get_json()

    response, body = _run(exercise())
    assert response.status_code == 200
    assert body['ok'] is True
    assert body['configured'] is False
    assert body['reason'] == 'no_text_model_transport'


def test_post_refuses_without_a_transport(app, route, monkeypatch):
    monkeypatch.setattr(route, '_resolve_pdf_translator', lambda owner: None)

    async def exercise():
        response = await app.test_client().post(
            '/api/v1/paper/translate/pdf',
            json={'paper_id': 'paper-1', 'paper_hash': 'hash-1',
                  'target_language': 'zh'})
        return response, await response.get_json()

    response, body = _run(exercise())
    assert response.status_code == 409
    assert body['ok'] is False
    assert 'No translation transport is configured' in body['error']
    assert 'cannot be translated' in body['error']


def test_post_returns_the_pipeline_artifact_for_the_offline_translator(
        app, route, monkeypatch):
    source = _single_column_source()
    prompts = []
    fake = _fake_translator('identity')

    def translate(prompt: str) -> str:
        prompts.append(prompt)
        return fake(prompt)

    monkeypatch.setattr(route, '_resolve_pdf_translator', lambda owner: translate)
    monkeypatch.setattr(
        route, '_load_owned_pdf',
        lambda owner, paper_id, paper_hash: source)

    async def exercise():
        response = await app.test_client().post(
            '/api/v1/paper/translate/pdf',
            json={'paper_id': 'paper-1', 'paper_hash': 'hash-1',
                  'target_language': 'zh'})
        return response, await response.get_data()

    response, payload = _run(exercise())
    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('application/pdf')
    assert response.headers['X-Tofu-Translation-Gate'] == 'pass'
    assert response.headers['X-Tofu-Translation-Pages'] == '1'
    assert payload.startswith(b'%PDF')
    assert prompts and all('<source_text>' in prompt for prompt in prompts)

    document = pymupdf.open(stream=payload, filetype='pdf')
    try:
        assert document.page_count == 1
        assert 'quick brown fox' in document[0].get_text()
    finally:
        document.close()


def test_v4_projection_keeps_status_json_and_pdf_bytes_raw(
        app, route, monkeypatch):
    source = _single_column_source()
    monkeypatch.setattr(route, '_pdf_translation_configured', lambda: True)
    monkeypatch.setattr(
        route, '_resolve_pdf_translator', lambda owner: _fake_translator('identity'))
    monkeypatch.setattr(
        route, '_load_owned_pdf', lambda owner, paper_id, paper_hash: source)

    async def exercise():
        client = app.test_client()
        status = await client.get('/api/v4/paper/translate/pdf/status')
        translated = await client.post(
            '/api/v4/paper/translate/pdf',
            json={'paper_id': 'paper-1', 'paper_hash': 'hash-1',
                  'target_language': 'zh'})
        return status, await status.get_json(), translated, await translated.get_data()

    status, body, translated, payload = _run(exercise())
    assert status.status_code == 200
    assert body['data']['configured'] is True
    assert translated.status_code == 200
    assert translated.headers['Content-Type'].startswith('application/pdf')
    assert payload.startswith(b'%PDF')


def test_post_reports_a_missing_owner_pdf(app, route, monkeypatch):
    monkeypatch.setattr(
        route, '_resolve_pdf_translator', lambda owner: _fake_translator('identity'))
    monkeypatch.setattr(
        route, '_load_owned_pdf', lambda owner, paper_id, paper_hash: None)

    async def exercise():
        response = await app.test_client().post(
            '/api/v1/paper/translate/pdf',
            json={'paper_id': 'missing', 'paper_hash': '',
                  'target_language': 'zh'})
        return response, await response.get_json()

    response, body = _run(exercise())
    assert response.status_code == 404
    assert body['ok'] is False
    assert 'Stored PDF not found' in body['error']
