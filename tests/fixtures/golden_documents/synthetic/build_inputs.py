"""Provenance of the synthetic golden-document inputs (not run by the corpus).

The committed bytes under ``tests/fixtures/golden_documents/inputs/synthetic``
are the corpus; this script records how they were made so a reviewer can see
which current test each one mirrors. python-pptx and python-docx stamp ZIP
times, so rerunning it produces different bytes: re-freeze the corpus
(``scripts/golden_documents.py freeze``) whenever an input is regenerated.

Usage: .venv/bin/python3 tests/fixtures/golden_documents/synthetic/build_inputs.py
"""

from __future__ import annotations

import io
import os
import random
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'inputs', 'synthetic')


def _png(width, height, seed):
    from PIL import Image
    pixels = random.Random(seed).randbytes(width * height * 3)
    image = Image.frombytes('RGB', (width, height), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _gradient_png(width, height):
    from PIL import Image
    image = Image.new('RGB', (width, height))
    image.putdata([(x * 255 // width, y * 255 // height, 160)
                   for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def build_pdf_pages(path):
    """tests/pdf_parser/test_pdf_classic_bounds.py page/text bounds."""
    import pymupdf
    document = pymupdf.open()
    for n in range(1, 6):
        page = document.new_page()
        page.insert_text((72, 72), f'page {n} label for bounded extraction')
        page.insert_text((72, 100), 'lorem ipsum dolor sit amet ' * 3)
    document.save(path, deflate=True, garbage=4)


def build_pdf_figures(path):
    """_figure_pdf (classic bounds) + table_document (figure/table demand)."""
    import pymupdf
    document = pymupdf.open()
    figure = _png(256, 128, 42)
    for i in range(3):
        page = document.new_page(width=400, height=500)
        page.insert_image(pymupdf.Rect(40, 40, 296, 168), stream=figure)
        page.insert_text((40, 195), f'Figure {i + 1}: selected source diagram')
    for caption in ('above', 'below', 'none'):
        page = document.new_page(width=400, height=500)
        if caption == 'above':
            page.insert_text((40, 65), 'Table 1. Measured results')
        elif caption == 'below':
            page.insert_text((40, 235), 'Table 1. Measured results')
        else:
            page.insert_text((40, 65), 'Ordinary page without a caption')
        for x in (40, 130, 220, 310):
            page.draw_line((x, 90), (x, 210))
        for y in (90, 120, 150, 180, 210):
            page.draw_line((40, y), (310, y))
        for row in range(4):
            for col in range(3):
                page.insert_text((45 + col * 90, 110 + row * 30), f'cell {row}:{col}',
                                 fontsize=8)
    page = document.new_page(width=400, height=500)
    page.insert_image(pymupdf.Rect(40, 40, 360, 300), stream=_gradient_png(320, 260))
    page.insert_text((40, 330), 'Figure 4: figure-only page with a gradient')
    document.save(path, deflate=True, garbage=4)


def build_docx(path):
    """Truncation honesty + source demand: headings, tables, CJK, long body."""
    import docx
    document = docx.Document()
    document.add_heading('Golden corpus 文档 extraction', level=1)
    document.add_paragraph('混合中文与 English 的段落：表格、标题与长正文。')
    table = document.add_table(rows=4, cols=3)
    for r, row in enumerate([['方案', '违约率', 'Notes'], ['A', '2.8', 'baseline'],
                             ['B', '', 'sparse cell'], ['C', '3.1', '合并前']]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    table.cell(3, 1).merge(table.cell(3, 2))
    document.add_heading('Long body', level=2)
    for n in range(400):
        document.add_paragraph(f'{n:03d} lorem ipsum dolor sit amet ' * 6)
    document.save(path)


def build_pptx(path):
    """tests/slides/test_import_pptx_fidelity.py _deck plus charts and pictures."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for n in range(3):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(8), Inches(1))
        p = box.text_frame.paragraphs[0]
        hostile = p.add_run()
        hostile.text = f'slide {n + 1} <iframe src="file:///etc/passwd"></iframe> A&B'
        safe = p.add_run()
        safe.text = 'safe link'
        safe.hyperlink.address = 'https://example.com/a?x=1&y=2'
        script = p.add_run()
        script.text = 'script link'
        script.hyperlink.address = 'javascript:alert(1)'
        table = slide.shapes.add_table(2, 3, Inches(0.5), Inches(2), Inches(6), Inches(1)).table
        for i, width in enumerate((1, 3, 2)):
            table.columns[i].width = Inches(width)
        table.rows[0].height, table.rows[1].height = Inches(0.25), Inches(0.75)
        cell = table.cell(1, 1)
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor(0xF3, 0xF5, 0xF8)
        run = cell.text_frame.paragraphs[0].add_run()
        run.text = 'Seed <b>intern</b>'
        run.font.size = Pt(9)
        run.hyperlink.address = 'https://jobs.example.com/p/1'
        table.cell(0, 0).text = '方案'
        table.cell(0, 1).text = '违约率'
    slide = prs.slides[1]
    data = CategoryChartData()
    data.categories = ['Q1', 'Q2', 'Q3']
    data.add_series('收入', (1.5, 2.25, 3.0))
    data.add_series('Cost', (1.0, 1.25, 1.75))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(7), Inches(2),
                           Inches(5), Inches(3.5), data)
    slide = prs.slides[2]
    pie = CategoryChartData()
    pie.categories = ['A', 'B', 'C']
    pie.add_series('Share', (50, 30, 20))
    slide.shapes.add_chart(XL_CHART_TYPE.PIE, Inches(7), Inches(2), Inches(4), Inches(3), pie)
    slide.shapes.add_picture(io.BytesIO(_gradient_png(160, 90)), Inches(0.5), Inches(4),
                             Inches(3.2), Inches(1.8))
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(4), Inches(4),
                                   Inches(2), Inches(1))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xD2, 0x60, 0x2A)
    shape.text_frame.text = '圆角 callout'
    empty = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(4), Inches(5.5), Inches(1), Inches(1))
    empty.fill.solid()
    empty.fill.fore_color.rgb = RGBColor(0x17, 0x32, 0x4D)
    prs.save(path)


def build_pptx_long(path):
    """test_pptx_truncation_states_the_deck_size (200 slides, budgeted)."""
    from pptx import Presentation
    prs = Presentation()
    for i in range(200):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f'Slide {i} ' + 'content ' * 50
    prs.save(path)


def _text(element_id, bounds, html, **content):
    return {'elementId': element_id, 'elementType': 'text', 'bounds': bounds,
            'content': {'text': html, **content}}


def build_pptd(root):
    """PPTD feature tour for export, HTML and PNG rendering paths."""
    os.makedirs(os.path.join(root, 'pages'), exist_ok=True)
    os.makedirs(os.path.join(root, 'media'), exist_ok=True)
    with open(os.path.join(root, 'media', 'gradient.png'), 'wb') as handle:
        handle.write(_gradient_png(320, 180))
    manifest = {
        'version': 'v2', 'title': 'Golden PPTD 特性巡览', 'size': [1280, 720],
        'theme': {
            'colors': {'bg': '#FBFAF7', 'ink': '#1C232B', 'primary': '#17324D',
                       'accent': '#D2602A', 'muted': '#5E6B78', 'hairline': '#D9DEE3'},
            'textStyles': {
                'title': {'fontSize': 32, 'color': '$ink', 'bold': True,
                          'fontFamily': 'MiSans', 'lineHeight': 1.25},
                'body': {'fontSize': 18, 'color': '$ink', 'fontFamily': 'Noto Sans SC',
                         'lineHeight': 1.45},
                'caption': {'fontSize': 13, 'color': '$muted', 'fontFamily': 'Noto Sans SC'},
            },
        },
        'pages': ['pages/01.page', 'pages/02.page', 'pages/03.page'],
    }
    cover = {'pageType': 'cover', 'background': {'type': 'solid', 'color': '$bg'}, 'elements': [
        _text('title', [80, 80, 1120, 110],
              '<p>同一模型只换 harness：<b>golden</b> corpus 封面</p>', style='$title'),
        _text('body', [80, 220, 700, 200],
              '<p>中文正文与 <i>English</i> 混排。</p><ul><li>第一点</li><li>second point</li></ul>',
              style='$body', fit='shrink', wrap=True),
        {'elementId': 'card', 'elementType': 'shape', 'bounds': [820, 220, 380, 220],
         'shapeName': 'roundRect',
         'fill': {'type': 'gradient', 'angle': 90,
                  'stops': [{'color': '$primary', 'position': 0},
                            {'color': '$accent', 'position': 1}]},
         'shadow': {'blur': 12, 'offset': [0, 4], 'color': '#00000033'},
         'border': {'style': 'solid', 'width': 2, 'color': '$hairline'}},
        {'elementId': 'rule', 'elementType': 'line', 'bounds': [80, 470, 1120, 20],
         'viewBox': [1120, 20], 'points': '0,10 1120,10', 'curve': 'sharp',
         'arrow': [None, 'triangle'],
         'border': {'style': 'solid', 'width': 2, 'color': '$accent'}},
        {'elementId': 'figure', 'elementType': 'image', 'bounds': [80, 510, 320, 180],
         'src': 'media/gradient.png', 'fit': {'mode': 'cover'}},
    ]}
    table_page = {'pageType': 'content', 'background': {'type': 'solid', 'color': '#FFFFFF'},
                  'elements': [
        _text('title', [64, 40, 1152, 70], '<p>表格：方案对比</p>', style='$title'),
        {'elementId': 'cells', 'elementType': 'table', 'bounds': [64, 140, 1152, 400],
         'columnWidths': [0.3, 0.3, 0.4], 'rowHeights': [0.25, 0.25, 0.25, 0.25],
         'rows': [['方案', '违约率', 'Notes'], ['A', 2.8, 'baseline'],
                  [{'text': 'B', 'bold': True}, {'text': '3.1'}, {'text': '合并 <b>rich</b>'}],
                  ['C', '', 'sparse']]},
        _text('source', [64, 660, 1152, 30], '<p>来源：synthetic golden corpus</p>',
              style='$caption'),
    ]}
    chart_page = {'pageType': 'content', 'background': {'type': 'solid', 'color': '$bg'},
                  'elements': [
        _text('title', [64, 40, 1152, 70], '<p>图表：column / line / pie</p>', style='$title'),
        {'elementId': 'column', 'elementType': 'chart', 'bounds': [64, 140, 560, 260],
         'chartType': 'column', 'data': {'categories': ['Q1', 'Q2', 'Q3'],
                                         'series': [{'name': '收入', 'values': [1.5, 2.25, 3]},
                                                    {'name': 'Cost', 'values': [1, 1.25, 1.75]}]}},
        {'elementId': 'trend', 'elementType': 'chart', 'bounds': [656, 140, 560, 260],
         'chartType': 'line', 'data': {'categories': ['1月', '2月', '3月', '4月'],
                                       'series': [{'name': 'Users', 'values': [3, 5, 4, 8]}]}},
        {'elementId': 'share', 'elementType': 'chart', 'bounds': [64, 420, 560, 260],
         'chartType': 'pie', 'data': {'categories': ['A', 'B', 'C'],
                                      'series': [{'name': 'Share', 'values': [50, 30, 20]}]}},
    ]}
    for name, page in (('01', cover), ('02', table_page), ('03', chart_page)):
        with open(os.path.join(root, 'pages', f'{name}.page'), 'w', encoding='utf-8') as handle:
            yaml.safe_dump(page, handle, allow_unicode=True, sort_keys=False)
    with open(os.path.join(root, 'deck.pptd'), 'w', encoding='utf-8') as handle:
        yaml.safe_dump(manifest, handle, allow_unicode=True, sort_keys=False)


def main():
    os.makedirs(ROOT, exist_ok=True)
    build_pdf_pages(os.path.join(ROOT, 'pdf_bounded_pages.pdf'))
    build_pdf_figures(os.path.join(ROOT, 'pdf_figures_tables.pdf'))
    build_docx(os.path.join(ROOT, 'docx_tables_cjk_long.docx'))
    build_pptx(os.path.join(ROOT, 'pptx_import_fidelity.pptx'))
    build_pptx_long(os.path.join(ROOT, 'pptx_200_slides.pptx'))
    build_pptd(os.path.join(ROOT, 'pptd_feature_tour'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
