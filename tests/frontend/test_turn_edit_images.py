"""Editing a turn keeps its submitted images loadable and durable.

Regression: the inline editor painted snapshot previews raw ('/api/images/x'),
which 404s behind a reverse-proxy BASE_PATH, and saved lazily projected images
back verbatim -- without bytes and fenced to a projection revision the edit
itself bumps, so the image could never load again.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

STORE = runtime_section_path('main/conversation_turn_store.js')
HELPERS = ('_turnImageDisplayUrl', '_isLazyTurnImagePreview', '_blobToBase64',
           '_durableEditedImages')


def _shipped_helpers() -> str:
    source = open(STORE, encoding='utf-8').read()
    chunks = []
    for name in HELPERS:
        match = re.search(rf'^(?:async )?function {name}\(.*?^}}\n', source,
                          re.M | re.S)
        assert match, name
        chunks.append(match.group(0))
    return '\n'.join(chunks)


def _run(scenario: str) -> dict:
    script = r"""
const vm = require('vm');
const reads = [], uploads = [];
const context = vm.createContext({
  console, Promise,
  apiUrl: (path) => '/proxy/15000' + path,
  FileReader: class {
    readAsDataURL(blob) { this.result = 'data:' + blob.type + ';base64,' + blob.b64; this.onload(); }
  },
  Api: { images: {
    async readTurnImage(link) {
      reads.push(link);
      if (link.includes('missing')) return { ok: false, status: 409 };
      return { ok: true, async blob() { return { type: 'image/jpeg', b64: 'QUJD' }; } };
    },
    async upload(payload) { uploads.push(payload); return { url: '/api/images/rehomed.jpg' }; },
  } },
});
vm.runInContext(HELPERS, context);
(async () => {
  const out = await vm.runInContext('(async () => {' + SCENARIO + '})()', context);
  console.log(JSON.stringify({ ...out, reads, uploads }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""".replace('HELPERS', json.dumps(_shipped_helpers())).replace(
        'SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', script], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_editor_thumbnails_resolve_through_the_base_path():
    out = _run("""
return {
  uploaded: _turnImageDisplayUrl('/api/images/a.png'),
  inline: _turnImageDisplayUrl('data:image/png;base64,AAAA'),
};""")
    assert out['uploaded'] == '/proxy/15000/api/images/a.png'
    assert out['inline'] == 'data:image/png;base64,AAAA'


def test_saving_rehomes_lazy_images_and_keeps_durable_ones():
    lazy = ('/api/v4/conversations/c/turns/t/images/0'
            '?projectionRevision=3&ownerScope=s')
    out = _run(f"""
const kept = {{ url: '/api/images/a.png', preview: '/api/images/a.png' }};
const inline = {{ base64: 'QQ==', mediaType: 'image/png', preview: 'data:image/png;base64,QQ==' }};
const lazy = {{ mediaType: 'image/png', preview: '{lazy}' }};
const saved = await _durableEditedImages([kept, inline, lazy]);
return {{ keptSame: saved[0] === kept, inlineSame: saved[1] === inline, rehomed: saved[2] }};""")
    assert out['keptSame'] and out['inlineSame']
    assert out['reads'] == [lazy]
    assert out['uploads'] == [{'base64': 'QUJD', 'mediaType': 'image/jpeg'}]
    assert out['rehomed'] == {'mediaType': 'image/jpeg',
                              'url': '/api/images/rehomed.jpg',
                              'preview': '/api/images/rehomed.jpg'}


def test_unreadable_lazy_image_fails_the_save_instead_of_storing_a_dead_link():
    out = _run("""
try {
  await _durableEditedImages([{ preview: '/api/v4/conversations/c/turns/t/images/0?missing=1' }]);
  return { failed: false };
} catch (error) { return { failed: true, message: error.message }; }""")
    assert out['failed'] is True
    assert '409' in out['message']
    assert out['uploads'] == []
