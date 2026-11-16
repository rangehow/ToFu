"""PPTAgent arm (icip-cas/PPTAgent, PyPI pptagent 1.1.38) driven headlessly.

Usage: python run_pptagent.py <input.md> <out.pptx> <lang> <pages>
(run with the tool's own isolated venv; ``OPENAI_API_BASE`` points at the
loopback relay ``evaluations.slides_bench.openai_proxy``).

Uses PPTAgent's own template-based pipeline (the EMNLP 2025 "PPTAgent"
Stage II generator in ``pptagent.pptgen.PPTAgent``) unchanged: its markdown
document parser (``Document.from_markdown``), its outline/layout/editor/coder
roles and its bundled ``default`` template with the shipped slide induction,
exactly as the package's own MCP server wires it. The same model serves as
language and vision model.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import mistune
from pptagent.document import Document
from pptagent.llms import AsyncLLM
from pptagent.pptgen import PPTAgent
from pptagent.presentation import Presentation
from pptagent.utils import Config, package_join


_PIPE_TABLE_RE = re.compile(r'(?:^\|.*\|[ \t]*\n)+', re.MULTILINE)


def _html_tables(markdown: str) -> str:
    """PPTAgent parses tables as HTML (its MinerU input format).

    Pipe tables are converted to HTML so its parser accepts the same document
    the other arms read; the text content is unchanged.
    """
    render = mistune.create_markdown(plugins=['table'])
    return _PIPE_TABLE_RE.sub(lambda m: render(m.group(0)) + '\n', markdown)


def _accept_pipe_tables() -> None:
    """Its document extractor may re-emit a table in pipe syntax; convert it."""
    from pptagent.document import element
    original = element.Table.parse

    def parse(self, image_dir):
        if '<table' not in (self.markdown_content or ''):
            self.markdown_content = mistune.create_markdown(
                plugins=['table'])(self.markdown_content or '')
        return original(self, image_dir)

    element.Table.parse = parse


async def main(src: str, out: str, _lang: str, pages: str) -> None:
    _accept_pipe_tables()
    model = AsyncLLM(os.environ.get('SLIDES_BENCH_MODEL', 'gpt-5.5'),
                     os.environ['OPENAI_API_BASE'], 'tofu-local')
    template = Path(package_join('templates')) / 'default'
    prs = Presentation.from_file(str(template / 'source.pptx'),
                                 Config(str(template)))
    induction = json.loads((template / 'slide_induction.json').read_text())
    agent = PPTAgent(language_model=model, vision_model=model)
    agent.set_reference(slide_induction=induction, presentation=prs)
    markdown = _html_tables(Path(src).read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as image_dir:
        doc = await Document.from_markdown(markdown, model, model, image_dir)
        result, _history = await agent.generate_pres(doc, int(pages))
    if result is None:
        raise SystemExit('PPTAgent produced no presentation')
    result.save(out)


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:5]))
