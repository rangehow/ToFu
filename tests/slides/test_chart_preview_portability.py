"""Charts retain every plotted category and the audited CJK label font."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

pytestmark = pytest.mark.unit


def _deck(tmp_path, *, width=640, height=340, categories=2, series=1,
          family='Noto Sans CJK SC', colors=None, background='#FFFFFF'):
    from lib.slides.pptd import parse_deck

    chart = {
        'elementId': 'evidence', 'elementType': 'chart',
        'bounds': [40, 40, width, height], 'chartType': 'bar',
        'options': {'legend': True, 'dataLabels': True},
        'data': {
            'categories': [f'条件{i}' for i in range(categories)],
            'series': [{'name': f'准确率{j}',
                        'values': [78 + i + j for i in range(categories)]}
                       for j in range(series)],
        },
    }
    (tmp_path / '01.page').write_text(yaml.safe_dump({
        'pageType': 'content', 'elements': [chart],
        'background': {'type': 'solid', 'color': background},
    }, allow_unicode=True), encoding='utf-8')
    manifest = tmp_path / 'deck.pptd'
    manifest.write_text(yaml.safe_dump({
        'version': 'v2', 'title': 'chart evidence', 'size': [1280, 720],
        'theme': {'textStyles': {'body': {'fontFamily': family}},
                  **({'colors': colors} if colors else {})},
        'pages': ['01.page'],
    }), encoding='utf-8')
    return parse_deck(str(manifest))


def _svg(deck, monkeypatch):
    from lib.slides import render_html

    monkeypatch.setattr(render_html, '_font_face_css', lambda _families, **_kwargs: '')
    html = render_html.render_page_html(deck, deck.pages[0])
    return ET.fromstring(re.search(r'<svg\b.*?</svg>', html, re.S).group())


@pytest.mark.parametrize('width,height', [(900, 240), (640, 340), (400, 600)])
@pytest.mark.parametrize('categories,series', [(2, 1), (4, 2)])
def test_horizontal_categories_and_series_remain_inside_plot(
        tmp_path, monkeypatch, width, height, categories, series):
    deck = _deck(tmp_path, width=width, height=height,
                 categories=categories, series=series)
    svg = _svg(deck, monkeypatch)
    # The first rectangles are legend swatches; remaining rectangles are data.
    bars = svg.findall('rect')[series:]
    assert len(bars) == categories * series
    for bar in bars:
        x, y, w, h = (float(bar.attrib[key]) for key in ('x', 'y', 'width', 'height'))
        assert w > 0 and h > 0
        assert 0 <= x < x + w <= width
        assert 30 <= y < y + h <= height
    for label in svg.findall('text'):
        assert 0 <= float(label.attrib['y']) <= height


def test_a_wide_legend_wraps_inside_the_chart_and_lines_label_categories(
        tmp_path, monkeypatch):
    deck = _deck(tmp_path, width=500, height=400, categories=5, series=7)
    chart = deck.pages[0].elements[0]
    chart['chartType'] = 'line'
    for series in chart['data']['series']:
        series['name'] = 'GPT-3.5-Turbo (16K) ' + series['name']
    svg = _svg(deck, monkeypatch)
    swatches = svg.findall('rect')
    assert len(swatches) == 7
    assert len({s.attrib['y'] for s in swatches}) > 1
    assert all(0 <= float(s.attrib['x']) < 500 for s in swatches)
    texts = [t.text for t in svg.findall('text')]
    for category in chart['data']['categories']:
        assert category in texts
    lowest_legend = max(float(s.attrib['y']) + 12 for s in swatches)
    for point in svg.findall('circle'):
        assert float(point.attrib['cy']) > lowest_legend


def test_bar_categories_label_their_bars_from_the_left(tmp_path, monkeypatch):
    deck = _deck(tmp_path, width=640, height=340, categories=3, series=1)
    deck.pages[0].elements[0]['options']['dataLabels'] = False
    svg = _svg(deck, monkeypatch)
    bars = svg.findall('rect')[1:]
    labels = [t for t in svg.findall('text') if t.text.startswith('条件')]
    assert len(labels) == 3
    left_of_bars = min(float(bar.attrib['x']) for bar in bars)
    for label in labels:
        assert label.attrib['text-anchor'] == 'end'
        assert float(label.attrib['x']) <= left_of_bars


def test_chart_preview_uses_canonical_body_font(tmp_path, monkeypatch):
    svg = _svg(_deck(tmp_path), monkeypatch)
    assert svg.attrib.get('font-family') == 'Noto Sans SC'


def test_chart_font_subset_includes_categories_series_and_axis_digits(tmp_path):
    from lib.slides.export_pptx import _collect_font_usage

    usage = _collect_font_usage(_deck(tmp_path))
    assert set(usage) == {'Noto Sans SC'}
    assert set('条件准确率0123456789.%−-') <= usage['Noto Sans SC']['regular']


def test_native_chart_binds_latin_and_cjk_typefaces(tmp_path):
    from lib.slides.export_pptx import export_pptx

    deck = _deck(tmp_path)
    output = tmp_path / 'chart.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    with zipfile.ZipFile(output) as archive:
        chart = ET.fromstring(archive.read('ppt/charts/chart1.xml'))
    namespaces = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                  'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart'}
    for script in ('latin', 'ea', 'cs'):
        face = chart.find(f'c:txPr/a:p/a:pPr/a:defRPr/a:{script}', namespaces)
        assert face is not None and face.attrib['typeface'] == 'Noto Sans SC'


def test_negative_bars_keep_their_sign_in_office_renderers(tmp_path):
    from lib.slides.export_pptx import export_pptx

    deck = _deck(tmp_path, categories=3)
    deck.pages[0].elements[0]['data']['series'][0]['values'][2] = -5
    output = tmp_path / 'chart.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    with zipfile.ZipFile(output) as archive:
        chart = ET.fromstring(archive.read('ppt/charts/chart1.xml'))
    c = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    invert = chart.find(f'.//{c}ser/{c}invertIfNegative')
    assert invert is not None and invert.attrib['val'] == '0'
    assert chart.find(f'.//{c}catAx/{c}tickLblPos').attrib['val'] == 'low'


#: The private interview deck's ink theme (PIPELINE_FINDINGS D2).
_INK_THEME = {'bg': '#101820', 'ink': '#EDF2F6', 'primary': '#22313F',
              'accent': '#D9B36C', 'muted': '#A9B6C2', 'hairline': '#2B3A48'}
_PAPER_THEME = {'bg': '#FAF8F4', 'ink': '#1B2430', 'primary': '#16283C',
                'accent': '#C0652B', 'muted': '#6B7280', 'hairline': '#D8D5CE'}


def _contrast(first: str, second: str) -> float:
    def luminance(value):
        channels = [int(value.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize('colors,background', [
    (_INK_THEME, '$bg'),        # dark theme
    (_PAPER_THEME, '$ink'),     # light theme, dark page
    (_PAPER_THEME, '$bg'),      # light theme
])
def test_native_chart_text_is_readable_on_its_page_background(
        tmp_path, colors, background):
    """Every chart text part (default, both axes, legend, data labels) carries
    an explicit colour derived from the page background: Office's default
    chart text is near-black, invisible on a dark slide."""
    from lib.slides.export_pptx import export_pptx
    from lib.slides.pptd import resolve_color

    deck = _deck(tmp_path, series=2, colors=colors, background=background)
    page_color = resolve_color(background, deck.theme)
    output = tmp_path / 'chart.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    with zipfile.ZipFile(output) as archive:
        chart = ET.fromstring(archive.read('ppt/charts/chart1.xml'))
    a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    c = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    parts = {'chart': chart.find(f'{c}txPr'),
             'category axis': chart.find(f'.//{c}catAx/{c}txPr'),
             'value axis': chart.find(f'.//{c}valAx/{c}txPr'),
             'legend': chart.find(f'.//{c}legend/{c}txPr'),
             'data labels': chart.find(f'.//{c}dLbls/{c}txPr')}
    for name, tx_pr in parts.items():
        assert tx_pr is not None, name
        fill = tx_pr.find(f'{a}p/{a}pPr/{a}defRPr/{a}solidFill/{a}srgbClr')
        assert fill is not None, f'{name} text has no explicit colour'
        ratio = _contrast('#' + fill.attrib['val'], page_color)
        assert ratio >= 4.5, f'{name} text contrast {ratio:.2f} on {page_color}'


@pytest.mark.parametrize('colors,background', [(_INK_THEME, '$bg'),
                                               (_PAPER_THEME, '$ink')])
def test_chart_preview_text_is_readable_on_its_page_background(
        tmp_path, monkeypatch, colors, background):
    from lib.slides.pptd import resolve_color

    deck = _deck(tmp_path, series=2, colors=colors, background=background)
    page_color = resolve_color(background, deck.theme)
    for label in _svg(deck, monkeypatch).findall('text'):
        ratio = _contrast(label.attrib['fill'], page_color)
        assert ratio >= 4.5, f'{label.text!r} contrast {ratio:.2f}'


#: WCAG 2.x success criterion 1.4.11 (non-text contrast): graphical
#: objects needed to understand content reach 3:1 against what they sit on.
_NON_TEXT_CONTRAST = 3.0


@pytest.mark.parametrize('colors,background', [
    (_INK_THEME, '$bg'),        # dark theme: $primary is a dark slate
    (_PAPER_THEME, '$ink'),     # light theme, dark page
    (_PAPER_THEME, '$bg'),      # light theme
])
def test_default_series_colours_are_visible_on_the_chart_background(
        tmp_path, monkeypatch, colors, background):
    """Uncoloured series take the theme palette in an order derived from the
    background, in the preview and the native chart alike."""
    from lib.slides.export_pptx import export_pptx
    from lib.slides.pptd import resolve_color

    deck = _deck(tmp_path, series=2, colors=colors, background=background)
    page_color = resolve_color(background, deck.theme)
    swatches = [rect.attrib['fill'] for rect in
                _svg(deck, monkeypatch).findall('rect')[:2]]
    output = tmp_path / 'chart.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    with zipfile.ZipFile(output) as archive:
        chart = ET.fromstring(archive.read('ppt/charts/chart1.xml'))
    a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    c = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    native = ['#' + ser.find(f'{c}spPr/{a}solidFill/{a}srgbClr').attrib['val']
              for ser in chart.iter(f'{c}ser')]
    assert {s.upper() for s in swatches} == {s.upper() for s in native}
    assert len(set(native)) == 2
    for color in native:
        ratio = _contrast(color, page_color)
        assert ratio >= _NON_TEXT_CONTRAST, f'{color} on {page_color}: {ratio:.2f}'


def test_chart_only_strict_export_refuses_missing_font(tmp_path):
    from lib.slides.export_pptx import ExportError, export_pptx

    deck = _deck(tmp_path, family='Unregistered Chart Font')
    with pytest.raises(ExportError, match='embedding incomplete'):
        export_pptx(deck, str(tmp_path / 'chart.pptx'), require_embedded_fonts=True)


def _peer_chart_deck(tmp_path):
    from lib.slides.pptd import parse_deck

    def chart(eid, x, width, categories, values, name='MAPE'):
        return {'elementId': eid, 'elementType': 'chart',
                'bounds': [x, 200, width, 190], 'chartType': 'bar',
                'options': {'legend': False, 'dataLabels': True},
                'data': {'categories': categories,
                         'series': [{'name': name, 'values': values}]}}

    (tmp_path / '01.page').write_text(yaml.safe_dump({
        'pageType': 'content', 'elements': [
            chart('spring', 532, 328, ['此前', '之后', '外采方案'], [31, 17, 24]),
            chart('national', 884, 340, ['此前', '之后'], [19, 12]),
            chart('cost', 40, 400, ['A', 'B'], [5, 4], name='Cost')]},
        allow_unicode=True), encoding='utf-8')
    (tmp_path / 'deck.pptd').write_text(yaml.safe_dump({
        'version': 'v2', 'title': 'peers', 'size': [1280, 720],
        'theme': {'textStyles': {'body': {'fontFamily': 'Noto Sans CJK SC'}}},
        'pages': ['01.page']}), encoding='utf-8')
    return parse_deck(str(tmp_path / 'deck.pptd'))


def test_peer_charts_of_one_measure_share_their_value_scale(tmp_path, monkeypatch):
    """e2e (2026-11-17): two MAPE bar charts side by side each scaled to
    their own maximum, so 19% drew longer than 31%."""
    from lib.slides import render_html
    from lib.slides.export_pptx import export_pptx

    deck = _peer_chart_deck(tmp_path)
    monkeypatch.setattr(render_html, '_font_face_css', lambda _f, **_k: '')
    html = render_html.render_page_html(deck, deck.pages[0])
    svgs = [ET.fromstring(svg) for svg in re.findall(r'<svg\b.*?</svg>', html, re.S)]
    spring, national = ([float(r.attrib['width']) for r in svg.findall('rect')]
                        for svg in svgs[:2])
    # Both plots scale to the shared peak, so 19 draws shorter than 31.
    assert national[0] < spring[0]

    output = tmp_path / 'peers.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    c = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    maxima = []
    with zipfile.ZipFile(output) as archive:
        for index in (1, 2, 3):
            chart = ET.fromstring(archive.read(f'ppt/charts/chart{index}.xml'))
            node = chart.find(f'.//{c}valAx/{c}scaling/{c}max')
            maxima.append(None if node is None else float(node.attrib['val']))
    assert maxima[0] is not None and maxima[0] == maxima[1] >= 31
    # A chart of another measure keeps its own automatic scale.
    assert maxima[2] is None
