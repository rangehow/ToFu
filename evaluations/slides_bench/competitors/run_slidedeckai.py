"""SlideDeck AI arm (barun-saha/slide-deck-ai 8.2.4) driven headlessly.

Usage: python run_slidedeckai.py <input.md> <out.pptx> <lang> <pages>
(run with the tool's own isolated venv; ``OPENAI_API_BASE`` points at the
loopback relay ``evaluations.slides_bench.openai_proxy``).

The tool's own flow is used unchanged: its initial prompt template, its
JSON → python-pptx renderer and its default template (index 0). The document
enters through the tool's designed document channel (``additional_info``,
which normally carries extracted PDF text). Benchmark substitutions:
- ``streamlit`` is stubbed (imported only for an upload type annotation);
- the icon *fallback* matcher uses icon-name word overlap instead of a
  TinyBERT embedding (avoids installing torch); exact icon names still match;
- Pexels image search has no key, so the tool's own no-image path runs.
"""

import os
import pathlib
import re
import shutil
import sys
import types

sys.modules['streamlit'] = types.SimpleNamespace(
    runtime=types.SimpleNamespace(uploaded_file_manager=types.SimpleNamespace(
        UploadedFile=object)))
_ice = types.ModuleType('slidedeckai.helpers.icons_embeddings')


def _find_icons(keywords):
    from slidedeckai.global_config import GlobalConfig
    names = [p.stem for p in GlobalConfig.ICONS_DIR.glob('*.png')]
    out = []
    for keyword in keywords:
        words = set(re.split(r'[-_\s]+', keyword.lower()))
        out.append(max(names, key=lambda n: len(words & set(n.split('-')))))
    return out


_ice.find_icons = _find_icons
sys.modules['slidedeckai.helpers.icons_embeddings'] = _ice

from slidedeckai import core  # noqa: E402
from slidedeckai.global_config import GlobalConfig  # noqa: E402


def main(src: str, out: str, lang: str, pages: str) -> None:
    model = os.environ.get('SLIDES_BENCH_MODEL', 'gpt-5.5')
    key = f'[oa]{model}'
    GlobalConfig.VALID_MODELS[key] = {
        'description': 'benchmark', 'max_new_tokens': 16384, 'paid': True}
    text = pathlib.Path(src).read_text(encoding='utf-8')
    title = text.splitlines()[0].lstrip('# ').strip()
    language = 'Chinese' if lang == 'zh' else 'English'
    topic = (f'{title}. Create {pages} slides in {language} from the '
             'attached document.')
    core.filem.get_pdf_contents = lambda *_args, **_kwargs: text
    deck = core.SlideDeckAI(model=key, topic=topic, api_key='tofu-local',
                            pdf_path_or_stream=src)
    path = deck.generate()
    if not path:
        raise SystemExit('SlideDeck AI produced no deck')
    shutil.move(str(path), out)


if __name__ == '__main__':
    main(*sys.argv[1:5])
