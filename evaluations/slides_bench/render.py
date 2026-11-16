"""One renderer for every arm: LibreOffice headless PPTX → PDF → per-slide PNG.

LibreOffice is the shared native-PPTX renderer, so the judge sees each arm's
exported file the way an office application draws it rather than each tool's
own preview. ``SLIDES_BENCH_SOFFICE`` names the binary (the benchmark uses an
extracted LibreOffice AppImage under the private data root).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

_CONVERT_DEADLINE_S = 600
_PNG_WIDTH_PX = 1600


def soffice() -> str:
    explicit = os.environ.get('SLIDES_BENCH_SOFFICE')
    if explicit:
        return explicit
    from evaluations.slides_bench.corpus import data_root
    return os.path.join(data_root(), 'tools', 'squashfs-root', 'opt',
                        'libreoffice25.8', 'program', 'soffice')


def _environment() -> dict:
    """Shared fonts for every arm: Noto Sans CJK SC plus Tofu's CJK faces.

    The host has no CJK system fonts, so without this every Chinese glyph
    renders as tofu boxes regardless of the deck that declared it.
    """
    from evaluations.slides_bench.corpus import data_root
    env = dict(os.environ)
    config = os.path.join(data_root(), 'tools', 'fonts.conf')
    if os.path.isfile(config):
        env['FONTCONFIG_FILE'] = config
    return env


def render(pptx: str, out_dir: str) -> list[str]:
    import fitz
    os.makedirs(out_dir, exist_ok=True)
    for stale in glob.glob(os.path.join(out_dir, 'slide-*.png')):
        os.remove(stale)
    with tempfile.TemporaryDirectory() as work:
        profile = os.path.join(work, 'profile')
        subprocess.run(
            [soffice(), f'-env:UserInstallation=file://{profile}',
             '--headless', '--convert-to', 'pdf', '--outdir', work, pptx],
            check=True, timeout=_CONVERT_DEADLINE_S, env=_environment(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdfs = glob.glob(os.path.join(work, '*.pdf'))
        if not pdfs:
            raise RuntimeError(f'LibreOffice produced no PDF for {pptx}')
        shutil.copy(pdfs[0], os.path.join(out_dir, 'deck.pdf'))
        doc = fitz.open(pdfs[0])
        paths = []
        for index, page in enumerate(doc):
            zoom = _PNG_WIDTH_PX / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            path = os.path.join(out_dir, f'slide-{index + 1:02d}.png')
            pix.save(path)
            paths.append(path)
    return paths


if __name__ == '__main__':
    print('\n'.join(render(sys.argv[1], sys.argv[2])))
