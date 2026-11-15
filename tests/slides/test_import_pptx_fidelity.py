"""PPTX import keeps what a page preview needs and never trusts document text.

The importer feeds both template replication and published-attachment page
previews; its rich text reaches headless Chrome, so escaping is a security
boundary, and tables/links are fidelity contracts.
"""
import http.server
import threading

import pytest
import yaml

pytestmark = pytest.mark.unit


def _deck(path, *, slides=1):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for n in range(slides):
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
    prs.save(str(path))
    return path


def _imported(tmp_path, **kwargs):
    from lib.slides.import_pptx import import_pptx
    report = import_pptx(str(_deck(tmp_path / 'in.pptx', slides=kwargs.pop('slides', 1))),
                         str(tmp_path / 'out'), **kwargs)
    pages = [yaml.safe_load((tmp_path / 'out' / name).read_text(encoding='utf-8'))
             for name in yaml.safe_load(open(report['manifest'], encoding='utf-8'))['pages']]
    return report, pages


def test_run_text_is_escaped_and_only_safe_links_survive(tmp_path):
    _report, [page] = _imported(tmp_path)
    text = next(el for el in page['elements'] if el['elementType'] == 'text')['content']['text']
    assert '<iframe' not in text and '&lt;iframe src="file:///etc/passwd"&gt;' in text
    assert 'A&amp;B' in text
    assert '<a href="https://example.com/a?x=1&amp;y=2">safe link</a>' in text
    assert 'javascript:' not in text and 'script link' in text


def test_table_keeps_geometry_cell_style_and_links(tmp_path):
    _report, [page] = _imported(tmp_path)
    table = next(el for el in page['elements'] if el['elementType'] == 'table')
    assert table['columnWidths'] == pytest.approx([1 / 6, 3 / 6, 2 / 6], abs=1e-3)
    assert table['rowHeights'] == pytest.approx([0.25, 0.75], abs=1e-3)
    cell = table['rows'][1][1]
    assert cell['fill'] == {'type': 'solid', 'color': '#F3F5F8'}
    assert cell['fontSize'] == 9
    assert cell['align'][0] == 'left'
    assert cell['text'] == ('<p><a href="https://jobs.example.com/p/1"><span style="font-size:9.0px">'
                            'Seed &lt;b&gt;intern&lt;/b&gt;</span></a></p>')


def test_rendered_page_html_carries_links_but_no_document_markup(tmp_path):
    from lib.slides.pptd import parse_deck
    from lib.slides.render_html import render_page_html
    report, _pages = _imported(tmp_path)
    deck = parse_deck(report['manifest'])
    html = render_page_html(deck, deck.pages[0])
    assert '<iframe' not in html and '<b>intern' not in html
    assert 'href="https://jobs.example.com/p/1"' in html
    assert 'javascript:' not in html


def test_slide_subset_imports_only_requested_pages(tmp_path):
    report, pages = _imported(tmp_path, slides=3, slide_numbers=[2])
    assert report['pages'] == 1
    text = next(el for el in pages[0]['elements'] if el['elementType'] == 'text')['content']['text']
    assert text.startswith('<p>slide 2 ')


@pytest.mark.slow
def test_untrusted_page_render_returns_link_boxes_and_never_reaches_network(tmp_path):
    pytest.importorskip('playwright')
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_page_png_with_links
    hits = []

    class Probe(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler name
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        report, pages = _imported(tmp_path)
        page_file = tmp_path / 'out' / 'pages' / '01.page'
        pages[0]['elements'].append({
            'elementId': 'probe', 'elementType': 'image', 'bounds': [0, 0, 10, 10],
            'src': f'http://127.0.0.1:{server.server_port}/leak'})
        page_file.write_text(yaml.safe_dump(pages[0], allow_unicode=True), encoding='utf-8')
        deck = parse_deck(report['manifest'])
        links = render_page_png_with_links(deck, 0, str(tmp_path / 'p.png'), scale=1.0)
    finally:
        server.shutdown()
        server.server_close()
    assert hits == []
    assert (tmp_path / 'p.png').read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'
    hrefs = {box['href'] for box in links}
    assert hrefs == {'https://example.com/a?x=1&y=2', 'https://jobs.example.com/p/1'}
    for box in links:
        assert 0 <= box['x'] < 1 and 0 <= box['y'] < 1 and 0 < box['w'] <= 1 and 0 < box['h'] <= 1


def test_plain_table_cells_stay_plain_text(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    from lib.slides.import_pptx import import_pptx
    prs = Presentation()
    table = prs.slides.add_slide(prs.slide_layouts[6]).shapes.add_table(
        1, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    table.cell(0, 0).text = 'R&D 指标'
    table.cell(0, 1).text = 'a<b'
    prs.save(str(tmp_path / 'plain.pptx'))
    report = import_pptx(str(tmp_path / 'plain.pptx'), str(tmp_path / 'out'))
    page = yaml.safe_load((tmp_path / 'out' / 'pages' / '01.page').read_text(encoding='utf-8'))
    [row] = next(el for el in page['elements'] if el['elementType'] == 'table')['rows']
    assert row[0]['text'] == 'R&D 指标'
    assert row[1]['text'] == '<p>a&lt;b</p>'
    assert report['pages'] == 1


def test_untrusted_preview_never_downloads_fonts(tmp_path, monkeypatch):
    import lib.design_sys.fonts as fonts
    import lib.slides.render_png as render_png
    from lib.slides.pptd import parse_deck
    from lib.slides.render_html import render_page_html
    report, _pages = _imported(tmp_path)
    deck = parse_deck(report['manifest'])
    downloads = []
    monkeypatch.setattr(fonts, 'ensure_font', lambda font_id, weight, *, download=True, **_kw:
                        downloads.append(download) or '')
    render_page_html(deck, deck.pages[0], fetch_fonts=False)
    assert downloads and not any(downloads)

    seen = {}

    def capture(_deck, _page, **kwargs):
        seen.update(kwargs)
        raise RuntimeError('stop before launching a browser')
    monkeypatch.setattr(render_png, 'render_page_html', capture)
    with pytest.raises(RuntimeError, match='stop before'):
        render_png.render_page_png_with_links(deck, 0, str(tmp_path / 'p.png'), scale=1.0)
    assert seen['fetch_fonts'] is False
    seen.clear()
    with pytest.raises(RuntimeError, match='stop before'):
        render_png.render_page_png(deck, 0, str(tmp_path / 'q.png'), scale=1.0)
    assert seen['fetch_fonts'] is True
